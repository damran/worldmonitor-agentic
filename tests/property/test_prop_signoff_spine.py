"""Property/metamorphic tests for Gate P3 — the sign-off spine durability lane (ADR 0108).

Four mandatory ``@given`` invariants (spec §3 / ADR 0108 §Decided SF-5), each real-Postgres +
real-Neo4j (the live graph, ``clean_graph``) + an ISOLATED second Neo4j fold target
(``diff_neo4j_client``, module-scoped, defined in THIS file — mirrors
``tests/integration/test_projection_diff.py``'s pattern; that file is out of scope for this
gate, so the container fixture is duplicated here, not imported):

P-SIGN-1  Approved-merge fold round-trip (Slice P3-a). After a real park (via
          ``resolve_pending`` block-mode) + ``signoff.approve()`` + a full-rebuild fold into
          the isolated target: ``measure_divergence(...).total == 0`` for the survivor AND its
          statement-bearing outbound edges; the fold node's bare anchors equal the live node's
          (provenance-stamped members); exactly one ``decision`` row (``kind="merge"``,
          ``decided_by == f"operator:{approver}"``, member-id set matches); the
          ``canonical_id_ledger`` self-row + member aliases resolve every member onto the
          survivor. Generator (``_p_sign_1_scenario``) draws over four kinds — **anchored**,
          **unanchored** (``wmc-`` durable id / ``wmc-`` self-row), **edge-bearing** (an
          ``Ownership`` whose owner is a reviewed member), and **parked-singleton**
          (``is_merge=False``: statements fold the survivor at its own id, ZERO decision rows,
          ZERO ledger aliases) — with ``@example`` pins guaranteeing every kind actually runs at
          least once regardless of Hypothesis's random exploration (the non-vacuity fence: an
          arm-selecting generator over a small discrete kind-space is not, by itself, a
          *guarantee* of coverage — house precedent: ``test_prop_llm_role_and_caller.py``'s
          ``@example`` fallback pin).

P-SIGN-2  Reject round-trip to member nodes (Slice P3-b). After ``signoff.reject()``: each
          member folds to its OWN node (with its own statements) and each statement-bearing
          outbound edge folds correctly (endpoints stay member ids, matching the unrewritten
          live edge) — ``.total == 0``; NO ``decision`` row; NO ``canonical_id_ledger`` alias
          for the members. Generator over the three merge kinds (anchored / unanchored / edge —
          a reject has no "is_merge=False" special case worth pinning separately from P-SIGN-1's
          singleton arm).

P-SIGN-3  Co-commit atomicity, RED-first WITH the positive control folded in (both slices). A
          **successful** commit writes the spine rows (statement + decision(if merge) + ledger
          (if merge) + ``sign_off``) with a ``session.commit`` call-count spy == 1 (proves no
          second commit was added); a **forced** commit failure (a monkeypatched ``.commit()``
          that raises) leaves a FRESH session with ZERO new spine rows and
          ``merge_audit.decision == "pending_review"``. The positive control is the
          non-vacuity fence: without it this test would be trivially green against a
          zero-spine-row master (nothing is written either way).

P-SIGN-4  B-1 idempotent re-run convergence (both slices). Running approve()/reject() TWICE —
          the second call either after a rolled-back first attempt (crash-before-commit) or
          after a fully-committed first attempt (the ``already_applied`` no-op) — converges:
          the fold explains the survivor identically to a single clean run on the same
          structural content (name/nationality/birthDate — the literal ids differ by
          construction, so the comparison is on values, not ids), and the survivor's
          decision-row count is exactly 1 (0 for reject) in BOTH the crash-first and
          commit-first arms. ``@example`` pins guarantee all four ``(kind, crash_first)``
          combinations run at least once.

All four are RED at collection time in the sense that follows from CURRENT master
(pre-Gate-P3): ``signoff.approve()``/``signoff.reject()`` write NO statement/decision/ledger
rows at all — every ``measure_divergence(...).total == 0`` assertion below currently fails
(``.total > 0``, the survivor/members are unexplained by a fold that has nothing to reconstruct
them from), and P-SIGN-1's/P-SIGN-3's decision-row assertions fail outright (zero rows where
one is expected). This is assertion-adjacent RED (not an ImportError), because Gate P3 adds no
new symbol — only additive ``session.add`` calls inside two already-importable functions.

Container-heavy examples wrap their per-example engine in ``try/finally: engine.dispose()``
(memory: given-red-tests-leak-connections). ``@pytest.mark.integration`` sits OUTERMOST, above
``@given``/``@example`` (house convention, ``test_prop_context_claim_capture.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    Base,
    CanonicalIdLedger,
    DecisionRecord,
    ErQueueItem,
    MergeAudit,
    SignOff,
    StatementRecord,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.snapshot import read_graph_snapshot
from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS, set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import signoff
from worldmonitor.resolution.divergence import ProjectionDivergence, measure_divergence
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import build_survivor_of, project

_SETTINGS = settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.data_too_large,
    ],
)

_ALNUM = "abcdefghij0123456789"
_RETRIEVED_AT = "2026-07-11T00:00:00Z"
_SRC = "src:p3-signoff-spine-test"
_COMPUTED_AT = datetime(2026, 7, 11, tzinfo=UTC)

# A SECOND, independent Neo4j image/password — deliberately distinct literals from
# conftest.py's NEO4J_TEST_PASSWORD (never the same instance as the `clean_graph` fixture's).
# Mirrors tests/integration/test_projection_diff.py's `diff_neo4j_client` pattern, duplicated
# here (that file is out of scope for this gate; per-file self-containment is the house norm,
# e.g. test_projection_diff.py's own docstring on why it doesn't extend conftest.py).
_DIFF_NEO4J_IMAGE = "neo4j:2026.05.0-community"
_DIFF_NEO4J_PW = "testpw-p3-prop-diff"  # pragma: allowlist secret


@pytest.fixture(scope="module")
def diff_neo4j_client() -> Iterator[Neo4jClient]:
    """A SECOND, isolated Neo4j container — the fold target for measure_divergence.

    Wiped manually inside each hypothesis example (via ``_fold_divergence``, not a
    function-scoped fixture) — the same per-example-reset idiom P-CTX-6/7 use for
    ``clean_graph``, since a container-backed fixture is instantiated ONCE per test function,
    not once per Hypothesis example.
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
# Scenario construction — a parked (pending_review) cluster of N sanctioned Person members
# (identical name/nationality/birthDate so Splink reliably clusters them, one member carries
# `topics: ["sanction"]` so the guard flags the cluster regardless of size — the exact
# `_sanctioned` shape from tests/integration/test_signoff.py, duplicated per-file convention),
# optionally anchored (a single shared wikidata_id across every member) and optionally carrying
# an outbound Ownership edge whose owner is the first (reviewed) member.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParkedScenario:
    kind: str  # "anchored" | "unanchored" | "edge" | "singleton"
    suffix: str
    n_members: int


_ALL_KINDS = ("anchored", "unanchored", "edge", "singleton")
_MERGE_KINDS = ("anchored", "unanchored", "edge")


@st.composite
def _p_sign_1_scenario(draw: st.DrawFn) -> _ParkedScenario:
    kind = draw(st.sampled_from(_ALL_KINDS))
    suffix = draw(st.text(alphabet=_ALNUM, min_size=4, max_size=8))
    n_members = 1 if kind == "singleton" else draw(st.integers(min_value=2, max_value=3))
    return _ParkedScenario(kind=kind, suffix=suffix, n_members=n_members)


@st.composite
def _p_sign_2_scenario(draw: st.DrawFn) -> _ParkedScenario:
    kind = draw(st.sampled_from(_MERGE_KINDS))
    suffix = draw(st.text(alphabet=_ALNUM, min_size=4, max_size=8))
    n_members = draw(st.integers(min_value=2, max_value=3))
    return _ParkedScenario(kind=kind, suffix=suffix, n_members=n_members)


def _stamp_person(entity_id: str, *, sanction: bool, anchor: str | None) -> FtmEntity:
    props: dict[str, list[str]] = {
        "name": ["Vladimir Example"],
        "nationality": ["ru"],
        "birthDate": ["1960-01-01"],
    }
    if sanction:
        props["topics"] = ["sanction"]
    entity = make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": ["t"]}
    )
    stamp(
        entity,
        Provenance(
            source_id=_SRC,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{entity_id}.json",
        ),
    )
    if anchor is not None:
        set_anchor(entity, "wikidata_id", anchor)
    return entity


def _stamp_company(entity_id: str, name: str) -> FtmEntity:
    entity = make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [name], "jurisdiction": ["cy"]},
            "datasets": ["t"],
        }
    )
    stamp(
        entity,
        Provenance(
            source_id=_SRC,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{entity_id}.json",
        ),
    )
    return entity


def _stamp_ownership(edge_id: str, owner: str, asset: str) -> FtmEntity:
    entity = make_entity(
        {
            "id": edge_id,
            "schema": "Ownership",
            "properties": {"owner": [owner], "asset": [asset]},
            "datasets": ["t"],
        }
    )
    stamp(
        entity,
        Provenance(
            source_id=_SRC,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{edge_id}.json",
        ),
    )
    return entity


def _queue_item(entity: FtmEntity) -> ErQueueItem:
    assert entity.id is not None
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="p3-signoff-spine-test",
        entity_id=entity.id,
        raw_entity=entity.to_dict(),
        source_record=f"s3://landing/{entity.id}.json",
        status="pending",
    )


def _anchor_value(suffix: str) -> str:
    """A deterministic, valid-shaped QID derived from ``suffix`` (no hash-randomization)."""
    return f"Q{100000 + (sum(ord(c) for c in suffix) % 800000)}"


def _build_scenario(
    scenario: _ParkedScenario,
) -> tuple[list[ErQueueItem], list[str], str | None]:
    """Return ``(queue_items, member_ids, asset_id_or_None)`` for ``scenario``."""
    prefix = f"p3sign-{scenario.kind}-{scenario.suffix}"
    member_ids = [f"{prefix}-m{i}" for i in range(scenario.n_members)]
    anchor = _anchor_value(scenario.suffix) if scenario.kind == "anchored" else None
    items = [
        _queue_item(_stamp_person(mid, sanction=(i == 0), anchor=anchor))
        for i, mid in enumerate(member_ids)
    ]
    asset_id: str | None = None
    if scenario.kind == "edge":
        asset_id = f"{prefix}-asset"
        items.append(_queue_item(_stamp_company(asset_id, f"Acme {scenario.suffix}")))
        items.append(_queue_item(_stamp_ownership(f"{prefix}-own", member_ids[0], asset_id)))
    return items, member_ids, asset_id


def _seed_and_park(
    sessions: sessionmaker[Session], live: Neo4jClient, scenario: _ParkedScenario
) -> tuple[str, list[str], str | None]:
    """Seed ``scenario``'s queue rows, resolve (block mode), and return the parked triple."""
    items, member_ids, asset_id = _build_scenario(scenario)
    with sessions() as session:
        for item in items:
            session.add(item)
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=live, guard_mode="block")
    assert stats.review == 1, (
        f"P-SIGN scenario kind={scenario.kind!r} suffix={scenario.suffix!r}: expected exactly "
        f"1 parked cluster, got review={stats.review} promoted={stats.promoted}"
    )
    with sessions() as session:
        parked = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        canonical_id = parked.canonical_id
        assert set(parked.source_ids) == set(member_ids), (
            f"parked source_ids {sorted(parked.source_ids)} != expected member_ids "
            f"{sorted(member_ids)}"
        )
    return canonical_id, member_ids, asset_id


def _fold_divergence(
    sessions: sessionmaker[Session], live: Neo4jClient, diff: Neo4jClient, *, now: datetime
) -> ProjectionDivergence:
    """Fold the WHOLE log into ``diff`` (wiped first) and measure divergence against ``live``."""
    diff.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        project(session, diff, full_rebuild=True, checkpoint_id="p3-sign-prop-diff")
    live_snapshot = read_graph_snapshot(live)
    fold_snapshot = read_graph_snapshot(diff)
    with sessions() as session:
        survivor_of = build_survivor_of(session)
    return measure_divergence(live_snapshot, fold_snapshot, survivor_of, computed_at=now)


def _cleanup_postgres(postgres_dsn: str) -> None:
    """Truncate ALL relational tables between hypothesis examples (P-FOLD-2 idiom)."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    engine.dispose()


def _read_props(client: Neo4jClient, node_id: str) -> dict[str, object]:
    rows = client.execute_read("MATCH (n {id: $id}) RETURN properties(n) AS props", id=node_id)
    return dict(rows[0]["props"]) if rows else {}


def _crash_first_commit(session: Session) -> None:
    """Patch ``session.commit`` to raise on its FIRST call only (B-1 crash-window simulation).

    Duplicated from ``tests/integration/test_b1_signoff_idempotency.py``'s helper of the same
    shape (per-file self-containment convention — this file must not import a sibling test
    module).
    """
    calls = {"n": 0}
    real_commit = session.commit

    def crashing_commit() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("P3-CRASH: graph write committed, before postgres commit")
        real_commit()

    session.commit = crashing_commit  # type: ignore[method-assign]


# ===========================================================================
# P-SIGN-1: approved-merge fold round-trip (Slice P3-a)
# ===========================================================================


@pytest.mark.integration
@given(scenario=_p_sign_1_scenario())
@example(scenario=_ParkedScenario(kind="anchored", suffix="pin1anch", n_members=2))
@example(scenario=_ParkedScenario(kind="unanchored", suffix="pin2unan", n_members=2))
@example(scenario=_ParkedScenario(kind="edge", suffix="pin3edge0", n_members=2))
@example(scenario=_ParkedScenario(kind="singleton", suffix="pin4sing", n_members=1))
@_SETTINGS
def test_p_sign_1_approved_merge_fold_round_trip(
    scenario: _ParkedScenario,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    diff_neo4j_client: Neo4jClient,
) -> None:
    """P-SIGN-1 / INV-SIGN-APPROVE-SPINE + INV-SIGN-DECIDED-BY + INV-SIGN-LEDGER +
    INV-SIGN-FOLD-EXPLAINED.

    RED today: approve() writes NO statement/decision/ledger rows — the merge-kind decision
    assertion fails outright (0 rows, not 1) and ``measure_divergence(...).total > 0`` for every
    kind (the survivor is unexplained by an empty fold).
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        canonical_id, member_ids, asset_id = _seed_and_park(sessions, clean_graph, scenario)
        approver = f"p3-op-{scenario.suffix}"
        is_merge = scenario.n_members > 1

        with sessions() as session:
            result = signoff.approve(
                session,
                clean_graph,
                canonical_id=canonical_id,
                approver=approver,
                reason="p-sign-1",
            )
        assert result.decision == "approved"

        with sessions() as session:
            decisions = list(
                session.execute(
                    select(DecisionRecord).where(DecisionRecord.canonical_id == canonical_id)
                ).scalars()
            )
            ledger_self = session.execute(
                select(CanonicalIdLedger).where(
                    CanonicalIdLedger.canonical_id == canonical_id,
                    CanonicalIdLedger.canonical_alias == canonical_id,
                )
            ).scalar_one_or_none()
            survivor_of = build_survivor_of(session)

        if is_merge:
            assert len(decisions) == 1, (
                f"P-SIGN-1 INV-SIGN-APPROVE-SPINE VIOLATED (kind={scenario.kind!r}): expected "
                f"exactly 1 decision row for a promoted merge, got {len(decisions)}"
            )
            decision = decisions[0]
            assert decision.kind == "merge"
            assert decision.decided_by == f"operator:{approver}", (
                f"P-SIGN-1 INV-SIGN-DECIDED-BY VIOLATED: decided_by={decision.decided_by!r} != "
                f"'operator:{approver}'"
            )
            assert set(decision.member_ids) == set(member_ids)
            assert ledger_self is not None, (
                "P-SIGN-1 INV-SIGN-LEDGER VIOLATED: no ledger self-row for a promoted merge "
                f"(canonical_id={canonical_id!r})"
            )
            for member_id in member_ids:
                assert survivor_of(member_id) == canonical_id, (
                    f"P-SIGN-1 INV-SIGN-LEDGER VIOLATED: survivor_of({member_id!r}) != "
                    f"{canonical_id!r}"
                )
        else:
            assert decisions == [], (
                f"P-SIGN-1 parked-singleton VIOLATED: expected ZERO decision rows, got "
                f"{len(decisions)}"
            )
            assert ledger_self is None, (
                "P-SIGN-1 parked-singleton VIOLATED: expected ZERO ledger rows (no self-row "
                "either — is_merge=False skips record_durable_id)"
            )

        if scenario.kind == "edge":
            assert asset_id is not None
            live_edge = clean_graph.execute_read(
                "MATCH (:Person {id: $cid})-[r:OWNS]->(:Company {id: $aid}) RETURN count(r) AS n",
                cid=canonical_id,
                aid=asset_id,
            )
            assert live_edge[0]["n"] == 1, (
                "P-SIGN-1 edge-bearing precondition: the live rewritten OWNS edge must exist "
                "after approve()"
            )

        divergence = _fold_divergence(sessions, clean_graph, diff_neo4j_client, now=_COMPUTED_AT)
        assert divergence.total == 0, (
            f"P-SIGN-1 INV-SIGN-FOLD-EXPLAINED VIOLATED (kind={scenario.kind!r}, "
            f"n_members={scenario.n_members}): total={divergence.total} "
            f"(unexplained_nodes={divergence.unexplained_nodes}, "
            f"unexplained_edges={divergence.unexplained_edges})"
        )

        live_props = _read_props(clean_graph, canonical_id)
        fold_props = _read_props(diff_neo4j_client, canonical_id)
        live_anchors = {f: live_props[f] for f in CANONICAL_ID_FIELDS if f in live_props}
        fold_anchors = {f: fold_props[f] for f in CANONICAL_ID_FIELDS if f in fold_props}
        assert fold_anchors == live_anchors, (
            f"P-SIGN-1 anchor-parity VIOLATED (kind={scenario.kind!r}): fold={fold_anchors!r} "
            f"!= live={live_anchors!r}"
        )
        if scenario.kind == "anchored":
            assert fold_anchors.get("wikidata_id"), (
                "P-SIGN-1 NON-VACUITY: the anchored example must carry a wikidata_id bare anchor"
            )
    finally:
        engine.dispose()


# ===========================================================================
# P-SIGN-2: reject round-trip to member nodes (Slice P3-b)
# ===========================================================================


@pytest.mark.integration
@given(scenario=_p_sign_2_scenario())
@example(scenario=_ParkedScenario(kind="anchored", suffix="pin5anch", n_members=2))
@example(scenario=_ParkedScenario(kind="unanchored", suffix="pin6unan", n_members=2))
@example(scenario=_ParkedScenario(kind="edge", suffix="pin7edge0", n_members=2))
@_SETTINGS
def test_p_sign_2_reject_round_trip_to_member_nodes(
    scenario: _ParkedScenario,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    diff_neo4j_client: Neo4jClient,
) -> None:
    """P-SIGN-2 / INV-SIGN-REJECT-SPINE.

    RED today: reject() writes NO statement rows for the members —
    ``measure_divergence(...).total > 0`` (every member is unexplained).
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        canonical_id, member_ids, asset_id = _seed_and_park(sessions, clean_graph, scenario)
        approver = f"p3-op-{scenario.suffix}"

        with sessions() as session:
            result = signoff.reject(
                session,
                clean_graph,
                canonical_id=canonical_id,
                approver=approver,
                reason="p-sign-2",
            )
        assert result.decision == "rejected"

        with sessions() as session:
            decisions = list(
                session.execute(
                    select(DecisionRecord).where(
                        DecisionRecord.canonical_id.in_([canonical_id, *member_ids])
                    )
                ).scalars()
            )
            assert decisions == [], (
                f"P-SIGN-2 INV-SIGN-REJECT-SPINE VIOLATED: expected ZERO decision rows, got "
                f"{len(decisions)}"
            )
            aliases = list(
                session.execute(
                    select(CanonicalIdLedger).where(
                        CanonicalIdLedger.canonical_alias.in_(member_ids)
                    )
                ).scalars()
            )
            assert aliases == [], (
                f"P-SIGN-2 INV-SIGN-LEDGER VIOLATED: reject() must write ZERO ledger alias "
                f"rows for the members, got {len(aliases)}"
            )
            survivor_of = build_survivor_of(session)
            for member_id in member_ids:
                assert survivor_of(member_id) == member_id, (
                    f"P-SIGN-2 VIOLATED: survivor_of({member_id!r}) != {member_id!r} — members "
                    "are their own survivors after reject()"
                )

        if scenario.kind == "edge":
            assert asset_id is not None
            live_edge = clean_graph.execute_read(
                "MATCH (:Person {id: $owner})-[r:OWNS]->(:Company {id: $aid}) RETURN count(r) AS n",
                owner=member_ids[0],
                aid=asset_id,
            )
            assert live_edge[0]["n"] == 1, (
                "P-SIGN-2 edge-bearing precondition: the unrewritten live OWNS edge (endpoint "
                "= the member's own id) must exist after reject()"
            )

        divergence = _fold_divergence(sessions, clean_graph, diff_neo4j_client, now=_COMPUTED_AT)
        assert divergence.total == 0, (
            f"P-SIGN-2 INV-SIGN-FOLD-EXPLAINED VIOLATED (kind={scenario.kind!r}): "
            f"total={divergence.total} (unexplained_nodes={divergence.unexplained_nodes}, "
            f"unexplained_edges={divergence.unexplained_edges})"
        )
    finally:
        engine.dispose()


# ===========================================================================
# P-SIGN-3: co-commit atomicity, RED-first with the positive control folded in
# ===========================================================================


@pytest.mark.integration
@given(
    kind=st.sampled_from(("approve", "reject")),
    suffix=st.text(alphabet=_ALNUM, min_size=4, max_size=8),
)
@example(kind="approve", suffix="atmcapp1")
@example(kind="reject", suffix="atmcrej1")
@_SETTINGS
def test_p_sign_3_co_commit_atomicity(
    kind: str, suffix: str, postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """P-SIGN-3 / INV-SIGN-ATOMIC.

    RED today (positive control): the success-path spine-row assertions fail outright (no rows
    are ever written by approve()/reject() today) — the non-vacuity fence proving this test is
    not trivially green against a zero-spine-row master.
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        op = getattr(signoff, kind)
        approver = f"p3-op-{suffix}"

        # --- POSITIVE CONTROL: a SUCCESSFUL commit writes every spine row, commit-count == 1 ---
        canonical_ok, members_ok, _ = _seed_and_park(
            sessions,
            clean_graph,
            _ParkedScenario(kind="unanchored", suffix=f"{suffix}ok", n_members=2),
        )
        with sessions() as session:
            real_commit = session.commit
            spy = MagicMock(side_effect=real_commit)
            session.commit = spy  # type: ignore[method-assign]
            result = op(
                session,
                clean_graph,
                canonical_id=canonical_ok,
                approver=approver,
                reason="p-sign-3 success",
            )
        assert spy.call_count == 1, (
            f"P-SIGN-3 INV-SIGN-ATOMIC VIOLATED: expected exactly 1 session.commit() call on "
            f"the success path, got {spy.call_count} (a two-commit impl for the spine writes "
            "is forbidden)"
        )
        assert result.decision in ("approved", "rejected")

        stmt_ids_ok = [canonical_ok] if kind == "approve" else members_ok
        with sessions() as session:
            audit_decision = session.execute(
                select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_ok)
            ).scalar_one()
            assert audit_decision == ("merged" if kind == "approve" else "rejected")

            signoffs = list(
                session.execute(
                    select(SignOff).where(SignOff.canonical_id == canonical_ok)
                ).scalars()
            )
            assert len(signoffs) == 1, "positive control: exactly one sign_off row"

            stmt_rows = list(
                session.execute(
                    select(StatementRecord).where(StatementRecord.canonical_id.in_(stmt_ids_ok))
                ).scalars()
            )
            assert stmt_rows, (
                "P-SIGN-3 NON-VACUITY VIOLATED: the SUCCESS path must write >= 1 statement "
                f"row for {stmt_ids_ok!r} — got 0 (this is the positive-control fence: without "
                "it, an unmodified master trivially satisfies the forced-failure branch below)"
            )

            if kind == "approve":
                decisions = list(
                    session.execute(
                        select(DecisionRecord).where(DecisionRecord.canonical_id == canonical_ok)
                    ).scalars()
                )
                assert len(decisions) == 1, (
                    "positive control: approve must write exactly 1 decision row, got "
                    f"{len(decisions)}"
                )
                ledger_self = session.execute(
                    select(CanonicalIdLedger).where(
                        CanonicalIdLedger.canonical_id == canonical_ok,
                        CanonicalIdLedger.canonical_alias == canonical_ok,
                    )
                ).scalar_one_or_none()
                assert ledger_self is not None, (
                    "positive control: approve must write the ledger self-row"
                )

        # --- NEGATIVE CONTROL: a FORCED commit failure rolls back EVERY new spine row ---
        canonical_fail, members_fail, _ = _seed_and_park(
            sessions,
            clean_graph,
            _ParkedScenario(kind="unanchored", suffix=f"{suffix}fl", n_members=2),
        )
        with sessions() as session:

            def _raise_commit() -> None:
                raise RuntimeError("P3-FORCED-COMMIT-FAILURE")

            session.commit = _raise_commit  # type: ignore[method-assign]
            with pytest.raises(RuntimeError, match="P3-FORCED-COMMIT-FAILURE"):
                op(
                    session,
                    clean_graph,
                    canonical_id=canonical_fail,
                    approver=approver,
                    reason="p-sign-3 forced failure",
                )

        stmt_ids_fail = [canonical_fail] if kind == "approve" else members_fail
        with sessions() as session:
            audit_decision = session.execute(
                select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_fail)
            ).scalar_one()
            assert audit_decision == "pending_review", (
                "P-SIGN-3 INV-SIGN-ATOMIC VIOLATED: a forced commit failure must leave "
                f"merge_audit.decision == 'pending_review', got {audit_decision!r}"
            )
            stmt_count = len(
                list(
                    session.execute(
                        select(StatementRecord).where(
                            StatementRecord.canonical_id.in_(stmt_ids_fail)
                        )
                    ).scalars()
                )
            )
            assert stmt_count == 0, (
                f"P-SIGN-3 INV-SIGN-ATOMIC VIOLATED: forced commit failure must roll back "
                f"statement rows, got {stmt_count}"
            )
            decision_count = len(
                list(
                    session.execute(
                        select(DecisionRecord).where(DecisionRecord.canonical_id == canonical_fail)
                    ).scalars()
                )
            )
            assert decision_count == 0
            ledger_count = len(
                list(
                    session.execute(
                        select(CanonicalIdLedger).where(
                            CanonicalIdLedger.canonical_id == canonical_fail
                        )
                    ).scalars()
                )
            )
            assert ledger_count == 0
            signoff_count = len(
                list(
                    session.execute(
                        select(SignOff).where(SignOff.canonical_id == canonical_fail)
                    ).scalars()
                )
            )
            assert signoff_count == 0, (
                f"P-SIGN-3 INV-SIGN-ATOMIC VIOLATED: forced commit failure must roll back the "
                f"sign_off row too, got {signoff_count}"
            )
    finally:
        engine.dispose()


# ===========================================================================
# P-SIGN-4: B-1 idempotent re-run convergence
# ===========================================================================

_STRUCTURAL_KEYS = ("name", "nationality", "birthDate")


def _assert_structural_equivalence(
    baseline: dict[str, object], rerun: dict[str, object], *, label: str
) -> None:
    for key in _STRUCTURAL_KEYS:
        assert baseline.get(key) == rerun.get(key), (
            f"P-SIGN-4 fold-signature equivalence VIOLATED on {key!r} ({label}): "
            f"baseline={baseline.get(key)!r} != re-run={rerun.get(key)!r}"
        )


@pytest.mark.integration
@given(
    kind=st.sampled_from(("approve", "reject")),
    crash_first=st.booleans(),
    suffix=st.text(alphabet=_ALNUM, min_size=4, max_size=8),
)
@example(kind="approve", crash_first=True, suffix="idmpaacf1")
@example(kind="approve", crash_first=False, suffix="idmpaacm1")
@example(kind="reject", crash_first=True, suffix="idmprjcf1")
@example(kind="reject", crash_first=False, suffix="idmprjcm1")
@_SETTINGS
def test_p_sign_4_idempotent_rerun_convergence(
    kind: str,
    crash_first: bool,
    suffix: str,
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    diff_neo4j_client: Neo4jClient,
) -> None:
    """P-SIGN-4 / INV-SIGN-IDEMPOTENT.

    RED today: neither the baseline single run nor the re-run writes any decision/statement
    rows, so the fold never explains the survivor (``.total > 0`` in both arms) and the
    decision-row-count assertions fail outright (0 where 1 is expected for approve).
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    diff_neo4j_client.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        op = getattr(signoff, kind)
        approver = f"p3-op-{suffix}"
        expected_decisions = 1 if kind == "approve" else 0

        # --- BASELINE: an independent, single clean run (same structural content) ---
        base_canonical, base_members, _ = _seed_and_park(
            sessions,
            clean_graph,
            _ParkedScenario(kind="unanchored", suffix=f"{suffix}bs", n_members=2),
        )
        with sessions() as session:
            op(
                session,
                clean_graph,
                canonical_id=base_canonical,
                approver=approver,
                reason="p-sign-4 baseline",
            )
        with sessions() as session:
            base_decision_count = len(
                list(
                    session.execute(
                        select(DecisionRecord).where(DecisionRecord.canonical_id == base_canonical)
                    ).scalars()
                )
            )
        assert base_decision_count == expected_decisions, (
            f"P-SIGN-4 baseline precondition VIOLATED: expected {expected_decisions} decision "
            f"row(s), got {base_decision_count}"
        )
        base_divergence = _fold_divergence(
            sessions, clean_graph, diff_neo4j_client, now=_COMPUTED_AT
        )
        assert base_divergence.total == 0, (
            f"P-SIGN-4 baseline precondition VIOLATED: single clean run must fold-explain, "
            f"got total={base_divergence.total}"
        )
        if kind == "approve":
            base_props = {"canonical": _read_props(clean_graph, base_canonical)}
        else:
            base_props = {mid: _read_props(clean_graph, mid) for mid in base_members}

        # --- TEST RUN: two calls — crash-first (rolled back) or commit-first (already_applied) ---
        test_canonical, test_members, _ = _seed_and_park(
            sessions,
            clean_graph,
            _ParkedScenario(kind="unanchored", suffix=f"{suffix}tr", n_members=2),
        )
        if crash_first:
            with sessions() as session:
                _crash_first_commit(session)
                with pytest.raises(RuntimeError, match="P3-CRASH"):
                    op(
                        session,
                        clean_graph,
                        canonical_id=test_canonical,
                        approver=approver,
                        reason="p-sign-4 crash",
                    )
            with sessions() as session:
                result = op(
                    session,
                    clean_graph,
                    canonical_id=test_canonical,
                    approver=approver,
                    reason="p-sign-4 recover",
                )
            assert result.already_applied is False
        else:
            with sessions() as session:
                result = op(
                    session,
                    clean_graph,
                    canonical_id=test_canonical,
                    approver=approver,
                    reason="p-sign-4 first",
                )
            assert result.already_applied is False
            with sessions() as session:
                again = op(
                    session,
                    clean_graph,
                    canonical_id=test_canonical,
                    approver=approver,
                    reason="p-sign-4 second",
                )
            assert again.already_applied is True, (
                "P-SIGN-4 INV-SIGN-IDEMPOTENT VIOLATED: a commit-first re-run must hit "
                "already_applied, not raise or re-apply"
            )

        with sessions() as session:
            test_decision_count = len(
                list(
                    session.execute(
                        select(DecisionRecord).where(DecisionRecord.canonical_id == test_canonical)
                    ).scalars()
                )
            )
        label = "crash-first" if crash_first else "commit-first"
        assert test_decision_count == expected_decisions, (
            f"P-SIGN-4 INV-SIGN-IDEMPOTENT VIOLATED: expected exactly {expected_decisions} "
            f"decision row(s) for the survivor after a {label} re-run, got "
            f"{test_decision_count} (a crash-first re-run must not double-write; a "
            "commit-first re-run must not duplicate either)"
        )

        test_divergence = _fold_divergence(
            sessions, clean_graph, diff_neo4j_client, now=_COMPUTED_AT
        )
        assert test_divergence.total == 0, (
            f"P-SIGN-4 CONVERGENCE VIOLATED: the {label} re-run fold does not converge "
            f"(.total={test_divergence.total})"
        )

        if kind == "approve":
            test_props = {"canonical": _read_props(clean_graph, test_canonical)}
        else:
            test_props = {mid: _read_props(clean_graph, mid) for mid in test_members}

        # Structural (content) equivalence — literal ids differ by construction (independent
        # scenarios), so compare property VALUES, not node identity.
        if kind == "approve":
            _assert_structural_equivalence(
                base_props["canonical"], test_props["canonical"], label=label
            )
        else:
            base_names = sorted(str(v.get("name")) for v in base_props.values())
            test_names = sorted(str(v.get("name")) for v in test_props.values())
            assert base_names == test_names, (
                f"P-SIGN-4 fold-signature equivalence VIOLATED ({label}): baseline member "
                f"names={base_names!r} != re-run member names={test_names!r}"
            )
    finally:
        engine.dispose()
