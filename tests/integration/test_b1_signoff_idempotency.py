"""B-1 Part 2 — sign-off is idempotent and crash-recoverable (ADR 0036).

Sign-off writes Neo4j before it commits Postgres, so a crash in that window leaves the
graph written while the audit row stays ``pending_review`` (Postgres rolled back). These
tests SIMULATE THAT CRASH WINDOW on the sign-off path (the audit's lesson: happy-path
fixtures hid every prior bug): they let ``write_entities`` commit the graph, fail the next
Postgres ``session.commit()`` (a crash-hook raise), assert the half-committed state is
**surfaced** (``list_parked(..., neo4j=...)`` flags ``graph_written``), then re-run the SAME
``approve``/``reject`` and assert it **converges** to one consistent outcome — no duplicate
canonical node, no orphan, no duplicate judgement/sign-off row — and that a further re-run
from the completed state is a clean no-op.

Real ER pipeline against ephemeral Neo4j + Postgres (testcontainers); ``integration``-gated.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, MergeAudit, ResolverJudgement, SignOff
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import signoff
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-23T00:00:00Z",
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


def _sanctioned(member_id: str, *, topics: list[str] | None = None) -> dict[str, object]:
    """A sanctioned person; two of these are duplicates that PARK under block mode."""
    props: dict[str, list[str]] = {
        "name": ["Vladimir Example"],
        "nationality": ["ru"],
        "birthDate": ["1960-01-01"],
    }
    if topics:
        props["topics"] = topics
    return {"id": member_id, "schema": "Person", "properties": props, "datasets": ["t"]}


def _ownership(edge_id: str, owner: str, asset: str) -> dict[str, object]:
    return {
        "id": edge_id,
        "schema": "Ownership",
        "properties": {"owner": [owner], "asset": [asset]},
        "datasets": ["t"],
    }


def _seed_parked(
    sessions: sessionmaker[Session], clean_graph: Neo4jClient, suffix: str = ""
) -> str:
    """Seed two sanctioned duplicates + an edge, resolve in block mode, return the parked id.

    ``suffix`` makes the seeded entities (and so the content-addressed canonical id)
    distinct, so a single test can stage two INDEPENDENT parked clusters. Pre-teardown
    that independence came from two ``tenant_id``s keying two distinct nodes; under
    single-tenancy (D1, ADR 0042) identical content is ONE node, so distinct content is
    what keeps the clusters apart. The new parked id is found by set-difference against
    the pre-existing ``pending_review`` audits (``MergeAudit`` is append-only).
    """
    p1, p2, acme_id, own = f"p1{suffix}", f"p2{suffix}", f"acme{suffix}", f"own-p1{suffix}"
    acme: dict[str, object] = {
        "id": acme_id,
        "schema": "Company",
        "properties": {"name": [f"Acme Holdings{suffix}"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    rows = [
        (_sanctioned(p1, topics=["sanction"]), p1),
        (_sanctioned(p2), p2),
        (acme, acme_id),
        (_ownership(own, p1, acme_id), own),
    ]
    with sessions() as session:
        before = set(
            session.execute(
                select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
            ).scalars()
        )
    with sessions() as session:
        for data, source in rows:
            session.add(_queue_item(data, source=source))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1, "the sanctioned duplicate pair must park under block mode"
    with sessions() as session:
        parked = set(
            session.execute(
                select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
            ).scalars()
        )
    new = parked - before
    assert len(new) == 1, "exactly one new cluster parks per seed"
    canonical_id = next(iter(new))
    # A parked cluster is never written during resolution.
    present = clean_graph.execute_read(
        "MATCH (n) WHERE n.id IN $ids RETURN count(n) AS n",
        ids=[canonical_id, p1, p2],
    )[0]["n"]
    assert present == 0, "block mode must not write the parked cluster"
    return canonical_id


def _crash_first_commit(session: Session) -> dict[str, int]:
    """Patch ``session.commit`` to raise on its FIRST call (the post-graph-write commit)."""
    calls = {"n": 0}
    real_commit = session.commit

    def crashing_commit() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("WM_CRASH_AFTER: graph write committed, before postgres commit")
        real_commit()

    session.commit = crashing_commit  # type: ignore[method-assign]
    return calls


def _counts(sessions: sessionmaker[Session]) -> tuple[int, int, int]:
    """(pending_review queue rows, resolver_judgement rows, sign_off rows)."""
    with sessions() as session:
        parked_rows = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.status == "pending_review")
        ).scalar_one()
        judgements = session.execute(
            select(func.count()).select_from(ResolverJudgement)
        ).scalar_one()
        signoffs = session.execute(select(func.count()).select_from(SignOff)).scalar_one()
    return parked_rows, judgements, signoffs


def test_approve_crash_window_is_surfaced_and_recovers_idempotently(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)
    canonical_id = _seed_parked(sessions, clean_graph)

    # --- CRASH: approve writes the graph, then the Postgres commit dies. ---
    with sessions() as session:
        _crash_first_commit(session)
        with pytest.raises(RuntimeError, match="WM_CRASH_AFTER"):
            signoff.approve(
                session,
                clean_graph,
                canonical_id=canonical_id,
                approver="alice",
            )

    # Surfaced: the canonical node is in the graph but the audit is still pending_review.
    persons = clean_graph.execute_read("MATCH (n:Person) RETURN n.id AS id")
    assert [p["id"] for p in persons] == [canonical_id], "approve committed the canonical node"
    with sessions() as session:
        parked = signoff.list_parked(session, clean_graph)
    assert len(parked) == 1 and parked[0].graph_written is True, (
        "the half-committed sign-off must be surfaced as graph_written, not silently stuck"
    )
    # Postgres rolled back entirely: still parked, no judgement, no sign-off row.
    assert _counts(sessions) == (2, 0, 0)

    # --- RECOVER: re-run the SAME approve. It must converge, not duplicate. ---
    with sessions() as session:
        result = signoff.approve(session, clean_graph, canonical_id=canonical_id, approver="alice")
    assert result.already_applied is False

    persons = clean_graph.execute_read("MATCH (n:Person) RETURN n.id AS id")
    assert [p["id"] for p in persons] == [canonical_id], "still ONE canonical node, not a duplicate"
    parked_rows, judgements, signoffs = _counts(sessions)
    assert parked_rows == 0, "members resolved"
    assert judgements == 1, "exactly one positive judgement for the pair (no duplicate)"
    assert signoffs == 1, "exactly one sign-off row (no duplicate audit row)"
    with sessions() as session:
        decision = session.execute(
            select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_id)
        ).scalar_one()
    assert decision == "merged"
    owns = clean_graph.execute_read(
        "MATCH (p:Person)-[r:OWNS]->(:Company) RETURN p.id AS owner",
    )
    assert {o["owner"] for o in owns} == {canonical_id}, "edge rewritten onto the canonical person"

    # --- IDEMPOTENT no-op: a further approve from the completed state changes nothing. ---
    with sessions() as session:
        again = signoff.approve(session, clean_graph, canonical_id=canonical_id, approver="alice")
    assert again.already_applied is True
    assert _counts(sessions) == (0, 1, 1), "no new judgement or sign-off row on re-run"
    engine.dispose()


def test_reject_crash_window_is_surfaced_and_recovers_idempotently(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)
    canonical_id = _seed_parked(sessions, clean_graph)

    # --- CRASH: reject writes the member nodes, then the Postgres commit dies. ---
    with sessions() as session:
        _crash_first_commit(session)
        with pytest.raises(RuntimeError, match="WM_CRASH_AFTER"):
            signoff.reject(
                session,
                clean_graph,
                canonical_id=canonical_id,
                approver="alice",
            )

    # Surfaced: the member nodes are in the graph (NOT the canonical), audit still pending.
    person_ids = {p["id"] for p in clean_graph.execute_read("MATCH (n:Person) RETURN n.id AS id")}
    assert person_ids == {"p1", "p2"}, "reject committed the members under their own ids"
    assert canonical_id not in person_ids, "reject never writes a canonical node"
    with sessions() as session:
        parked = signoff.list_parked(session, clean_graph)
    assert len(parked) == 1 and parked[0].graph_written is True, "crashed reject must be surfaced"
    assert _counts(sessions) == (2, 0, 0), "Postgres rolled back: still parked"

    # --- RECOVER: re-run the SAME reject. Members keep their ids → idempotent re-write. ---
    with sessions() as session:
        result = signoff.reject(session, clean_graph, canonical_id=canonical_id, approver="alice")
    assert result.already_applied is False

    person_ids = {p["id"] for p in clean_graph.execute_read("MATCH (n:Person) RETURN n.id AS id")}
    assert person_ids == {"p1", "p2"}, "still the two separate members, no canonical, no duplicate"
    parked_rows, judgements, signoffs = _counts(sessions)
    assert parked_rows == 0
    assert judgements == 1, "exactly one negative judgement for the pair"
    assert signoffs == 1, "exactly one sign-off row"
    with sessions() as session:
        decision = session.execute(
            select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_id)
        ).scalar_one()
        judgement = session.execute(select(ResolverJudgement.judgement)).scalar_one()
    assert decision == "rejected"
    assert judgement == "negative", "a rejected pair persists a negative judgement"

    # --- IDEMPOTENT no-op from the completed state. ---
    with sessions() as session:
        again = signoff.reject(session, clean_graph, canonical_id=canonical_id, approver="alice")
    assert again.already_applied is True
    assert _counts(sessions) == (0, 1, 1)
    engine.dispose()


def test_cross_op_after_crash_is_refused_no_orphan(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """Switching operation mid-recovery is refused (would orphan the other op's nodes).

    A parked cluster is never written during resolution, so a node already in the graph is
    the signature of a crashed sign-off. Completing the OPPOSITE operation would strand that
    node (append-only — no delete), so each guard refuses and names the op to re-run.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    # Crashed APPROVE (canonical node written) → reject must refuse, not orphan the canonical.
    canonical_1 = _seed_parked(sessions, clean_graph, suffix="-a")
    with sessions() as session:
        _crash_first_commit(session)
        with pytest.raises(RuntimeError, match="WM_CRASH_AFTER"):
            signoff.approve(session, clean_graph, canonical_id=canonical_1, approver="alice")
    with sessions() as session, pytest.raises(signoff.SignOffError, match="re-run approve"):
        signoff.reject(session, clean_graph, canonical_id=canonical_1, approver="bob")

    # Crashed REJECT (member nodes written) → approve must refuse, not orphan the members.
    canonical_2 = _seed_parked(sessions, clean_graph, suffix="-b")
    with sessions() as session:
        _crash_first_commit(session)
        with pytest.raises(RuntimeError, match="WM_CRASH_AFTER"):
            signoff.reject(session, clean_graph, canonical_id=canonical_2, approver="alice")
    with sessions() as session, pytest.raises(signoff.SignOffError, match="re-run reject"):
        signoff.approve(session, clean_graph, canonical_id=canonical_2, approver="bob")

    engine.dispose()
