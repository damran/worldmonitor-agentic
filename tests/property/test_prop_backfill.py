"""MANDATORY property/metamorphic tests for Gate 2b — statement/context-claim log backfill
(ADR 0113, spec ``docs/reviews/GATE_2B_BACKFILL_SPEC.md`` §3 P-BACKFILL-1..4).

Container-backed (real Postgres + real Neo4j, testcontainers via ``conftest.py``'s
``postgres_dsn``/``clean_graph`` fixtures) — Docker IS available locally; run this suite locally,
not CI-only (memory: docker-available-run-integration-locally). Each ``@given`` example TRUNCATEs
Postgres and wipes Neo4j at its start (mirrors ``test_prop_zeroprop_disposition.py``'s
``P-ZEROPROP-4`` pattern — a pytest fixture is NOT re-run per Hypothesis example, so the wipe must
happen INSIDE the test body); every per-example engine is disposed in ``try/finally`` (the
heavy-``@given``-leaks-connections hygiene, memory ``given-red-tests-leak-connections``).

Each test seeds a **synthetic pre-2a corpus**: ``canonical_id_ledger`` supersession alias rows
(via ``resolution.canonical.record_canonical``/``record_alias``, mirroring the durable-id ledger
ADR 0044 writes at a real promote point) + ``ErQueueItem`` rows (the SF-1 backfill substrate) —
but deliberately **NO** ``StatementRecord``/``ContextClaimRecord`` rows, reproducing "every
contribution resolved before Gate 2a's dual-write began" (ADR 0113 §Context).

INV-BACKFILL-COMPLETE / P-BACKFILL-1  (completeness / WPI-2 discharge): before ``backfill_spine``,
    ``project(full_rebuild=True)`` RAISES ``IncompleteAliasedSurvivorError`` (the WPI-2 obligation,
    ADR 0111, is un-discharged on a pre-2a corpus); after ``backfill_spine``,
    ``find_incomplete_aliased_survivors(...) == set()`` and a fresh ``full_rebuild`` reconstructs
    the survivor. METAMORPHIC NEGATIVE: a survivor whose ``ErQueueItem`` members are NEVER
    inserted (dropped from the backfill source) stays incomplete after backfill, and
    ``full_rebuild`` still raises for it.

INV-BACKFILL-FAITHFUL / INV-BACKFILL-IDEMPOTENT / P-BACKFILL-2  (byte-faithful dedup /
    idempotence): the backfilled ``statement_id`` set is EXACTLY what driving
    ``fuse_statement_rows`` over the same members directly would mint (the dual-write oracle); a
    SECOND ``backfill_spine`` call writes ZERO new rows (``skipped_duplicate`` absorbs them); a
    post-2a row already dual-written (via a real ``resolve_pending`` call) is never re-written.

INV-BACKFILL-STAMPED / P-BACKFILL-3  (rider-1 stamped-ness): every backfilled row's ``dataset``
    equals a real contributing member's ``source_id``; a member with an EMPTY ``source_id`` is
    skipped-and-logged (``skipped_source_unreachable``), never written with a source-unreachable
    ``dataset``, and contributes NO row at all (checked by ``entity_id``, not just by counting).

INV-BACKFILL-FORGET-SAFE / P-BACKFILL-4  (forget-safety, THE person-affecting invariant): a
    member whose source is enumerated in the erase-audit exclusion set (``TaskRun(kind="erase",
    status="ok")``) contributes ZERO backfilled rows (``skipped_erased_source``); a member whose
    ``ErQueueItem.raw_entity`` is already an ``{"erased": True, ...}`` shell contributes ZERO
    backfilled rows (``skipped_redacted_shell`` — a SEPARATE, independent mechanism from the
    exclusion set, proven by testing them in isolation); after backfill, a fresh
    ``full_rebuild`` into a wiped target contains NOTHING of either excluded source — checked via
    reached statement/context rows, the reconstructed prop VALUES, and a DIRECT live-node read for
    the anchor (never ``measure_divergence``, which is excluded on anchors — spec §3 P-BACKFILL-4
    oracle guidance).

All tests are RED at collection time: the module-level import of ``BackfillResult`` /
``backfill_spine`` / ``load_erased_sources`` / ``BackfillIncompleteError`` from
``worldmonitor.resolution.backfill`` fails with ``ImportError`` — that module does not exist until
the builder creates it (ADR 0113 / Gate 2b).

API-CONTRACT NOTE (flagged for the builder, not silently resolved): the gate's pinned signature
``backfill_spine(session, *, dry_run: bool = False) -> BackfillResult`` names no Neo4j client, yet
the mechanism description requires it to run ``scrub_stock(session)`` as the post-backfill
re-scrub (ADR 0113 SF-3(iii)) — and ``scrub_stock``'s FROZEN signature
(``resolution/erasure_scrub.py``, spec §8) is ``scrub_stock(session, *, neo4j: Neo4jClient)``.
Since ``erasure_scrub.py`` is FROZEN, ``backfill_spine`` MUST itself accept a Neo4j client to pass
through. Every call below therefore passes ``backfill_spine(session, neo4j=<client>, ...)`` (a
keyword named to match ``scrub_stock``'s own kwarg) — the builder should confirm or adjust the
exact parameter name/position; whichever they choose, these calls need the keyword updated to
match (a one-line, mechanical reconciliation, not a design change).
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    Base,
    ContextClaimRecord,
    ErQueueItem,
    StatementRecord,
    TaskRun,
)
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

# ---- GATE IMPORT — does not exist yet (RED for the right reason, ADR 0113 / Gate 2b) ----
from worldmonitor.resolution.backfill import (
    BackfillIncompleteError,
    BackfillResult,
    backfill_spine,
    load_erased_sources,
)
from worldmonitor.resolution.canonical import record_alias, record_canonical
from worldmonitor.resolution.merge import (
    ResolvedCluster,
    _merge_entities,  # pyright: ignore[reportPrivateUsage]
)
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import (
    _load_alias_map,  # pyright: ignore[reportPrivateUsage]
    project,
)
from worldmonitor.resolution.spine_integrity import (
    IncompleteAliasedSurvivorError,
    find_incomplete_aliased_survivors,
)
from worldmonitor.resolution.statements import fuse_statement_rows

pytestmark = pytest.mark.integration

_SUFFIX = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=2, max_size=6
)

_DB_SETTINGS = settings(
    deadline=None,
    max_examples=8,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
_DB_SETTINGS_HEAVY = settings(
    deadline=None,
    max_examples=5,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


@st.composite
def _member_suffixes(draw: st.DrawFn, *, n_min: int = 2, n_max: int = 4) -> list[str]:
    n = draw(st.integers(min_value=n_min, max_value=n_max))
    return draw(st.lists(_SUFFIX, min_size=n, max_size=n, unique=True))


def _cleanup_postgres(postgres_dsn: str) -> None:
    """TRUNCATE ALL relational tables to isolate hypothesis examples (also resets the ``seq``
    IDENTITY counters). Mirrors ``tests/property/test_prop_zeroprop_disposition.py``."""
    from sqlalchemy import text

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        with engine.begin() as conn:
            tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    finally:
        engine.dispose()


def _member_entity(
    member_id: str,
    *,
    name: str,
    source_id: str,
    alias_value: str | None = None,
    retrieved_at: str = "2026-07-12T00:00:00Z",
) -> FtmEntity:
    """A stamped, prop-bearing Person member (mirrors ``test_erasure_scrub.py``'s ``_person``)."""
    props: dict[str, list[str]] = {
        "name": [name],
        "nationality": ["ru"],
        "birthDate": ["1975-03-03"],
    }
    if alias_value is not None:
        props["alias"] = [alias_value]
    entity = make_entity({"id": member_id, "schema": "Person", "properties": props})
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=retrieved_at,
            reliability="A",
            source_record=f"s3://landing/{member_id}.json",
        ),
    )


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


def _wikidata_value(tag: str) -> str:
    return f"Q{700000 + (sum(ord(c) for c in tag) % 90000)}"


# ===========================================================================
# P-BACKFILL-1 — completeness / WPI-2 discharge (positive + metamorphic negative)
# ===========================================================================


@given(suffixes=_member_suffixes())
@_DB_SETTINGS
def test_p_backfill_1_positive_completeness_discharge(
    suffixes: list[str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    tag = uuid.uuid4().hex[:8]
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        member_ids = tuple(f"bf1-{tag}-{s}" for s in suffixes)
        canonical_id = f"wm-backfill1-{tag}"
        source_id = f"src:backfill1-{tag}"
        by_id = {
            mid: _member_entity(
                mid,
                name="Backfill Precondition Corp",
                source_id=source_id,
                alias_value=f"Alias-{mid}",
            )
            for mid in member_ids
        }

        with sessions() as session:
            for mid in member_ids:
                session.add(_queue_item(by_id[mid]))
            record_canonical(session, canonical_id)
            for mid in member_ids:
                record_alias(session, canonical_id, mid)
            session.commit()

        # Decorative pre-2a live graph node (spec §3: "graph nodes + ledger + er_queue members,
        # but NO spine rows") — inert for the fold assertions below (the target is wiped before
        # every full_rebuild call), included for corpus fidelity to the spec's stated shape.
        ensure_constraints(clean_graph)
        merged, dropped = _merge_entities(canonical_id, member_ids, by_id)
        assert dropped == ()
        write_entities(clean_graph, [merged])

        # --- Precondition: full_rebuild RAISES before backfill (no spine rows at all) ---
        with sessions() as session, pytest.raises(IncompleteAliasedSurvivorError):
            project(session, clean_graph, full_rebuild=True)

        with sessions() as session:
            result = backfill_spine(session, neo4j=clean_graph)

        assert isinstance(result, BackfillResult), (
            f"backfill_spine must return a BackfillResult, got {type(result).__name__!r}"
        )
        assert result.statements_written >= len(member_ids), (
            f"P-BACKFILL-1: expected >= {len(member_ids)} statement rows written for a "
            f"prop-bearing cluster, got {result.statements_written}"
        )
        assert result.survivors_covered >= 1

        # --- find_incomplete_aliased_survivors == set() via the SAME production alias map ---
        with sessions() as session:
            alias_map = _load_alias_map(session)
            stmt_rows = list(session.execute(select(StatementRecord)).scalars())
        incomplete = find_incomplete_aliased_survivors(alias_map, stmt_rows)
        assert incomplete == set(), (
            "INV-BACKFILL-COMPLETE VIOLATED: after backfill_spine, "
            f"find_incomplete_aliased_survivors must be empty on this single-cluster corpus, "
            f"got {sorted(incomplete)!r}"
        )

        # --- full_rebuild now succeeds and reconstructs the survivor, from a WIPED target ---
        clean_graph.execute_write("MATCH (n) DETACH DELETE n")
        with sessions() as session:
            proj_result = project(session, clean_graph, full_rebuild=True)
        assert proj_result.entities_written >= 1

        rows = clean_graph.execute_read(
            "MATCH (n {id: $id}) RETURN properties(n) AS props", id=canonical_id
        )
        assert len(rows) == 1, (
            f"INV-BACKFILL-COMPLETE VIOLATED: expected exactly 1 reconstructed node for "
            f"{canonical_id!r} on a WIPED target, got {len(rows)}"
        )
        props = rows[0]["props"] or {}
        assert props.get("prov_source_id") == source_id
        assert sorted(props.get("alias") or []) == sorted(f"Alias-{mid}" for mid in member_ids), (
            f"the fold-reconstructed alias set must match every backfilled member's contribution; "
            f"got {sorted(props.get('alias') or [])!r}"
        )
    finally:
        engine.dispose()


@given(suffixes_a=_member_suffixes(), suffixes_b=_member_suffixes())
@_DB_SETTINGS
def test_p_backfill_1_metamorphic_negative_dropped_survivor_stays_incomplete(
    suffixes_a: list[str],
    suffixes_b: list[str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """METAMORPHIC NEGATIVE: cluster B's ``ErQueueItem`` members are NEVER inserted (dropped from
    the backfill source) — B must stay incomplete after backfill, and ``full_rebuild`` must still
    raise for the whole corpus (cluster A alone becomes complete)."""
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    tag = uuid.uuid4().hex[:8]
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        member_ids_a = tuple(f"bf1neg-a-{tag}-{s}" for s in suffixes_a)
        member_ids_b = tuple(f"bf1neg-b-{tag}-{s}" for s in suffixes_b)
        canonical_a = f"wm-backfill1neg-a-{tag}"
        canonical_b = f"wm-backfill1neg-b-{tag}"
        src_a = f"src:backfill1neg-a-{tag}"
        by_id_a = {
            mid: _member_entity(mid, name="Neg Corp A", source_id=src_a) for mid in member_ids_a
        }

        with sessions() as session:
            for mid in member_ids_a:
                session.add(_queue_item(by_id_a[mid]))
            # Cluster B's er_queue members are DELIBERATELY OMITTED — "dropped from the source".
            record_canonical(session, canonical_a)
            for mid in member_ids_a:
                record_alias(session, canonical_a, mid)
            record_canonical(session, canonical_b)
            for mid in member_ids_b:
                record_alias(session, canonical_b, mid)
            session.commit()

        with sessions() as session, pytest.raises(IncompleteAliasedSurvivorError):
            project(session, clean_graph, full_rebuild=True)

        with sessions() as session:
            backfill_spine(session, neo4j=clean_graph)

        with sessions() as session:
            alias_map = _load_alias_map(session)
            stmt_rows = list(session.execute(select(StatementRecord)).scalars())
        incomplete = find_incomplete_aliased_survivors(alias_map, stmt_rows)

        assert canonical_a not in incomplete, (
            f"cluster A (fully sourced) must be covered after backfill; "
            f"incomplete={sorted(incomplete)!r}"
        )
        assert canonical_b in incomplete, (
            "METAMORPHIC NEGATIVE VIOLATED: cluster B, whose er_queue members were never "
            "inserted, must STILL be reported incomplete after backfill; "
            f"incomplete={sorted(incomplete)!r}"
        )

        with sessions() as session, pytest.raises(IncompleteAliasedSurvivorError):
            project(session, clean_graph, full_rebuild=True)
    finally:
        engine.dispose()


# ===========================================================================
# P-BACKFILL-2 — byte-faithful dedup / idempotence
# ===========================================================================


@given(suffixes=_member_suffixes())
@_DB_SETTINGS
def test_p_backfill_2_idempotent_second_run_and_byte_faithful_statement_id(
    suffixes: list[str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    tag = uuid.uuid4().hex[:8]
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        member_ids = tuple(f"bf2-{tag}-{s}" for s in suffixes)
        canonical_id = f"wm-backfill2-{tag}"
        source_id = f"src:backfill2-{tag}"
        by_id = {
            mid: _member_entity(
                mid, name="Idempotence Corp", source_id=source_id, alias_value=f"A-{mid}"
            )
            for mid in member_ids
        }

        # The byte-faithful oracle — what the dual-write path WOULD have minted, driven directly
        # over the SAME frozen fuse_statement_rows (order-independent per its own docstring).
        expected_cluster = ResolvedCluster(
            canonical_id=canonical_id,
            member_ids=tuple(sorted(member_ids)),
            entity=by_id[member_ids[0]],
            score=1.0,
        )
        expected_ids = {row.statement_id for row in fuse_statement_rows(expected_cluster, by_id)}
        assert expected_ids, "sanity: a prop-bearing cluster must yield >= 1 expected statement_id"

        with sessions() as session:
            for mid in member_ids:
                session.add(_queue_item(by_id[mid]))
            record_canonical(session, canonical_id)
            for mid in member_ids:
                record_alias(session, canonical_id, mid)
            session.commit()

        with sessions() as session:
            first = backfill_spine(session, neo4j=clean_graph)
        assert first.statements_written == len(expected_ids), (
            f"expected exactly {len(expected_ids)} statement rows written on the FIRST run, "
            f"got {first.statements_written}"
        )
        assert first.skipped_duplicate == 0, "nothing pre-exists on the first run"

        with sessions() as session:
            actual_ids = {
                row.statement_id
                for row in session.execute(
                    select(StatementRecord).where(StatementRecord.canonical_id == canonical_id)
                ).scalars()
            }
        assert actual_ids == expected_ids, (
            "INV-BACKFILL-FAITHFUL VIOLATED: the backfilled statement_id set does not equal what "
            f"the dual-write path would mint; expected={sorted(expected_ids)!r}, "
            f"got={sorted(actual_ids)!r}"
        )

        # --- SECOND run: idempotent, ZERO new rows ---
        with sessions() as session:
            second = backfill_spine(session, neo4j=clean_graph)
        assert second.statements_written == 0, (
            f"INV-BACKFILL-IDEMPOTENT VIOLATED: a second backfill_spine must write 0 new "
            f"statement rows, got {second.statements_written}"
        )
        assert second.context_claims_written == 0
        assert second.skipped_duplicate >= len(expected_ids), (
            f"the second run must report >= {len(expected_ids)} skipped_duplicate rows, "
            f"got {second.skipped_duplicate}"
        )

        with sessions() as session:
            actual_ids_after_second = {
                row.statement_id
                for row in session.execute(
                    select(StatementRecord).where(StatementRecord.canonical_id == canonical_id)
                ).scalars()
            }
        assert actual_ids_after_second == expected_ids, (
            "a second backfill must not duplicate any row"
        )
    finally:
        engine.dispose()


def test_p_backfill_2_post2a_dual_written_row_is_not_duplicated(
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """A row already dual-written via the LIVE Gate-2a pipeline (``resolve_pending``) must NOT be
    re-written by ``backfill_spine`` — the pre-filter reaches post-2a rows too (SF-2)."""
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        source_id = "src:backfill2-post2a"
        entity = _member_entity("bf2-post2a-m1", name="Post2A Singleton Corp", source_id=source_id)

        with sessions() as session:
            session.add(_queue_item(entity, status="pending"))
            session.commit()

        with sessions() as session:
            stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
        assert stats.promoted == 1

        with sessions() as session:
            pre_count = session.execute(
                select(func.count())
                .select_from(StatementRecord)
                .where(StatementRecord.canonical_id == "bf2-post2a-m1")
            ).scalar_one()
        assert pre_count >= 1, "precondition: resolve_pending must dual-write >= 1 statement row"

        with sessions() as session:
            result = backfill_spine(session, neo4j=clean_graph)
        assert result.statements_written == 0, (
            "INV-BACKFILL-IDEMPOTENT VIOLATED: a post-2a row already dual-written must NOT be "
            f"re-written by the backfill; got statements_written={result.statements_written}"
        )
        assert result.skipped_duplicate >= pre_count

        with sessions() as session:
            post_count = session.execute(
                select(func.count())
                .select_from(StatementRecord)
                .where(StatementRecord.canonical_id == "bf2-post2a-m1")
            ).scalar_one()
        assert post_count == pre_count, "row count for this canonical must be UNCHANGED"
    finally:
        engine.dispose()


# ===========================================================================
# P-BACKFILL-3 — rider-1 stamped-ness on the backfill path
# ===========================================================================


@given(suffixes=_member_suffixes(), data=st.data())
@_DB_SETTINGS
def test_p_backfill_3_rider1_empty_source_id_skipped_and_logged(
    suffixes: list[str],
    data: st.DataObject,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    tag = uuid.uuid4().hex[:8]
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        member_ids = tuple(f"bf3-{tag}-{s}" for s in suffixes)
        canonical_id = f"wm-backfill3-{tag}"
        flags = data.draw(
            st.lists(st.booleans(), min_size=len(member_ids), max_size=len(member_ids))
        )
        blanked = {mid for mid, flag in zip(member_ids, flags, strict=True) if flag}
        # Keep >= 1 reachable member so the cluster has SOME row (non-vacuity for the reachable
        # side); the interesting case here is the blanked members' skip, not total cluster loss.
        if blanked == set(member_ids):
            blanked.discard(member_ids[0])

        by_id: dict[str, FtmEntity] = {}
        for mid in member_ids:
            src = "" if mid in blanked else f"src:backfill3-{tag}-{mid}"
            by_id[mid] = _member_entity(
                mid, name="Rider1 Corp", source_id=src, alias_value=f"A-{mid}"
            )

        with sessions() as session:
            for mid in member_ids:
                session.add(_queue_item(by_id[mid]))
            record_canonical(session, canonical_id)
            for mid in member_ids:
                record_alias(session, canonical_id, mid)
            session.commit()

        with sessions() as session:
            result = backfill_spine(session, neo4j=clean_graph)

        assert result.skipped_source_unreachable >= len(blanked), (
            f"expected >= {len(blanked)} skipped_source_unreachable, got "
            f"{result.skipped_source_unreachable}"
        )

        with sessions() as session:
            rows = list(
                session.execute(
                    select(StatementRecord).where(StatementRecord.canonical_id == canonical_id)
                ).scalars()
            )
        written_entity_ids = {row.entity_id for row in rows}
        assert not (written_entity_ids & blanked), (
            "rider-1 VIOLATED: a blanked (empty-source_id) member has a written statement row: "
            f"{sorted(written_entity_ids & blanked)!r}"
        )
        assert all(row.dataset for row in rows), (
            "rider-1 VIOLATED: a written row's dataset must never be empty"
        )
        assert not any(row.dataset in blanked for row in rows), (
            "rider-1 VIOLATED: a written row's dataset must never equal a blanked member's own id "
            "(the source-unreachable fallback P2's scrub cannot reach)"
        )
    finally:
        engine.dispose()


# ===========================================================================
# P-BACKFILL-4 — forget-safety (person-affecting invariant)
# ===========================================================================


@given(kept_suffix=_SUFFIX)
@_DB_SETTINGS_HEAVY
def test_p_backfill_4_forget_safety_erased_and_shell_sources_contribute_nothing(
    kept_suffix: str,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """A member excluded via the erase-audit ``TaskRun`` set AND a member whose ``ErQueueItem``
    is already an ``{"erased": True}`` shell each contribute ZERO backfilled rows — tested as TWO
    INDEPENDENT mechanisms (neither source has both applied to it). After backfill, a fresh
    ``full_rebuild`` into a wiped target reconstructs the survivor WITHOUT either source's
    contribution: reached rows, the reconstructed alias VALUE, and a DIRECT live-node
    anchor read.
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    tag = uuid.uuid4().hex[:8]
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        canonical_id = f"wm-backfill4-{tag}"
        kept_src = f"ksrc-backfill4:{tag}"
        shell_src = f"shellsrc-backfill4:{tag}"
        excl_src = f"exclsrc-backfill4:{tag}"

        m_kept = f"bf4-kept-{tag}-{kept_suffix}"
        m_shell = f"bf4-shell-{tag}"
        m_excl = f"bf4-excl-{tag}"

        kept_entity = _member_entity(
            m_kept, name="Forget Safety Corp", source_id=kept_src, alias_value="OnlyFromKept"
        )
        excl_entity = _member_entity(
            m_excl, name="Forget Safety Corp", source_id=excl_src, alias_value="OnlyFromExcluded"
        )
        set_anchor(excl_entity, "wikidata_id", _wikidata_value(tag))

        with sessions() as session:
            session.add(_queue_item(kept_entity))
            session.add(_queue_item(excl_entity))
            # m_shell: an ALREADY-redacted er_queue shell (erasure.py's exact redaction shape) —
            # never a parseable entity, contributes NOTHING regardless of the exclusion set.
            session.add(
                ErQueueItem(
                    id=str(uuid.uuid4()),
                    connector_id="opensanctions",
                    entity_id=m_shell,
                    raw_entity={"erased": True, "source_id": shell_src},
                    source_record=f"s3://landing/{m_shell}.json",
                    status="resolved",
                )
            )
            record_canonical(session, canonical_id)
            record_alias(session, canonical_id, m_kept)
            record_alias(session, canonical_id, m_shell)
            record_alias(session, canonical_id, m_excl)
            # The erase-audit exclusion set (mirrors scrub_stock's real TaskRun enumeration; no
            # actual erase_source() call needed to exercise this INDEPENDENT mechanism).
            session.add(
                TaskRun(
                    id=str(uuid.uuid4()),
                    kind="erase",
                    status="ok",
                    stats={
                        "source_id": excl_src,
                        "authorized_by": "it-backfill4-op",
                        "nodes_deleted": 0,
                    },
                )
            )
            session.commit()

        with sessions() as session:
            erased = load_erased_sources(session)
        assert excl_src in erased, "load_erased_sources must surface the TaskRun-enumerated source"
        assert shell_src not in erased, (
            "the shell source must NOT be in the exclusion set — its skip is an INDEPENDENT "
            "mechanism (raw_entity shell detection), not the erase-audit TaskRun enumeration"
        )

        with sessions() as session:
            result = backfill_spine(session, neo4j=clean_graph)

        assert result.skipped_erased_source >= 1, (
            f"expected >= 1 skipped_erased_source, got {result.skipped_erased_source}"
        )
        assert result.skipped_redacted_shell >= 1, (
            f"expected >= 1 skipped_redacted_shell, got {result.skipped_redacted_shell}"
        )
        assert result.members_scanned >= 3

        with sessions() as session:
            rows = list(
                session.execute(
                    select(StatementRecord).where(StatementRecord.canonical_id == canonical_id)
                ).scalars()
            )
            ctx_rows = list(
                session.execute(
                    select(ContextClaimRecord).where(
                        ContextClaimRecord.canonical_id == canonical_id
                    )
                ).scalars()
            )

        assert not any(row.dataset == excl_src for row in rows), (
            "FORGET-SAFETY VIOLATED: a statement row carries the excluded source's dataset"
        )
        assert not any(row.dataset == shell_src for row in rows), (
            "FORGET-SAFETY VIOLATED: a statement row carries the shell source's dataset"
        )
        assert not any(row.entity_id in (m_shell, m_excl) for row in rows), (
            "FORGET-SAFETY VIOLATED: a statement row is attributed to an excluded/shell member"
        )
        assert not any(c.entity_id in (m_shell, m_excl) for c in ctx_rows), (
            "FORGET-SAFETY VIOLATED: a context_claim row (the excluded member's anchor) was "
            "backfilled"
        )

        # --- rebuild-contains-no-erased-source: fresh full_rebuild, direct-node oracle ---
        clean_graph.execute_write("MATCH (n) DETACH DELETE n")
        with sessions() as session:
            project(session, clean_graph, full_rebuild=True)

        fold_rows = clean_graph.execute_read(
            "MATCH (n {id: $id}) RETURN properties(n) AS props", id=canonical_id
        )
        assert len(fold_rows) == 1
        fold_props = fold_rows[0]["props"] or {}
        fold_alias = list(fold_props.get("alias") or [])
        assert "OnlyFromExcluded" not in fold_alias, (
            "FORGET-SAFETY VIOLATED: the excluded source's value resurrects on a fresh "
            f"full_rebuild: alias={fold_alias!r}"
        )
        assert "OnlyFromKept" in fold_alias, (
            "non-vacuity: the kept source's contribution must survive the backfill"
        )

        # DIRECT live-node anchor read (spec §3: NOT measure_divergence, which excludes anchors).
        anchor_row = clean_graph.execute_read(
            "MATCH (n {id: $id}) RETURN n.wikidata_id AS wid", id=canonical_id
        )[0]
        assert anchor_row["wid"] is None, (
            "FORGET-SAFETY VIOLATED: the excluded source's anchor resurrects on a fresh "
            f"full_rebuild: wikidata_id={anchor_row['wid']!r}"
        )
    finally:
        engine.dispose()


# ===========================================================================
# Sanity: the exception class shape
# ===========================================================================


def test_backfill_incomplete_error_is_a_runtime_error_subclass() -> None:
    assert issubclass(BackfillIncompleteError, RuntimeError)
