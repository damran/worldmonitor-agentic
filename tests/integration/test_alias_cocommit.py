"""Integration tests for Gate WPI-2 — the alias<->co-commit invariant (ADR 0111).

Exercises the REAL ``project(full_rebuild=True)`` fold-side completeness check against real
Postgres + real Neo4j (testcontainers) — the same fixture shape as
``tests/integration/test_projector.py`` (``clean_graph`` + ``postgres_dsn`` + ``resolve_pending``
seeding via ``_candidates()``-style corpora).

INV-ALIAS-COCOMMIT (docs/reviews/GATE_WPI2_ALIAS_COCOMMIT_SPEC.md): for every supersession alias
``prior -> survivor`` in ``canonical_id_ledger``, the final survivor ``survivor_of(prior)`` must
have >= 1 ``StatementRecord`` folding into it at rebuild (statement lane only — the fold
materialises a node solely from statement rows; a context-only survivor materialises no node and
is WPI-1 / ADR 0112's concern). A ``full_rebuild`` fold over a log that violates this FAILS LOUD
(``IncompleteAliasedSurvivorError``) BEFORE any Neo4j write.

Tests
-----
IT-ALIAS-1  Fold-side negative: seed a healthy log via ``resolve_pending``, then hand-inject a
            supersession alias (``wpi2-ghost-prior -> wpi2-ghost-survivor``) whose target has NO
            statement/context row anywhere in the log. ``project(full_rebuild=True)`` must raise
            ``IncompleteAliasedSurvivorError``.

IT-ALIAS-2  Fold-side positive: the SAME healthy corpus, with NO ghost alias injected.
            ``project(full_rebuild=True)`` must NOT raise.

IT-ALIAS-3  Producer co-commit — pipeline: a real 2-member merge promoted through
            ``resolve_pending`` co-commits BOTH a ``CanonicalIdLedger`` supersession alias row AND
            >= 1 ``StatementRecord`` for the survivor in the SAME batch transaction (ADR 0111
            §Context, ``pipeline.py::_resolve_batch``); ``project(full_rebuild=True)`` over the
            resulting log raises nothing.

IT-ALIAS-4  Producer co-commit — sign-off: a parked (sanctioned) merge approved via
            ``signoff.approve(...)`` co-commits the SAME two artefacts in the SAME sign-off
            transaction (ADR 0111 §Context, ``signoff.py::approve``); ``project(full_rebuild=True)``
            over the resulting log raises nothing.

All tests are RED at collection time: the module-level import of
``IncompleteAliasedSurvivorError`` from ``worldmonitor.resolution.spine_integrity`` fails with
``ImportError`` because that module does not exist until the builder creates it (ADR 0111 option
(a)). IT-ALIAS-1/2/3/4 pin the behaviour end-to-end once it does.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    CanonicalIdLedger,
    ErQueueItem,
    MergeAudit,
    StatementRecord,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import signoff
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import project  # existing symbol, unaffected import
from worldmonitor.resolution.spine_integrity import (  # gate import: RED until builder lands it
    IncompleteAliasedSurvivorError,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Seed helpers — mirror test_projector.py / test_statement_spine.py exactly.
# ---------------------------------------------------------------------------


def _queue_item(entity: dict[str, object], *, source: str) -> ErQueueItem:
    source_record = f"s3://landing/{source}.json"
    stamped = stamp(
        make_entity(entity),
        Provenance(
            source_id="src:wpi2-alias-cocommit-test",
            retrieved_at="2026-07-12T00:00:00Z",
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


def _merge_pair_candidates(prefix: str) -> list[dict[str, object]]:
    """A clear 2-member dup pair — deterministic Splink merge (identical name+jurisdiction)."""
    return [
        {
            "id": f"{prefix}-a",
            "schema": "Company",
            "properties": {"name": ["Alias Cocommit Corp"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": f"{prefix}-b",
            "schema": "Company",
            "properties": {"name": ["Alias Cocommit Corp"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
    ]


def _sanctioned_pair_candidates(prefix: str) -> list[dict[str, object]]:
    """A clear 2-member dup pair carrying a sanction topic -> parks under guard_mode='block'."""
    base_props: dict[str, object] = {
        "name": ["Wpi2 Ghost Person"],
        "nationality": ["ru"],
        "birthDate": ["1970-01-01"],
    }
    flagged = dict(base_props)
    flagged["topics"] = ["sanction"]
    return [
        {"id": f"{prefix}-p1", "schema": "Person", "properties": flagged, "datasets": ["t"]},
        {"id": f"{prefix}-p2", "schema": "Person", "properties": base_props, "datasets": ["t"]},
    ]


# ===========================================================================
# IT-ALIAS-1 — fold-side negative: a ghost alias with no content row raises
# ===========================================================================


def test_it_alias_1_ghost_alias_with_no_content_row_raises(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _merge_pair_candidates("ita1"):
            session.add(_queue_item(candidate, source=str(candidate["id"])))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.promoted == 1, (
        f"IT-ALIAS-1 precondition: expected 1 promoted (merged) cluster, got {stats.promoted}"
    )

    # Hand-inject a supersession alias whose target has NO statement/context row anywhere.
    with sessions() as session:
        session.add(
            CanonicalIdLedger(
                id=str(uuid.uuid4()),
                canonical_alias="wpi2-ghost-prior",
                canonical_id="wpi2-ghost-survivor",
            )
        )
        session.commit()

    with sessions() as session, pytest.raises(IncompleteAliasedSurvivorError) as excinfo:
        project(session, clean_graph, full_rebuild=True)

    assert "wpi2-ghost-survivor" in str(excinfo.value), (
        "IT-ALIAS-1: IncompleteAliasedSurvivorError message must name the incomplete survivor "
        f"'wpi2-ghost-survivor'; got: {excinfo.value}"
    )

    # And no Neo4j write happened for the ghost survivor (raise is BEFORE write_entities).
    ghost_rows = clean_graph.execute_read(
        "MATCH (n {id: $cid}) RETURN count(n) AS n", cid="wpi2-ghost-survivor"
    )
    assert ghost_rows[0]["n"] == 0, (
        "IT-ALIAS-1: the raise must fire BEFORE any Neo4j write — no node for the incomplete "
        "ghost survivor may exist"
    )
    engine.dispose()


# ===========================================================================
# IT-ALIAS-2 — fold-side positive: a healthy corpus (no ghost) never raises
# ===========================================================================


def test_it_alias_2_healthy_corpus_does_not_raise(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _merge_pair_candidates("ita2"):
            session.add(_queue_item(candidate, source=str(candidate["id"])))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.promoted == 1, (
        f"IT-ALIAS-2 precondition: expected 1 promoted (merged) cluster, got {stats.promoted}"
    )

    # No ghost alias injected — the log is fully self-consistent (co-commit held).
    with sessions() as session:
        result = project(session, clean_graph, full_rebuild=True)

    assert result.entities_written >= 1, (
        f"IT-ALIAS-2: expected >= 1 entity written on a healthy corpus, got "
        f"{result.entities_written}"
    )
    engine.dispose()


# ===========================================================================
# IT-ALIAS-3 — producer co-commit: pipeline (resolve_pending) promotes a merge
# ===========================================================================


def test_it_alias_3_pipeline_promote_co_commits_alias_and_statements(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _merge_pair_candidates("ita3"):
            session.add(_queue_item(candidate, source=str(candidate["id"])))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.promoted == 1, (
        f"IT-ALIAS-3 precondition: expected 1 promoted (merged) cluster, got {stats.promoted}"
    )

    with sessions() as session:
        merge_audit = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "merged")
        ).scalar_one()
        canonical_id = merge_audit.canonical_id
        assert {"ita3-a", "ita3-b"} <= set(merge_audit.source_ids)

        # --- BOTH producer artefacts present for the survivor ---
        alias_count = session.execute(
            select(func.count())
            .select_from(CanonicalIdLedger)
            .where(
                CanonicalIdLedger.canonical_id == canonical_id,
                CanonicalIdLedger.canonical_alias != canonical_id,
            )
        ).scalar_one()
        assert alias_count >= 1, (
            f"IT-ALIAS-3: expected >= 1 CanonicalIdLedger supersession alias row for survivor "
            f"{canonical_id!r} (member ids collapsed onto it), got {alias_count}"
        )

        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == canonical_id)
        ).scalar_one()
        assert stmt_count >= 1, (
            f"IT-ALIAS-3: expected >= 1 StatementRecord for survivor {canonical_id!r}, "
            f"got {stmt_count}"
        )

    # --- project(full_rebuild=True) over this log raises nothing ---
    with sessions() as session:
        result = project(session, clean_graph, full_rebuild=True)
    assert result.entities_written >= 1
    engine.dispose()


# ===========================================================================
# IT-ALIAS-4 — producer co-commit: sign-off (signoff.approve) on a parked merge
# ===========================================================================


def test_it_alias_4_signoff_approve_co_commits_alias_and_statements(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _sanctioned_pair_candidates("ita4"):
            session.add(_queue_item(candidate, source=str(candidate["id"])))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1, (
        f"IT-ALIAS-4 precondition: expected 1 parked (pending_review) cluster, got {stats.review}"
    )

    with sessions() as session:
        canonical_id = session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    with sessions() as session:
        result = signoff.approve(
            session,
            clean_graph,
            canonical_id=canonical_id,
            approver="wpi2-op",
            reason="it-alias-4",
        )
    assert result.decision == "approved"

    with sessions() as session:
        # --- BOTH producer artefacts present for the approved survivor ---
        alias_count = session.execute(
            select(func.count())
            .select_from(CanonicalIdLedger)
            .where(
                CanonicalIdLedger.canonical_id == canonical_id,
                CanonicalIdLedger.canonical_alias != canonical_id,
            )
        ).scalar_one()
        assert alias_count >= 1, (
            f"IT-ALIAS-4: expected >= 1 CanonicalIdLedger supersession alias row for the "
            f"approved survivor {canonical_id!r}, got {alias_count}"
        )

        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == canonical_id)
        ).scalar_one()
        assert stmt_count >= 1, (
            f"IT-ALIAS-4: expected >= 1 StatementRecord for the approved survivor "
            f"{canonical_id!r}, got {stmt_count}"
        )

    # --- project(full_rebuild=True) over this log raises nothing ---
    with sessions() as session:
        fold_result = project(session, clean_graph, full_rebuild=True)
    assert fold_result.entities_written >= 1
    engine.dispose()
