"""Gate 3b LOW-1 end-to-end (container-backed): the single-read plumbing changes NOTHING but
where the ledger is read.

The pure referent-walk properties live in ``tests/property/test_prop_projection_single_read.py``;
this file proves the two invariants that need a real Postgres + Neo4j:

INV-LOW1-FOLD-IDENTICAL   A full-rebuild fold run with the INJECTED ``(alias_map, survivor_of)``
                          pair (one up-front ledger read, the driver diff guard's new shape)
                          produces the SAME ``ProjectionResult`` and the SAME graph as the
                          internal-build fold on an identical seeded corpus — divergence 0.

INV-LOW1-CHECK-INTACT     The WPI-2 completeness check (``IncompleteAliasedSurvivorError`` on an
                          aliased survivor with no foldable statement row, ADR 0111) still fires
                          when driven through the injected plumbing.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import StatementRecord
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.snapshot import read_graph_snapshot
from worldmonitor.resolution.canonical import record_alias, record_canonical
from worldmonitor.resolution.divergence import measure_divergence
from worldmonitor.resolution.projector import load_alias_map_and_survivor_of, project
from worldmonitor.resolution.spine_integrity import IncompleteAliasedSurvivorError

pytestmark = pytest.mark.integration

_RETRIEVED_AT = "2026-07-18T00:00:00Z"


def _stmt(canonical_id: str, entity_id: str, prop: str, value: str) -> StatementRecord:
    return StatementRecord(
        id=str(uuid.uuid4()),
        statement_id=str(uuid.uuid4()),
        canonical_id=canonical_id,
        entity_id=entity_id,
        schema="Person",
        prop=prop,
        value=value,
        dataset="lowtest:ds",
        reliability="A",
        retrieved_at=_RETRIEVED_AT,
        raw_pointer=f"s3://landing/lowtest/{entity_id}.json",
        first_seen=_RETRIEVED_AT,
        last_seen=_RETRIEVED_AT,
        method=None,
        scope="default",
    )


def test_injected_plumbing_folds_identically_to_internal_build(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """INV-LOW1-FOLD-IDENTICAL: internal-build vs injected (alias_map, survivor_of) — same
    ProjectionResult, same graph (zero divergence both ways round)."""
    ensure_constraints(clean_graph)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        # Survivor c1 reached via the alias m1 -> c1 (exercises the referent rewrite) + a plain
        # survivor c2. Statement rows carry the MEMBER canonical id (m1), as the promote path does.
        record_canonical(session, canonical_id="low-c1", anchor_kind="mint", anchor_value="")
        record_alias(session, canonical_id="low-c1", alias="low-m1")
        record_canonical(session, canonical_id="low-c2", anchor_kind="mint", anchor_value="")
        session.add_all(
            [
                _stmt("low-m1", "low-m1", "name", "Alias Reached Person"),
                _stmt("low-m1", "low-m1", "country", "de"),
                _stmt("low-c2", "low-c2", "name", "Plain Survivor"),
            ]
        )
        session.commit()

    # Run A — internal build (both kwargs omitted: the pre-LOW-1 caller shape).
    with sessions() as session:
        result_internal = project(session, clean_graph, full_rebuild=True)
    snap_internal = read_graph_snapshot(clean_graph)

    # Run B — injected single-read plumbing (the driver diff guard's new shape).
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        alias_map, survivor_of = load_alias_map_and_survivor_of(session)
        result_injected = project(
            session,
            clean_graph,
            full_rebuild=True,
            survivor_of=survivor_of,
            alias_map=alias_map,
        )
    snap_injected = read_graph_snapshot(clean_graph)

    assert result_injected == result_internal, (
        "INV-LOW1-FOLD-IDENTICAL VIOLATED: the injected plumbing changed the fold's counts "
        f"({result_injected} != {result_internal})"
    )
    assert (
        {n.id for n in snap_internal.nodes}
        == {n.id for n in snap_injected.nodes}
        == {
            "low-c1",
            "low-c2",
        }
    )
    divergence = measure_divergence(
        snap_internal,
        snap_injected,
        survivor_of,
        computed_at=datetime(2026, 7, 18, tzinfo=UTC),
    )
    assert divergence.total == 0, (
        f"INV-LOW1-FOLD-IDENTICAL VIOLATED: divergence {divergence} between the internal-build "
        "and injected-plumbing folds of the same corpus"
    )

    engine.dispose()


def test_completeness_check_still_fires_through_injected_plumbing(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """INV-LOW1-CHECK-INTACT: an aliased survivor with NO foldable statement row raises
    IncompleteAliasedSurvivorError under full_rebuild, exactly as before, when survivor_of and
    alias_map are injected."""
    ensure_constraints(clean_graph)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        record_canonical(session, canonical_id="low-c9", anchor_kind="mint", anchor_value="")
        record_alias(session, canonical_id="low-c9", alias="low-m9")  # NO statement rows at all
        session.add(_stmt("low-c2", "low-c2", "name", "Unrelated Survivor"))
        session.commit()

    with sessions() as session:
        alias_map, survivor_of = load_alias_map_and_survivor_of(session)
        with pytest.raises(IncompleteAliasedSurvivorError):
            project(
                session,
                clean_graph,
                full_rebuild=True,
                survivor_of=survivor_of,
                alias_map=alias_map,
            )

    engine.dispose()
