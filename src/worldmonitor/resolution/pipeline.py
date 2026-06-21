"""Entity-resolution pipeline: ER queue → score → cluster/merge → guard → graph.

Reads pending candidates for a tenant, resolves them (Splink score → nomenklatura
cluster → FtM merge), applies the catastrophic-merge guard, records the audit
trail, and upserts only auto-promoted canonical entities into Neo4j. Sensitive /
oversized merges are parked as ``pending_review`` and never written to the graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.audit import record_merge
from worldmonitor.resolution.merge import DEFAULT_MERGE_THRESHOLD, cluster_and_merge
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import score_pairs


@dataclass(frozen=True, slots=True)
class ResolveStats:
    """Counts from one resolution run."""

    pending: int
    clusters: int
    promoted: int
    review: int


def resolve_pending(
    *,
    session: Session,
    neo4j: Neo4jClient,
    tenant_id: str,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
) -> ResolveStats:
    """Resolve all pending ER-queue candidates for ``tenant_id``."""
    items = list(
        session.execute(
            select(ErQueueItem).where(
                ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending"
            )
        ).scalars()
    )
    if not items:
        return ResolveStats(pending=0, clusters=0, promoted=0, review=0)

    items_by_entity_id = {item.raw_entity["id"]: item for item in items}
    entities = [make_entity(item.raw_entity) for item in items]
    by_id: dict[str | None, FtmEntity] = {entity.id: entity for entity in entities}

    pairs = score_pairs(entities)
    clusters = cluster_and_merge(entities, pairs, merge_threshold=merge_threshold)

    promoted_entities: list[FtmEntity] = []
    promoted = review = 0
    for cluster in clusters:
        flagged, reason = needs_review(cluster, by_id)
        decision = "pending_review" if flagged else "merged"
        record_merge(session, cluster, tenant_id=tenant_id, decision=decision, reason=reason)
        for member_id in cluster.member_ids:
            item = items_by_entity_id.get(member_id)
            if item is not None:
                item.status = decision if flagged else "resolved"
        if flagged:
            review += 1
        else:
            promoted_entities.append(cluster.entity)
            promoted += 1

    if promoted_entities:
        write_entities(neo4j, promoted_entities, tenant_id=tenant_id)
    session.commit()
    return ResolveStats(
        pending=len(items), clusters=len(clusters), promoted=promoted, review=review
    )
