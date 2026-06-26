"""Entity-resolution pipeline: ER queue → score → cluster/merge → guard → graph.

Reads pending candidates and resolves them (Splink score → nomenklatura cluster →
FtM merge), applies the catastrophic-merge guard, records the audit trail, rewrites
referents to canonical ids, and upserts auto-promoted canonical entities into Neo4j.

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
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem, IngestDeadLetter, ResolverJudgement
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.guard.sensitivity import has_nonexemptible_sensitivity
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.resolution.audit import record_merge, record_merge_alert
from worldmonitor.resolution.canonical import (
    record_durable_id,
    resolve_durable_id,
)
from worldmonitor.resolution.merge import (
    DEFAULT_MERGE_THRESHOLD,
    ResolvedCluster,
    StoredJudgement,
    cluster_and_merge,
    rekey_cluster,
)
from worldmonitor.resolution.referents import build_referent_map, rewrite_referents
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import score_pairs
from worldmonitor.settings import get_settings

logger = logging.getLogger(__name__)

# Bounded exception summary stored on a dead-letter row.
_ERROR_SUMMARY_MAX = 2000


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
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    enrich: Callable[[FtmEntity], FtmEntity] | None = None,
    guard_mode: str | None = None,
    batch_size: int | None = None,
) -> ResolveStats:
    """Resolve pending ER-queue candidates, draining in batches.

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
    # ephemeral resolver so a reviewed cluster never re-parks.
    judgements = _load_judgements(session)
    pending = clusters = promoted = review = alerts = batches = 0
    while True:
        items = list(
            session.execute(
                select(ErQueueItem)
                .where(ErQueueItem.status == "pending")
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


def _load_judgements(session: Session) -> list[StoredJudgement]:
    """Load the durable sign-off judgements (ADR 0031) to seed each batch."""
    rows = session.execute(select(ResolverJudgement)).scalars()
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


def _summarize(exc: Exception) -> str:
    """A bounded one-line summary of an exception for a dead-letter row."""
    return f"{type(exc).__name__}: {exc}"


def _quarantine(session: Session, items: Sequence[ErQueueItem], *, stage: str, reason: str) -> None:
    """Mark queue rows ``invalid`` and record a dead-letter (audit B-2, ADR 0038).

    Containment for a poison input at any resolution stage: the offending rows leave the
    ``pending`` set (so the bounded drain always terminates) and are recorded in
    ``ingest_dead_letter`` with their ``source_record`` — replayable, never silently lost —
    mirroring the ingest land/map dead-letter pattern. A poison row/batch must never wedge the
    queue by re-loading and re-failing forever.
    """
    summary = reason[:_ERROR_SUMMARY_MAX]
    for item in items:
        item.status = "invalid"
        session.add(
            IngestDeadLetter(
                id=str(uuid.uuid4()),
                connector_id=item.connector_id,
                source_key=item.entity_id or item.source_record or item.id,
                source_record=item.source_record,
                stage=stage,
                error=summary,
            )
        )
    if items:
        logger.warning(
            "resolve: quarantined %d ER-queue row(s) at stage '%s' (invalid + dead-letter): %s",
            len(items),
            stage,
            summary[:200],
        )


def _record_skip(
    session: Session, items: Sequence[ErQueueItem], *, stage: str, reason: str
) -> None:
    """Durably record a NON-status-mutating skip event (H-2, ADR 0041).

    Unlike ``_quarantine``, this does NOT set ``item.status`` — it only writes a replayable
    ``IngestDeadLetter`` for each row. A schema-incompatible member that was dropped from a
    transitive cluster is re-emitted as its own correct-schema singleton (merge.py), so its row
    must still resolve normally to ``'resolved'`` with its own node; the dead-letter is the audit
    of the drop (closing the previously-silent log-only gap), not a quarantine. Recording it as a
    status-mutating quarantine would mark the row ``'invalid'`` and prevent its own node being
    written.
    """
    summary = reason[:_ERROR_SUMMARY_MAX]
    for item in items:
        session.add(
            IngestDeadLetter(
                id=str(uuid.uuid4()),
                connector_id=item.connector_id,
                source_key=item.entity_id or item.source_record or item.id,
                source_record=item.source_record,
                stage=stage,
                error=summary,
            )
        )
    if items:
        logger.warning(
            "resolve: recorded %d schema-incompatible member skip(s) at stage '%s' "
            "(dead-letter only, row still resolves to its own node): %s",
            len(items),
            stage,
            summary[:200],
        )


def _resolve_batch(
    session: Session,
    neo4j: Neo4jClient,
    items: list[ErQueueItem],
    *,
    mode: str,
    merge_threshold: float,
    enrich: Callable[[FtmEntity], FtmEntity] | None,
    judgements: Sequence[StoredJudgement] = (),
) -> ResolveStats:
    """Resolve one bounded batch of queue ``items`` (the caller commits).

    Scores + clusters the batch, applies the catastrophic-merge guard, records the
    audit/alert trail, rewrites referents to canonical ids (G2, ADR 0025), and writes the
    auto-promoted canonical entities. Every queue row leaves ``pending`` — all rows sharing
    one FtM id move together — so a drained batch can never be re-loaded by the outer loop.

    **Every stage that can raise on bad input is isolated (audit B-2, ADR 0038)** so a single
    poison input never aborts the batch and wedges the drain. A bad ROW is quarantined
    individually (construction); an unscoreable/unclusterable BATCH is quarantined as a set;
    an invalid merged entity is quarantined before the write. Quarantine = status ``invalid`` +
    a dead-letter (replayable). A genuine write/infra failure is left to propagate so the driver
    retries it idempotently (deterministic canonical id, ADR 0036) — only *poison input* is
    quarantined, not a transient outage.
    """
    # Stage 1 — construct per row. A row whose raw_entity cannot be parsed into an FtM entity
    # is quarantined individually; the good rows proceed.
    items_by_entity_id: dict[str | None, list[ErQueueItem]] = defaultdict(list)
    entities: list[FtmEntity] = []
    for item in items:
        try:
            entity = make_entity(item.raw_entity)
        except Exception as exc:  # bad/unknown schema, malformed raw_entity, ...
            _quarantine(session, [item], stage="resolve-row", reason=_summarize(exc))
            continue
        entities.append(entity)
        items_by_entity_id[entity.id].append(item)

    # Stage 2 — score + cluster the constructed window. A batch that cannot be scored or
    # clustered as a whole (e.g. an all-no-name window that trips Splink's name blocking) is
    # quarantined as a set so the drain still terminates. Nothing has been written or audited
    # yet, so there is no partial state to undo.
    try:
        pairs = score_pairs(entities)
        clusters = cluster_and_merge(
            entities, pairs, merge_threshold=merge_threshold, judgements=judgements
        )
    except Exception as exc:
        constructed = [item for rows in items_by_entity_id.values() for item in rows]
        _quarantine(session, constructed, stage="resolve-batch", reason=_summarize(exc))
        return ResolveStats(
            pending=len(items), clusters=0, promoted=0, review=0, alerts=0, batches=1
        )

    by_id: dict[str | None, FtmEntity] = {entity.id: entity for entity in entities}
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
        if cluster.merge_incompatible:
            # H-2 (ADR 0041): this singleton was demoted out of a transitive cluster because its
            # schema had no common FtM base with the other members. Durably record the drop
            # WITHOUT mutating status, so the row still resolves normally below to its OWN
            # correct-schema node (the singleton path). This closes the previously log-only
            # audit gap; the leftover safety sweep is unaffected because the member is now in
            # clustered_ids via this singleton (so it is not double-quarantined as resolve-noid).
            _record_skip(
                session,
                items_by_entity_id.get(cluster.member_ids[0], []),
                stage="resolve-incompat",
                reason="schema-incompatible member dropped from transitive cluster (H-2)",
            )

        # Anchor-preferred DURABLE id (Gate B-front / ADR 0044): for a MERGE, re-key the cluster
        # under the durable id derived from its members' anchors BEFORE the guard/audit/write, so
        # every downstream reference (audit canonical_id, the graph MERGE key, the referent map)
        # uses the stable id (``qid:``/``lei:``/…), not the ``wmc-`` idempotency fingerprint. This
        # is the read-only, adopt-preferring resolution; the ledger WRITE is deferred to the promote
        # point so a parked cluster never pollutes the ledger. ``wmc-`` survives ONLY as the
        # unanchored-merge fallback (DENY D1). Scoped to ``is_merge`` (slice-1): a SINGLETON keeps
        # its own id (the established "singleton keeps its own node id" contract), and graph-level
        # re-key stability for an un-merged anchored entity is entangled with cross-batch /
        # streaming dedup, which is DEFERRED (ADR 0019). The merge survivor derivation (spec §7) is
        # what slice-1 wires; the durable adopt/derive API itself (canonical.py) is
        # singleton-agnostic.
        prior_id = cluster.canonical_id
        if cluster.is_merge:
            cluster_members = [by_id[m] for m in cluster.member_ids if m in by_id]
            durable_id = resolve_durable_id(session, cluster_members, fallback_id=prior_id)
            cluster = rekey_cluster(cluster, durable_id)

        flagged, reason = needs_review(cluster, by_id, neo4j=neo4j)
        members = set(cluster.member_ids)
        # The approved-group-exemption fence (Gate E / ADR 0047 Decision 5 + slice-3 refinement).
        # This implements the user's decision "re-review a newly-detected sensitivity ONCE":
        # deny-by-default now flags a cluster the legacy denylist MISSED (e.g. a role.rca /
        # crime.war / off-ontology member), so a cluster whose members are a subset of an approval
        # recorded BEFORE that topic was understood sensitive must NOT be silently un-flagged ->
        # auto-merged: a sign-off approving "these records are the same entity" could not have
        # considered a risk it never saw, so the cluster re-parks for a fresh human look.
        # Non-exemptibility is computed by the STRUCTURED probe ``has_nonexemptible_sensitivity``
        # over ALL THREE newly-detectable signals — a newly-broadened TOPIC, Stage-2 k-hop graph
        # proximity, and the Stage-3 Chow abstain band — each evaluated INDEPENDENTLY of which flag
        # short-circuited ``needs_review``. slice-3 fixes the slice-2 masking fail-open: the old
        # reason-string classifier saw only the FIRST flag, so an exemptible-first flag (size>10 /
        # anchor-conflict / a legacy-caught topic like `sanction`) MASKED a co-occurring
        # k-hop/Chow/newly-broadened signal and the stale exemption silently un-flagged it. A
        # sensitivity the legacy guard ALREADY caught (e.g. `sanction`) was visible at approval
        # time, so it stays exemptible — as do the SIZE flag and the anchor-conflict flag (ADR
        # 0040). That scoping is why it is "once," not forever-churn: an already-reviewable
        # sensitivity is not re-parked, and the existing non-sensitive AND legacy-caught
        # approve->promote paths are preserved unchanged. KNOWN PROPERTY (intended, conservative):
        # a newly-broadened-sensitive cluster re-parks on every RE-INGEST / re-resolve (the fence
        # keys on legacy-visibility / structural signal, not "was ever approved") — the deliberate
        # fail-closed posture, not a bug. The structured probe is evaluated LAZILY — only when a
        # cluster is both flagged AND a subset of an approved group (the sole path the exemption can
        # un-flag) — so the common case never pays the probe's k-hop graph read a second time on the
        # resolve hot path (``and`` short-circuits — needs_review already ran Stage-2 above).
        if (
            flagged
            and any(members <= group for group in approved_groups)
            and not has_nonexemptible_sensitivity(cluster, by_id, neo4j=neo4j)
        ):
            flagged = False  # an already-approved, exemptible merge — promote

        # mode="block": park the flagged cluster for human review; never write it.
        if flagged and mode == "block":
            record_merge(session, cluster, decision="pending_review", reason=reason)
            _set_status(cluster.member_ids, "pending_review")
            review += 1
            continue

        # This cluster WILL be written (auto-promote, or alert-mode flagged). Build the canonical
        # entity (enrich runs only here — never for a parked cluster) and Stage 3 — write-stage
        # poison guard: a merged canonical that is not a valid FtM entity is quarantined BEFORE
        # anything is recorded, so an invalid entity cannot abort the batch's write OR leave a
        # spurious audit/alert row. (A genuine write/infra failure in write_entities below is left
        # to propagate so the driver retries it idempotently — ADR 0036; same class as enrich.)
        entity = enrich(cluster.entity) if enrich is not None else cluster.entity
        try:
            validate_or_raise(entity.to_dict())
        except Exception as exc:
            _quarantine(
                session,
                [it for mid in cluster.member_ids for it in items_by_entity_id.get(mid, [])],
                stage="resolve-write",
                reason=_summarize(exc),
            )
            continue

        # mode="alert": the guard still flagged it, but the build phase proceeds —
        # write the merge and record a durable, auditable merge_alerts row.
        if flagged:
            record_merge_alert(session, cluster, reason=reason)
            alerts += 1
            logger.warning(
                "catastrophic-merge guard ALERT (MERGE_GUARD_MODE=alert): merged flagged "
                "cluster %s anyway — %s; %d alert(s) this batch",
                cluster.canonical_id,
                reason,
                alerts,
            )

        # Record the PROMOTED MERGE's durable id + collapsed-member aliases in the ledger
        # (ADR 0044, spec §6/§7): every member id (and the prior ``wmc-`` fingerprint, when the
        # durable id differs) resolves to the surviving durable id — append-only, idempotent, so a
        # re-ingest re-adopts the stable id and a graph lookup by a superseded id resolves to the
        # surviving node. Only PROMOTED merges write here (a parked cluster never rewrites/keys; a
        # singleton keeps its own id and has no collapsed members to alias).
        if cluster.is_merge:
            record_durable_id(
                session,
                cluster.canonical_id,
                member_ids=cluster.member_ids,
                prior_id=prior_id,
            )

        record_merge(session, cluster, decision="merged", reason=reason)
        _set_status(cluster.member_ids, "resolved")
        promoted_entities.append(entity)
        promoted_clusters.append(cluster)
        promoted += 1

    # Safety sweep: any constructed row whose id never clustered (a missing/None FtM id, or an
    # entity dropped during clustering) gets no status above and would be re-loaded by the drain
    # forever — quarantine it (invalid + dead-letter) so the loop always makes progress.
    clustered_ids = {member_id for c in clusters for member_id in c.member_ids}
    leftover = [
        item
        for entity_id, rows in items_by_entity_id.items()
        if entity_id not in clustered_ids
        for item in rows
    ]
    if leftover:
        _quarantine(
            session,
            leftover,
            stage="resolve-noid",
            reason="unclustered: missing/None FtM id or dropped during clustering",
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
        write_entities(neo4j, promoted_entities)

    return ResolveStats(
        pending=len(items),
        clusters=len(clusters),
        promoted=promoted,
        review=review,
        alerts=alerts,
        batches=1,
    )
