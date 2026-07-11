"""Integration tests for Gate P3 — sign-off spine durability (ADR 0108, spec §4).

Real Postgres (+ two Neo4j instances: the LIVE graph via ``clean_graph``, and a SECOND,
ISOLATED fold target defined in THIS file, mirroring
``tests/integration/test_projection_diff.py``'s ``diff_neo4j_client`` pattern — that file is
out of scope for this gate, so the fixture is duplicated here per the house per-file
self-containment convention) — Docker IS available locally; run this suite locally, not
CI-only (memory: docker-available-run-integration-locally).

Covers spec §4's integration items:

IT-SIGN-approve            A real park (``resolve_pending`` block-mode on a sanctioned Person
                            pair that ALSO promotes an ``Ownership`` edge whose owner is a
                            reviewed member — the exact ``_ownership`` shape from
                            ``tests/integration/test_signoff.py``, duplicated per-file
                            convention) -> ``approve(..., approver="op-x")``: asserts the
                            ``statement``/``decision``/``canonical_id_ledger`` rows, then
                            ``project(full_rebuild=True)`` into the isolated target yields a
                            NODE for the survivor (the un-dormanting — flips the P1 no-op) and
                            ``measure_divergence(...).total == 0`` (node AND the rewritten edge
                            explained).
IT-SIGN-reject              The same park -> ``reject(...)``: per-member statement rows, ZERO
                            decision rows, ZERO member aliases; ``full_rebuild`` yields each
                            member's OWN node + the unrewritten edge, ``.total == 0``.
IT-SIGN-atomic               The P-SIGN-3 success-then-forced-failure pair, at real-DB scale,
                            for BOTH approve and reject.
IT-SIGN-decided-by-bound     ``approver`` of length 246 (exact fit; ``decided_by`` == 255 chars
                            persists without truncation); the 247-char overflow is documented
                            (comment only — not a hard-failing assert, per spec §4).
IT-SIGN-pending-edge         An outbound edge still ``pending`` (never drained by
                            ``resolve_pending``) at ``approve()`` time is transiently
                            unexplained (``.total >= 1`` for the edge) until the edge gets its
                            own statement rows (its own pipeline promotion) — a pre-existing
                            ingestion-ordering property (SF-EDGE), NOT a P3 defect.
parked-SINGLETON approve     A parked cluster CAN be a singleton (``needs_review`` runs on
                            every cluster, ``pipeline.py:394``): statements bank under the
                            member's own id, ZERO decision rows, ZERO ledger rows,
                            ``.total == 0``.

All tests are RED today (assertion-adjacent, not ImportError — Gate P3 adds no new symbol):
``signoff.approve()``/``signoff.reject()`` write no statement/decision/ledger rows, so every
row-presence and ``measure_divergence(...).total == 0`` assertion below currently fails.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    CanonicalIdLedger,
    DecisionRecord,
    ErQueueItem,
    MergeAudit,
    SignOff,
    StatementRecord,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.snapshot import read_graph_snapshot
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import signoff
from worldmonitor.resolution.divergence import ProjectionDivergence, measure_divergence
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import build_survivor_of, project
from worldmonitor.resolution.statements import record_statements

pytestmark = pytest.mark.integration

_COMPUTED_AT = datetime(2026, 7, 11, tzinfo=UTC)

# A SECOND, independent Neo4j image/password — deliberately distinct literals from
# conftest.py's NEO4J_TEST_PASSWORD (never the same instance as `clean_graph`). Mirrors
# tests/integration/test_projection_diff.py's fixture, duplicated per-file convention.
_DIFF_NEO4J_IMAGE = "neo4j:2026.05.0-community"
_DIFF_NEO4J_PW = "testpw-p3-it-diff"  # pragma: allowlist secret


@pytest.fixture(scope="module")
def diff_neo4j_client() -> Iterator[Neo4jClient]:
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer(_DIFF_NEO4J_IMAGE, password=_DIFF_NEO4J_PW) as container:
        client = Neo4jClient.connect(
            uri=container.get_connection_url(), user="neo4j", password=_DIFF_NEO4J_PW
        )
        client.verify()
        yield client
        client.close()


@pytest.fixture
def clean_diff_graph(diff_neo4j_client: Neo4jClient) -> Neo4jClient:
    diff_neo4j_client.execute_write("MATCH (n) DETACH DELETE n")
    return diff_neo4j_client


# ---------------------------------------------------------------------------
# Seed helpers — the exact _sanctioned/_ownership shape from tests/integration/test_signoff.py,
# duplicated (that file is out of scope for this gate; sibling test modules are never imported).
# ---------------------------------------------------------------------------


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-07-11T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )
    entity = stamp(make_entity(data), provenance)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity=entity.to_dict(),
        source_record=provenance.source_record,
        status="pending",
    )


def _sanctioned(member_id: str, *, flag: bool = True) -> dict[str, object]:
    properties: dict[str, object] = {
        "name": ["Vladimir Example"],
        "nationality": ["ru"],
        "birthDate": ["1960-01-01"],
    }
    if flag:
        properties["topics"] = ["sanction"]
    return {"id": member_id, "schema": "Person", "properties": properties, "datasets": ["t"]}


def _ownership(edge_id: str, owner: str, asset: str) -> dict[str, object]:
    return {
        "id": edge_id,
        "schema": "Ownership",
        "properties": {"owner": [owner], "asset": [asset]},
        "datasets": ["t"],
    }


def _fold_divergence(
    sessions: sessionmaker[Session], live: Neo4jClient, diff: Neo4jClient, *, now: datetime
) -> ProjectionDivergence:
    diff.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        project(session, diff, full_rebuild=True, checkpoint_id="p3-it-diff")
    live_snapshot = read_graph_snapshot(live)
    fold_snapshot = read_graph_snapshot(diff)
    with sessions() as session:
        survivor_of = build_survivor_of(session)
    return measure_divergence(live_snapshot, fold_snapshot, survivor_of, computed_at=now)


# ===========================================================================
# IT-SIGN-approve
# ===========================================================================


def test_it_sign_approve_writes_spine_and_undormants_fold_node(
    clean_graph: Neo4jClient, clean_diff_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """IT-SIGN-approve: real park + approve() writes statement/decision/ledger rows; the
    fold un-dormants the sign-off survivor (flips the P1 no-op) and ``.total == 0`` for BOTH
    the node and its rewritten OWNS edge.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    acme = {
        "id": "itsa-acme",
        "schema": "Company",
        "properties": {"name": ["Acme Holdings"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    with sessions() as session:
        session.add(_queue_item(_sanctioned("itsa-p1"), source="itsa-p1"))
        session.add(_queue_item(_sanctioned("itsa-p2", flag=False), source="itsa-p2"))
        session.add(_queue_item(acme, source="itsa-acme"))
        session.add(_queue_item(_ownership("itsa-own", "itsa-p1", "itsa-acme"), source="itsa-own"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1

    with sessions() as session:
        canonical_id = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    with sessions() as session:
        result = signoff.approve(
            session,
            clean_graph,
            canonical_id=canonical_id,
            approver="op-x",
            reason="it-sign-approve",
        )
    assert result.decision == "approved"

    with sessions() as session:
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == canonical_id)
        ).scalar_one()
        assert stmt_count > 0, "IT-SIGN-approve: expected >= 1 statement row for the survivor"

        decision = session.execute(
            select(DecisionRecord).where(DecisionRecord.canonical_id == canonical_id)
        ).scalar_one()
        assert decision.kind == "merge"
        assert decision.decided_by == "operator:op-x"
        assert set(decision.member_ids) == {"itsa-p1", "itsa-p2"}

        ledger_self = session.execute(
            select(CanonicalIdLedger).where(
                CanonicalIdLedger.canonical_id == canonical_id,
                CanonicalIdLedger.canonical_alias == canonical_id,
            )
        ).scalar_one_or_none()
        assert ledger_self is not None, "IT-SIGN-approve: expected the ledger self-row"
        survivor_of = build_survivor_of(session)
        assert survivor_of("itsa-p1") == canonical_id
        assert survivor_of("itsa-p2") == canonical_id

    # --- Un-dormanting: project() now yields a NODE for the survivor (the P1 no-op flip) ---
    clean_diff_graph.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        project(session, clean_diff_graph, full_rebuild=True, checkpoint_id="p3-it-diff")
    fold_rows = clean_diff_graph.execute_read(
        "MATCH (n {id: $cid}) RETURN count(n) AS n", cid=canonical_id
    )
    assert fold_rows[0]["n"] == 1, (
        "IT-SIGN-approve UN-DORMANTING VIOLATED: expected a fold node for the sign-off "
        "survivor (P1 pinned this as a graceful no-op; P3 must un-dormant it)"
    )

    divergence = _fold_divergence(sessions, clean_graph, clean_diff_graph, now=_COMPUTED_AT)
    assert divergence.total == 0, (
        f"IT-SIGN-approve: expected .total==0, got {divergence.total} "
        f"(nodes={divergence.unexplained_nodes}, edges={divergence.unexplained_edges})"
    )

    owns_live = clean_graph.execute_read(
        "MATCH (:Person {id: $cid})-[r:OWNS]->(:Company {id: 'itsa-acme'}) RETURN count(r) AS n",
        cid=canonical_id,
    )
    assert owns_live[0]["n"] == 1
    engine.dispose()


# ===========================================================================
# IT-SIGN-reject
# ===========================================================================


def test_it_sign_reject_writes_per_member_statements_no_decision_no_alias(
    clean_graph: Neo4jClient, clean_diff_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """IT-SIGN-reject: real park + reject() writes per-member statement rows only; each
    member folds to its OWN node (+ the unrewritten edge), ``.total == 0``, ZERO decision
    rows, ZERO ledger aliases.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    acme = {
        "id": "itsr-acme",
        "schema": "Company",
        "properties": {"name": ["Acme Reject Ltd"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    with sessions() as session:
        session.add(_queue_item(_sanctioned("itsr-p1"), source="itsr-p1"))
        session.add(_queue_item(_sanctioned("itsr-p2", flag=False), source="itsr-p2"))
        session.add(_queue_item(acme, source="itsr-acme"))
        session.add(_queue_item(_ownership("itsr-own", "itsr-p1", "itsr-acme"), source="itsr-own"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1

    with sessions() as session:
        canonical_id = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    with sessions() as session:
        result = signoff.reject(
            session,
            clean_graph,
            canonical_id=canonical_id,
            approver="op-y",
            reason="it-sign-reject",
        )
    assert result.decision == "rejected"

    with sessions() as session:
        for member_id in ("itsr-p1", "itsr-p2"):
            stmt_count = session.execute(
                select(func.count())
                .select_from(StatementRecord)
                .where(StatementRecord.canonical_id == member_id)
            ).scalar_one()
            assert stmt_count > 0, f"IT-SIGN-reject: expected >= 1 statement row for {member_id!r}"

        decision_count = session.execute(
            select(func.count())
            .select_from(DecisionRecord)
            .where(DecisionRecord.canonical_id.in_(["itsr-p1", "itsr-p2", canonical_id]))
        ).scalar_one()
        assert decision_count == 0, "IT-SIGN-reject: expected ZERO decision rows"

        ledger_count = session.execute(
            select(func.count())
            .select_from(CanonicalIdLedger)
            .where(CanonicalIdLedger.canonical_alias.in_(["itsr-p1", "itsr-p2"]))
        ).scalar_one()
        assert ledger_count == 0, "IT-SIGN-reject: expected ZERO ledger alias rows"

        survivor_of = build_survivor_of(session)
        assert survivor_of("itsr-p1") == "itsr-p1"
        assert survivor_of("itsr-p2") == "itsr-p2"

    divergence = _fold_divergence(sessions, clean_graph, clean_diff_graph, now=_COMPUTED_AT)
    assert divergence.total == 0, (
        f"IT-SIGN-reject: expected .total==0, got {divergence.total} "
        f"(nodes={divergence.unexplained_nodes}, edges={divergence.unexplained_edges})"
    )

    owns_live = clean_graph.execute_read(
        "MATCH (:Person {id: 'itsr-p1'})-[r:OWNS]->(:Company {id: 'itsr-acme'}) "
        "RETURN count(r) AS n",
    )
    assert owns_live[0]["n"] == 1, (
        "the unrewritten edge (endpoint = the member's own id) must exist"
    )
    engine.dispose()


# ===========================================================================
# IT-SIGN-atomic (approve + reject, real-DB scale)
# ===========================================================================


def test_it_sign_atomic_approve_success_writes_rows_and_failure_rolls_back(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    # --- success ---
    with sessions() as session:
        session.add(_queue_item(_sanctioned("itsat-a-p1"), source="itsat-a-p1"))
        session.add(_queue_item(_sanctioned("itsat-a-p2", flag=False), source="itsat-a-p2"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1
    with sessions() as session:
        canonical_ok = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    with sessions() as session:
        real_commit = session.commit
        spy = MagicMock(side_effect=real_commit)
        session.commit = spy  # type: ignore[method-assign]
        result = signoff.approve(
            session,
            clean_graph,
            canonical_id=canonical_ok,
            approver="op-atomic-ok",
            reason="it-sign-atomic success",
        )
    assert result.decision == "approved"
    assert spy.call_count == 1

    with sessions() as session:
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == canonical_ok)
        ).scalar_one()
        assert stmt_count > 0
        decision_count = session.execute(
            select(func.count())
            .select_from(DecisionRecord)
            .where(DecisionRecord.canonical_id == canonical_ok)
        ).scalar_one()
        assert decision_count == 1
        signoff_count = session.execute(
            select(func.count()).select_from(SignOff).where(SignOff.canonical_id == canonical_ok)
        ).scalar_one()
        assert signoff_count == 1
        audit_decision = session.execute(
            select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_ok)
        ).scalar_one()
        assert audit_decision == "merged"

    # --- forced failure ---
    with sessions() as session:
        session.add(_queue_item(_sanctioned("itsat-b-p1"), source="itsat-b-p1"))
        session.add(_queue_item(_sanctioned("itsat-b-p2", flag=False), source="itsat-b-p2"))
        session.commit()
    with sessions() as session:
        stats2 = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats2.review == 1
    with sessions() as session:
        canonical_fail = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    with sessions() as session:

        def _raise_commit() -> None:
            raise RuntimeError("IT-SIGN-ATOMIC-FORCED-FAILURE")

        session.commit = _raise_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="IT-SIGN-ATOMIC-FORCED-FAILURE"):
            signoff.approve(
                session,
                clean_graph,
                canonical_id=canonical_fail,
                approver="op-atomic-fail",
                reason="it-sign-atomic failure",
            )

    with sessions() as session:
        audit_decision = session.execute(
            select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_fail)
        ).scalar_one()
        assert audit_decision == "pending_review"
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == canonical_fail)
        ).scalar_one()
        assert stmt_count == 0
        decision_count = session.execute(
            select(func.count())
            .select_from(DecisionRecord)
            .where(DecisionRecord.canonical_id == canonical_fail)
        ).scalar_one()
        assert decision_count == 0
        ledger_count = session.execute(
            select(func.count())
            .select_from(CanonicalIdLedger)
            .where(CanonicalIdLedger.canonical_id == canonical_fail)
        ).scalar_one()
        assert ledger_count == 0
        signoff_count = session.execute(
            select(func.count()).select_from(SignOff).where(SignOff.canonical_id == canonical_fail)
        ).scalar_one()
        assert signoff_count == 0
    engine.dispose()


def test_it_sign_atomic_reject_success_writes_rows_and_failure_rolls_back(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    # --- success ---
    with sessions() as session:
        session.add(_queue_item(_sanctioned("itsatr-a-p1"), source="itsatr-a-p1"))
        session.add(_queue_item(_sanctioned("itsatr-a-p2", flag=False), source="itsatr-a-p2"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1
    with sessions() as session:
        canonical_ok = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    with sessions() as session:
        real_commit = session.commit
        spy = MagicMock(side_effect=real_commit)
        session.commit = spy  # type: ignore[method-assign]
        result = signoff.reject(
            session,
            clean_graph,
            canonical_id=canonical_ok,
            approver="op-atomic-r-ok",
            reason="it-sign-atomic reject success",
        )
    assert result.decision == "rejected"
    assert spy.call_count == 1

    with sessions() as session:
        for member_id in ("itsatr-a-p1", "itsatr-a-p2"):
            stmt_count = session.execute(
                select(func.count())
                .select_from(StatementRecord)
                .where(StatementRecord.canonical_id == member_id)
            ).scalar_one()
            assert stmt_count > 0
        decision_count = session.execute(
            select(func.count())
            .select_from(DecisionRecord)
            .where(DecisionRecord.canonical_id.in_(["itsatr-a-p1", "itsatr-a-p2", canonical_ok]))
        ).scalar_one()
        assert decision_count == 0
        signoff_count = session.execute(
            select(func.count()).select_from(SignOff).where(SignOff.canonical_id == canonical_ok)
        ).scalar_one()
        assert signoff_count == 1
        audit_decision = session.execute(
            select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_ok)
        ).scalar_one()
        assert audit_decision == "rejected"

    # --- forced failure ---
    with sessions() as session:
        session.add(_queue_item(_sanctioned("itsatr-b-p1"), source="itsatr-b-p1"))
        session.add(_queue_item(_sanctioned("itsatr-b-p2", flag=False), source="itsatr-b-p2"))
        session.commit()
    with sessions() as session:
        stats2 = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats2.review == 1
    with sessions() as session:
        canonical_fail = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    with sessions() as session:

        def _raise_commit() -> None:
            raise RuntimeError("IT-SIGN-ATOMIC-REJECT-FORCED-FAILURE")

        session.commit = _raise_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="IT-SIGN-ATOMIC-REJECT-FORCED-FAILURE"):
            signoff.reject(
                session,
                clean_graph,
                canonical_id=canonical_fail,
                approver="op-atomic-r-fail",
                reason="it-sign-atomic reject failure",
            )

    with sessions() as session:
        audit_decision = session.execute(
            select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_fail)
        ).scalar_one()
        assert audit_decision == "pending_review"
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id.in_(["itsatr-b-p1", "itsatr-b-p2"]))
        ).scalar_one()
        assert stmt_count == 0
        signoff_count = session.execute(
            select(func.count()).select_from(SignOff).where(SignOff.canonical_id == canonical_fail)
        ).scalar_one()
        assert signoff_count == 0
    engine.dispose()


# ===========================================================================
# IT-SIGN-decided-by-bound
# ===========================================================================


def test_it_sign_decided_by_bound_246_exact_fit_persists(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """decision.decided_by is String(255); ``"operator:"`` (9 chars) + a 246-char approver is
    the EXACT fit (255 total) and must persist WITHOUT truncation (ADR 0108 SF-3 boundary).

    The 247-char overflow is documented here, not exercised as a hard-failing assertion (per
    the gate spec §4 IT-SIGN-decided-by-bound): a 247-char approver makes ``decided_by`` 256
    chars — one past ``String(255)``. Whether that surfaces as a Postgres-driver-level
    truncation or a ``DataError`` is not a behaviour this gate pins; only the 246-char EXACT
    FIT is the hard invariant (the cosign-disclosed boundary, ADR 0108 §Decided SF-3).
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    approver = "a" * 246
    assert len(f"operator:{approver}") == 255

    with sessions() as session:
        session.add(_queue_item(_sanctioned("itsdb-p1"), source="itsdb-p1"))
        session.add(_queue_item(_sanctioned("itsdb-p2", flag=False), source="itsdb-p2"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1

    with sessions() as session:
        canonical_id = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    with sessions() as session:
        result = signoff.approve(
            session,
            clean_graph,
            canonical_id=canonical_id,
            approver=approver,
            reason="it-sign-decided-by-bound",
        )
    assert result.decision == "approved"

    with sessions() as session:
        decided_by = session.execute(
            select(DecisionRecord.decided_by).where(DecisionRecord.canonical_id == canonical_id)
        ).scalar_one()
    assert decided_by == f"operator:{approver}"
    assert len(decided_by) == 255, (
        f"expected the exact-fit 255-char decided_by, got {len(decided_by)}"
    )
    engine.dispose()


# ===========================================================================
# IT-SIGN-pending-edge (SF-EDGE honesty)
# ===========================================================================


def test_it_sign_pending_edge_transiently_unexplained_until_own_promotion(
    clean_graph: Neo4jClient, clean_diff_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """SF-EDGE honesty: an outbound edge still ``pending`` (never drained by
    ``resolve_pending``) at ``approve()`` time is transiently unexplained by the fold — a
    pre-existing ingestion-ordering property shared with the pipeline promote path, NOT a P3
    defect — until the edge gets its OWN statement rows (its own pipeline promotion), at which
    point P3's ledger alias rewrites the fold edge's endpoint to match the live rewrite.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    acme = {
        "id": "itspe-acme",
        "schema": "Company",
        "properties": {"name": ["Acme Pending Edge Ltd"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    with sessions() as session:
        session.add(_queue_item(_sanctioned("itspe-p1"), source="itspe-p1"))
        session.add(_queue_item(_sanctioned("itspe-p2", flag=False), source="itspe-p2"))
        session.add(_queue_item(acme, source="itspe-acme"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1

    with sessions() as session:
        canonical_id = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    # Queue the outbound edge AFTER the pair has parked — it is never drained by
    # resolve_pending in this test, so it still carries NO statement rows when approve() runs
    # (`_outbound_edges` is a full, status-agnostic scan — it picks it up regardless).
    with sessions() as session:
        session.add(
            _queue_item(_ownership("itspe-own", "itspe-p1", "itspe-acme"), source="itspe-own")
        )
        session.commit()

    with sessions() as session:
        result = signoff.approve(
            session,
            clean_graph,
            canonical_id=canonical_id,
            approver="op-pending-edge",
            reason="it-sign-pending-edge",
        )
    assert result.decision == "approved"
    assert result.edges_written == 1, (
        "approve() must pick up the still-pending outbound edge (SF-EDGE: _outbound_edges is "
        "a full, status-agnostic scan)"
    )

    live_edge = clean_graph.execute_read(
        "MATCH (:Person {id: $cid})-[r:OWNS]->(:Company {id: 'itspe-acme'}) RETURN count(r) AS n",
        cid=canonical_id,
    )
    assert live_edge[0]["n"] == 1, "precondition: the live edge must exist (approve() wrote it)"

    # --- Transiently unexplained: the edge has no statement rows of its own yet ---
    divergence_before = _fold_divergence(sessions, clean_graph, clean_diff_graph, now=_COMPUTED_AT)
    assert divergence_before.unexplained_edges >= 1, (
        f"IT-SIGN-pending-edge precondition VIOLATED: expected >= 1 unexplained edge before "
        f"the edge's own promotion, got {divergence_before.unexplained_edges}"
    )

    # --- The edge's OWN pipeline promotion (its own statement rows) — SF-EDGE ---
    with sessions() as session:
        own_row = session.execute(
            select(ErQueueItem).where(ErQueueItem.entity_id == "itspe-own")
        ).scalar_one()
        edge_entity = make_entity(own_row.raw_entity)
        cluster = ResolvedCluster(
            canonical_id="itspe-own", member_ids=("itspe-own",), entity=edge_entity, score=1.0
        )
        record_statements(session, cluster, {"itspe-own": edge_entity})
        session.commit()

    divergence_after = _fold_divergence(sessions, clean_graph, clean_diff_graph, now=_COMPUTED_AT)
    assert divergence_after.total == 0, (
        f"IT-SIGN-pending-edge VIOLATED: after the edge's own promotion, expected .total==0, "
        f"got {divergence_after.total} (nodes={divergence_after.unexplained_nodes}, "
        f"edges={divergence_after.unexplained_edges}) — the P3 ledger alias must rewrite the "
        "fold edge's endpoint to match the live rewrite (SF-EDGE)"
    )
    engine.dispose()


# ===========================================================================
# parked-SINGLETON approve
# ===========================================================================


def test_it_sign_approve_parked_singleton_writes_statements_no_decision_no_alias(
    clean_graph: Neo4jClient, clean_diff_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A parked cluster CAN be a singleton (``needs_review`` runs on every cluster,
    ``pipeline.py:394``) — ``approve()`` must still bank its statement rows (fold-explained)
    while skipping the decision/ledger writes entirely (``is_merge=False``).
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        session.add(_queue_item(_sanctioned("itss-p1"), source="itss-p1"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1
    assert stats.promoted == 0

    with sessions() as session:
        parked = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        canonical_id = parked.canonical_id
    assert canonical_id == "itss-p1", "a parked singleton keeps its own member id as canonical_id"

    with sessions() as session:
        result = signoff.approve(
            session,
            clean_graph,
            canonical_id=canonical_id,
            approver="op-singleton",
            reason="it-sign-singleton",
        )
    assert result.decision == "approved"

    with sessions() as session:
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == canonical_id)
        ).scalar_one()
        assert stmt_count > 0, (
            "IT-SIGN singleton: expected >= 1 statement row under the member's own id"
        )

        decision_count = session.execute(
            select(func.count())
            .select_from(DecisionRecord)
            .where(DecisionRecord.canonical_id == canonical_id)
        ).scalar_one()
        assert decision_count == 0, "a parked SINGLETON approve must write ZERO decision rows"

        ledger_count = session.execute(
            select(func.count())
            .select_from(CanonicalIdLedger)
            .where(CanonicalIdLedger.canonical_id == canonical_id)
        ).scalar_one()
        assert ledger_count == 0, (
            "a parked SINGLETON approve must write ZERO ledger rows (no self-row either)"
        )

    divergence = _fold_divergence(sessions, clean_graph, clean_diff_graph, now=_COMPUTED_AT)
    assert divergence.total == 0, f"IT-SIGN singleton: expected .total==0, got {divergence.total}"
    engine.dispose()
