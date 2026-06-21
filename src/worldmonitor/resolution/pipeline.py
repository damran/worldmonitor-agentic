"""Entity-resolution pipeline: ER queue → score → cluster/merge → guard → graph.

Reads pending candidates for a tenant and resolves them (Splink score →
nomenklatura cluster → FtM merge), applies the catastrophic-merge guard, records
the audit trail, rewrites referents to canonical ids, and upserts auto-promoted
canonical entities into Neo4j.

The queue is drained in **bounded batches** (ADR 0026): ``resolve_pending`` loads
at most ``RESOLVE_BATCH_SIZE`` pending rows, resolves that window, commits, and
repeats until the queue is empty — so memory and per-pass cost stay bounded.
Dedup is *within* a batch; cross-batch / incremental dedup against
already-resolved entities is deferred to the ER-streaming gate (ADR 0019).

The guard *evaluation* (``resolution/review.py``) is unconditional; only the
*action* on a flagged (oversized / PEP / sanctioned) cluster depends on
``MERGE_GUARD_MODE`` (ADR 0024):

* ``"block"`` — park the cluster as ``pending_review``; never write it.
* ``"alert"`` (build-phase default) — write the merge anyway and record a durable
  ``merge_alerts`` row (plus a WARNING log). MUST return to ``"block"`` with human
  sign-off before production.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem, ResolverJudgement
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.audit import record_merge, record_merge_alert
from worldmonitor.resolution.merge import (
    DEFAULT_MERGE_THRESHOLD,
    ResolvedCluster,
    StoredJudgement,
    cluster_and_merge,
)
from worldmonitor.resolution.referents import build_referent_map, rewrite_referents
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import score_pairs
from worldmonitor.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolveStats:
    """Counts from one resolution run (summed across the batches it drained)."""

    pending: int
    clusters: int
    promoted: int
    review: int
    alerts: int
    """Flagged clusters merged anyway under ``MERGE_GUARD_MODE="alert"``."""
    batches: int
    """Number of bounded batches drained from the queue (ADR 0026)."""


def resolve_pending(
    *,
    session: Session,
    neo4j: Neo4jClient,
    tenant_id: str,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    enrich: Callable[[FtmEntity], FtmEntity] | None = None,
    guard_mode: str | None = None,
    batch_size: int | None = None,
) -> ResolveStats:
    """Resolve pending ER-queue candidates for ``tenant_id``, draining in batches.

    The queue is processed in bounded windows of ``batch_size`` rows (default
    ``RESOLVE_BATCH_SIZE`` from settings): each batch is scored, clustered,
    guarded, referent-rewritten, written, and **committed** before the next is
    loaded, so memory and per-pass cost stay bounded (ADR 0026). Dedup is *within*
    a batch — cross-batch / incremental dedup against already-resolved entities is
    deferred to the ER-streaming gate (ADR 0019).

    If ``enrich`` is given, each auto-promoted canonical entity is passed through
    it (e.g. the Wikidata anchor enricher) before being written to the graph.
    ``guard_mode`` (``"alert"`` / ``"block"``) overrides ``MERGE_GUARD_MODE`` from
    settings; it controls only the *action* on a flagged cluster (ADR 0024).
    """
    settings = get_settings()
    mode = guard_mode if guard_mode is not None else settings.merge_guard_mode
    size = batch_size if batch_size is not None else settings.resolve_batch_size
    if size <= 0:
        raise ValueError(f"batch_size must be a positive integer, got {size}")

    # Durable sign-off judgements (ADR 0031) — loaded once, seeded into every batch's
    # ephemeral resolver so a reviewed cluster never re-parks. Tenant-scoped (G4).
    judgements = _load_judgements(session, tenant_id)
    pending = clusters = promoted = review = alerts = batches = 0
    while True:
        items = list(
            session.execute(
                select(ErQueueItem)
                .where(
                    ErQueueItem.tenant_id == tenant_id,
                    ErQueueItem.status == "pending",
                )
                .order_by(ErQueueItem.created_at, ErQueueItem.id)
                .limit(size)
            ).scalars()
        )
        if not items:
            break
        stats = _resolve_batch(
            session,
            neo4j,
            items,
            tenant_id=tenant_id,
            mode=mode,
            merge_threshold=merge_threshold,
            enrich=enrich,
            judgements=judgements,
        )
        session.commit()
        pending += stats.pending
        clusters += stats.clusters
        promoted += stats.promoted
        review += stats.review
        alerts += stats.alerts
        batches += 1

    return ResolveStats(
        pending=pending,
        clusters=clusters,
        promoted=promoted,
        review=review,
        alerts=alerts,
        batches=batches,
    )


def _load_judgements(session: Session, tenant_id: str) -> list[StoredJudgement]:
    """Load this tenant's durable sign-off judgements (ADR 0031) to seed each batch."""
    rows = session.execute(
        select(ResolverJudgement).where(ResolverJudgement.tenant_id == tenant_id)
    ).scalars()
    return [StoredJudgement(row.left_id, row.right_id, row.judgement) for row in rows]


def _approved_groups(judgements: Sequence[StoredJudgement]) -> list[frozenset[str]]:
    """Connected components of APPROVED (positive) judgement pairs.

    Each component is one human-reviewed merge. Used to exempt a flagged cluster from
    the guard ONLY when the cluster is an exact re-formation of a single approved group
    (so a never-reviewed member accreting onto it, or two approved groups fusing, still
    re-parks). Union-find over the positive pairs.
    """
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:  # path-compress
            parent[node], node = root, parent[node]
        return root

    for judgement in judgements:
        if judgement.judgement == "positive":
            parent[find(judgement.left_id)] = find(judgement.right_id)

    groups: dict[str, set[str]] = defaultdict(set)
    for node in list(parent):
        groups[find(node)].add(node)
    return [frozenset(members) for members in groups.values()]


def _resolve_batch(
    session: Session,
    neo4j: Neo4jClient,
    items: list[ErQueueItem],
    *,
    tenant_id: str,
    mode: str,
    merge_threshold: float,
    enrich: Callable[[FtmEntity], FtmEntity] | None,
    judgements: Sequence[StoredJudgement] = (),
) -> ResolveStats:
    """Resolve one bounded batch of queue ``items`` (the caller commits).

    Scores + clusters the batch, applies the catastrophic-merge guard, records the
    audit/alert trail, rewrites referents to canonical ids (G2, ADR 0025), and
    writes the auto-promoted canonical entities. Every queue row in the batch is
    transitioned out of ``pending`` — all rows sharing one FtM id move together —
    so a drained batch can never be re-loaded by the outer loop.
    """
    items_by_entity_id: dict[str | None, list[ErQueueItem]] = defaultdict(list)
    for item in items:
        items_by_entity_id[item.raw_entity.get("id")].append(item)
    entities = [make_entity(item.raw_entity) for item in items]
    by_id: dict[str | None, FtmEntity] = {entity.id: entity for entity in entities}

    pairs = score_pairs(entities)
    clusters = cluster_and_merge(
        entities, pairs, merge_threshold=merge_threshold, judgements=judgements
    )
    # Connected components of human-APPROVED (positive) pairs — each is exactly one
    # human-reviewed merge (ADR 0031). A flagged cluster is exempt from the guard ONLY
    # when ALL its members fall inside a SINGLE approved group: a new member (sensitive or
    # not) accreting onto an approved merge, or two approved groups fusing, is a fresh
    # UNREVIEWED merge and must re-park — this preserves "never auto-merge a sensitive
    # entity" and routes canonical-canonical fusion through the guard, not around it.
    approved_groups = _approved_groups(judgements)

    def _set_status(member_ids: tuple[str, ...], status: str) -> None:
        for member_id in member_ids:
            for item in items_by_entity_id.get(member_id, []):
                item.status = status

    promoted_entities: list[FtmEntity] = []
    promoted_clusters: list[ResolvedCluster] = []
    promoted = review = alerts = 0
    for cluster in clusters:
        flagged, reason = needs_review(cluster, by_id)
        members = set(cluster.member_ids)
        if flagged and any(members <= group for group in approved_groups):
            flagged = False  # exactly an already-approved merge — promote, never re-park

        # mode="block": park the flagged cluster for human review; never write it.
        if flagged and mode == "block":
            record_merge(
                session, cluster, tenant_id=tenant_id, decision="pending_review", reason=reason
            )
            _set_status(cluster.member_ids, "pending_review")
            review += 1
            continue

        # mode="alert": the guard still flagged it, but the build phase proceeds —
        # write the merge and record a durable, auditable merge_alerts row.
        if flagged:
            record_merge_alert(session, cluster, tenant_id=tenant_id, reason=reason)
            alerts += 1
            logger.warning(
                "catastrophic-merge guard ALERT (MERGE_GUARD_MODE=alert): merged flagged "
                "cluster %s anyway — %s; %d alert(s) this batch",
                cluster.canonical_id,
                reason,
                alerts,
            )

        record_merge(session, cluster, tenant_id=tenant_id, decision="merged", reason=reason)
        _set_status(cluster.member_ids, "resolved")
        entity = enrich(cluster.entity) if enrich is not None else cluster.entity
        promoted_entities.append(entity)
        promoted_clusters.append(cluster)
        promoted += 1

    # Safety sweep: cluster_and_merge drops any entity with a missing / None FtM id,
    # so the queue rows carrying it get no status above and would be re-loaded by the
    # bounded-drain loop forever. Quarantine them as "invalid" so the loop always makes
    # progress (never fire-and-forget — log what was skipped).
    clustered_ids = {member_id for c in clusters for member_id in c.member_ids}
    skipped = 0
    for entity_id, rows in items_by_entity_id.items():
        if entity_id not in clustered_ids:
            for row in rows:
                row.status = "invalid"
            skipped += len(rows)
    if skipped:
        logger.warning(
            "resolve_pending: quarantined %d ER-queue row(s) with an unusable FtM id as "
            "'invalid' (unresolvable; kept the bounded-drain loop terminating)",
            skipped,
        )

    if promoted_entities:
        # Referent rewriting (G2, ADR 0025): redirect every reference to a
        # merged-away source id onto its surviving canonical id before the write,
        # so no edge dangles at a node that was never materialised. The map is
        # built from PROMOTED clusters only — a block-mode parked cluster never
        # rewrites anything, while an alert-mode flagged merge does.
        referents = build_referent_map(promoted_clusters)
        for entity in promoted_entities:
            rewrite_referents(entity, referents)
        write_entities(neo4j, promoted_entities, tenant_id=tenant_id)

    return ResolveStats(
        pending=len(items),
        clusters=len(clusters),
        promoted=promoted,
        review=review,
        alerts=alerts,
        batches=1,
    )
