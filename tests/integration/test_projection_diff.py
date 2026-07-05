"""Integration tests for Gate 3a-ii-B — the projection rebuild-and-diff guard (ADR 0102).

End-to-end anchor for the projection-integrity measure: fold the WHOLE statement log into a
SECOND, ISOLATED Neo4j container via ``project(..., checkpoint_id="projection-diff")``, read BOTH
graphs read-only via ``read_graph_snapshot``, and ``measure_divergence`` against the real live
graph (``clean_graph``, populated by ``resolve_pending`` — the direct write path, exactly as
``test_projector.py``'s ``IT-PROJ-2`` seeds it).

IT-DIV-1  Zero divergence + checkpoint isolation: on a ``_candidates()``-style single-batch,
          single-source corpus (E1/E2/E3 null, mirroring ``test_projector.py`` IT-PROJ-2), folding
          the WHOLE log into the diff target reproduces the live graph with ``total == 0``, the
          pre-seeded ``"neo4j"`` ``ProjectionCheckpoint`` row (a high sentinel watermark) is left
          BYTE-UNCHANGED, and a SEPARATE ``"projection-diff"`` row now exists (D5 checkpoint
          isolation, ADR 0102).

IT-DIV-2  Rot is detected: after reaching the same zero-divergence state, injecting a fresh node
          into the LIVE graph (an id never present in the statement log) makes
          ``measure_divergence`` report ``total >= 1``.

All tests are RED at collection time: the module-level imports of ``measure_divergence`` /
``read_graph_snapshot`` (``worldmonitor.resolution.divergence`` / ``worldmonitor.graph.snapshot``)
and ``build_survivor_of`` (``worldmonitor.resolution.projector``) fail with ``ImportError`` —
those symbols do not exist until the builder lands them. That is the correct, intended TDD failure
mode (the Gate 3a-i precedent); once the two new modules exist, ``project(..., checkpoint_id=...)``
would still fail with a ``TypeError`` on the not-yet-added keyword — either way, RED for the right
reason.

The second-Neo4j fixture is defined INSIDE this file (mirroring ``conftest.py``'s
``neo4j_gds_client`` testcontainers pattern) — ``conftest.py`` itself is out of scope for this
gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, ProjectionCheckpoint
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.snapshot import read_graph_snapshot  # gate import: RED until builder lands
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.plugins.registry import Registry
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.divergence import measure_divergence  # gate import: RED
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import (
    build_survivor_of,
    project,
)  # gate: RED (build_survivor_of)
from worldmonitor.runner.driver import IngestDriver
from worldmonitor.settings import Settings

pytestmark = pytest.mark.integration

# A SECOND, independent Neo4j image/password — deliberately distinct literals from conftest.py's
# NEO4J_TEST_PASSWORD (this container is never the same instance as the `clean_graph` fixture's).
_DIFF_NEO4J_IMAGE = "neo4j:2026.05.0-community"
_DIFF_NEO4J_PW = "testpw-diff"  # pragma: allowlist secret (>=8 chars, Neo4j minimum)
_COMPUTED_AT = datetime(2026, 7, 5, 0, 0, 0, tzinfo=UTC)
# A watermark far beyond anything this test's log ever reaches — pre-seeded on the "neo4j" row so
# an ACCIDENTAL move by the guard (a checkpoint-isolation regression) is unmistakable.
_SENTINEL_WATERMARK = 999_999


@pytest.fixture(scope="module")
def diff_neo4j_client() -> Iterator[Neo4jClient]:
    """A SECOND, isolated Neo4j container — the projection-diff guard's designated fold target.

    Mirrors ``conftest.py``'s ``neo4j_gds_client`` testcontainers pattern (module-scoped, its own
    independent container, lazily started only when a test in this module requests it) but WITHOUT
    the GDS plugin — the diff target only ever needs plain MATCH/MERGE/DETACH DELETE.
    """
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
    """Wipe the diff-target container before each test (function-scoped clean state)."""
    diff_neo4j_client.execute_write("MATCH (n) DETACH DELETE n")
    return diff_neo4j_client


# ---------------------------------------------------------------------------
# Seed helpers — the _candidates()/_queue_item() shape from test_projector.py, REPLICATED here
# (that file is out of scope for this gate; this is a deliberate, documented copy, not an import).
# Single batch, ONE shared source -> E1/E2/E3 are all null (the same null-divergence base case
# test_projector.py's IT-PROJ-2 exercises for the direct-vs-fold comparison).
# ---------------------------------------------------------------------------


def _queue_item(entity: dict[str, object]) -> ErQueueItem:
    source_record = f"s3://landing/{entity['id']}.json"
    stamped = stamp(
        make_entity(entity),
        Provenance(
            source_id="src:projection-diff-test",
            retrieved_at="2026-07-05T00:00:00Z",
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


def _candidates() -> list[dict[str, object]]:
    """Single-batch, single-source corpus (mirrors test_projector.py's _candidates() shape)."""
    return [
        {
            "id": "divc1",
            "schema": "Company",
            "properties": {"name": ["Acme Divergence Ltd"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "divc2",
            "schema": "Company",
            "properties": {"name": ["Acme Divergence Ltd"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "divc3",
            "schema": "Company",
            "properties": {"name": ["Globex Divergence Inc"], "jurisdiction": ["gb"]},
            "datasets": ["t"],
        },
    ]


# ===========================================================================
# IT-DIV-1: zero divergence + checkpoint isolation
# ===========================================================================


def test_zero_divergence_and_checkpoint_isolation(
    clean_graph: Neo4jClient,
    clean_diff_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        with sessions() as session:
            for candidate in _candidates():
                session.add(_queue_item(candidate))
            session.commit()

        with sessions() as session:
            resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

        # Pre-seed the "neo4j" checkpoint at a high sentinel BEFORE the guard's fold runs, so any
        # accidental move (a checkpoint-isolation regression) is unmistakable.
        with sessions() as session:
            session.add(
                ProjectionCheckpoint(
                    id="neo4j",
                    last_statement_seq=_SENTINEL_WATERMARK,
                    last_decision_seq=_SENTINEL_WATERMARK,
                )
            )
            session.commit()

        # The guard's fold, driven directly (not through the whole driver) so the
        # checkpoint-isolation assertion below is unambiguous (ADR 0102 spec §4 IT-DIV-1 guidance).
        with sessions() as session:
            project(session, clean_diff_graph, full_rebuild=True, checkpoint_id="projection-diff")

        live_snap = read_graph_snapshot(clean_graph)
        fold_snap = read_graph_snapshot(clean_diff_graph)
        with sessions() as session:
            survivor_of = build_survivor_of(session)

        result = measure_divergence(live_snap, fold_snap, survivor_of, computed_at=_COMPUTED_AT)
        assert result.total == 0, (
            "IT-DIV-1 VIOLATED: folding the WHOLE log into the isolated diff target must fully "
            f"explain the live graph on this single-batch/single-source corpus (got "
            f"total={result.total}, unexplained_nodes={result.unexplained_nodes}, "
            f"unexplained_edges={result.unexplained_edges})."
        )

        # --- Checkpoint isolation (D5): the "neo4j" row is UNCHANGED; "projection-diff" is NEW ---
        with sessions() as session:
            live_checkpoint = session.get(ProjectionCheckpoint, "neo4j")
            diff_checkpoint = session.get(ProjectionCheckpoint, "projection-diff")

        assert live_checkpoint is not None
        assert live_checkpoint.last_statement_seq == _SENTINEL_WATERMARK, (
            "IT-DIV-1 CHECKPOINT ISOLATION VIOLATED: the 'neo4j' checkpoint watermark moved during "
            "the guard's fold — project(checkpoint_id='projection-diff') must NEVER advance the "
            "live projector's own watermark (ADR 0102 D5)."
        )
        assert live_checkpoint.last_decision_seq == _SENTINEL_WATERMARK, (
            "IT-DIV-1 CHECKPOINT ISOLATION VIOLATED: the 'neo4j' decision watermark moved during "
            "the guard's fold (ADR 0102 D5)."
        )
        assert diff_checkpoint is not None, (
            "IT-DIV-1: expected a SEPARATE 'projection-diff' ProjectionCheckpoint row after the "
            "guard's fold; none was found (ADR 0102 D5 checkpoint isolation)."
        )
        assert diff_checkpoint.last_statement_seq > 0, (
            "IT-DIV-1: the 'projection-diff' checkpoint must have advanced past 0 after folding a "
            "non-empty log (mirrors ADR 0100 D1's watermark invariant, now under the isolated id)."
        )
    finally:
        engine.dispose()


# ===========================================================================
# IT-DIV-2: rot is detected
# ===========================================================================


def test_rot_is_detected_in_live_graph(
    clean_graph: Neo4jClient,
    clean_diff_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        with sessions() as session:
            for candidate in _candidates():
                session.add(_queue_item(candidate))
            session.commit()

        with sessions() as session:
            resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

        with sessions() as session:
            project(session, clean_diff_graph, full_rebuild=True, checkpoint_id="projection-diff")

        # Sanity: this corpus is zero-divergence BEFORE the injected rot (same base case as
        # IT-DIV-1) — a precondition, not the invariant under test here.
        with sessions() as session:
            survivor_of = build_survivor_of(session)
        baseline = measure_divergence(
            read_graph_snapshot(clean_graph),
            read_graph_snapshot(clean_diff_graph),
            survivor_of,
            computed_at=_COMPUTED_AT,
        )
        assert baseline.total == 0, (
            f"IT-DIV-2 precondition failed: baseline divergence={baseline.total} (expected 0 "
            "before rot is injected — see IT-DIV-1)"
        )

        # --- Inject rot into the LIVE graph: a node with a fresh id NEVER present in the log ---
        clean_graph.execute_write(
            "CREATE (n:Company {id: $id, name: ['Unlogged Rot Corp']})",
            id="projection-diff-rot-node-not-in-log",
        )

        rotted_live_snap = read_graph_snapshot(clean_graph)
        fold_snap = read_graph_snapshot(clean_diff_graph)
        with sessions() as session:
            survivor_of = build_survivor_of(session)

        result = measure_divergence(
            rotted_live_snap, fold_snap, survivor_of, computed_at=_COMPUTED_AT
        )
        assert result.total >= 1, (
            "IT-DIV-2 VIOLATED: an injected live node with an id NEVER present in the statement "
            f"log must be reported as unexplained (got total={result.total}) — the fold genuinely "
            "cannot reproduce it (ADR 0102 D6)."
        )
        assert result.unexplained_nodes >= 1, (
            f"IT-DIV-2: expected unexplained_nodes >= 1, got {result.unexplained_nodes}"
        )
    finally:
        engine.dispose()


# ===========================================================================
# IT-DIV-3: the DRIVER path end-to-end (_run_projection_diff against two REAL containers)
# ===========================================================================


class _InertLanding:
    """Stand-in for ``LandingStore`` — never touched by ``_run_projection_diff``."""


class _InertCipher:
    """Stand-in for ``ConfigCipher`` — never touched by ``_run_projection_diff``."""


def test_driver_run_projection_diff_end_to_end(
    clean_graph: Neo4jClient,
    clean_diff_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """IT-DIV-3: drive the driver's OWN ``_run_projection_diff`` against two REAL containers.

    Proves what the unit stubs cannot: (a) the D3 identity handshake's ``CALL db.info()``
    works on the pinned Neo4j image and returns DISTINCT ids for two real instances (the
    textual fence also passes here — same loopback host, different published ports); (b) the
    happy-path composition wipes/folds the DIFF target and never writes the live one; (c) the
    live/fold snapshot ARGUMENT ORDER is correct — rot injected into the LIVE graph must be
    reported (a swapped order would score it on the fold side and stay silent).
    """
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        with sessions() as session:
            for candidate in _candidates():
                session.add(_queue_item(candidate))
            session.commit()

        with sessions() as session:
            resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

        from pydantic import SecretStr

        settings = Settings(
            neo4j_uri=clean_graph.uri,
            projection_diff_enabled=True,
            projection_diff_neo4j_uri=clean_diff_graph.uri,
            projection_diff_neo4j_user="neo4j",
            projection_diff_neo4j_password=SecretStr(_DIFF_NEO4J_PW),
        )
        assert clean_graph.uri != clean_diff_graph.uri, (
            "IT-DIV-3 precondition: the two containers must publish distinct URIs"
        )
        driver = IngestDriver(
            sessions=sessions,
            landing=_InertLanding(),  # type: ignore[arg-type]
            neo4j=clean_graph,
            registry=Registry(),
            cipher=_InertCipher(),  # type: ignore[arg-type]
            settings=settings,
        )

        # Happy path: fence passes (distinct ports), handshake passes (distinct REAL db ids —
        # this is the on-image proof of CALL db.info()), wipe+fold the diff target, measure.
        result = driver._run_projection_diff(now=_COMPUTED_AT)
        assert result.total == 0, (
            f"IT-DIV-3 VIOLATED: end-to-end driver path expected zero divergence on the "
            f"single-batch corpus (got total={result.total})."
        )

        # Argument-order proof: rot in the LIVE graph must be reported. A swapped
        # live/fold order would score the rot on the fold side and stay silent.
        clean_graph.execute_write(
            "CREATE (n:Company {id: $id, name: ['Unlogged Rot Corp']})",
            id="it-div-3-rot-node-not-in-log",
        )
        rotted = driver._run_projection_diff(now=_COMPUTED_AT)
        assert rotted.total >= 1, (
            "IT-DIV-3 VIOLATED: rot injected into the LIVE graph was not reported — either the "
            "live/fold snapshot argument order is swapped or the measure lost its direction."
        )
    finally:
        engine.dispose()
