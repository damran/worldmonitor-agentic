"""Human sign-off on parked (sensitive / oversized) merges (ADR 0031, 0036).

Under ``MERGE_GUARD_MODE="block"`` the catastrophic-merge guard parks a flagged
cluster as ``pending_review`` and never writes it (ADR 0024 → 0031). An operator
reviews it and either **approves** (the merge is promoted to the graph, with the
entity's outbound edges) or **rejects** (the members are written as separate
entities). Both persist a durable resolver judgement (positive / negative) so future
batches respect the decision and the cluster never re-parks, and both record a
``sign_off`` audit row — the human-sign-off trail CLAUDE.md requires for changes
affecting a real person.

**Idempotent / crash-recoverable (B-1 Part 2, ADR 0036).** Like the resolve pipeline,
sign-off writes Neo4j *before* it commits Postgres, so a crash in that window leaves the
graph written while the audit row stays ``pending_review`` (Postgres rolled back). That
half-committed state is now (a) **visible** — ``list_parked(..., neo4j=...)`` flags it via
``graph_written`` — and (b) **recoverable** — re-running the SAME ``approve``/``reject``
converges to one consistent outcome: the graph write is idempotent (the canonical id is
deterministic, ADR 0036 Part 1; members keep their own ids), judgements insert
ON-CONFLICT-DO-NOTHING, the audit row is mutated (never duplicated), and re-running an
already-completed sign-off is a no-op (``already_applied``). Approving an already-rejected
merge (or vice-versa) is refused, as is rejecting a merge whose canonical node a prior
approve already wrote (which would orphan that node — there is no delete path; append-only).

v0 is CLI-driven (``python -m worldmonitor.review``); the API/UI surface is Phase 2.
Inbound cross-references (edges pointing AT the approved entity) are NOT restored here
— that is deferred Gate C, and is reconstructable from the retained landing zone +
queue rows (nothing is ever deleted), so it is deferral, not loss.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from worldmonitor.db.models import (
    ErQueueItem,
    IngestDeadLetter,
    MergeAudit,
    ResolverJudgement,
    SignOff,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.canonical import record_durable_id
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.referents import rewrite_referents
from worldmonitor.resolution.statements import (
    record_context_claims,
    record_decision,
    record_statements,
)

# Bounded exception summary stored on a dead-letter row (mirrors the pipeline's bound).
_ERROR_SUMMARY_MAX = 2000


class SignOffError(RuntimeError):
    """A parked merge could not be signed off (not found, or its members are gone)."""


@dataclass(frozen=True, slots=True)
class ParkedMerge:
    """A flagged cluster awaiting human review (a ``pending_review`` merge_audit row)."""

    canonical_id: str
    source_ids: tuple[str, ...]
    score: float
    reason: str
    graph_written: bool = False
    """True if the graph already holds a node for the canonical id or any member — the
    signature of a sign-off whose graph write committed but whose Postgres audit did not
    (the B-1 cross-store crash window). Only populated when ``list_parked`` is given a
    ``neo4j`` client; ``False`` otherwise."""


@dataclass(frozen=True, slots=True)
class SignOffResult:
    """Outcome of an approve/reject: what was written."""

    canonical_id: str
    decision: str  # "approved" | "rejected"
    entities_written: int
    edges_written: int
    already_applied: bool = False
    """True when the decision was already committed (an idempotent re-run / no-op)."""


def _node_exists(neo4j: Neo4jClient, node_id: str) -> bool:
    rows = neo4j.execute_read("MATCH (n {id: $id}) RETURN count(n) AS n", id=node_id)
    return bool(rows and rows[0]["n"])


def _any_node_exists(neo4j: Neo4jClient, ids: Sequence[str]) -> bool:
    rows = neo4j.execute_read(
        "MATCH (n) WHERE n.id IN $ids RETURN count(n) AS n",
        ids=list(ids),
    )
    return bool(rows and rows[0]["n"])


def list_parked(session: Session, neo4j: Neo4jClient | None = None) -> list[ParkedMerge]:
    """All parked (``pending_review``) merges awaiting sign-off.

    When ``neo4j`` is given, each merge is annotated with ``graph_written`` — True if a
    node for the canonical id OR any member already exists in the graph. A parked merge is
    never written during resolution (block mode), so a present node means a sign-off's graph
    write committed but its Postgres audit did not (the B-1 crash window): the half-committed
    state is surfaced here so an operator can re-run the SAME approve/reject to recover it,
    rather than it sitting silently stuck.
    """
    rows = list(
        session.execute(select(MergeAudit).where(MergeAudit.decision == "pending_review")).scalars()
    )
    parked: list[ParkedMerge] = []
    for r in rows:
        graph_written = (
            _any_node_exists(neo4j, [r.canonical_id, *r.source_ids]) if neo4j is not None else False
        )
        parked.append(
            ParkedMerge(r.canonical_id, tuple(r.source_ids), r.score, r.reason, graph_written)
        )
    return parked


def _require_audit(session: Session, canonical_id: str) -> MergeAudit:
    """The merge_audit row for ``canonical_id`` regardless of decision (most recent).

    Unlike a pending-only lookup, this lets ``approve``/``reject`` reason about the decision
    state machine (already-merged / already-rejected) so a re-run converges instead of
    failing to find a now-terminal row.
    """
    audit = session.execute(
        select(MergeAudit)
        .where(MergeAudit.canonical_id == canonical_id)
        # created_at can tie (server-clock, same-txn); id as a deterministic tie-break.
        .order_by(MergeAudit.created_at.desc(), MergeAudit.id.desc())
    ).scalar()
    if audit is None:
        raise SignOffError(f"no merge {canonical_id!r}")
    return audit


def _dead_letter_poison(session: Session, row: ErQueueItem, exc: Exception) -> None:
    """Durably record a poison sign-off queue row (ADR 0041 slice-2).

    A single malformed ``raw_entity`` in the queue must not raise mid-scan and wedge
    approve/reject for EVERY parked merge (the B-2 per-input isolation pattern,
    never previously applied to sign-off). The offending row is skipped and recorded here —
    replayable, not silently swallowed — at stage ``'signoff-poison'`` (14 chars, fits
    ``String(16)``). This does NOT mutate the row's status (sign-off does not own the queue's
    quarantine lifecycle); it only adds an audit trail of the skip.
    """
    session.add(
        IngestDeadLetter(
            id=str(uuid.uuid4()),
            connector_id=row.connector_id,
            source_key=row.entity_id or row.source_record or row.id,
            source_record=row.source_record,
            stage="signoff-poison",
            error=f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX],
        )
    )


def _member_rows(session: Session, source_ids: list[str]) -> list[ErQueueItem]:
    wanted = set(source_ids)
    rows = session.execute(
        select(ErQueueItem).where(ErQueueItem.status == "pending_review")
    ).scalars()
    # The status == 'pending_review' filter is UNCHANGED (it selects exactly the parked rows);
    # only the per-row id extraction is hardened so a poison raw_entity cannot crash the scan.
    members: list[ErQueueItem] = []
    for row in rows:
        try:
            row_id = row.raw_entity.get("id")
        except Exception as exc:  # malformed raw_entity (not a mapping, etc.)
            _dead_letter_poison(session, row, exc)
            continue
        if row_id in wanted:
            members.append(row)
    return members


def _outbound_edges(session: Session, source_ids: list[str]) -> list[FtmEntity]:
    """Edge entities the members ASSERT (member is the edge's source endpoint).

    These connect the reviewed entity outward (e.g. an Ownership whose owner is a
    parked member). Loaded from the queue (v0: a full scan — sign-off is a manual,
    infrequent operation).

    Per-row hardened (ADR 0041 slice-2): a single poison ``raw_entity`` that ``make_entity``
    cannot parse is skipped and dead-lettered (``'signoff-poison'``) rather than raising and
    wedging approve/reject for the whole queue. The set of rows scanned is unchanged.
    """
    members = set(source_ids)
    rows = session.execute(select(ErQueueItem)).scalars()
    edges: list[FtmEntity] = []
    for row in rows:
        try:
            entity = make_entity(row.raw_entity)
        except Exception as exc:  # unknown schema / malformed raw_entity
            _dead_letter_poison(session, row, exc)
            continue
        source_prop = entity.schema.source_prop
        if not entity.schema.edge or source_prop is None:
            continue
        if set(entity.get(source_prop, quiet=True)) & members:
            edges.append(entity)
    return edges


def _record_judgements(session: Session, source_ids: list[str], verdict: str) -> None:
    """Persist a durable judgement for every member pair (ADR 0031).

    Idempotent (B-1 Part 2): ON CONFLICT DO NOTHING on ``uq_resolver_judgement_pair`` so a
    re-run of the same sign-off keeps the existing judgement rather than violating the pair
    uniqueness. Pairs are stored canonically ordered (``left <= right``), matching the
    constraint, and the judgement-consumption path (``cluster_and_merge``) is untouched — so
    this does not change how judgements are read (relevant to the H-1 follow-up).
    """
    values = [
        {
            "id": str(uuid.uuid4()),
            "left_id": left,
            "right_id": right,
            "judgement": verdict,
            "source": "signoff",
        }
        for left, right in itertools.combinations(sorted(set(source_ids)), 2)
    ]
    if not values:
        return
    session.execute(
        pg_insert(ResolverJudgement)
        .values(values)
        .on_conflict_do_nothing(constraint="uq_resolver_judgement_pair")
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
    canonical_id: str,
    approver: str,
    reason: str = "",
) -> SignOffResult:
    """Promote a parked merge: write the canonical entity + its outbound edges.

    Persists a POSITIVE judgement (so future batches always merge these members) and a
    ``sign_off`` row, marks the queue rows resolved, and flips the audit to ``merged``.
    Idempotent: an already-merged audit is a no-op; an already-rejected one is refused.
    """
    audit = _require_audit(session, canonical_id)
    if audit.decision == "merged":
        return SignOffResult(canonical_id, "approved", 0, 0, already_applied=True)
    if audit.decision == "rejected":
        raise SignOffError(f"merge {canonical_id!r} was already rejected; cannot approve")

    source_ids = list(audit.source_ids)
    # Orphan guard (mirror of reject's): a parked cluster is never written during resolution,
    # so a member node already in the graph means a prior REJECT wrote the members standalone.
    # Promoting now would strand those member nodes beside the canonical (append-only — no
    # delete). Direct the operator to complete the reject instead.
    if _any_node_exists(neo4j, source_ids):
        raise SignOffError(
            f"merge {canonical_id!r} already has member node(s) in the graph (a reject was "
            "started); re-run reject to complete it rather than approving"
        )
    member_rows = _member_rows(session, source_ids)
    if not member_rows:
        raise SignOffError(f"parked merge {canonical_id!r} has no member rows to promote")

    canonical = _merge_members(canonical_id, [make_entity(r.raw_entity) for r in member_rows])
    referents = dict.fromkeys(source_ids, canonical_id)
    edges = _outbound_edges(session, source_ids)
    for edge in edges:
        rewrite_referents(edge, referents)
    # Graph write before the Postgres commit — idempotent on a re-run because the canonical
    # id is deterministic (ADR 0036 Part 1), so a crashed approve re-MERGEs the same node.
    write_entities(neo4j, [canonical, *edges])

    _record_judgements(session, source_ids, "positive")
    for row in member_rows:
        row.status = "resolved"
    audit.decision = "merged"
    session.add(_signoff_row(canonical_id, source_ids, "approved", approver, reason))
    # Gate P1 (ADR 0106): additive context-claim capture at the sign-off promote point — banks
    # each approved member's canonical anchors (P1 does NOT add statement/decision rows here;
    # that sign-off statement/decision gap is Gate P3, see the module docstring).
    record_context_claims(session, canonical_id, [make_entity(r.raw_entity) for r in member_rows])
    # Gate P3 (ADR 0108): co-commit the SoR spine writes beside the P1 context capture —
    # statements (so a rebuild reconstructs this human-approved merge), a decision row
    # attributing it to the operator, and the ledger self-row + member aliases (so
    # survivor_of resolves the collapsed members AND rewrites the outbound edges' endpoints,
    # SF-EDGE). SAME transaction as the SignOff/judgement rows: a crash before session.commit()
    # rolls them ALL back (SF-2); the graph write above is idempotent on the B-1 re-run.
    members = [make_entity(r.raw_entity) for r in member_rows]
    signoff_cluster = ResolvedCluster(
        canonical_id=canonical_id,
        member_ids=tuple(sorted({m.id for m in members if m.id})),
        entity=canonical,
        score=audit.score,
    )
    by_id = {m.id: m for m in members if m.id}
    if signoff_cluster.is_merge:
        record_durable_id(
            session, canonical_id, member_ids=list(signoff_cluster.member_ids), prior_id=None
        )
    record_statements(session, signoff_cluster, by_id)
    if signoff_cluster.is_merge:
        record_decision(session, signoff_cluster, reason=reason, decided_by=f"operator:{approver}")
    session.commit()
    return SignOffResult(canonical_id, "approved", 1, len(edges))


def reject(
    session: Session,
    neo4j: Neo4jClient,
    *,
    canonical_id: str,
    approver: str,
    reason: str = "",
) -> SignOffResult:
    """Reject a parked merge: write each member as its own entity (+ its outbound edges).

    Persists a NEGATIVE judgement (so future batches never re-merge these members) and a
    ``sign_off`` row, marks the queue rows resolved, and flips the audit to ``rejected``.
    Idempotent: an already-rejected audit is a no-op; an already-merged one is refused.
    """
    audit = _require_audit(session, canonical_id)
    if audit.decision == "rejected":
        return SignOffResult(canonical_id, "rejected", 0, 0, already_applied=True)
    if audit.decision == "merged":
        raise SignOffError(f"merge {canonical_id!r} was already approved; cannot reject")

    # Orphan guard: a parked cluster is never written during resolution, so a canonical node
    # in the graph means a prior approve already wrote it. Completing a reject would write the
    # members alongside that node and strand it (append-only — no delete). Direct the operator
    # to complete the approve instead. (A crashed reject writes only members under their own
    # ids — no canonical node — so a same-op reject re-run is unaffected by this guard.)
    if _node_exists(neo4j, canonical_id):
        raise SignOffError(
            f"merge {canonical_id!r} already has a canonical node in the graph (an approve was "
            "started); re-run approve to complete it rather than rejecting"
        )

    source_ids = list(audit.source_ids)
    member_rows = _member_rows(session, source_ids)
    if not member_rows:
        raise SignOffError(f"parked merge {canonical_id!r} has no member rows to write")

    members = [make_entity(r.raw_entity) for r in member_rows]
    # Members keep their own ids, so their outbound edges already reference them — no
    # rewrite needed; just write the members alongside their edges. Idempotent on a re-run
    # (MERGE on each member's own id).
    edges = _outbound_edges(session, source_ids)
    write_entities(neo4j, [*members, *edges])

    _record_judgements(session, source_ids, "negative")
    for row in member_rows:
        row.status = "resolved"
    audit.decision = "rejected"
    session.add(_signoff_row(canonical_id, source_ids, "rejected", approver, reason))
    # Gate P3 (ADR 0108): co-commit statement rows for EACH rejected member kept as its own
    # entity, so a rebuild reconstructs the reject-written member nodes (else a full rebuild
    # silently drops them — consult §6b). Each member is its own canonical/survivor: NO merge
    # decision, NO ledger alias. Outbound edges (unrewritten, endpoints = member ids)
    # reconstruct from their own pipeline promotion (SF-EDGE). SAME transaction (SF-2).
    for member in members:
        if member.id is None:
            continue
        member_cluster = ResolvedCluster(
            canonical_id=member.id, member_ids=(member.id,), entity=member, score=1.0
        )
        record_statements(session, member_cluster, {member.id: member})
    session.commit()
    return SignOffResult(canonical_id, "rejected", len(members), len(edges))


def _signoff_row(
    canonical_id: str,
    source_ids: list[str],
    decision: str,
    approver: str,
    reason: str,
) -> SignOff:
    return SignOff(
        id=str(uuid.uuid4()),
        canonical_id=canonical_id,
        source_ids=source_ids,
        decision=decision,
        approver=approver,
        reason=reason,
    )
