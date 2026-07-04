"""Integration tests for Gate 2a — Statement spine, step 1 (ADR 0099).

Mirrors tests/integration/test_resolution_pipeline.py's seed shape (clear dup pair +
distinct entity + sanctioned dup pair) and exercises the full statement + decision
dual-write path end-to-end through resolve_pending.

Invariants verified:
- Every PROMOTED cluster has statement rows keyed by its canonical_id.
- Every promoted MERGE (is_merge=True) has exactly one decision row (kind="merge",
  decided_by="auto:resolver", member_ids/score consistent with merge_audit).
- The SANCTIONED (parked) cluster has NO statement rows and NO decision row.
- Dual-write on vs. off fence: the Neo4j node content and the merge_audit rows are
  byte-identical by content (excluding uuid/created_at) whether dual-write runs or is
  monkeypatched to no-ops — proving the dual-write is a pure side-effect.

All tests are RED on the current tree: the module-level import of StatementRecord and
DecisionRecord from worldmonitor.db.models fails because those classes do not exist yet.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text

from worldmonitor.db.engine import create_all, make_engine, session_factory

# ----- GATE IMPORTS — fail at collection until builder creates them (RED for right reason) -----
from worldmonitor.db.models import (  # noqa: E402
    Base,
    DecisionRecord,
    ErQueueItem,
    MergeAudit,
    StatementRecord,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Seed helpers — mirror test_resolution_pipeline.py exactly
# ---------------------------------------------------------------------------


def _queue_item(entity: dict[str, object]) -> ErQueueItem:
    """Build a stamped ErQueueItem from a raw entity dict.

    Mirrors _queue_item in test_resolution_pipeline.py: every queued entity
    must carry prov_* so the writer does not fail closed (ADR 0060).
    """
    source_record = f"s3://landing/{entity['id']}.json"
    stamped = stamp(
        make_entity(entity),
        Provenance(
            source_id="src:statement-spine-test",
            retrieved_at="2026-06-21T00:00:00Z",
            reliability="A",
            source_record=source_record,
        ),
    )
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity=stamped.to_dict(),
        source_record=source_record,
        status="pending",
    )


def _candidates() -> list[dict[str, Any]]:
    """Canonical fixture: clear dup pair + distinct entity + sanctioned dup pair.

    Same fixture as test_resolution_pipeline.py so existing pipeline behaviour is
    a known baseline we can compare against.
    """
    return [
        {
            "id": "c1",
            "schema": "Company",
            "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "c2",
            "schema": "Company",
            "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "c3",
            "schema": "Company",
            "properties": {"name": ["Globex Incorporated"], "jurisdiction": ["gb"]},
            "datasets": ["t"],
        },
        {
            "id": "p1",
            "schema": "Person",
            "properties": {
                "name": ["Vladimir Example"],
                "nationality": ["ru"],
                "birthDate": ["1960-01-01"],
                "topics": ["sanction"],
            },
            "datasets": ["t"],
        },
        {
            "id": "p2",
            "schema": "Person",
            "properties": {
                "name": ["Vladimir Example"],
                "nationality": ["ru"],
                "birthDate": ["1960-01-01"],
            },
            "datasets": ["t"],
        },
    ]


# ---------------------------------------------------------------------------
# Test 1: every promoted cluster has statement rows
# ---------------------------------------------------------------------------


def test_promoted_clusters_have_statement_rows(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """After resolve_pending, each PROMOTED cluster has at least one statement row.

    The fixture produces two promoted clusters:
    - {c1,c2} merged → canonical_id is a wmc-<hash> or anchor-derived id
    - {c3} singleton

    Both must have statement rows keyed by their canonical_id.

    RED today: ImportError — StatementRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # Verify the pipeline still behaves as before (regression guard)
    assert stats.promoted == 2, (
        f"Expected 2 promoted clusters ({'{c1,c2}'} + {'{c3}'}), got {stats.promoted}"
    )
    assert stats.review == 1, f"Expected 1 parked cluster, got {stats.review}"

    with sessions() as session:
        promoted_rows = list(
            session.execute(select(MergeAudit).where(MergeAudit.decision == "merged")).scalars()
        )
        assert len(promoted_rows) == 2, (
            f"Expected 2 merge_audit rows with decision='merged', got {len(promoted_rows)}"
        )

        for audit_row in promoted_rows:
            stmt_count = session.execute(
                select(func.count())
                .select_from(StatementRecord)
                .where(StatementRecord.canonical_id == audit_row.canonical_id)
            ).scalar_one()
            assert stmt_count > 0, (
                f"Promoted cluster canonical_id={audit_row.canonical_id!r} "
                f"(source_ids={audit_row.source_ids}) has 0 statement rows — "
                "every promoted cluster must have at least one statement row (ADR 0099)"
            )

    engine.dispose()


# ---------------------------------------------------------------------------
# Test 2: each promoted MERGE has exactly one decision row
# ---------------------------------------------------------------------------


def test_promoted_merges_have_exactly_one_decision_row(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """Each promoted MERGE (is_merge=True) has exactly one decision row; singleton has none.

    Expected outcome:
    - {c1,c2} merge → 1 decision row (kind="merge", decided_by="auto:resolver",
      member_ids=["c1","c2"], score>0)
    - {c3} singleton → 0 decision rows
    Total: 1 decision row.

    RED today: ImportError — DecisionRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    with sessions() as session:
        total_decisions = session.execute(
            select(func.count()).select_from(DecisionRecord)
        ).scalar_one()
        assert total_decisions == 1, (
            f"Expected exactly 1 decision row (for the c1+c2 merge), got {total_decisions}. "
            "The singleton {c3} must NOT produce a decision row."
        )

        decision = session.execute(select(DecisionRecord)).scalar_one()

        assert decision.kind == "merge", (
            f"decision.kind={decision.kind!r} — must be 'merge' (ADR 0099)"
        )
        assert decision.decided_by == "auto:resolver", (
            f"decision.decided_by={decision.decided_by!r} — must be 'auto:resolver'"
        )
        assert sorted(decision.member_ids) == ["c1", "c2"], (
            f"decision.member_ids (sorted)={sorted(decision.member_ids)} — "
            "must be ['c1','c2'] (the two merged Company entities)"
        )
        assert decision.score > 0.0, (
            f"decision.score={decision.score} — must be a positive probability"
        )
        assert decision.supersedes is None, (
            f"decision.supersedes={decision.supersedes!r} — must be NULL in step 1"
        )
        assert decision.superseded_by is None, (
            f"decision.superseded_by={decision.superseded_by!r} — must be NULL in step 1"
        )

        # decision.canonical_id must match the merge_audit row for the c1+c2 cluster
        # Fetch both promoted audit rows and find the one for c1+c2
        all_audits = list(
            session.execute(select(MergeAudit).where(MergeAudit.decision == "merged")).scalars()
        )
        c1c2_audit = next(
            (a for a in all_audits if sorted(a.source_ids) == ["c1", "c2"]),
            None,
        )
        assert c1c2_audit is not None, "Could not find merge_audit row for c1+c2"
        assert decision.canonical_id == c1c2_audit.canonical_id, (
            f"decision.canonical_id={decision.canonical_id!r} != "
            f"merge_audit.canonical_id={c1c2_audit.canonical_id!r} — "
            "decision and merge_audit for the same cluster must share canonical_id"
        )

    engine.dispose()


# ---------------------------------------------------------------------------
# Test 3: parked cluster has NO statement or decision rows
# ---------------------------------------------------------------------------


def test_parked_cluster_no_statement_or_decision_rows(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """A sanctioned (parked / pending_review) cluster writes NO statement or decision rows.

    The {p1,p2} pair is parked because it contains a sanctioned Person. After resolve_pending
    in block mode:
    - merge_audit has a pending_review row for the p1+p2 canonical_id
    - StatementRecord must have 0 rows for that canonical_id
    - DecisionRecord must have 0 rows for that canonical_id

    RED today: ImportError — StatementRecord, DecisionRecord do not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    with sessions() as session:
        parked_audit = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        parked_canonical_id = parked_audit.canonical_id

        assert sorted(parked_audit.source_ids) == ["p1", "p2"], (
            f"Unexpected parked source_ids={parked_audit.source_ids} — "
            "expected ['p1','p2'] (the sanctioned Person pair)"
        )

        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == parked_canonical_id)
        ).scalar_one()
        assert stmt_count == 0, (
            f"PARKED INVARIANT VIOLATED: {stmt_count} statement row(s) found for parked "
            f"cluster canonical_id={parked_canonical_id!r} — a pending_review cluster "
            "must write NO statement rows (ADR 0099 / pipeline 'continue' gate)"
        )

        decision_count = session.execute(
            select(func.count())
            .select_from(DecisionRecord)
            .where(DecisionRecord.canonical_id == parked_canonical_id)
        ).scalar_one()
        assert decision_count == 0, (
            f"PARKED INVARIANT VIOLATED: {decision_count} decision row(s) found for "
            f"parked cluster canonical_id={parked_canonical_id!r} — a pending_review "
            "cluster must write NO decision rows (ADR 0099 / pipeline 'continue' gate)"
        )

    engine.dispose()


# ---------------------------------------------------------------------------
# Test 4: dual-write on vs off — pure side-effect fence
# ---------------------------------------------------------------------------


def test_dual_write_on_vs_off_neo4j_and_merge_audit_identical(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dual-write is a pure side-effect: Neo4j nodes + merge_audit are identical
    whether the statement/decision writers run or are monkeypatched to no-ops.

    The builder imports record_statements and record_decision into pipeline.py as
    module-level names, so the correct patch targets are:
      'worldmonitor.resolution.pipeline.record_statements'
      'worldmonitor.resolution.pipeline.record_decision'

    This test verifies the Decision B invariant from ADR 0099: "projected Neo4j entity
    + merge_audit are byte-identical (by content) with dual-write on or off."

    RED today: ImportError — StatementRecord, DecisionRecord do not exist; additionally
    'worldmonitor.resolution.pipeline.record_statements' does not exist yet, so the
    monkeypatch.setattr call would raise AttributeError.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    # ----- Scenario A: dual-write ON (normal pipeline) -----
    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # Collect content-stable Neo4j node signatures (exclude uuid-like dynamic fields)
    node_rows_a = sorted(
        clean_graph.execute_read("MATCH (n) RETURN n.id AS nid, labels(n) AS lbls ORDER BY n.id"),
        key=lambda r: r["nid"] or "",
    )
    nodes_a: list[tuple[str, tuple[str, ...]]] = [
        (str(r["nid"] or ""), tuple(sorted(r["lbls"] or []))) for r in node_rows_a
    ]

    with sessions() as session:
        audit_a = sorted(
            (a.canonical_id, tuple(sorted(a.source_ids)), a.decision, a.reason or "")
            for a in session.execute(select(MergeAudit)).scalars()
        )

    # ----- Reset between scenarios -----
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    with sessions() as session:
        all_table_names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        session.execute(text(f"TRUNCATE {all_table_names} RESTART IDENTITY CASCADE"))
        session.commit()

    # ----- Scenario B: dual-write OFF (monkeypatched to no-ops) -----
    # These setattr calls will fail with AttributeError until the builder imports
    # record_statements and record_decision into pipeline.py.
    monkeypatch.setattr(
        "worldmonitor.resolution.pipeline.record_statements",
        lambda *_args, **_kw: None,
    )
    monkeypatch.setattr(
        "worldmonitor.resolution.pipeline.record_decision",
        lambda *_args, **_kw: None,
    )

    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    node_rows_b = sorted(
        clean_graph.execute_read("MATCH (n) RETURN n.id AS nid, labels(n) AS lbls ORDER BY n.id"),
        key=lambda r: r["nid"] or "",
    )
    nodes_b: list[tuple[str, tuple[str, ...]]] = [
        (str(r["nid"] or ""), tuple(sorted(r["lbls"] or []))) for r in node_rows_b
    ]

    with sessions() as session:
        audit_b = sorted(
            (a.canonical_id, tuple(sorted(a.source_ids)), a.decision, a.reason or "")
            for a in session.execute(select(MergeAudit)).scalars()
        )

    # ----- Assert identical (content-stable comparison, excludes uuid/created_at) -----
    assert nodes_a == nodes_b, (
        "DUAL-WRITE FENCE VIOLATED: Neo4j node set differs with vs without dual-write.\n"
        f"  with dual-write:    {nodes_a}\n"
        f"  without dual-write: {nodes_b}\n"
        "The dual-write must be a pure side-effect — it must never alter the graph write."
    )
    assert audit_a == audit_b, (
        "DUAL-WRITE FENCE VIOLATED: merge_audit content differs with vs without dual-write.\n"
        f"  with dual-write:    {audit_a}\n"
        f"  without dual-write: {audit_b}\n"
        "The dual-write must be a pure side-effect — it must never alter the audit trail."
    )

    engine.dispose()
