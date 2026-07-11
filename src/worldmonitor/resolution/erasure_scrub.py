"""Gate P2 — right-to-forget reaches the SoR: the sanctioned log-scrub + live-value-prune
(ADR 0107).

Today's cross-store ``erase_source`` (``erasure.py`` + ``graph/ops.py::erase_source_graph``)
never touches the Gate-2a/P1 statement + decision + context-claim log: a ``full_rebuild`` fold
resurrects every erased claim, and the live-graph prune is prop-granular (a co-witnessed value
or a bare anchor key contributed ONLY by the erased source survives). This module closes both
gaps, as the SANCTIONED SECOND exception to append-only (alongside ``erase_source_graph`` itself,
ADR 0049 / CLAUDE.md) — confined to these two entry points and PROVEN unreachable from any normal
writer/pipeline/agent path (``tests/integration/test_erasure_scrub.py::
test_it_erase_appendonly_a_...`` / ``..._b_...``).

Public surface (SF-1/SF-2/SF-4/SF-5, spec ``docs/reviews/GATE_P2_ERASURE_SPEC.md`` §2):

* :func:`scrub_log_lanes` — DELETEs the erased source's ``statement`` + ``context_claim`` rows
  (reached by ``(dataset == source_id) OR (entity_id IN erased_member_ids)``) and REDACTS
  ``decision.member_ids`` for every decision that referenced an erased member. Rows are
  PRESERVED (redacted, never dropped); ``canonical_id_ledger`` / ``ResolverJudgement`` /
  ``SignOff`` / ``MergeAudit`` are NEVER touched (the ADR-0049 no-un-merge carve-out).
* :func:`prune_live_to_fold` — for every survivor touched by the scrub, reconstructs the
  post-scrub target via the FROZEN pure :func:`~worldmonitor.resolution.projector.
  reconstruct_entities` (the single source of truth — no second, hand-rolled value-diff) and
  prunes the LIVE node to match via :func:`~worldmonitor.graph.ops.set_node_values`
  (provenance-preserving; anchors REMOVE-only, never SET — a Neo4j ``UNIQUE`` constraint on
  every ``CANONICAL_ID_FIELDS`` prop makes a surfaced conflict-resolved anchor value a
  transaction-aborting hazard).
* :func:`scrub_stock` — the one-off retroactive driver over the dual-write window: enumerates
  every erasure via ``TaskRun(kind="erase", status="ok").stats["source_id"]`` (read PYTHON-SIDE,
  never a Postgres-only ``stats->>`` query, so this also runs on the Docker-free SQLite unit
  lane) and scrubs + prunes each distinct source exactly once, idempotently.

Cross-store non-atomicity (mirrors ``erasure.py``'s existing split — plan-verify LOW):
:func:`scrub_log_lanes`'s DELETEs / redactions are STAGED on ``session`` for the CALLER to
commit; :func:`prune_live_to_fold` writes Neo4j IMMEDIATELY. A crash (or a commit failure)
between the two leaves the live graph pruned but the log un-scrubbed — a ``full_rebuild`` taken
in that window can momentarily resurrect the erased-only values onto the already-pruned live
graph. The contract is **idempotent-retry-recovers**: re-running both calls (this time
committed) re-scrubs the log and re-prunes the live graph to convergence — resurrection is
transient, never permanent (pinned by
``tests/integration/test_erasure_scrub.py::test_it_erase_idempotent_b_cross_store_crash_recovery_converges``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, cast

from followthemoney import registry
from sqlalchemy import CursorResult, delete, or_, select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ContextClaimRecord, DecisionRecord, StatementRecord, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.ops import set_node_values
from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS, get_anchors
from worldmonitor.resolution.divergence import _excluded  # pyright: ignore[reportPrivateUsage]
from worldmonitor.resolution.projector import build_survivor_of, reconstruct_entities


@dataclass(frozen=True, slots=True)
class LogScrubResult:
    """Per-source counts + identifiers from one :func:`scrub_log_lanes` call."""

    source_id: str
    erased_member_ids: frozenset[str]
    """Every ``entity_id`` reached (DISTINCT, computed BEFORE the delete) — the SF-1 member set."""
    touched_survivors: frozenset[str]
    """``survivor_of(canonical_id)`` for every REACHED row — :func:`prune_live_to_fold`'s input."""
    statements_scrubbed: int
    context_claims_scrubbed: int
    decisions_redacted: int


def scrub_log_lanes(session: Session, source_id: str) -> LogScrubResult:
    """DELETE the erased source's ``statement``/``context_claim`` rows + redact ``decision`` refs.

    Ordering is LOAD-BEARING (ADR 0107 surprise #2): ``erased_member_ids`` — and the reach
    predicate's canonical-id read for :attr:`LogScrubResult.touched_survivors` — are BOTH
    computed BEFORE any DELETE. A delete-first implementation would compute an EMPTY
    ``erased_member_ids`` (the SELECT would then find nothing left to match).

    Reach predicate (SF-1, the fallback-keyed residual closure): a ``statement``/``context_claim``
    row is reached iff ``dataset == source_id`` OR ``entity_id IN erased_member_ids`` — so a
    member with at least one ``dataset == source_id`` row ALSO loses its ``member.id``-keyed
    fallback row (``resolution/statements.py:196``'s ``dataset = prov.source_id or member.id or
    ""`` residual).

    Decision redaction (SF-2): ``DecisionRecord.member_ids`` is a plain JSONB column with NO
    ``MutableList``/``as_mutable`` wrapper, so the redaction REASSIGNS a new list — an in-place
    ``.remove()`` is invisible to SQLAlchemy's change-detection and would silently NOT persist.
    The row itself (``kind``/``score``/``decided_by``/``evidence``/surviving members) is always
    PRESERVED, never deleted. ``canonical_id_ledger`` / ``ResolverJudgement`` / ``SignOff`` /
    ``MergeAudit`` are NEVER touched here (the ADR-0049 no-un-merge carve-out).

    Caller commits (staged on ``session``, mirrors ``erasure.py``'s existing DB-write split).
    """
    erased_member_ids: set[str] = set(
        session.execute(
            select(StatementRecord.entity_id).where(StatementRecord.dataset == source_id)
        ).scalars()
    )
    erased_member_ids |= set(
        session.execute(
            select(ContextClaimRecord.entity_id).where(ContextClaimRecord.dataset == source_id)
        ).scalars()
    )

    stmt_reach = or_(
        StatementRecord.dataset == source_id, StatementRecord.entity_id.in_(erased_member_ids)
    )
    ctx_reach = or_(
        ContextClaimRecord.dataset == source_id,
        ContextClaimRecord.entity_id.in_(erased_member_ids),
    )

    # touched_survivors: the (pre-delete) canonical ids of every REACHED row, resolved through
    # the FULL canonical_id_ledger — the SF-4 live-prune orchestrator's input.
    survivor_of = build_survivor_of(session)
    reached_canonical_ids: set[str] = set(
        session.execute(select(StatementRecord.canonical_id).where(stmt_reach)).scalars()
    )
    reached_canonical_ids |= set(
        session.execute(select(ContextClaimRecord.canonical_id).where(ctx_reach)).scalars()
    )
    touched_survivors = {survivor_of(canonical_id) for canonical_id in reached_canonical_ids}

    stmt_delete_result = cast(
        "CursorResult[Any]", session.execute(delete(StatementRecord).where(stmt_reach))
    )
    ctx_delete_result = cast(
        "CursorResult[Any]", session.execute(delete(ContextClaimRecord).where(ctx_reach))
    )
    statements_scrubbed = stmt_delete_result.rowcount or 0
    context_claims_scrubbed = ctx_delete_result.rowcount or 0

    # erased_refs = erased_member_ids ∪ {entity_id of every reached row}. By construction every
    # reached row's entity_id is ALREADY a member of erased_member_ids (a dataset-matched row
    # contributed its own entity_id to the set; an entity_id-matched row matched BECAUSE its
    # entity_id is already in the set) — so the union is provably erased_member_ids itself; kept
    # as its own name for spec-fidelity / readability at the redaction call site.
    erased_refs = erased_member_ids
    decisions_redacted = 0
    if erased_refs:
        for row in session.execute(select(DecisionRecord)).scalars():
            remaining = [member_id for member_id in row.member_ids if member_id not in erased_refs]
            if len(remaining) != len(row.member_ids):
                row.member_ids = remaining  # REASSIGN — never in-place .remove() (SF-2)
                decisions_redacted += 1

    return LogScrubResult(
        source_id=source_id,
        erased_member_ids=frozenset(erased_member_ids),
        touched_survivors=frozenset(touched_survivors),
        statements_scrubbed=statements_scrubbed,
        context_claims_scrubbed=context_claims_scrubbed,
        decisions_redacted=decisions_redacted,
    )


def _read_node_props(neo4j: Neo4jClient, node_id: str) -> dict[str, Any] | None:
    """Read a live node's full current properties, or ``None`` if it no longer exists."""
    rows = neo4j.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN properties(n) AS props", id=node_id
    )
    if not rows:
        return None
    props = rows[0]["props"]
    return cast("dict[str, Any]", props) if isinstance(props, dict) else None


def prune_live_to_fold(
    session: Session, neo4j: Neo4jClient, touched_survivors: Iterable[str]
) -> None:
    """Prune every touched survivor's LIVE node to the fold's row-granular result (SF-4).

    For each survivor, reconstructs the post-scrub target via the FROZEN pure
    :func:`~worldmonitor.resolution.projector.reconstruct_entities` over the REMAINING (already
    scrubbed) ``statement``/``context_claim`` history — the single source of truth; no second,
    hand-rolled value-diff. ``compared_props`` is built from every NON-``_excluded`` property
    currently on the live node (``resolution.divergence._excluded`` — the SAME predicate the
    projection-divergence guard applies), set to the fold's value-set for that property (a
    property the fold no longer carries any value for is REMOVEd). Anchor keys
    (``ontology.anchors.CANONICAL_ID_FIELDS``) present on the live node but ABSENT from the
    fold's :func:`~worldmonitor.ontology.anchors.get_anchors` are REMOVEd — REMOVE-only, NEVER
    surfaced/SET (every anchor carries a Neo4j ``UNIQUE`` constraint,
    ``graph/constraints.py``; a surfaced conflict-resolved value could collide with another node
    and abort the erasure mid-transaction).

    A survivor whose live node is already gone (e.g. a sole-source node
    :func:`~worldmonitor.graph.ops.erase_source_graph` already ``DETACH DELETE``d) is a no-op —
    nothing left to prune. A survivor with NO remaining fold entity (every contributing row was
    erased) has every non-excluded live value/anchor removed (no evidence survives to keep it).

    Writes Neo4j IMMEDIATELY (see the module docstring's cross-store non-atomicity contract).
    """
    touched = {survivor for survivor in touched_survivors if survivor}
    if not touched:
        return

    survivor_of = build_survivor_of(session)
    remaining_stmt_rows = list(session.execute(select(StatementRecord)).scalars())
    remaining_ctx_rows = list(session.execute(select(ContextClaimRecord)).scalars())
    fold_by_id = {
        entity.id: entity
        for entity in reconstruct_entities(
            remaining_stmt_rows, survivor_of, context_claim_rows=remaining_ctx_rows
        )
        if entity.id is not None
    }

    for survivor in touched:
        current_props = _read_node_props(neo4j, survivor)
        if current_props is None:
            continue  # already gone (e.g. a sole-source node erase_source_graph DETACH DELETEd)

        fold_entity = fold_by_id.get(survivor)

        compared_props: dict[str, list[str]] = {}
        for prop_name in current_props:
            if _excluded(prop_name):
                continue
            fold_prop = fold_entity.schema.get(prop_name) if fold_entity is not None else None
            if (
                fold_entity is not None
                and fold_prop is not None
                and fold_prop.type != registry.entity
            ):
                compared_props[prop_name] = sorted(str(v) for v in fold_entity.get(fold_prop))
            else:
                compared_props[prop_name] = []

        fold_anchors = get_anchors(fold_entity) if fold_entity is not None else {}
        remove_anchor_keys = [
            key for key in CANONICAL_ID_FIELDS if key in current_props and key not in fold_anchors
        ]

        set_node_values(
            neo4j, survivor, compared_props=compared_props, remove_anchor_keys=remove_anchor_keys
        )


def scrub_stock(session: Session, *, neo4j: Neo4jClient) -> list[LogScrubResult]:
    """One-off retroactive stock scrub over the dual-write window (SF-5).

    Enumerates every erasure via ``TaskRun(kind="erase", status="ok").stats["source_id"]``,
    reading the JSONB ``stats`` payload PYTHON-SIDE — never a Postgres-only
    ``stats->>'source_id'`` query, which would break the Docker-free SQLite unit lane
    (``TaskRun.kind``/``TaskRun.status`` ARE plain column filters, SQLite-safe; only the JSONB
    payload access happens in Python). Scrubs each DISTINCT ``source_id`` exactly once
    (idempotent — a source already scrubbed reaches nothing on a repeat call) and applies the
    SF-4 live value/anchor prune for every survivor it touches. Operator-invoked (one-off),
    never autonomous — mirrors ``erase_source``'s own posture (no live API/runner/MCP caller).
    """
    task_runs = session.execute(
        select(TaskRun).where(TaskRun.kind == "erase", TaskRun.status == "ok")
    ).scalars()

    seen: set[str] = set()
    results: list[LogScrubResult] = []
    for run in task_runs:
        stats = run.stats
        source_id = stats.get("source_id") if isinstance(stats, dict) else None
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            continue
        seen.add(source_id)
        result = scrub_log_lanes(session, source_id)
        prune_live_to_fold(session, neo4j, result.touched_survivors)
        results.append(result)
    return results
