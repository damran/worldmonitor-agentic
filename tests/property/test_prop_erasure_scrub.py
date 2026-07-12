"""Property/metamorphic tests for Gate P2 — right-to-forget reaches the SoR (ADR 0107).

Four mandatory ``@given`` invariants (spec ``docs/reviews/GATE_P2_ERASURE_SPEC.md`` §3):

P-ERASE-1  The round-trip asserts BOTH surfaces, with TWO vacuity fences (Slice P2-c). After
           ``erase_source(..., source_id)`` on a seeded corpus, then
           ``project(full_rebuild=True)`` into a FRESH isolated Neo4j target: (i) the fresh
           rebuild contains NOTHING of the erased source — zero ``statement``/``context_claim``
           rows reached by the ``(dataset == source_id)`` predicate, no reconstructed node/edge
           value equal to an erased-source-ONLY value, no ``decision.member_ids`` referencing an
           erased member; AND (ii) the LIVE graph no longer holds the erased values —
           sole-source nodes gone, co-witnessed erased-only values removed, erased anchor values
           removed (verified by a DIRECT live-node property read, never ``measure_divergence``,
           which ``_excludes`` ``CANONICAL_ID_FIELDS`` — divergence.py:96-102 — and would go
           green with a residual erased anchor still on the node).

P-ERASE-2  ``full_rebuild`` over the scrubbed log is erased-free UNCONDITIONALLY, regardless of
           whether a prior fold ran before the erasure (the SF-7 DR-path bound; P-FOLD-2 stays
           byte-frozen in its own no-deletion regime — untouched here).

P-ERASE-3  Decision-row redaction preserves the judgement, removes only the reference: the row
           EXISTS with byte-identical kind/score/decided_by/evidence; ``member_ids`` has exactly
           the erased ids removed; a ``full_rebuild`` reconstructs the survivor identically
           (proving the projector never consumes ``member_ids``, ``projector.py:346-349,417``).

P-ERASE-4  The live value-prune preserves G1 provenance: every surviving pruned node still
           carries a non-empty ``prov_source_id`` and its (pruned) ``prov_witnesses``/``id`` — the
           value-prune removes erased values WITHOUT wiping provenance.

Every corpus is a HAND-CRAFTED (but faithful) fixture — direct ``StatementRecord`` /
``ContextClaimRecord`` / ``DecisionRecord`` rows mirroring EXACTLY the shape
``resolution/statements.py`` writes (Gate 2a/P1 dual-write), plus a Neo4j live graph built via the
SAME production merge path (``resolution.merge._merge_entities`` + ``graph.writer.write_entities``,
mirroring ``tests/integration/test_erasure_graph.py``) — not a full ER-pipeline run, so the
scenario shape (sole-source node / co-witnessed-erased-only-value survivor / erased-source anchor
/ decision referencing an erased member) is deterministic and Splink-threshold-independent.

RED today (against master, pre-Gate-P2): P-ERASE-1/2/4 run against the EXISTING entry points
(``erase_source``, ``project``, direct Neo4j/Postgres reads — no new symbol) and FAIL on
assertions: (i) resurrects on rebuild (the log is never scrubbed), the co-witnessed erased-only
value + the erased anchor survive BOTH the fresh fold and the live graph
(``erase_source_graph`` is prop-granular / never touches bare anchor keys, ``graph/ops.py:106-
136``). P-ERASE-3 imports the NOT-YET-EXISTING ``worldmonitor.resolution.erasure_scrub`` LOCALLY
(inside the test function only, so this file's OTHER tests still collect and run) and fails with
``ImportError`` at test-run time.

Container-heavy examples wrap their per-example engine in ``try/finally: engine.dispose()``
(memory: given-red-tests-leak-connections). Every anchor/id is minted from a per-example-unique
``suffix`` (memory: P-CTX-6 duplicate-anchor-across-examples trap) AND the live/fold graphs are
wiped at the TOP of each test body (inside the ``@given``-wrapped function, so it re-runs per
Hypothesis example — the ``test_prop_signoff_spine.py`` idiom, not a function-scoped fixture,
which only wipes once per test FUNCTION, not per example).
``@pytest.mark.integration`` sits OUTERMOST, above ``@given``/``@example`` (house convention).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import Base, ContextClaimRecord, DecisionRecord, StatementRecord
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import _merge_entities  # pyright: ignore[reportPrivateUsage]
from worldmonitor.resolution.projector import project
from worldmonitor.storage.landing import LandingStore

_SETTINGS = settings(
    max_examples=5,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.data_too_large,
    ],
)

_ALNUM = "abcdefghij0123456789"
_RETRIEVED_AT = "2026-07-11T00:00:00Z"

# A SECOND, isolated Neo4j image/password — deliberately distinct from conftest.py's
# NEO4J_TEST_PASSWORD and from the other property suites' diff-target literals (never the same
# instance as `clean_graph`). Duplicated per-file (test_prop_signoff_spine.py's convention).
_DIFF_NEO4J_IMAGE = "neo4j:2026.05.0-community"
_DIFF_NEO4J_PW = "testpw-p2-erase-prop-diff"  # pragma: allowlist secret


@pytest.fixture(scope="module")
def diff_neo4j_client() -> Any:
    """A SECOND, isolated Neo4j container — the fold target for the fresh-rebuild oracle.

    Wiped manually inside each Hypothesis example (not a function-scoped fixture — a
    container-backed fixture is instantiated ONCE per test function, not once per example).
    """
    from testcontainers.neo4j import Neo4jContainer

    with Neo4jContainer(_DIFF_NEO4J_IMAGE, password=_DIFF_NEO4J_PW) as container:
        client = Neo4jClient.connect(
            uri=container.get_connection_url(), user="neo4j", password=_DIFF_NEO4J_PW
        )
        client.verify()
        yield client
        client.close()


# ---------------------------------------------------------------------------
# Corpus construction — hand-crafted Postgres log rows + a matching Neo4j live graph.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EraseScenario:
    suffix: str


@st.composite
def _p_erase_scenario(draw: st.DrawFn) -> _EraseScenario:
    suffix = draw(st.text(alphabet=_ALNUM, min_size=6, max_size=10))
    return _EraseScenario(suffix=suffix)


@dataclass(frozen=True)
class _CorpusIds:
    erased_src: str
    keep_src: str
    sole_id: str
    survivor_id: str
    m1: str
    m2: str
    anchor_value: str


def _anchor_value(suffix: str) -> str:
    """A deterministic, valid-shaped QID derived from ``suffix`` (no hash-randomization;
    mirrors ``test_prop_signoff_spine.py``'s ``_anchor_value``, duplicated per-file)."""
    return f"Q{100000 + (sum(ord(c) for c in suffix) % 800000)}"


def _stmt(
    canonical_id: str, entity_id: str, prop: str, value: str, dataset: str
) -> StatementRecord:
    """One hand-crafted ``StatementRecord``, mirroring exactly the shape
    ``resolution/statements.py::fuse_statement_rows`` writes at merge time."""
    return StatementRecord(
        id=str(uuid.uuid4()),
        statement_id=str(uuid.uuid4()),
        canonical_id=canonical_id,
        entity_id=entity_id,
        schema="Person",
        prop=prop,
        value=value,
        dataset=dataset,
        reliability="A",
        retrieved_at=_RETRIEVED_AT,
        raw_pointer=f"s3://landing/{dataset}/{entity_id}.json",
        first_seen=_RETRIEVED_AT,
        last_seen=_RETRIEVED_AT,
        method=None,
        scope="default",
    )


def _person(entity_id: str, source_id: str, props: dict[str, list[str]]) -> FtmEntity:
    """A single-source FtM Person stamped with provenance tracing to ``source_id``."""
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


def _seed_corpus(session: Session, neo4j: Neo4jClient, scenario: _EraseScenario) -> _CorpusIds:
    """Seed BOTH the Postgres SoR log AND the Neo4j live graph for one P-ERASE scenario.

    Contains, per the spec's mandatory generator: (1) a SOLE-source node (``sole_id``, entirely
    from ``erased_src``); (2) a MULTI-source survivor (``survivor_id``, members ``m1``/``m2``)
    whose ``alias`` prop is CO-WITNESSED (both sources contribute a value) but ONE value
    (``"OnlyFromErased"``) is erased-source-ONLY — the SF-4 hard case (``graph/ops.py:106-136``,
    prop-granular witness map, no per-value attribution); (3) an ERASED-SOURCE ANCHOR
    (``wikidata_id``) on the surviving node, claimed ONLY by ``m1`` (the erased member) in the
    ``context_claim`` log — bare anchor keys are never in the witness map, so
    ``erase_source_graph`` never removes them (surprise #1); (4) a ``decision`` row referencing
    the erased member ``m1`` (SF-2).
    """
    sfx = scenario.suffix
    erased_src = f"esrc:{sfx}"
    keep_src = f"ksrc:{sfx}"
    sole_id = f"sole-{sfx}"
    survivor_id = f"surv-{sfx}"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    anchor_value = _anchor_value(sfx)

    # --- Postgres: the SoR log (statement + context_claim + decision) ---
    session.add(_stmt(sole_id, sole_id, "name", "Sole PII Name", erased_src))
    session.add(_stmt(survivor_id, m1, "name", "Shared Name", erased_src))
    session.add(_stmt(survivor_id, m2, "name", "Shared Name", keep_src))
    session.add(_stmt(survivor_id, m1, "alias", "OnlyFromErased", erased_src))
    session.add(_stmt(survivor_id, m2, "alias", "OnlyFromKept", keep_src))
    session.add(
        ContextClaimRecord(
            id=str(uuid.uuid4()),
            canonical_id=survivor_id,
            entity_id=m1,
            key="wikidata_id",
            value=anchor_value,
            dataset=erased_src,
            method="connector:map",
            retrieved_at=_RETRIEVED_AT,
            scope="default",
        )
    )
    session.add(
        DecisionRecord(
            id=str(uuid.uuid4()),
            canonical_id=survivor_id,
            kind="merge",
            member_ids=[m1, m2],
            score=0.91,
            decided_by="auto:resolver",
            evidence={"reason": "p2-prop-corpus"},
            supersedes=None,
            superseded_by=None,
            scope="default",
        )
    )
    session.commit()

    # --- Neo4j: the LIVE graph mirroring that log ---
    ensure_constraints(neo4j)
    sole_entity = _person(sole_id, erased_src, {"name": ["Sole PII Name"]})
    by_id = {
        m1: _person(m1, erased_src, {"name": ["Shared Name"], "alias": ["OnlyFromErased"]}),
        m2: _person(m2, keep_src, {"name": ["Shared Name"], "alias": ["OnlyFromKept"]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2), by_id)
    assert dropped == (), f"unexpected schema-incompatible drop: {dropped!r}"
    set_anchor(merged, "wikidata_id", anchor_value)
    write_entities(neo4j, [sole_entity, merged])

    return _CorpusIds(
        erased_src=erased_src,
        keep_src=keep_src,
        sole_id=sole_id,
        survivor_id=survivor_id,
        m1=m1,
        m2=m2,
        anchor_value=anchor_value,
    )


def _landing(minio: tuple[str, str, str]) -> LandingStore:
    """A LandingStore on a per-example bucket (mirrors ``tests/integration/test_erasure.py``)."""
    endpoint, access_key, secret_key = minio
    store = LandingStore.connect(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=f"landing-p2-{uuid.uuid4().hex[:8]}",
    )
    store.ensure_bucket()
    return store


def _read_node(client: Neo4jClient, node_id: str) -> dict[str, Any] | None:
    rows = client.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN properties(n) AS props", id=node_id
    )
    return dict(rows[0]["props"]) if rows else None


def _cleanup_postgres(postgres_dsn: str) -> None:
    """Truncate ALL relational tables between Hypothesis examples (P-FOLD-2 / P-SIGN idiom)."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    engine.dispose()


# ===========================================================================
# P-ERASE-1: the round-trip asserts BOTH surfaces, with TWO vacuity fences
# ===========================================================================


@pytest.mark.integration
@given(scenario=_p_erase_scenario())
@example(scenario=_EraseScenario(suffix="pin1erase"))
@_SETTINGS
def test_p_erase_1_round_trip_both_surfaces(
    scenario: _EraseScenario,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    diff_neo4j_client: Neo4jClient,
    minio: tuple[str, str, str],
) -> None:
    """P-ERASE-1 / INV-ERASE-3LANE + INV-ERASE-DECISION-REDACT + INV-ERASE-LIVE-VALUE +
    INV-ERASE-NONRESURRECT + INV-ERASE-BOTH-SURFACES.

    RED today: EVERY assertion below fails against master — the log is never scrubbed (the fresh
    rebuild resurrects the sole-source node, the erased-source-only alias value, and the
    erased-source anchor; the decision row still references the erased member) AND the live
    graph still holds the co-witnessed erased-only value + the erased anchor (``erase_source_graph``
    is prop-granular and never touches bare anchor keys).
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    diff_neo4j_client.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        landing = _landing(minio)

        with sessions() as session:
            corpus = _seed_corpus(session, clean_graph, scenario)

        from worldmonitor.erasure import erase_source

        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=corpus.erased_src,
                authorized_by="p2-prop-op-1",
            )
            session.commit()

        # ---- Clause (i): a fresh full_rebuild into an ISOLATED target contains NOTHING of the
        # erased source (the fresh-target-only vacuity fence is satisfied by ALSO asserting
        # clause (ii) below — a fresh-target-only oracle alone is REJECTED, SF-6). ----
        with sessions() as session:
            stmt_reached = session.execute(
                select(func.count())
                .select_from(StatementRecord)
                .where(StatementRecord.dataset == corpus.erased_src)
            ).scalar_one()
            ctx_reached = session.execute(
                select(func.count())
                .select_from(ContextClaimRecord)
                .where(ContextClaimRecord.dataset == corpus.erased_src)
            ).scalar_one()
        assert stmt_reached == 0, (
            "P-ERASE-1 INV-ERASE-3LANE VIOLATED: "
            f"{stmt_reached} statement row(s) with dataset={corpus.erased_src!r} survive "
            "erase_source (the log is never scrubbed on master)"
        )
        assert ctx_reached == 0, (
            "P-ERASE-1 INV-ERASE-3LANE VIOLATED: "
            f"{ctx_reached} context_claim row(s) with dataset={corpus.erased_src!r} survive "
            "erase_source (the log is never scrubbed on master)"
        )

        with sessions() as session:
            decision = session.execute(
                select(DecisionRecord).where(DecisionRecord.canonical_id == corpus.survivor_id)
            ).scalar_one()
        assert corpus.m1 not in decision.member_ids, (
            "P-ERASE-1 INV-ERASE-DECISION-REDACT VIOLATED: "
            f"decision.member_ids={decision.member_ids!r} still references the erased member "
            f"{corpus.m1!r}"
        )
        assert corpus.m2 in decision.member_ids, "non-vacuity: the KEPT member must survive"

        with sessions() as session:
            project(session, diff_neo4j_client, full_rebuild=True, checkpoint_id="p2-erase-1-diff")

        fold_sole = _read_node(diff_neo4j_client, corpus.sole_id)
        assert fold_sole is None, (
            "P-ERASE-1 INV-ERASE-NONRESURRECT VIOLATED: the sole-source node "
            f"{corpus.sole_id!r} resurrects on a fresh full_rebuild"
        )
        fold_survivor = _read_node(diff_neo4j_client, corpus.survivor_id)
        assert fold_survivor is not None, "the survivor must still fold (2 non-erased-only rows)"
        fold_alias = list(fold_survivor.get("alias") or [])
        assert "OnlyFromErased" not in fold_alias, (
            "P-ERASE-1 INV-ERASE-NONRESURRECT VIOLATED: the erased-source-only co-witnessed "
            f"value survives a fresh full_rebuild: alias={fold_alias!r}"
        )
        assert "OnlyFromKept" in fold_alias, "non-vacuity: the KEPT value must still fold"
        assert fold_survivor.get("wikidata_id") is None, (
            "P-ERASE-1 INV-ERASE-NONRESURRECT VIOLATED: the erased-source-only anchor "
            f"{corpus.anchor_value!r} survives a fresh full_rebuild"
        )

        # ---- Clause (ii), MANDATORY: the LIVE graph no longer holds the erased values. A
        # fresh-target-only oracle is REJECTED (SF-6) — this is the vacuity fence. ----
        assert _read_node(clean_graph, corpus.sole_id) is None, (
            "P-ERASE-1 clause (ii) / fresh-target-only vacuity fence: the sole-source LIVE node "
            "must be gone too, not just absent from the fold"
        )
        live_survivor = _read_node(clean_graph, corpus.survivor_id)
        assert live_survivor is not None, "the multi-source survivor must SURVIVE"
        live_alias = list(live_survivor.get("alias") or [])
        assert "OnlyFromErased" not in live_alias, (
            "P-ERASE-1 INV-ERASE-LIVE-VALUE VIOLATED: the co-witnessed erased-source-only value "
            f"survives on the LIVE node: alias={live_alias!r} "
            "(erase_source_graph is prop-granular — graph/ops.py:106-136)"
        )
        assert "OnlyFromKept" in live_alias, "non-vacuity: the KEPT value must survive live too"

        # Checker-flagged gap (round-3): `_seed_corpus` ALSO co-witnesses `name` with the
        # IDENTICAL literal value "Shared Name" across BOTH m1 (erased_src) and m2 (keep_src) —
        # but until now nothing asserted on the live `name` VALUE post-erasure. A removal filter
        # keyed ONLY on "this scrub's deleted rows carried this exact literal value" (ignoring
        # whether a surviving source's row ALSO still yields it) wipes "Shared Name" here even
        # though m2's (survivor_id, "name", "Shared Name", keep_src) row is never reached by the
        # scrub at all — the round-2 narrower bug this round's fix must close.
        assert live_survivor.get("name") == ["Shared Name"], (
            "P-ERASE-1 CHECKER-FLAGGED GAP VIOLATED: the identical `name` value co-witnessed by "
            "BOTH the erased source (m1) and a SURVIVING source (m2) must survive on the LIVE "
            f"node, got name={live_survivor.get('name')!r} — a removal filter keyed only on "
            "erased-attribution (never consulting the post-scrub fold for a second, independent "
            "still-vouched-for signal) wipes a value a surviving source still legitimately holds"
        )

        # Anchor-oracle vacuity fence (MANDATORY, SF-6): a DIRECT live-node property read — NEVER
        # measure_divergence, which `_excludes` CANONICAL_ID_FIELDS (divergence.py:96-102) and
        # would go green with a residual erased anchor still on the node.
        live_anchor = clean_graph.execute_read(
            "MATCH (n:Entity {id: $id}) RETURN n.wikidata_id AS wid", id=corpus.survivor_id
        )[0]["wid"]
        assert live_anchor is None, (
            "P-ERASE-1 INV-ERASE-LIVE-VALUE / anchor-oracle-vacuity-fence VIOLATED: the "
            f"erased-source-only anchor {corpus.anchor_value!r} still lives on the node "
            "(bare anchor keys are never in the witness map — erase_source_graph never removes "
            "them; a REMOVE-only prune is required, HIGH-2)"
        )
    finally:
        engine.dispose()


# ===========================================================================
# P-ERASE-2: full_rebuild over the scrubbed log is erased-free UNCONDITIONALLY
# ===========================================================================


@st.composite
def _p_erase_2_scenario(draw: st.DrawFn) -> tuple[_EraseScenario, bool]:
    suffix = draw(st.text(alphabet=_ALNUM, min_size=6, max_size=10))
    fold_before_erase = draw(st.booleans())
    return _EraseScenario(suffix=suffix), fold_before_erase


@pytest.mark.integration
@given(scenario_and_interleave=_p_erase_2_scenario())
@example(scenario_and_interleave=(_EraseScenario(suffix="p2foldbef1"), True))
@example(scenario_and_interleave=(_EraseScenario(suffix="p2foldaft1"), False))
@_SETTINGS
def test_p_erase_2_full_rebuild_erased_free_regardless_of_interleaving(
    scenario_and_interleave: tuple[_EraseScenario, bool],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    diff_neo4j_client: Neo4jClient,
    minio: tuple[str, str, str],
) -> None:
    """P-ERASE-2 / INV-ERASE-FOLD-DR (the DR path). ``full_rebuild`` over the scrubbed log is
    erased-free unconditionally, regardless of whether a fold happened to run BEFORE the erasure
    (an ongoing cadence must not somehow immunise a LATER fold from picking up the scrub).
    ``P-FOLD-2`` stays byte-frozen (its own no-deletion regime, untouched by this sibling).

    RED today: the fresh full_rebuild resurrects the sole-source node AND the erased-source-only
    co-witnessed value in BOTH interleaving arms (the log is never scrubbed on master).
    """
    scenario, fold_before_erase = scenario_and_interleave
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    diff_neo4j_client.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        landing = _landing(minio)

        with sessions() as session:
            corpus = _seed_corpus(session, clean_graph, scenario)

        if fold_before_erase:
            # A fold BEFORE the erase (a prior incremental/DR cadence tick) must not somehow
            # immunise a LATER fold from picking up the scrub.
            with sessions() as session:
                project(
                    session, diff_neo4j_client, full_rebuild=True, checkpoint_id="p2-erase-2-diff"
                )
            diff_neo4j_client.execute_write("MATCH (n) DETACH DELETE n")

        from worldmonitor.erasure import erase_source

        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=corpus.erased_src,
                authorized_by="p2-prop-op-2",
            )
            session.commit()

        with sessions() as session:
            project(session, diff_neo4j_client, full_rebuild=True, checkpoint_id="p2-erase-2-diff")

        assert _read_node(diff_neo4j_client, corpus.sole_id) is None, (
            "P-ERASE-2 INV-ERASE-FOLD-DR VIOLATED "
            f"(fold_before_erase={fold_before_erase}): the sole-source node resurrects on a "
            "post-erase full_rebuild"
        )
        fold_survivor = _read_node(diff_neo4j_client, corpus.survivor_id)
        assert fold_survivor is not None
        fold_alias = list(fold_survivor.get("alias") or [])
        assert "OnlyFromErased" not in fold_alias, (
            "P-ERASE-2 INV-ERASE-FOLD-DR VIOLATED "
            f"(fold_before_erase={fold_before_erase}): a post-erase full_rebuild is NOT "
            f"erased-free — the erased-source-only value survives: alias={fold_alias!r}"
        )
        assert fold_survivor.get("wikidata_id") is None, (
            "P-ERASE-2 INV-ERASE-FOLD-DR VIOLATED "
            f"(fold_before_erase={fold_before_erase}): the erased-source-only anchor survives a "
            "post-erase full_rebuild"
        )
    finally:
        engine.dispose()


# ===========================================================================
# P-ERASE-3: decision-row redaction preserves the judgement, removes the reference
# ===========================================================================


def _seed_decision_corpus(
    session: Session, scenario: _EraseScenario, kind: str
) -> tuple[str, str, str, str]:
    """Seed a decision row + matching statement rows for one P-ERASE-3 ``kind`` (all-erased /
    some-erased / none-erased). Returns ``(erased_src, survivor_id, m1, m2)``."""
    sfx = scenario.suffix
    erased_src = f"esrc3:{sfx}"
    keep_src = f"ksrc3:{sfx}"
    survivor_id = f"surv3-{sfx}"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"

    m1_dataset = erased_src if kind in ("all", "some") else keep_src
    m2_dataset = erased_src if kind == "all" else keep_src

    session.add(_stmt(survivor_id, m1, "name", "Shared Name", m1_dataset))
    session.add(_stmt(survivor_id, m2, "name", "Shared Name", m2_dataset))
    session.add(
        DecisionRecord(
            id=str(uuid.uuid4()),
            canonical_id=survivor_id,
            kind="merge",
            member_ids=[m1, m2],
            score=0.87,
            decided_by="auto:resolver",
            evidence={"reason": "p2-erase-3"},
            supersedes=None,
            superseded_by=None,
            scope="default",
        )
    )
    session.commit()
    return erased_src, survivor_id, m1, m2


@st.composite
def _p_erase_3_scenario(draw: st.DrawFn) -> tuple[_EraseScenario, str]:
    suffix = draw(st.text(alphabet=_ALNUM, min_size=6, max_size=10))
    kind = draw(st.sampled_from(("all", "some", "none")))
    return _EraseScenario(suffix=suffix), kind


@pytest.mark.integration
@given(scenario_and_kind=_p_erase_3_scenario())
@example(scenario_and_kind=(_EraseScenario(suffix="p3all00001"), "all"))
@example(scenario_and_kind=(_EraseScenario(suffix="p3some0001"), "some"))
@example(scenario_and_kind=(_EraseScenario(suffix="p3none0001"), "none"))
@_SETTINGS
def test_p_erase_3_decision_redaction_preserves_judgement(
    scenario_and_kind: tuple[_EraseScenario, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """P-ERASE-3 / INV-ERASE-DECISION-REDACT.

    RED today: ``ImportError`` — ``worldmonitor.resolution.erasure_scrub`` does not exist yet
    (imported LOCALLY inside this function only, so the other P-ERASE-* tests in this file still
    collect and run as genuine assertion-RED against today's code).
    """
    scenario, kind = scenario_and_kind
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        with sessions() as session:
            erased_src, survivor_id, m1, m2 = _seed_decision_corpus(session, scenario, kind)
            before = session.execute(
                select(DecisionRecord).where(DecisionRecord.canonical_id == survivor_id)
            ).scalar_one()
            before_kind = before.kind
            before_score = before.score
            before_decided_by = before.decided_by
            before_evidence = before.evidence

        # ---- GATE IMPORT — does not exist yet (RED for the right reason) ----
        from worldmonitor.resolution.erasure_scrub import scrub_log_lanes

        with sessions() as session:
            result = scrub_log_lanes(session, erased_src)
            session.commit()

        with sessions() as session:
            after = session.execute(
                select(DecisionRecord).where(DecisionRecord.canonical_id == survivor_id)
            ).scalar_one_or_none()

        assert after is not None, (
            f"P-ERASE-3 INV-ERASE-DECISION-REDACT VIOLATED (kind={kind!r}): the decision row "
            "must be PRESERVED (redacted, never deleted)"
        )
        assert after.kind == before_kind
        assert after.score == before_score
        assert after.decided_by == before_decided_by
        assert after.evidence == before_evidence

        if kind == "all":
            expected: set[str] = set()
        elif kind == "some":
            expected = {m2}
        else:
            expected = {m1, m2}
        assert set(after.member_ids) == expected, (
            f"P-ERASE-3 INV-ERASE-DECISION-REDACT VIOLATED (kind={kind!r}): "
            f"member_ids={after.member_ids!r} expected={sorted(expected)!r}"
        )

        if kind == "none":
            assert not result.erased_member_ids, (
                "non-vacuity: an UNRELATED erased source must reach nothing"
            )
        else:
            assert result.erased_member_ids, (
                "non-vacuity: the scrub must actually reach the erased member(s)"
            )

        # Confirmation #3 (ADR 0107 surprise 3): the projector reads `decision` rows ONLY for the
        # watermark, never `member_ids` — so redacting it cannot corrupt reconstruction.
        with sessions() as session:
            project(session, clean_graph, full_rebuild=True, checkpoint_id="p2-erase-3-diff")
        fold_node = _read_node(clean_graph, survivor_id)
        if kind == "all":
            assert fold_node is None, (
                "no statement rows remain to reconstruct once every member is erased"
            )
        else:
            assert fold_node is not None, (
                "P-ERASE-3 confirmation #3 VIOLATED: the survivor must still fold from its "
                "remaining (non-erased) statement rows"
            )
            assert fold_node.get("name") == ["Shared Name"], (
                "P-ERASE-3 confirmation #3 VIOLATED: redacting decision.member_ids must not "
                f"corrupt node reconstruction; got name={fold_node.get('name')!r}"
            )
    finally:
        engine.dispose()


# ===========================================================================
# P-ERASE-4: the live value-prune preserves G1 provenance
# ===========================================================================


@pytest.mark.integration
@given(scenario=_p_erase_scenario())
@example(scenario=_EraseScenario(suffix="p4prov0001"))
@_SETTINGS
def test_p_erase_4_live_value_prune_preserves_provenance(
    scenario: _EraseScenario,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    minio: tuple[str, str, str],
) -> None:
    """P-ERASE-4 / INV-ERASE-PROV-PRESERVED (plan-verify HIGH-1).

    Asserts BOTH halves of the non-vacuity fence: (a) the co-witnessed erased-only value is
    actually REMOVED (an impl that does nothing fails HERE), and (b) G1 provenance
    (``prov_source_id``/``prov_witnesses``/``id``) is NEVER wiped (an impl using a bare
    ``SET n = <partial map>`` of only compared_props/anchors would pass (a) but fail HERE).

    RED today: (a) fails — ``erase_source_graph`` never removes the co-witnessed erased-only
    value (prop-granular witness map, no per-value attribution).
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        landing = _landing(minio)

        with sessions() as session:
            corpus = _seed_corpus(session, clean_graph, scenario)

        from worldmonitor.erasure import erase_source

        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=corpus.erased_src,
                authorized_by="p2-prop-op-4",
            )
            session.commit()

        after = _read_node(clean_graph, corpus.survivor_id)
        assert after is not None, "the multi-source survivor must SURVIVE a partial erase"

        alias = list(after.get("alias") or [])
        assert "OnlyFromErased" not in alias, (
            "P-ERASE-4 (a) VIOLATED: the co-witnessed erased-source-only value survives on the "
            f"live node: alias={alias!r} — the value-prune never ran"
        )
        assert "OnlyFromKept" in alias, "non-vacuity: the KEPT value must survive the prune"

        # (b) G1: the value-prune must never wipe provenance.
        assert after.get("prov_source_id"), (
            "P-ERASE-4 (b) / INV-ERASE-PROV-PRESERVED VIOLATED: prov_source_id is empty/missing "
            "after the live value-prune — G1 provenance was wiped by a bare partial SET"
        )
        assert after.get("id") == corpus.survivor_id, (
            "P-ERASE-4 (b) / INV-ERASE-PROV-PRESERVED VIOLATED: the node's own id was wiped by "
            "the value-prune write"
        )
        witnesses_raw = after.get("prov_witnesses")
        assert witnesses_raw, (
            "P-ERASE-4 (b) / INV-ERASE-PROV-PRESERVED VIOLATED: prov_witnesses was wiped by the "
            "value-prune write"
        )
        parsed = json.loads(str(witnesses_raw))
        assert corpus.erased_src not in json.dumps(parsed), (
            "the erased source must be pruned OUT of the (preserved) witness map"
        )
    finally:
        engine.dispose()


# ===========================================================================
# P-ERASE-5: legacy (never-log-backed) live data survives an UNRELATED erasure
# (the CRITICAL bug — fix design SS1, positive-attribution gate)
# ===========================================================================


@dataclass(frozen=True)
class _LegacyCorpusIds:
    erased_src: str
    keep_src: str
    legacy_src: str
    survivor_id: str
    m1: str
    m2: str
    m3: str
    legacy_prop_value: str
    legacy_anchor_value: str


def _legacy_anchor_value(suffix: str) -> str:
    """A deterministic, per-example-unique ``lei``-shaped value — a DIFFERENT anchor field
    (its own Neo4j UNIQUE constraint, ``graph/constraints.py``) than ``_anchor_value``'s
    ``wikidata_id``-shaped ``Q...`` values, so the two never collide even within one example."""
    return f"LEILEGACY{100000 + (sum(ord(c) for c in suffix) % 800000)}"


def _seed_legacy_scenario(
    session: Session, neo4j: Neo4jClient, scenario: _EraseScenario
) -> _LegacyCorpusIds:
    """Seed the P-ERASE-5 CRITICAL-bug corpus.

    (a) ``m1``/``m2`` mirror the existing SF-4 hard case EXACTLY: an erased-source-only value
    on a STATEMENT-LOGGED prop (``alias``) that MUST still be correctly removed by erasing
    ``erased_src`` — this half is NOT the bug, it must keep passing after the fix too.

    (b) ``m3`` contributes a DIFFERENT prop (``profession``) AND anchor (``lei``) from a source
    (``legacy_src``) that is NEVER erased and has ZERO backing ``StatementRecord``/
    ``ContextClaimRecord`` row at all — no ``_stmt(...)`` call, no ``ContextClaimRecord`` are
    ever added for ``m3`` — written DIRECTLY onto the live node via the SAME production
    merge/write path this file's other corpus-builders use (``_merge_entities`` +
    ``write_entities``), simulating legacy/pre-ADR-0099-dual-write live data (or data from a
    source structurally never statement-logged). Erasing ``erased_src`` must leave (b)
    COMPLETELY UNTOUCHED — today's ``prune_live_to_fold`` treats "the fold has no evidence for
    this prop/anchor" as "erased-source-only" and WIPES it, even though ``legacy_src`` is never
    named in this erasure at all (the CRITICAL data-loss bug this test proves is real).
    """
    sfx = scenario.suffix
    erased_src = f"esrc5:{sfx}"
    keep_src = f"ksrc5:{sfx}"
    legacy_src = f"lsrc5:{sfx}"
    survivor_id = f"surv5-{sfx}"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    m3 = f"{survivor_id}-m3"
    legacy_prop_value = f"LegacyProfession-{sfx}"
    legacy_anchor_value = _legacy_anchor_value(sfx)

    # --- Postgres: ONLY m1/m2's contributions are logged; m3's are NEVER logged (legacy). ---
    session.add(_stmt(survivor_id, m1, "name", "Shared Name Five", erased_src))
    session.add(_stmt(survivor_id, m2, "name", "Shared Name Five", keep_src))
    session.add(_stmt(survivor_id, m1, "alias", "OnlyFromErasedFive", erased_src))
    session.commit()

    # --- Neo4j: the LIVE graph carries m3's legacy contribution too (SAME production
    # merge/write path), with NO corresponding log row anywhere. ---
    ensure_constraints(neo4j)
    by_id = {
        m1: _person(
            m1, erased_src, {"name": ["Shared Name Five"], "alias": ["OnlyFromErasedFive"]}
        ),
        m2: _person(m2, keep_src, {"name": ["Shared Name Five"]}),
        m3: _person(m3, legacy_src, {"profession": [legacy_prop_value]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2, m3), by_id)
    assert dropped == (), f"unexpected schema-incompatible drop: {dropped!r}"
    set_anchor(merged, "lei", legacy_anchor_value)
    write_entities(neo4j, [merged])

    return _LegacyCorpusIds(
        erased_src=erased_src,
        keep_src=keep_src,
        legacy_src=legacy_src,
        survivor_id=survivor_id,
        m1=m1,
        m2=m2,
        m3=m3,
        legacy_prop_value=legacy_prop_value,
        legacy_anchor_value=legacy_anchor_value,
    )


@pytest.mark.integration
@given(scenario=_p_erase_scenario())
@example(scenario=_EraseScenario(suffix="p5legacy01"))
@_SETTINGS
def test_p_erase_5_legacy_unlogged_data_survives_unrelated_erasure(
    scenario: _EraseScenario,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    minio: tuple[str, str, str],
) -> None:
    """P-ERASE-5 — the CRITICAL bug (fix design SS1, the positive-attribution gate).

    Today's ``prune_live_to_fold`` treats "the post-scrub fold has no evidence for this live
    prop/anchor" as "this value was erased-source-only" and wipes it — WRONG whenever a
    survivor's live-graph data isn't 100% covered by the statement/context_claim log (data
    written before the ADR-0099 statement dual-write existed, CLAUDE.md: "the live SoR stays
    Neo4j until the F1 projector cutover"; or data from a source never itself statement-logged
    for a structural reason). Erasing ``erased_src`` must remove ONLY its own statement-logged,
    erased-source-only contribution (``alias``, the existing SF-4 case); the legacy,
    never-logged ``profession``/``lei`` values from ``legacy_src`` — a source NEVER named in
    this erasure — must survive on the live node BYTE-IDENTICAL.

    RED today: ``profession`` and ``lei`` are WIPED by ``prune_live_to_fold`` even though
    ``legacy_src`` was never erased (the fold has zero evidence for either, since neither was
    ever statement/context-claim-logged) — this is the CRITICAL data-loss bug, proven here
    against TODAY's EXISTING ``erase_source`` entry point (no new symbol required).
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        landing = _landing(minio)

        with sessions() as session:
            corpus = _seed_legacy_scenario(session, clean_graph, scenario)

        from worldmonitor.erasure import erase_source

        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=corpus.erased_src,
                authorized_by="p2-prop-op-5",
            )
            session.commit()

        after = _read_node(clean_graph, corpus.survivor_id)
        assert after is not None, "the multi-source survivor must SURVIVE a partial erase"

        # (a) the SF-4 hard case: the erased-source-only, STATEMENT-LOGGED value must still be
        # correctly removed (this half is NOT the bug — it must keep passing after the fix too).
        alias = list(after.get("alias") or [])
        assert "OnlyFromErasedFive" not in alias, (
            "P-ERASE-5 (a) VIOLATED: the erased-source-only statement-logged alias value "
            f"survives on the live node: alias={alias!r}"
        )

        # (b) THE CRITICAL BUG: legacy, NEVER-log-backed data from a source that was NEVER
        # erased must survive COMPLETELY UNTOUCHED.
        assert after.get("profession") == [corpus.legacy_prop_value], (
            "P-ERASE-5 (b) CRITICAL VIOLATED: the legacy (never statement-logged) `profession` "
            f"value from an UN-erased source was wiped: got {after.get('profession')!r}, "
            f"expected [{corpus.legacy_prop_value!r}] — prune_live_to_fold treated 'no fold "
            "evidence' as 'erased-source-only' for a prop the erased source never touched"
        )
        live_anchor = clean_graph.execute_read(
            "MATCH (n:Entity {id: $id}) RETURN n.lei AS lei", id=corpus.survivor_id
        )[0]["lei"]
        assert live_anchor == corpus.legacy_anchor_value, (
            "P-ERASE-5 (b) CRITICAL VIOLATED: the legacy (never context-claim-logged) `lei` "
            f"anchor from an UN-erased source was wiped: got {live_anchor!r}, expected "
            f"{corpus.legacy_anchor_value!r} — the anchor REMOVE-only gate must be POSITIVELY "
            "attributed to the erased source, not inferred from fold absence"
        )
    finally:
        engine.dispose()


# ===========================================================================
# P-ERASE-6: caption recompute must fire even when compared_props ends up empty
# (fix-round NEW-1, HIGH — the sole-witnessed-caption-source bug)
# ===========================================================================


@dataclass(frozen=True)
class _CaptionCorpusIds:
    erased_src: str
    keep_src: str
    survivor_id: str
    m1: str
    m2: str
    name_value: str
    alias_value: str


def _seed_p6_scenario(
    session: Session, neo4j: Neo4jClient, scenario: _EraseScenario
) -> _CaptionCorpusIds:
    """Seed the NEW-1 corpus: ``name`` (the caption-relevant prop) is SOLE-witnessed by
    ``erased_src`` — no other source contributes a ``name`` value for this survivor at all. A
    DIFFERENT, UNRELATED prop (``alias``) is contributed only by ``keep_src`` — ``erased_src``
    never touches it. ``erase_source_graph`` (frozen, runs BEFORE ``prune_live_to_fold``) has
    already popped ``name`` from the live node's props wholesale (its sole witness is gone) by
    the time ``prune_live_to_fold`` reads ``current_props`` — so ``name`` never enters the
    per-prop loop at all, and since ``alias`` was never touched by ``erased_src`` either,
    ``compared_props`` ends up completely EMPTY for this survivor.
    """
    sfx = scenario.suffix
    erased_src = f"esrc6:{sfx}"
    keep_src = f"ksrc6:{sfx}"
    survivor_id = f"surv6-{sfx}"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    name_value = f"SoleCaptionName-{sfx}"
    alias_value = f"UnrelatedKeptAlias-{sfx}"

    session.add(_stmt(survivor_id, m1, "name", name_value, erased_src))
    session.add(_stmt(survivor_id, m2, "alias", alias_value, keep_src))
    session.commit()

    ensure_constraints(neo4j)
    by_id = {
        m1: _person(m1, erased_src, {"name": [name_value]}),
        m2: _person(m2, keep_src, {"alias": [alias_value]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2), by_id)
    assert dropped == (), f"unexpected schema-incompatible drop: {dropped!r}"
    write_entities(neo4j, [merged])
    # Force the PRE-erasure live caption deterministically (mirrors
    # test_it_erase_caption_recomputed_not_stale's convention) — FtM's own pick order already
    # favours `name` over `alias` here, but forcing keeps the precondition explicit/robust.
    neo4j.execute_write(
        "MATCH (n:Entity {id: $id}) SET n.caption = $caption", id=survivor_id, caption=name_value
    )

    return _CaptionCorpusIds(
        erased_src=erased_src,
        keep_src=keep_src,
        survivor_id=survivor_id,
        m1=m1,
        m2=m2,
        name_value=name_value,
        alias_value=alias_value,
    )


@pytest.mark.integration
@given(scenario=_p_erase_scenario())
@example(scenario=_EraseScenario(suffix="p6caption1"))
@_SETTINGS
def test_p_erase_6_sole_witnessed_caption_source_still_recomputes_caption(
    scenario: _EraseScenario,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    minio: tuple[str, str, str],
) -> None:
    """P-ERASE-6 — fix-round NEW-1 (HIGH): the caption recompute must fire off ``fold_entity``
    alone, NEVER gated on ``compared_props`` being non-empty.

    RED today: ``erase_source_graph`` already pops the sole-witnessed ``name`` prop from the
    live node BEFORE ``prune_live_to_fold`` ever runs, so ``name`` never enters the per-prop
    loop at all; ``alias`` was never touched by ``erased_src`` either, so ``compared_props``
    ends up EMPTY. Today's gate (``if compared_props and fold_entity is not None:``) then NEVER
    recomputes the caption — the stale erased-source-only name survives on ``n.caption``
    forever, even though the fold (``alias`` remains) has a perfectly good new pick.
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        landing = _landing(minio)

        with sessions() as session:
            corpus = _seed_p6_scenario(session, clean_graph, scenario)

        pre = _read_node(clean_graph, corpus.survivor_id)
        assert pre is not None and pre.get("caption") == corpus.name_value, (
            "precondition: the live caption must start as the erased-source-only name"
        )

        from worldmonitor.erasure import erase_source

        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=corpus.erased_src,
                authorized_by="p2-prop-op-6",
            )
            session.commit()

        # Independent oracle: the SAME frozen reconstruct_entities prune_live_to_fold itself
        # must use, over the POST-scrub remaining rows — not a second, hand-rolled expectation.
        from worldmonitor.resolution.projector import build_survivor_of, reconstruct_entities

        with sessions() as session:
            survivor_of = build_survivor_of(session)
            remaining_rows = list(session.execute(select(StatementRecord)).scalars())
            fold_entities = {
                e.id: e
                for e in reconstruct_entities(remaining_rows, survivor_of)
                if e.id is not None
            }
        expected_caption = fold_entities[corpus.survivor_id].caption

        after = _read_node(clean_graph, corpus.survivor_id)
        assert after is not None, "the multi-source survivor must SURVIVE a partial erase"
        assert after.get("caption") == expected_caption, (
            f"P-ERASE-6 NEW-1 VIOLATED: n.caption={after.get('caption')!r} does not match the "
            f"post-scrub fold's caption {expected_caption!r} — a caption recompute gated on "
            "compared_props being non-empty skips recomputation whenever the ONLY genuinely-"
            "erased prop was already popped by erase_source_graph before prune_live_to_fold "
            "ever ran"
        )
        assert after.get("caption") != corpus.name_value, (
            "non-vacuity: the caption must actually CHANGE off the sole-witnessed erased name"
        )
    finally:
        engine.dispose()


# ===========================================================================
# P-ERASE-7: a value-level positive-attribution gate for multi-valued props
# (fix-round NEW-2, MEDIUM — the shared-prop legacy-value bug)
# ===========================================================================


@dataclass(frozen=True)
class _SharedPropCorpusIds:
    erased_src: str
    keep_src: str
    legacy_src: str
    survivor_id: str
    m1: str
    m2: str
    m3: str
    erased_alias_value: str
    kept_alias_value: str
    legacy_alias_value: str


def _seed_p7_scenario(
    session: Session, neo4j: Neo4jClient, scenario: _EraseScenario
) -> _SharedPropCorpusIds:
    """Seed the NEW-2 corpus: ONE multi-valued prop (``alias``) with THREE live values —
    ``erased_alias_value`` (statement-logged, dataset==erased_src, genuinely reached),
    ``kept_alias_value`` (statement-logged, dataset==keep_src, survives the scrub), and
    ``legacy_alias_value`` (NEVER statement-logged at all — contributed by a THIRD,
    never-erased source, mirroring pre-dual-write legacy data). ``erase_source_graph``'s
    witness map is PROP-granular (not per-value): since ``alias`` still has surviving witnesses
    (keep_src/legacy_src) after ``erased_src`` is pruned from the witness map, the whole live
    value LIST is left byte-identical by ``erase_source_graph`` — the value-level filter is
    entirely ``prune_live_to_fold``'s job.
    """
    sfx = scenario.suffix
    erased_src = f"esrc7:{sfx}"
    keep_src = f"ksrc7:{sfx}"
    legacy_src = f"lsrc7:{sfx}"
    survivor_id = f"surv7-{sfx}"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    m3 = f"{survivor_id}-m3"
    erased_alias_value = f"ErasedAlias-{sfx}"
    kept_alias_value = f"KeptAlias-{sfx}"
    legacy_alias_value = f"LegacyAlias-{sfx}"
    shared_name = f"SharedNameSeven-{sfx}"

    session.add(_stmt(survivor_id, m1, "name", shared_name, erased_src))
    session.add(_stmt(survivor_id, m2, "name", shared_name, keep_src))
    session.add(_stmt(survivor_id, m1, "alias", erased_alias_value, erased_src))
    session.add(_stmt(survivor_id, m2, "alias", kept_alias_value, keep_src))
    # m3's alias contribution is DELIBERATELY never logged (legacy/pre-dual-write data) — no
    # `_stmt(...)` call, no `ContextClaimRecord`, for m3 anywhere.
    session.commit()

    ensure_constraints(neo4j)
    by_id = {
        m1: _person(m1, erased_src, {"name": [shared_name], "alias": [erased_alias_value]}),
        m2: _person(m2, keep_src, {"name": [shared_name], "alias": [kept_alias_value]}),
        m3: _person(m3, legacy_src, {"alias": [legacy_alias_value]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2, m3), by_id)
    assert dropped == (), f"unexpected schema-incompatible drop: {dropped!r}"
    write_entities(neo4j, [merged])

    return _SharedPropCorpusIds(
        erased_src=erased_src,
        keep_src=keep_src,
        legacy_src=legacy_src,
        survivor_id=survivor_id,
        m1=m1,
        m2=m2,
        m3=m3,
        erased_alias_value=erased_alias_value,
        kept_alias_value=kept_alias_value,
        legacy_alias_value=legacy_alias_value,
    )


@pytest.mark.integration
@given(scenario=_p_erase_scenario())
@example(scenario=_EraseScenario(suffix="p7shared01"))
@_SETTINGS
def test_p_erase_7_legacy_value_survives_when_sharing_a_prop_with_an_erased_value(
    scenario: _EraseScenario,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    minio: tuple[str, str, str],
) -> None:
    """P-ERASE-7 — fix-round NEW-2 (MEDIUM): the positive-attribution gate must be VALUE-level,
    not prop-level, for a multi-valued prop.

    RED today: ``compared_props[prop_name]`` is computed from the fold's RECONSTRUCTED value
    set (``sorted(str(v) for v in fold_entity.get(fold_prop))``), which only ever contains
    values the (remaining) statement log still carries. The legacy, never-logged
    ``legacy_alias_value`` is NOT in the fold's set either — even though ``legacy_src`` was
    never named in this erasure at all — so it is wiped ALONGSIDE the genuinely-erased value.
    Only the kept, STILL-logged value survives.
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        landing = _landing(minio)

        with sessions() as session:
            corpus = _seed_p7_scenario(session, clean_graph, scenario)

        pre = _read_node(clean_graph, corpus.survivor_id)
        assert pre is not None
        pre_alias = set(pre.get("alias") or [])
        assert pre_alias == {
            corpus.erased_alias_value,
            corpus.kept_alias_value,
            corpus.legacy_alias_value,
        }, f"precondition: all three alias values must be live pre-erasure, got {pre_alias!r}"

        from worldmonitor.erasure import erase_source

        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=corpus.erased_src,
                authorized_by="p2-prop-op-7",
            )
            session.commit()

        after = _read_node(clean_graph, corpus.survivor_id)
        assert after is not None, "the multi-source survivor must SURVIVE a partial erase"
        after_alias = set(after.get("alias") or [])

        assert corpus.erased_alias_value not in after_alias, (
            "P-ERASE-7 (a) VIOLATED: the genuinely erased-source-logged value survives: "
            f"alias={after_alias!r}"
        )
        assert corpus.kept_alias_value in after_alias, (
            "non-vacuity: the kept, still-logged value must survive"
        )
        assert corpus.legacy_alias_value in after_alias, (
            "P-ERASE-7 (b) NEW-2 VIOLATED: the legacy (never statement-logged) alias value from "
            f"an UN-erased source was wiped: alias={after_alias!r} — a PROP-level positive-"
            "attribution gate wrongly wipes every value in a shared multi-valued prop whenever "
            "ANY one of its values was genuinely erased, wiping innocent co-located values too"
        )
        assert after_alias == {corpus.kept_alias_value, corpus.legacy_alias_value}, (
            f"P-ERASE-7 VIOLATED: expected EXACTLY the kept + legacy values, got {after_alias!r}"
        )
    finally:
        engine.dispose()


# ===========================================================================
# P-ERASE-8: a value co-witnessed by a SURVIVING source must not be wiped merely because
# the erased source ALSO logged the identical literal value (round-3 narrower bug, checker-
# confirmed against round-2's fix)
# ===========================================================================


@dataclass(frozen=True)
class _SharedValueCorpusIds:
    erased_src: str
    keep_src: str
    survivor_id: str
    m1: str
    m2: str
    shared_alias_value: str
    erased_only_alias_value: str


def _seed_p8_scenario(
    session: Session, neo4j: Neo4jClient, scenario: _EraseScenario
) -> _SharedValueCorpusIds:
    """Seed the round-3 corpus: ONE multi-valued prop (``alias``) where the erased member
    (``m1``) contributes TWO values via TWO separate statement rows — ``shared_alias_value``
    (the SAME literal ALSO independently logged by the surviving member ``m2``, a DIFFERENT
    ``entity_id``/``dataset``) and ``erased_only_alias_value`` (logged ONLY by ``m1``, no other
    row anywhere carries it). Both of ``m1``'s rows land in ``erased_survivor_values`` once
    ``erased_src`` is scrubbed (both are literally erased-attributed) — but ONLY
    ``erased_only_alias_value`` should actually disappear from the live node:
    ``shared_alias_value`` is STILL vouched for by ``m2``'s row, which this scrub never reaches
    at all (``m2``'s dataset is ``keep_src``, its entity_id is never in ``erased_member_ids``).
    """
    sfx = scenario.suffix
    erased_src = f"esrc8:{sfx}"
    keep_src = f"ksrc8:{sfx}"
    survivor_id = f"surv8-{sfx}"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    shared_name = f"SharedNameEight-{sfx}"
    shared_alias_value = f"SharedAliasEight-{sfx}"
    erased_only_alias_value = f"ErasedOnlyAliasEight-{sfx}"

    # --- Postgres: m1 logs BOTH alias values; m2 independently logs the SHARED one too. ---
    session.add(_stmt(survivor_id, m1, "name", shared_name, erased_src))
    session.add(_stmt(survivor_id, m2, "name", shared_name, keep_src))
    session.add(_stmt(survivor_id, m1, "alias", shared_alias_value, erased_src))
    session.add(_stmt(survivor_id, m1, "alias", erased_only_alias_value, erased_src))
    session.add(_stmt(survivor_id, m2, "alias", shared_alias_value, keep_src))
    session.commit()

    # --- Neo4j: the LIVE graph mirroring that log (SAME production merge/write path). ---
    ensure_constraints(neo4j)
    by_id = {
        m1: _person(
            m1,
            erased_src,
            {"name": [shared_name], "alias": [shared_alias_value, erased_only_alias_value]},
        ),
        m2: _person(m2, keep_src, {"name": [shared_name], "alias": [shared_alias_value]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2), by_id)
    assert dropped == (), f"unexpected schema-incompatible drop: {dropped!r}"
    write_entities(neo4j, [merged])

    return _SharedValueCorpusIds(
        erased_src=erased_src,
        keep_src=keep_src,
        survivor_id=survivor_id,
        m1=m1,
        m2=m2,
        shared_alias_value=shared_alias_value,
        erased_only_alias_value=erased_only_alias_value,
    )


@pytest.mark.integration
@given(scenario=_p_erase_scenario())
@example(scenario=_EraseScenario(suffix="p8shared01"))
@_SETTINGS
def test_p_erase_8_identical_value_survives_when_the_erased_source_also_matches_it(
    scenario: _EraseScenario,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    minio: tuple[str, str, str],
) -> None:
    """P-ERASE-8 — round-3 narrower bug (checker-confirmed runtime reproduction against
    round-2's fix).

    Round-2's per-prop value filter removes a live value ``v`` whenever
    ``(survivor, prop, v) in scrub_result.erased_survivor_values`` — i.e. whenever THIS
    scrub's deleted rows carried that exact literal value, with NO regard for whether a
    DIFFERENT, non-erased source ALSO independently logged the identical literal value on the
    same prop. Since Neo4j/FtM store a prop's value-set deduplicated, erasing ``m1`` makes
    ``(survivor, "alias", shared_alias_value)`` appear in ``erased_survivor_values`` (from
    ``m1``'s now-deleted row) even though ``m2``'s row for the SAME literal value was NEVER
    reached by the scrub at all — the correct removal condition must ALSO consult whether the
    post-scrub fold still reconstructs that value for the prop (a surviving source still
    vouches for it) before removing it.

    RED today: BOTH ``shared_alias_value`` AND ``erased_only_alias_value`` are wiped from the
    live node's ``alias`` list — the filter has no fold-consultation at all, so
    ``shared_alias_value`` is removed purely because the erased source's OWN row also happened
    to carry the identical literal, with no regard for ``m2``'s still-present, never-reached row.
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        landing = _landing(minio)

        with sessions() as session:
            corpus = _seed_p8_scenario(session, clean_graph, scenario)

        pre = _read_node(clean_graph, corpus.survivor_id)
        assert pre is not None
        pre_alias = set(pre.get("alias") or [])
        assert pre_alias == {corpus.shared_alias_value, corpus.erased_only_alias_value}, (
            f"precondition: both alias values must be live pre-erasure, got {pre_alias!r}"
        )

        from worldmonitor.erasure import erase_source

        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=corpus.erased_src,
                authorized_by="p2-prop-op-8",
            )
            session.commit()

        after = _read_node(clean_graph, corpus.survivor_id)
        assert after is not None, "the multi-source survivor must SURVIVE a partial erase"
        after_alias = set(after.get("alias") or [])

        assert corpus.shared_alias_value in after_alias, (
            "P-ERASE-8 VIOLATED: a value co-witnessed by a SURVIVING, non-erased source "
            f"(dataset={corpus.keep_src!r}) was wiped from the live node merely because the "
            "erased source's OWN (now-deleted) row ALSO logged the identical literal value: "
            f"alias={after_alias!r} — the removal filter must check fold-presence too, not just "
            "erased-attribution"
        )
        assert corpus.erased_only_alias_value not in after_alias, (
            "P-ERASE-8 non-vacuity VIOLATED: the genuinely erased-source-only value must still "
            f"be removed: alias={after_alias!r}"
        )
        assert after_alias == {corpus.shared_alias_value}, (
            "P-ERASE-8 VIOLATED: expected EXACTLY the surviving-source-vouched value, got "
            f"{after_alias!r}"
        )
    finally:
        engine.dispose()
