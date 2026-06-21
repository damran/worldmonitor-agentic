"""Human sign-off on parked (sensitive / oversized) merges (ADR 0031).

Under ``MERGE_GUARD_MODE="block"`` the catastrophic-merge guard parks a flagged
cluster as ``pending_review`` and never writes it (ADR 0024 → 0031). An operator
reviews it and either **approves** (the merge is promoted to the graph, with the
entity's outbound edges) or **rejects** (the members are written as separate
entities). Both persist a durable, **tenant-scoped** resolver judgement (positive /
negative) so future batches respect the decision and the cluster never re-parks, and
both record a ``sign_off`` audit row — the human-sign-off trail CLAUDE.md requires for
changes affecting a real person.

v0 is CLI-driven (``python -m worldmonitor.review``); the API/UI surface is Phase 2.
Inbound cross-references (edges pointing AT the approved entity) are NOT restored here
— that is deferred Gate C, and is reconstructable from the retained landing zone +
queue rows (nothing is ever deleted), so it is deferral, not loss.
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem, MergeAudit, ResolverJudgement, SignOff
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.referents import rewrite_referents


class SignOffError(RuntimeError):
    """A parked merge could not be signed off (not found, or its members are gone)."""


@dataclass(frozen=True, slots=True)
class ParkedMerge:
    """A flagged cluster awaiting human review (a ``pending_review`` merge_audit row)."""

    canonical_id: str
    source_ids: tuple[str, ...]
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class SignOffResult:
    """Outcome of an approve/reject: what was written."""

    canonical_id: str
    decision: str  # "approved" | "rejected"
    entities_written: int
    edges_written: int


def list_parked(session: Session, tenant_id: str) -> list[ParkedMerge]:
    """All parked (``pending_review``) merges awaiting sign-off for ``tenant_id``."""
    rows = session.execute(
        select(MergeAudit).where(
            MergeAudit.tenant_id == tenant_id, MergeAudit.decision == "pending_review"
        )
    ).scalars()
    return [ParkedMerge(r.canonical_id, tuple(r.source_ids), r.score, r.reason) for r in rows]


def _parked_audit(session: Session, tenant_id: str, canonical_id: str) -> MergeAudit:
    audit = session.execute(
        select(MergeAudit).where(
            MergeAudit.tenant_id == tenant_id,
            MergeAudit.canonical_id == canonical_id,
            MergeAudit.decision == "pending_review",
        )
    ).scalar_one_or_none()
    if audit is None:
        raise SignOffError(f"no parked merge {canonical_id!r} for tenant {tenant_id!r}")
    return audit


def _member_rows(session: Session, tenant_id: str, source_ids: list[str]) -> list[ErQueueItem]:
    wanted = set(source_ids)
    rows = session.execute(
        select(ErQueueItem).where(
            ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending_review"
        )
    ).scalars()
    return [row for row in rows if row.raw_entity.get("id") in wanted]


def _outbound_edges(session: Session, tenant_id: str, source_ids: list[str]) -> list[FtmEntity]:
    """Edge entities the members ASSERT (member is the edge's source endpoint).

    These connect the reviewed entity outward (e.g. an Ownership whose owner is a
    parked member). Loaded from the tenant's queue (v0: a full scan — sign-off is a
    manual, infrequent operation).
    """
    members = set(source_ids)
    rows = session.execute(select(ErQueueItem).where(ErQueueItem.tenant_id == tenant_id)).scalars()
    edges: list[FtmEntity] = []
    for row in rows:
        entity = make_entity(row.raw_entity)
        source_prop = entity.schema.source_prop
        if not entity.schema.edge or source_prop is None:
            continue
        if set(entity.get(source_prop, quiet=True)) & members:
            edges.append(entity)
    return edges


def _record_judgements(
    session: Session, tenant_id: str, source_ids: list[str], verdict: str
) -> None:
    """Persist a durable, tenant-scoped judgement for every member pair (ADR 0031)."""
    for left, right in itertools.combinations(sorted(set(source_ids)), 2):
        session.add(
            ResolverJudgement(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                left_id=left,
                right_id=right,
                judgement=verdict,
                source="signoff",
            )
        )


def _merge_members(canonical_id: str, members: list[FtmEntity]) -> FtmEntity:
    """FtM-merge the parked members into one canonical entity under ``canonical_id``."""
    ordered = sorted(members, key=lambda entity: entity.id or "")
    merged = make_entity({**ordered[0].to_dict(), "id": canonical_id})
    for member in ordered:
        merged.merge(member)
    return merged


def approve(
    session: Session,
    neo4j: Neo4jClient,
    *,
    tenant_id: str,
    canonical_id: str,
    approver: str,
    reason: str = "",
) -> SignOffResult:
    """Promote a parked merge: write the canonical entity + its outbound edges.

    Persists a POSITIVE judgement (so future batches always merge these members) and a
    ``sign_off`` row, marks the queue rows resolved, and flips the audit to ``merged``.
    """
    audit = _parked_audit(session, tenant_id, canonical_id)
    source_ids = list(audit.source_ids)
    member_rows = _member_rows(session, tenant_id, source_ids)
    if not member_rows:
        raise SignOffError(f"parked merge {canonical_id!r} has no member rows to promote")

    canonical = _merge_members(canonical_id, [make_entity(r.raw_entity) for r in member_rows])
    referents = dict.fromkeys(source_ids, canonical_id)
    edges = _outbound_edges(session, tenant_id, source_ids)
    for edge in edges:
        rewrite_referents(edge, referents)
    write_entities(neo4j, [canonical, *edges], tenant_id=tenant_id)

    _record_judgements(session, tenant_id, source_ids, "positive")
    for row in member_rows:
        row.status = "resolved"
    audit.decision = "merged"
    session.add(_signoff_row(tenant_id, canonical_id, source_ids, "approved", approver, reason))
    session.commit()
    return SignOffResult(canonical_id, "approved", 1, len(edges))


def reject(
    session: Session,
    neo4j: Neo4jClient,
    *,
    tenant_id: str,
    canonical_id: str,
    approver: str,
    reason: str = "",
) -> SignOffResult:
    """Reject a parked merge: write each member as its own entity (+ its outbound edges).

    Persists a NEGATIVE judgement (so future batches never re-merge these members) and a
    ``sign_off`` row, marks the queue rows resolved, and flips the audit to ``rejected``.
    """
    audit = _parked_audit(session, tenant_id, canonical_id)
    source_ids = list(audit.source_ids)
    member_rows = _member_rows(session, tenant_id, source_ids)
    if not member_rows:
        raise SignOffError(f"parked merge {canonical_id!r} has no member rows to write")

    members = [make_entity(r.raw_entity) for r in member_rows]
    # Members keep their own ids, so their outbound edges already reference them — no
    # rewrite needed; just write the members alongside their edges.
    edges = _outbound_edges(session, tenant_id, source_ids)
    write_entities(neo4j, [*members, *edges], tenant_id=tenant_id)

    _record_judgements(session, tenant_id, source_ids, "negative")
    for row in member_rows:
        row.status = "resolved"
    audit.decision = "rejected"
    session.add(_signoff_row(tenant_id, canonical_id, source_ids, "rejected", approver, reason))
    session.commit()
    return SignOffResult(canonical_id, "rejected", len(members), len(edges))


def _signoff_row(
    tenant_id: str,
    canonical_id: str,
    source_ids: list[str],
    decision: str,
    approver: str,
    reason: str,
) -> SignOff:
    return SignOff(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        canonical_id=canonical_id,
        source_ids=source_ids,
        decision=decision,
        approver=approver,
        reason=reason,
    )
