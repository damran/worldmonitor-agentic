"""MANDATORY property/metamorphic tests for Gate WPI-1 — zero-prop-entity disposition (ADR 0112).

``INV-ZEROPROP-DISPOSITION``: a promoted entity with ZERO FtM properties (and, since these
fixtures never set an anchor, always the zero-anchor sub-case too) has a decided, tested
disposition at BOTH promote points (pipeline ``_resolve_batch`` + sign-off ``approve``/
``reject``): it emits >= 1 ``wm:exists`` existence-claim ``StatementRecord`` so a
``full_rebuild`` reproduces its node.

Fast/pure arm (no DB, no Neo4j — ``fuse_statement_rows`` only reads its ``cluster``/``by_id``
arguments, mirrors the hygiene of ``tests/property/test_prop_alias_cocommit.py``):

P-ZEROPROP-1  one WM_EXISTS row PER MEMBER, with the deterministic sha256 statement_id.
P-ZEROPROP-2  rider-1 negative — members with an empty ``source_id`` are skipped-and-logged;
              no row's ``dataset`` is ever empty or a blanked member's own id.
P-ZEROPROP-3  rider-2 — every member id is derivable as an existence claim's ``entity_id``,
              even when ``canonical_id`` (a real-merge survivor id) differs from all of them.

DB-backed arm (container-backed, small ``max_examples``, ``deadline=None``, per-example engine
disposed in ``try/finally`` — the heavy-``@given``-leaks-connections hygiene, see memory
``given-red-tests-leak-connections``):

P-ZEROPROP-4  dispositioned at the PIPELINE promote point + the fold reproduces it: N (1..3)
              zero-prop SINGLETON entities promoted via ``resolve_pending`` (``batch_size=1``,
              so Splink is never invoked on a propertyless batch — ``score_pairs`` short-circuits
              for < 2 entities) each leave >= 1 ``WM_EXISTS`` statement row; wiping the graph and
              ``project(full_rebuild=True)`` reproduces a BARE node per survivor: id + schema
              label + ``prov_source_id``, NO ``prov_witnesses``, NO bare ``CANONICAL_ID_FIELDS``
              key (the zero-anchor sub-case).

All tests are RED today: ``from worldmonitor.resolution.statements import WM_EXISTS`` fails with
``ImportError`` — the module constant does not exist until the builder adds it (ADR 0112
Mechanism) — so this file fails to collect at all (gate-import idiom).
"""

from __future__ import annotations

import hashlib
import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import Base, ErQueueItem, StatementRecord
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import project
from worldmonitor.resolution.statements import (  # gate import: RED until builder adds WM_EXISTS
    WM_EXISTS,
    fuse_statement_rows,
)

# ---------------------------------------------------------------------------
# Hypothesis settings
# ---------------------------------------------------------------------------

_FAST_SETTINGS = settings(deadline=None, max_examples=50)
_DB_SETTINGS = settings(
    deadline=None,
    max_examples=15,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

_MEMBER_SUFFIX = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=2, max_size=6
)


def _zeroprop_entity(
    member_id: str, *, schema: str = "Person", source_id: str = "src:zp"
) -> FtmEntity:
    """A zero-prop AND zero-anchor stamped FtM entity (no anchor is ever set here)."""
    entity = make_entity({"id": member_id, "schema": schema, "properties": {}})
    stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at="2026-07-12T00:00:00Z",
            reliability="A",
            source_record=f"s3://landing/{member_id}.json",
        ),
    )
    return entity


@st.composite
def _zeroprop_cluster(
    draw: st.DrawFn, *, min_members: int = 1
) -> tuple[str, tuple[str, ...], dict[str, str]]:
    """Draws a zero-prop cluster: (canonical_id, member_ids, {member_id: source_id})."""
    n = draw(st.integers(min_value=min_members, max_value=max(min_members, 4)))
    suffixes = draw(st.lists(_MEMBER_SUFFIX, min_size=n, max_size=n, unique=True))
    member_ids = tuple(f"zp-p-{s}" for s in suffixes)
    sources = {mid: f"src:zp-p-{mid}" for mid in member_ids}
    canonical_id = "zp-p-canon-" + "-".join(sorted(suffixes))
    return canonical_id, member_ids, sources


# ===========================================================================
# P-ZEROPROP-1 — one WM_EXISTS row per member, deterministic statement_id
# ===========================================================================


@given(cluster=_zeroprop_cluster())
@_FAST_SETTINGS
def test_p_zeroprop_1_one_wm_exists_row_per_member(
    cluster: tuple[str, tuple[str, ...], dict[str, str]],
) -> None:
    canonical_id, member_ids, sources = cluster
    by_id = {
        mid: _zeroprop_entity(mid, schema="Person", source_id=sources[mid]) for mid in member_ids
    }
    resolved = ResolvedCluster(
        canonical_id=canonical_id, member_ids=member_ids, entity=by_id[member_ids[0]], score=1.0
    )

    rows = fuse_statement_rows(resolved, by_id)

    assert len(rows) == len(member_ids), (
        f"P-ZEROPROP-1 VIOLATED: expected exactly 1 WM_EXISTS row per member "
        f"({len(member_ids)} members), got {len(rows)}"
    )
    by_entity = {r.entity_id: r for r in rows}
    assert set(by_entity) == set(member_ids)
    for mid in member_ids:
        row = by_entity[mid]
        assert row.prop == WM_EXISTS
        assert row.value == ""
        assert row.canonical_id == canonical_id
        assert row.dataset == sources[mid]
        expected_stmt_id = hashlib.sha256(
            f"{canonical_id}\x00{mid}\x00{WM_EXISTS}\x00{sources[mid]}".encode()
        ).hexdigest()
        assert row.statement_id == expected_stmt_id, (
            f"P-ZEROPROP-1: statement_id must be the deterministic hash; expected "
            f"{expected_stmt_id!r}, got {row.statement_id!r}"
        )


# ===========================================================================
# P-ZEROPROP-2 — rider-1 negative: empty source_id members are skipped, never written
# ===========================================================================


@given(cluster=_zeroprop_cluster(), blank_flags=st.data())
@_FAST_SETTINGS
def test_p_zeroprop_2_rider1_empty_source_id_never_written(
    cluster: tuple[str, tuple[str, ...], dict[str, str]],
    blank_flags: st.DataObject,
) -> None:
    canonical_id, member_ids, sources = cluster
    flags = blank_flags.draw(
        st.lists(st.booleans(), min_size=len(member_ids), max_size=len(member_ids))
    )
    blanked = {mid for mid, flag in zip(member_ids, flags, strict=True) if flag}

    by_id = {
        mid: _zeroprop_entity(
            mid, schema="Person", source_id=("" if mid in blanked else sources[mid])
        )
        for mid in member_ids
    }
    resolved = ResolvedCluster(
        canonical_id=canonical_id, member_ids=member_ids, entity=by_id[member_ids[0]], score=1.0
    )

    rows = fuse_statement_rows(resolved, by_id)

    expected_valid = {mid for mid in member_ids if mid not in blanked}
    got_ids = {r.entity_id for r in rows}
    assert got_ids == expected_valid, (
        f"P-ZEROPROP-2 (rider-1) VIOLATED: blanked members {sorted(blanked)!r} must be "
        f"skipped-and-logged; got entity_ids={sorted(got_ids)!r}, "
        f"expected {sorted(expected_valid)!r}"
    )
    assert all(r.dataset for r in rows), (
        "rider-1 VIOLATED: every written row's dataset must be non-empty"
    )
    assert not any(r.dataset in blanked for r in rows), (
        "rider-1 VIOLATED: a row's dataset must never equal a (blanked) member id "
        "(the source-unreachable fallback P2's scrub cannot reach)"
    )


# ===========================================================================
# P-ZEROPROP-3 — rider-2: every member id is derivable, distinct from canonical_id
# ===========================================================================


@given(cluster=_zeroprop_cluster(min_members=2))
@_FAST_SETTINGS
def test_p_zeroprop_3_rider2_member_ids_derivable_and_distinct_from_canonical(
    cluster: tuple[str, tuple[str, ...], dict[str, str]],
) -> None:
    canonical_id, member_ids, sources = cluster
    by_id = {
        mid: _zeroprop_entity(mid, schema="Person", source_id=sources[mid]) for mid in member_ids
    }
    # A canonical_id guaranteed distinct from every member id (mirrors a real wmc- merge id).
    merge_canonical_id = "wmc-" + canonical_id
    resolved = ResolvedCluster(
        canonical_id=merge_canonical_id,
        member_ids=member_ids,
        entity=by_id[member_ids[0]],
        score=1.0,
    )
    assert merge_canonical_id not in member_ids  # sanity: the distinguishing property holds

    rows = fuse_statement_rows(resolved, by_id)

    entity_ids = {r.entity_id for r in rows}
    assert entity_ids == set(member_ids), (
        f"P-ZEROPROP-3 (rider-2) VIOLATED: every member id must be derivable as an existence "
        f"claim's entity_id even though canonical_id={merge_canonical_id!r} differs from all of "
        f"them; got entity_ids={sorted(entity_ids)!r}"
    )
    assert all(r.canonical_id == merge_canonical_id for r in rows)


# ===========================================================================
# P-ZEROPROP-4 — DB-backed: pipeline promote point + fold reproduces the bare node
# ===========================================================================


def _cleanup_postgres(postgres_dsn: str) -> None:
    """Truncate ALL relational tables to isolate hypothesis examples (also resets the
    ``seq`` IDENTITY counters). Mirrors ``tests/property/test_prop_single_writer_seq_gap.py``."""
    from sqlalchemy import text

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        with engine.begin() as conn:
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    finally:
        engine.dispose()


@st.composite
def _zeroprop_singleton_ids(draw: st.DrawFn) -> list[str]:
    n = draw(st.integers(min_value=1, max_value=3))
    suffixes = draw(st.lists(_MEMBER_SUFFIX, min_size=n, max_size=n, unique=True))
    # uuid-namespaced so distinct hypothesis examples never collide across TRUNCATEs.
    return [f"zp-db-{uuid.uuid4().hex[:8]}-{s}" for s in suffixes]


@given(member_ids=_zeroprop_singleton_ids())
@_DB_SETTINGS
def test_p_zeroprop_4_pipeline_promote_leaves_row_and_fold_reproduces_bare_node(
    member_ids: list[str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        with sessions() as session:
            for mid in member_ids:
                entity = _zeroprop_entity(mid, schema="Person", source_id=f"src:{mid}")
                session.add(
                    ErQueueItem(
                        id=str(uuid.uuid4()),
                        connector_id="opensanctions",
                        raw_entity=entity.to_dict(),
                        source_record=f"s3://landing/{mid}.json",
                        status="pending",
                    )
                )
            session.commit()

        with sessions() as session:
            stats = resolve_pending(
                session=session, neo4j=clean_graph, guard_mode="block", batch_size=1
            )
        assert stats.promoted == len(member_ids), (
            f"P-ZEROPROP-4 precondition: expected {len(member_ids)} promoted singletons, got "
            f"{stats.promoted}"
        )

        with sessions() as session:
            for mid in member_ids:
                count = session.execute(
                    select(func.count())
                    .select_from(StatementRecord)
                    .where(StatementRecord.canonical_id == mid)
                ).scalar_one()
                assert count >= 1, (
                    f"P-ZEROPROP-4 VIOLATED: zero-prop singleton {mid!r} left NO statement row "
                    "after pipeline promotion — INV-ZEROPROP-DISPOSITION requires >= 1 WM_EXISTS "
                    "row at the pipeline promote point"
                )

        clean_graph.execute_write("MATCH (n) DETACH DELETE n")
        with sessions() as session:
            project(session, clean_graph, full_rebuild=True)

        for mid in member_ids:
            rows = clean_graph.execute_read(
                "MATCH (n {id: $id}) RETURN properties(n) AS props, labels(n) AS lbls", id=mid
            )
            assert len(rows) == 1, (
                f"P-ZEROPROP-4 VIOLATED: full_rebuild must reproduce exactly 1 bare node for "
                f"zero-prop survivor {mid!r}, got {len(rows)}"
            )
            props = rows[0]["props"] or {}
            labels = rows[0]["lbls"] or []
            assert "Person" in labels, f"expected the Person schema label on {mid!r}, got {labels}"
            assert props.get("prov_source_id"), (
                f"bare node for {mid!r} must carry prov_source_id (G1)"
            )
            assert "prov_witnesses" not in props, (
                f"bare node for {mid!r} must have NO prov_witnesses (empty witness map, "
                "stamp_witness_map({}) no-op)"
            )
            assert not (set(props) & set(CANONICAL_ID_FIELDS)), (
                f"bare node for {mid!r} must carry NO bare CANONICAL_ID_FIELDS key "
                "(zero-prop-AND-zero-anchor sub-case)"
            )
    finally:
        engine.dispose()
