"""Property/metamorphic tests for Gate 2a — Statement spine, step 1 (ADR 0099).

Three mandatory @given invariants (CLAUDE.md build-discipline):

P-STMT-1  Lossless projection — the set of persisted statement rows equals the set
          independently derived from the member entities (NO call to fuse_statement_rows
          in the oracle path). Exercised through a real Postgres round-trip so column
          length / NULL fidelity is proven.

P-STMT-2  Non-mutation fence — fuse_statement_rows + record_statements + record_decision
          leave cluster.entity.to_dict() and every by_id[m].to_dict() byte-identical to
          a before-snapshot; the writers session.add() ONLY StatementRecord / DecisionRecord
          (never MergeAudit, never an FtM entity mutation).

P-STMT-3  Exactly-one-decision + parked-writes-nothing — a promoted merge (is_merge=True)
          produces exactly ONE decision row consistent with its inputs; a promoted singleton
          produces statement rows but NO decision row; a parked (pending_review) cluster
          produces NO statement rows and NO decision row.

All three tests in this file are RED on the current tree because the module-level imports
of ``StatementRecord``, ``DecisionRecord``, ``fuse_statement_rows``, ``record_statements``,
and ``record_decision`` do not exist yet — they fail with ``ImportError`` at collection time.
That is the correct, intended TDD failure mode.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

import pytest
import strategies as wm
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from worldmonitor.db.engine import make_engine

# ----- GATE IMPORTS — fail at collection until builder creates them (RED for right reason) -----
from worldmonitor.db.models import Base, DecisionRecord, MergeAudit, StatementRecord  # noqa: E402
from worldmonitor.provenance.model import get_provenance
from worldmonitor.resolution.audit import record_merge
from worldmonitor.resolution.merge import ResolvedCluster, cluster_and_merge
from worldmonitor.resolution.splink_model import ScoredPair

# This import fails until builder creates worldmonitor/resolution/statements.py
from worldmonitor.resolution.statements import (  # noqa: E402
    fuse_statement_rows,
    record_decision,
    record_statements,
)

# ---------------------------------------------------------------------------
# SQLite JSONB shim (DecisionRecord.member_ids and .evidence are JSONB on Postgres;
# for SQLite in-memory sessions in P-STMT-3, compile JSONB as JSON instead).
# Same shim used by test_prop_landing_gc_reference_safety.py — idempotent if
# both files import in the same process.
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SETTINGS = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _chain_pairs(entities: list) -> list[ScoredPair]:
    """Build a chain of above-threshold scored pairs linking consecutive entities."""
    return [
        ScoredPair(entities[i].id or "", entities[i + 1].id or "", 0.95)
        for i in range(len(entities) - 1)
        if entities[i].id and entities[i + 1].id
    ]


def _fresh_sqlite_session() -> Session:
    """An isolated, in-memory SQLite session for one hypothesis example.

    Uses StaticPool + check_same_thread=False so the session can be created and used
    within a single example without thread-safety issues. Base.metadata.create_all
    builds every table (including the new statement / decision tables once the builder
    adds their ORM models) on a fresh engine per example — no cross-example contamination.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _oracle_dataset(member: Any) -> str:
    """Independent reproduction of merge._member_source WITHOUT importing that function.

    This is the ORACLE path for P-STMT-1 — it derives the dataset (source_id) that each
    member's statements should carry, directly from the member's Provenance, with the
    same fallback logic as _member_source. Never calls fuse_statement_rows.
    """
    prov = get_provenance(member)
    if prov is not None and prov.source_id:
        return prov.source_id
    return member.id or ""


# ===========================================================================
# P-STMT-1: Lossless projection (independent oracle, real Postgres round-trip)
# ===========================================================================


@pytest.mark.integration
@given(
    entities=st.lists(
        wm.source_tagged_entity(),
        min_size=2,
        max_size=4,
        unique_by=lambda e: e.id,
    )
)
@_SETTINGS
def test_p_stmt_1_lossless_projection(entities: list, postgres_dsn: str) -> None:
    """P-STMT-1: persisted statement rows are the EXACT lossless projection of the members.

    The oracle is constructed directly from the raw member entities — it NEVER calls
    fuse_statement_rows (which would make this a tautology). The oracle computes, for each
    member m in cluster.member_ids and each (prop, value) pair on m where prop != "id":
      (canonical_id, m.id, m.schema.name, prop, str(value), dataset_of(m))
    and verifies the persisted rows match that set exactly — none invented, none dropped.

    Uses a real Postgres round-trip so column types (String length, NULL propagation,
    server_default) are proven. Each example rolls back its writes so the session-scoped
    Postgres container is never polluted between examples.

    RED today: ImportError — StatementRecord, record_statements do not exist yet.
    """
    pairs = _chain_pairs(entities)
    if not pairs:
        return  # degenerate: no valid ids to form a chain; skip

    clusters = cluster_and_merge(entities, pairs)
    merged = [c for c in clusters if c.is_merge]
    if not merged:
        return  # shouldn't happen with chain pairs above threshold, but skip gracefully

    cluster = merged[0]
    by_id = {e.id: e for e in entities if e.id is not None}

    engine = make_engine(postgres_dsn)
    try:
        Base.metadata.create_all(engine)  # idempotent; adds new tables when builder adds models

        with Session(engine) as session:
            record_statements(session, cluster, by_id)
            session.flush()  # visible within this transaction, NEVER committed

            rows = list(
                session.execute(
                    select(StatementRecord).where(
                        StatementRecord.canonical_id == cluster.canonical_id
                    )
                ).scalars()
            )

            # ------ INDEPENDENT ORACLE — never calls fuse_statement_rows ------
            expected_tuples: set[tuple[str, str, str, str, str, str]] = set()
            for mid in cluster.member_ids:
                if mid not in by_id:
                    continue
                m = by_id[mid]
                src = _oracle_dataset(m)
                for prop in m.properties:
                    if prop == "id":
                        continue  # the "id" pseudo-property MUST be excluded
                    for value in m.get(prop):
                        expected_tuples.add(
                            (
                                cluster.canonical_id,
                                mid,
                                m.schema.name,
                                prop,
                                str(value),
                                src,
                            )
                        )

            actual_tuples = {
                (r.canonical_id, r.entity_id, r.schema, r.prop, r.value, r.dataset) for r in rows
            }

            assert actual_tuples == expected_tuples, (
                f"P-STMT-1 LOSSLESS PROJECTION VIOLATED for cluster {cluster.canonical_id!r}\n"
                f"  expected {len(expected_tuples)} claim tuple(s), got {len(actual_tuples)}\n"
                f"  invented (in actual, not in oracle): {actual_tuples - expected_tuples}\n"
                f"  dropped  (in oracle, not in actual): {expected_tuples - actual_tuples}"
            )

            # Explicit id-pseudo-statement exclusion guard
            id_rows = [r for r in rows if r.prop == "id"]
            assert not id_rows, (
                f"P-STMT-1: {len(id_rows)} 'id' pseudo-statement row(s) found — "
                "the id pseudo-property MUST be excluded from the statement log (G1)"
            )

            # Per-row G1 provenance: reliability / retrieved_at / raw_pointer
            # must match the member's Provenance
            for row in rows:
                mid = row.entity_id
                if mid not in by_id:
                    continue
                member = by_id[mid]
                prov = get_provenance(member)
                if prov is not None:
                    assert row.reliability == prov.reliability, (
                        f"P-STMT-1 G1: row.reliability={row.reliability!r} != "
                        f"prov.reliability={prov.reliability!r} for member {mid!r}"
                    )
                    assert row.retrieved_at == prov.retrieved_at, (
                        f"P-STMT-1 G1: row.retrieved_at={row.retrieved_at!r} != "
                        f"prov.retrieved_at={prov.retrieved_at!r} for member {mid!r}"
                    )
                    assert row.raw_pointer == prov.source_record, (
                        f"P-STMT-1 G1: row.raw_pointer={row.raw_pointer!r} != "
                        f"prov.source_record={prov.source_record!r} for member {mid!r}"
                    )
                else:
                    # Unstamped member: reliability must be NULL (no invented provenance)
                    assert row.reliability is None, (
                        f"P-STMT-1 G1: unstamped member {mid!r} must have NULL reliability, "
                        f"got {row.reliability!r} — no invented provenance (G1)"
                    )

            session.rollback()  # ALWAYS rollback — this example never commits to the container
    finally:
        engine.dispose()


# ===========================================================================
# P-STMT-2: Non-mutation fence
# ===========================================================================


@given(
    entities=st.lists(
        wm.source_tagged_entity(),
        min_size=2,
        max_size=4,
        unique_by=lambda e: e.id,
    )
)
@_SETTINGS
def test_p_stmt_2_non_mutation_fence(entities: list) -> None:
    """P-STMT-2: the dual-write writers are pure side-effects — they mutate nothing.

    Snapshot cluster.entity.to_dict() and every by_id[m].to_dict() BEFORE calling
    fuse_statement_rows, record_statements, and record_decision. Assert byte-identical AFTER.
    Assert that every session.add() call received ONLY a StatementRecord or DecisionRecord —
    never a MergeAudit, never a raw FtM entity dict.

    Uses a MagicMock session so no DB is needed; the mutation check is purely about Python
    objects. The absence of MergeAudit calls proves the writers do not touch the audit trail.

    RED today: ImportError — StatementRecord, DecisionRecord, fuse_statement_rows, etc.
    """
    pairs = _chain_pairs(entities)
    if not pairs:
        return

    clusters = cluster_and_merge(entities, pairs)
    merged = [c for c in clusters if c.is_merge]
    if not merged:
        return

    cluster = merged[0]
    by_id = {e.id: e for e in entities if e.id is not None}

    # Deep snapshots BEFORE any writer call
    entity_snapshot_before = copy.deepcopy(cluster.entity.to_dict())
    member_snapshots_before = {
        mid: copy.deepcopy(by_id[mid].to_dict()) for mid in cluster.member_ids if mid in by_id
    }

    # Call all three writers with a mock session (no DB needed for the mutation check)
    mock_session = MagicMock()
    _ = fuse_statement_rows(cluster, by_id)
    record_statements(mock_session, cluster, by_id)
    record_decision(mock_session, cluster, reason="test-reason")

    # ------ Assert no mutation to the cluster entity or any member entity ------
    entity_snapshot_after = cluster.entity.to_dict()
    assert entity_snapshot_after == entity_snapshot_before, (
        "P-STMT-2 NON-MUTATION: cluster.entity.to_dict() changed after writer calls.\n"
        f"  before: {entity_snapshot_before}\n"
        f"  after:  {entity_snapshot_after}"
    )

    for mid in cluster.member_ids:
        if mid not in by_id:
            continue
        member_after = by_id[mid].to_dict()
        assert member_after == member_snapshots_before[mid], (
            f"P-STMT-2 NON-MUTATION: by_id[{mid!r}].to_dict() changed after writer calls.\n"
            f"  before: {member_snapshots_before[mid]}\n"
            f"  after:  {member_after}"
        )

    # ------ Assert writers only session.add() StatementRecord or DecisionRecord ------
    added_args = [c.args[0] for c in mock_session.add.call_args_list]
    for arg in added_args:
        assert isinstance(arg, (StatementRecord, DecisionRecord)), (
            f"P-STMT-2 NON-MUTATION: session.add() received unexpected type "
            f"{type(arg).__name__!r}; "
            "writers must ONLY add StatementRecord or DecisionRecord (never MergeAudit)"
        )
        assert not isinstance(arg, MergeAudit), (
            "P-STMT-2 NON-MUTATION: session.add() received a MergeAudit row — "
            "record_statements and record_decision must never write to the audit trail"
        )


# ===========================================================================
# P-STMT-3a: Promoted merge → exactly ONE decision row
# ===========================================================================


@given(
    entities=st.lists(
        wm.source_tagged_entity(),
        min_size=2,
        max_size=4,
        unique_by=lambda e: e.id,
    )
)
@_SETTINGS
def test_p_stmt_3a_merged_cluster_exactly_one_decision_row(entities: list) -> None:
    """P-STMT-3a: a promoted merge writes exactly one decision row with correct fields.

    Calls record_decision(session, cluster, reason=reason) for a cluster with is_merge=True
    and asserts exactly ONE DecisionRecord row exists, with:
      - canonical_id == cluster.canonical_id
      - kind == "merge"
      - decided_by == "auto:resolver"
      - member_ids == list(cluster.member_ids) (same elements, order-independent)
      - score == cluster.score

    RED today: ImportError — DecisionRecord, record_decision do not exist yet.
    """
    pairs = _chain_pairs(entities)
    if not pairs:
        return

    clusters = cluster_and_merge(entities, pairs)
    merged = [c for c in clusters if c.is_merge]
    if not merged:
        return

    cluster = merged[0]
    assert cluster.is_merge, "test pre-condition: cluster must be a merge"
    reason = "test-reason-for-p-stmt-3a"

    session = _fresh_sqlite_session()
    record_decision(session, cluster, reason=reason)
    session.flush()

    rows = session.query(DecisionRecord).all()

    assert len(rows) == 1, (
        f"P-STMT-3a EXACTLY-ONE-DECISION: expected 1 decision row for promoted merge "
        f"{cluster.canonical_id!r}, got {len(rows)}"
    )

    row = rows[0]
    assert row.canonical_id == cluster.canonical_id, (
        f"P-STMT-3a: row.canonical_id={row.canonical_id!r} != "
        f"cluster.canonical_id={cluster.canonical_id!r}"
    )
    assert row.kind == "merge", (
        f"P-STMT-3a: row.kind={row.kind!r} — decision row must have kind='merge'"
    )
    assert row.decided_by == "auto:resolver", (
        f"P-STMT-3a: row.decided_by={row.decided_by!r} — must be 'auto:resolver'"
    )
    assert sorted(row.member_ids) == sorted(cluster.member_ids), (
        f"P-STMT-3a: row.member_ids={sorted(row.member_ids)} != "
        f"cluster.member_ids={sorted(cluster.member_ids)}"
    )
    assert row.score == cluster.score, (
        f"P-STMT-3a: row.score={row.score} != cluster.score={cluster.score}"
    )
    assert row.evidence == {"reason": reason}, (
        f"P-STMT-3a: row.evidence={row.evidence!r} != {{'reason': {reason!r}}}"
    )


# ===========================================================================
# P-STMT-3b: Promoted singleton → statement rows, NO decision row
# ===========================================================================


@given(entity=wm.source_tagged_entity())
@_SETTINGS
def test_p_stmt_3b_singleton_no_decision_row(entity: Any) -> None:
    """P-STMT-3b: a promoted singleton writes statement rows but NO decision row.

    Constructs a ResolvedCluster with a single member (is_merge=False), calls both
    record_statements AND record_decision (because record_decision must guard on
    cluster.is_merge internally and be a no-op for singletons), and asserts:
      - 0 decision rows in the decision table
      - >= 1 statement rows in the statement table (one per claim, if the entity has props)

    This pins the invariant that record_decision must be safe to call for singletons
    (returns without writing) — not just that the pipeline avoids calling it.

    RED today: ImportError — StatementRecord, DecisionRecord, record_statements,
    record_decision do not exist yet.
    """
    if not entity.id:
        return  # degenerate entity with no id; skip

    singleton = ResolvedCluster(
        canonical_id=entity.id,
        member_ids=(entity.id,),
        entity=entity,
        score=1.0,
    )
    assert not singleton.is_merge, "test pre-condition: singleton must not be is_merge"

    by_id = {entity.id: entity}
    session = _fresh_sqlite_session()

    record_statements(session, singleton, by_id)
    # record_decision must guard on is_merge — a no-op for a singleton
    record_decision(session, singleton, reason="should-not-write")
    session.flush()

    decision_rows = session.query(DecisionRecord).all()
    statement_rows = session.query(StatementRecord).all()

    assert len(decision_rows) == 0, (
        f"P-STMT-3b SINGLETON-NO-DECISION: singleton {entity.id!r} produced "
        f"{len(decision_rows)} decision row(s) — must be 0 (record_decision must "
        "be a no-op for is_merge=False)"
    )

    # A singleton with properties must produce statement rows (G1 per-claim)
    non_id_props = [p for p in entity.properties if p != "id"]
    if non_id_props:
        assert len(statement_rows) > 0, (
            f"P-STMT-3b: singleton {entity.id!r} with properties {non_id_props} "
            "produced 0 statement rows — singletons with claims must produce statement rows"
        )


# ===========================================================================
# P-STMT-3c: Parked cluster → NO statement rows AND NO decision rows
# ===========================================================================


@given(entity=wm.source_tagged_entity())
@_SETTINGS
def test_p_stmt_3c_parked_cluster_no_rows(entity: Any) -> None:
    """P-STMT-3c: a parked (pending_review) cluster produces NO statement or decision rows.

    Simulates the pipeline's parked path: only record_merge(decision="pending_review") is
    called (the pipeline's `continue` skips record_statements and record_decision). Asserts
    that after this path, the statement and decision tables are completely empty.

    This pins the invariant that the audit trail (MergeAudit) and the statement spine are
    INDEPENDENT — a pending_review row in merge_audit must never bleed into statement/decision.

    RED today: ImportError — StatementRecord, DecisionRecord do not exist yet.
    """
    if not entity.id:
        return

    cluster = ResolvedCluster(
        canonical_id=entity.id,
        member_ids=(entity.id,),
        entity=entity,
        score=1.0,
    )
    session = _fresh_sqlite_session()

    # Simulate the parked path: call ONLY record_merge (the pipeline does this then `continue`)
    record_merge(session, cluster, decision="pending_review", reason="sensitive-entity")
    session.flush()

    statement_rows = session.query(StatementRecord).all()
    decision_rows = session.query(DecisionRecord).all()

    assert len(statement_rows) == 0, (
        f"P-STMT-3c PARKED-WRITES-NOTHING: parked cluster {entity.id!r} produced "
        f"{len(statement_rows)} statement row(s) — a pending_review cluster must write "
        "NO statement rows (the pipeline's 'continue' must skip record_statements)"
    )
    assert len(decision_rows) == 0, (
        f"P-STMT-3c PARKED-WRITES-NOTHING: parked cluster {entity.id!r} produced "
        f"{len(decision_rows)} decision row(s) — a pending_review cluster must write "
        "NO decision rows (the pipeline's 'continue' must skip record_decision)"
    )
