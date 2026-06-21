"""Entity-resolution pipeline: ER queue → score → cluster/merge → guard → graph.

Reads pending candidates for a tenant, resolves them (Splink score → nomenklatura
cluster → FtM merge), applies the catastrophic-merge guard, records the audit
trail, and upserts auto-promoted canonical entities into Neo4j.

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
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.audit import record_merge, record_merge_alert
from worldmonitor.resolution.merge import (
    DEFAULT_MERGE_THRESHOLD,
    ResolvedCluster,
    cluster_and_merge,
)
from worldmonitor.resolution.referents import build_referent_map, rewrite_referents
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import score_pairs
from worldmonitor.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolveStats:
    """Counts from one resolution run."""

    pending: int
    clusters: int
    promoted: int
    review: int
    alerts: int
    """Flagged clusters merged anyway under ``MERGE_GUARD_MODE="alert"``."""


def resolve_pending(
    *,
    session: Session,
    neo4j: Neo4jClient,
    tenant_id: str,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    enrich: Callable[[FtmEntity], FtmEntity] | None = None,
    guard_mode: str | None = None,
) -> ResolveStats:
    """Resolve all pending ER-queue candidates for ``tenant_id``.

    If ``enrich`` is given, each auto-promoted canonical entity is passed through
    it (e.g. the Wikidata anchor enricher) before being written to the graph.
    ``guard_mode`` (``"alert"`` / ``"block"``) overrides ``MERGE_GUARD_MODE`` from
    settings; it controls only the *action* on a flagged cluster (ADR 0024).
    """
    mode = guard_mode if guard_mode is not None else get_settings().merge_guard_mode
    items = list(
        session.execute(
            select(ErQueueItem).where(
                ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending"
            )
        ).scalars()
    )
    if not items:
        return ResolveStats(pending=0, clusters=0, promoted=0, review=0, alerts=0)

    items_by_entity_id = {item.raw_entity["id"]: item for item in items}
    entities = [make_entity(item.raw_entity) for item in items]
    by_id: dict[str | None, FtmEntity] = {entity.id: entity for entity in entities}

    pairs = score_pairs(entities)
    clusters = cluster_and_merge(entities, pairs, merge_threshold=merge_threshold)

    promoted_entities: list[FtmEntity] = []
    promoted_clusters: list[ResolvedCluster] = []
    promoted = review = alerts = 0
    for cluster in clusters:
        flagged, reason = needs_review(cluster, by_id)

        # mode="block": park the flagged cluster for human review; never write it.
        if flagged and mode == "block":
            record_merge(
                session, cluster, tenant_id=tenant_id, decision="pending_review", reason=reason
            )
            for member_id in cluster.member_ids:
                item = items_by_entity_id.get(member_id)
                if item is not None:
                    item.status = "pending_review"
            review += 1
            continue

        # mode="alert": the guard still flagged it, but the build phase proceeds —
        # write the merge and record a durable, auditable merge_alerts row.
        if flagged:
            record_merge_alert(session, cluster, tenant_id=tenant_id, reason=reason)
            alerts += 1
            logger.warning(
                "catastrophic-merge guard ALERT (MERGE_GUARD_MODE=alert): merged flagged "
                "cluster %s anyway — %s; %d alert(s) this run",
                cluster.canonical_id,
                reason,
                alerts,
            )

        record_merge(session, cluster, tenant_id=tenant_id, decision="merged", reason=reason)
        for member_id in cluster.member_ids:
            item = items_by_entity_id.get(member_id)
            if item is not None:
                item.status = "resolved"
        entity = enrich(cluster.entity) if enrich is not None else cluster.entity
        promoted_entities.append(entity)
        promoted_clusters.append(cluster)
        promoted += 1

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
    session.commit()
    return ResolveStats(
        pending=len(items),
        clusters=len(clusters),
        promoted=promoted,
        review=review,
        alerts=alerts,
    )
