"""Integration tests for Gate 2b — statement/context-claim log backfill (ADR 0113, spec
``docs/reviews/GATE_2B_BACKFILL_SPEC.md`` §4).

Real Postgres + Neo4j (+ MinIO for the ``erase_source`` half) — Docker IS available locally; run
this suite locally, not CI-only (memory: docker-available-run-integration-locally).

IT-BACKFILL-1  Seeds a synthetic **pre-2a** corpus (direct-written live graph node + a
               ``canonical_id_ledger`` supersession alias + ``ErQueueItem`` members, but NO
               ``StatementRecord``/``ContextClaimRecord`` rows) with TWO survivors: a
               prop-bearing merge (cluster 1) and a **zero-prop** merge (cluster 2, the WPI-1
               interlock, ADR 0112). ``project(full_rebuild=True)`` RAISES
               ``IncompleteAliasedSurvivorError`` BEFORE ``backfill_spine``; after it, the SAME
               call succeeds into a FRESH, ISOLATED fold target, cluster 2 materialises a bare
               node backed by >= 2 ``WM_EXISTS`` existence-claim rows (WPI-1/WPI-2 interlock
               discharged), and the whole-graph divergence (ADR 0102's
               ``measure_divergence``/``read_graph_snapshot``, excluded axes: id/caption/bare
               anchor keys/datasets/prov_*) between the LIVE graph and the fold target is exactly
               ``0`` — the spec §6 acceptance criterion, verified via the SAME production
               instrument ``test_projection_diff.py::IT-DIV-1`` uses (independent of however the
               builder resolves ``assert_backfill_complete``'s Neo4j-client ambiguity — see the
               module docstring in ``tests/property/test_prop_backfill.py``).

IT-BACKFILL-2  Forget-safety end-to-end (SF-3, the person-affecting invariant): seeds a pre-2a
               survivor with an erased-source-only value + a kept-source value, runs the REAL
               ``erasure.erase_source(...)`` (which prunes the live graph + redacts the
               ``ErQueueItem`` to a shell + records the ``TaskRun(kind="erase")`` audit row),
               THEN ``backfill_spine`` — the erase-audit exclusion set must already cover
               ``erased_src`` (``load_erased_sources``); the backfill contributes ZERO statement/
               context rows for it; a fresh ``full_rebuild`` into a wiped LIVE graph
               (rebuild-contains-no-erased-source, mirroring ``test_erasure_scrub.py``'s SF-6
               oracle) reconstructs the survivor WITHOUT the erased-source-only value.

All tests are RED at collection time: the module-level import of ``BackfillResult`` /
``backfill_spine`` / ``load_erased_sources`` from ``worldmonitor.resolution.backfill`` fails with
``ImportError`` — that module does not exist until the builder creates it (ADR 0113 / Gate 2b).

The second-Neo4j (fold-target) fixture is defined INSIDE this file (mirroring
``test_projection_diff.py``'s own note that ``conftest.py`` is out of scope for this class of
gate) — a SEPARATE, isolated container from ``clean_graph`` (the "live" graph).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ContextClaimRecord, ErQueueItem, StatementRecord
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.snapshot import read_graph_snapshot
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

# ---- GATE IMPORT — does not exist yet (RED for the right reason, ADR 0113 / Gate 2b) ----
from worldmonitor.resolution.backfill import BackfillResult, backfill_spine, load_erased_sources
from worldmonitor.resolution.canonical import record_alias, record_canonical
from worldmonitor.resolution.divergence import measure_divergence
from worldmonitor.resolution.merge import _merge_entities  # pyright: ignore[reportPrivateUsage]
from worldmonitor.resolution.projector import build_survivor_of, project
from worldmonitor.resolution.spine_integrity import IncompleteAliasedSurvivorError
from worldmonitor.resolution.spine_lock import ConcurrentSpineWriterError
from worldmonitor.resolution.statements import WM_EXISTS
from worldmonitor.settings import get_settings
from worldmonitor.storage.landing import LandingStore

pytestmark = pytest.mark.integration

_RETRIEVED_AT = "2026-07-12T00:00:00Z"
_DIFF_NEO4J_IMAGE = "neo4j:2026.05.0-community"
_DIFF_NEO4J_PW = "testpw-backfill-diff"  # pragma: allowlist secret (>=8 chars, Neo4j minimum)
_COMPUTED_AT = datetime(2026, 7, 12, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The second, ISOLATED Neo4j container — the fold target (mirrors test_projection_diff.py).
# ---------------------------------------------------------------------------


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
# Shared, file-local helpers (duplicated across suites per house convention).
# ---------------------------------------------------------------------------


def _sessions(postgres_dsn: str) -> tuple[Any, Any]:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    return engine, session_factory(engine)


def _landing(minio: tuple[str, str, str]) -> LandingStore:
    endpoint, access_key, secret_key = minio
    store = LandingStore.connect(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=f"landing-backfill-it-{uuid.uuid4().hex[:8]}",
    )
    store.ensure_bucket()
    return store


def _read_node(client: Neo4jClient, node_id: str) -> dict[str, Any] | None:
    rows = client.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN properties(n) AS props", id=node_id
    )
    return dict(rows[0]["props"]) if rows else None


def _person(entity_id: str, source_id: str, props: dict[str, list[str]]) -> FtmEntity:
    entity = make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": [source_id]}
    )
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{source_id}/{entity_id}.json",
        ),
    )


def _zero_prop_person(entity_id: str, source_id: str) -> FtmEntity:
    return _person(entity_id, source_id, {})


def _queue_item(entity: FtmEntity, *, status: str = "resolved") -> ErQueueItem:
    assert entity.id is not None
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        entity_id=entity.id,
        raw_entity=entity.to_dict(),
        source_record=f"s3://landing/{entity.id}.json",
        status=status,
    )


# =========================================================================================
# IT-BACKFILL-1: completeness + fold reconstruction + divergence-clean (WPI-1/WPI-2 interlock)
# =========================================================================================


def test_it_backfill_completeness_and_fold_reconstruction(
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    clean_diff_graph: Neo4jClient,
) -> None:
    engine, sessions = _sessions(postgres_dsn)

    # --- Cluster 1: a prop-bearing pre-2a merge survivor ---
    src1 = "src:it-backfill1"
    m1a, m1b = "it-bf1-m1", "it-bf1-m2"
    canonical1 = "wm-it-backfill1"
    e1a = _person(m1a, src1, {"name": ["Backfill IT Corp"], "alias": ["Alias-M1A"]})
    e1b = _person(m1b, src1, {"name": ["Backfill IT Corp"], "alias": ["Alias-M1B"]})
    by_id1 = {m1a: e1a, m1b: e1b}

    # --- Cluster 2: a ZERO-PROP pre-2a merge survivor (WPI-1 interlock, ADR 0112) ---
    src2 = "src:it-backfill2"
    m2a, m2b = "it-bf2-m1", "it-bf2-m2"
    canonical2 = "wm-it-backfill2"
    e2a = _zero_prop_person(m2a, src2)
    e2b = _zero_prop_person(m2b, src2)

    with sessions() as session:
        for entity in (e1a, e1b, e2a, e2b):
            session.add(_queue_item(entity))
        record_canonical(session, canonical1)
        record_alias(session, canonical1, m1a)
        record_alias(session, canonical1, m1b)
        record_canonical(session, canonical2)
        record_alias(session, canonical2, m2a)
        record_alias(session, canonical2, m2b)
        session.commit()

    ensure_constraints(clean_graph)
    merged1, dropped1 = _merge_entities(canonical1, (m1a, m1b), by_id1)
    assert dropped1 == ()
    write_entities(clean_graph, [merged1])
    # Cluster 2 gets NO live node — its pre-existing state is purely fold-driven (the whole
    # point of the WPI-1/WPI-2 interlock this gate discharges).

    # --- Precondition: full_rebuild RAISES before backfill (no spine rows at all) ---
    with sessions() as session, pytest.raises(IncompleteAliasedSurvivorError):
        project(session, clean_diff_graph, full_rebuild=True)

    with sessions() as session:
        result = backfill_spine(session, neo4j=clean_graph)

    assert isinstance(result, BackfillResult), (
        f"backfill_spine must return a BackfillResult, got {type(result).__name__!r}"
    )
    assert result.statements_written >= 2, (
        f"expected >= 2 statement rows for cluster 1's prop-bearing members, got "
        f"{result.statements_written}"
    )
    assert result.existence_claims_written >= 2, (
        f"expected >= 2 existence-claim rows for cluster 2's zero-prop members, got "
        f"{result.existence_claims_written}"
    )
    assert result.survivors_covered >= 2

    with sessions() as session:
        wm_exists_rows = list(
            session.execute(
                select(StatementRecord).where(
                    StatementRecord.canonical_id == canonical2,
                    StatementRecord.prop == WM_EXISTS,
                )
            ).scalars()
        )
    assert len(wm_exists_rows) >= 2, (
        f"WPI-1 INTERLOCK VIOLATED: expected >= 2 WM_EXISTS rows for the zero-prop survivor "
        f"{canonical2!r}, got {len(wm_exists_rows)}"
    )
    assert {row.entity_id for row in wm_exists_rows} == {m2a, m2b}

    # --- full_rebuild now succeeds into the ISOLATED fold target ---
    with sessions() as session:
        proj_result = project(session, clean_diff_graph, full_rebuild=True)
    assert proj_result.entities_written >= 2

    # Cluster 2's bare node materialises (WPI-2 discharge via WPI-1's existence claim).
    bare_rows = clean_diff_graph.execute_read(
        "MATCH (n {id: $id}) RETURN properties(n) AS props, labels(n) AS lbls", id=canonical2
    )
    assert len(bare_rows) == 1, (
        f"WPI-2 DISCHARGE VIOLATED: expected exactly 1 reconstructed bare node for the zero-prop "
        f"survivor {canonical2!r}, got {len(bare_rows)}"
    )
    bare_props = bare_rows[0]["props"] or {}
    assert "Person" in (bare_rows[0]["lbls"] or [])
    assert bare_props.get("prov_source_id"), "G1: the bare node must still carry prov_source_id"

    # --- whole-graph divergence over the excluded axes must be 0 (spec §6 acceptance) ---
    with sessions() as session:
        survivor_of = build_survivor_of(session)
    live_snap = read_graph_snapshot(clean_graph)
    fold_snap = read_graph_snapshot(clean_diff_graph)
    assert live_snap.nodes, "sanity: the live graph must be non-empty for a meaningful comparison"

    divergence = measure_divergence(live_snap, fold_snap, survivor_of, computed_at=_COMPUTED_AT)
    assert divergence.total == 0, (
        "INV-BACKFILL-COMPLETE VIOLATED (spec §6 acceptance criterion): the whole-graph "
        f"divergence over the excluded axes must be 0 after backfill, got total="
        f"{divergence.total} (unexplained_nodes={divergence.unexplained_nodes}, "
        f"unexplained_edges={divergence.unexplained_edges})"
    )

    engine.dispose()


# =========================================================================================
# IT-BACKFILL-2: forget-safety end-to-end — erase, THEN backfill, THEN rebuild-clean
# =========================================================================================


def test_it_backfill_forget_safety_erase_then_backfill_then_rebuild_contains_nothing(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)

    erased_src = "esrc-it-backfill:erase"
    kept_src = "ksrc-it-backfill:keep"
    canonical3 = "wm-it-backfill3"
    m3_erased = "it-bf3-erased"
    m3_kept = "it-bf3-kept"

    e_erased = _person(
        m3_erased, erased_src, {"name": ["Backfill Erase Corp"], "alias": ["OnlyFromErased3"]}
    )
    e_kept = _person(
        m3_kept, kept_src, {"name": ["Backfill Erase Corp"], "alias": ["OnlyFromKept3"]}
    )
    by_id3 = {m3_erased: e_erased, m3_kept: e_kept}

    with sessions() as session:
        session.add(_queue_item(e_erased))
        session.add(_queue_item(e_kept))
        record_canonical(session, canonical3)
        record_alias(session, canonical3, m3_erased)
        record_alias(session, canonical3, m3_kept)
        session.commit()

    ensure_constraints(clean_graph)
    merged3, dropped3 = _merge_entities(canonical3, (m3_erased, m3_kept), by_id3)
    assert dropped3 == ()
    write_entities(clean_graph, [merged3])

    pre_live = _read_node(clean_graph, canonical3)
    assert pre_live is not None
    assert "OnlyFromErased3" in list(pre_live.get("alias") or []), (
        "precondition: the pre-2a live node must carry the (soon-to-be-erased) value"
    )

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=erased_src,
            authorized_by="it-backfill-erase-op",
        )
        session.commit()

    # NOTE (corrected precondition — Gate 2b forget-safety is a LOG + REBUILD surface, not a live
    # surface). It is FALSE on a PRE-2a corpus that erase_source alone strips "OnlyFromErased3"
    # from the live node: with zero statement rows, erase_source's P2 live prune
    # (erase_source_graph, PROP-granular) leaves an erased-source-ONLY value on a prop still
    # co-witnessed by a surviving source (the documented P2 KNOWN RESIDUAL / log-completeness
    # boundary; ADR 0107 SF-4's value-granular prune only fires when statement rows exist, and
    # scrub_stock's post-backfill re-scrub finds no erased-source rows to drive a re-prune because
    # the backfill excluded that source). Gate 2b does NOT retroactively re-prune the live graph
    # for an erasure that PREDATES the backfill — its forget-safety is realized on the LOG +
    # REBUILD surface, proven below: (a) the backfill writes ZERO erased-source rows
    # (reached_stmt/ctx == 0), and (b) a full_rebuild is forget-safe (the oracle). The live-surface
    # residual for a pre-backfill erasure is a pre-existing P2 limitation (NOT a 2b regression, NOT
    # a resurrection into the SoR), healed at the 3b cutover rebuild — out of Gate 2b scope.

    with sessions() as session:
        erased_set = load_erased_sources(session)
    assert erased_src in erased_set, (
        "load_erased_sources must surface the source erase_source just recorded via "
        "TaskRun(kind='erase', status='ok')"
    )

    with sessions() as session:
        result = backfill_spine(session, neo4j=clean_graph)
    assert result.skipped_erased_source >= 1, (
        f"expected >= 1 skipped_erased_source after backfilling a post-erase corpus, got "
        f"{result.skipped_erased_source}"
    )

    with sessions() as session:
        reached_stmt = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == erased_src)
        ).scalar_one()
        reached_ctx = session.execute(
            select(func.count())
            .select_from(ContextClaimRecord)
            .where(ContextClaimRecord.dataset == erased_src)
        ).scalar_one()
    assert reached_stmt == 0, (
        "FORGET-SAFETY VIOLATED: the erased source's contribution was written to the statement "
        f"log by the backfill ({reached_stmt} row(s))"
    )
    assert reached_ctx == 0, (
        "FORGET-SAFETY VIOLATED: the erased source's contribution was written to the "
        f"context_claim log by the backfill ({reached_ctx} row(s))"
    )

    # --- rebuild-contains-no-erased-source (SF-6-style oracle, mirrors test_erasure_scrub.py) ---
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)

    fold_after = _read_node(clean_graph, canonical3)
    assert fold_after is not None, (
        "the survivor (with a surviving kept-source contribution) must still materialise"
    )
    fold_alias = list(fold_after.get("alias") or [])
    assert "OnlyFromErased3" not in fold_alias, (
        "FORGET-SAFETY VIOLATED: the erased source's value resurrects on a fresh full_rebuild "
        f"after backfill: alias={fold_alias!r}"
    )
    assert "OnlyFromKept3" in fold_alias, (
        "non-vacuity: the kept source's contribution must survive the backfill + rebuild"
    )

    engine.dispose()


# =========================================================================================
# IT-BACKFILL-3: INV-SINGLE-WRITER — backfill_spine is a NEW SoR-spine writer and takes the
# WPI-3 advisory lock (ADR 0110); a concurrent holder refuses it (fail-closed), then a retry
# after release succeeds. RED before backfill_spine takes acquire_spine_writer_lock.
# =========================================================================================


def test_it_backfill_refused_by_concurrent_spine_writer(
    postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    engine, sessions = _sessions(postgres_dsn)
    src = "ksrc-it-backfill-lock:keep"
    m1 = "it-bf-lock-m1"
    e1 = _person(m1, src, {"name": ["Lock Corp"]})
    with sessions() as session:
        session.add(_queue_item(e1))
        record_canonical(session, m1)  # singleton self-row → survivor_of(m1) == m1
        session.commit()

    lock_key = get_settings().spine_writer_lock_key
    conn_a = engine.connect()
    session_b = sessions()
    try:
        trans_a = conn_a.begin()
        acquired = conn_a.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
        ).scalar()
        assert acquired is True, (
            "setup precondition: connection A must acquire the advisory lock first "
            "(nothing else holds it)"
        )

        with pytest.raises(ConcurrentSpineWriterError):
            backfill_spine(session_b, neo4j=clean_graph)

        # Release A (pg_try_advisory_xact_lock is TRANSACTION-scoped — commit auto-releases).
        trans_a.commit()

        # B's refused attempt must not poison the session: fresh transaction before the retry.
        session_b.rollback()
        result = backfill_spine(session_b, neo4j=clean_graph)
        assert result.statements_written >= 1, (
            "after the lock releases, the backfill must proceed and write the single member's row"
        )
    finally:
        session_b.close()
        conn_a.close()
        engine.dispose()


# =========================================================================================
# IT-BACKFILL-4: EDGE reconstruction — the completeness claim covers the WHOLE graph, not just
# nodes. An edge-schema entity (Ownership) in er_queue is backfilled through the same frozen
# fuse path, so a full_rebuild re-materialises the relationship and the whole-graph divergence
# (INCLUDING unexplained_edges) is 0 — non-vacuously (the fold carries >= 1 relationship).
# =========================================================================================


def _stamped(entity_id: str, schema: str, source_id: str, props: dict[str, list[str]]) -> FtmEntity:
    entity = make_entity(
        {"id": entity_id, "schema": schema, "properties": props, "datasets": [source_id]}
    )
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{source_id}/{entity_id}.json",
        ),
    )


def test_it_backfill_reconstructs_edges(
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    clean_diff_graph: Neo4jClient,
) -> None:
    engine, sessions = _sessions(postgres_dsn)
    src = "ksrc-it-backfill-edge:keep"
    a_id, b_id, own_id = "it-bf-edge-owner", "it-bf-edge-asset", "it-bf-edge-own1"

    owner = _stamped(a_id, "Person", src, {"name": ["Edge Owner"]})
    asset = _stamped(b_id, "Company", src, {"name": ["Edge Asset Co"]})
    ownership = _stamped(own_id, "Ownership", src, {"owner": [a_id], "asset": [b_id]})
    corpus = [owner, asset, ownership]

    # pre-2a: live graph + ledger self-rows + er_queue members, but NO spine rows.
    with sessions() as session:
        for entity in corpus:
            session.add(_queue_item(entity))
            assert entity.id is not None
            record_canonical(session, entity.id)  # singleton self-rows
        session.commit()

    ensure_constraints(clean_graph)
    write_entities(clean_graph, corpus)

    with sessions() as session:
        result = backfill_spine(session, neo4j=clean_graph)
    assert result.statements_written >= 3, (
        "the edge's owner/asset entity-typed claims + both endpoints' name claims must be "
        f"backfilled, got statements_written={result.statements_written}"
    )

    with sessions() as session:
        project(session, clean_diff_graph, full_rebuild=True)

    # Non-vacuity: the fold must actually carry a relationship, so unexplained_edges==0 is real.
    rel_count = clean_diff_graph.execute_read("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]
    assert rel_count >= 1, (
        "EDGE COMPLETENESS: the full_rebuild must re-materialise the Ownership relationship "
        "from the backfilled owner/asset claims (0 relationships would make the divergence "
        "check below vacuous)"
    )

    with sessions() as session:
        survivor_of = build_survivor_of(session)
    divergence = measure_divergence(
        read_graph_snapshot(clean_graph),
        read_graph_snapshot(clean_diff_graph),
        survivor_of,
        computed_at=_COMPUTED_AT,
    )
    assert divergence.total == 0 and divergence.unexplained_edges == 0, (
        "INV-BACKFILL-COMPLETE (edges): the whole-graph divergence must be 0 after backfilling an "
        f"edge corpus, got total={divergence.total} "
        f"(unexplained_edges={divergence.unexplained_edges})"
    )

    engine.dispose()
