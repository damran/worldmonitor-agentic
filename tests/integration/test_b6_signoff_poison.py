"""B-6 slice-2 (sign-off poison-row guard, ADR 0041) — INV-6 + INV-7.

``signoff.approve``/``reject`` scan the tenant's ER queue twice: ``_member_rows`` (filtered to
``status == 'pending_review'``) and ``_outbound_edges`` (ALL tenant rows, NO status filter),
both calling ``make_entity(row.raw_entity)`` unguarded. A single malformed/poison ``raw_entity``
anywhere in the tenant's queue therefore raises ``InvalidData`` mid-scan and aborts the WHOLE
approve/reject — wedging the human-review path for EVERY parked merge of that tenant (the B-2
per-input isolation pattern was never applied to sign-off).

These tests seed a VALID parked sensitive merge AND an UNRELATED poison row in the same tenant,
then assert:

* INV-6 — ``approve()``/``reject()`` still succeed for the valid parked merge: the canonical /
  member nodes and outbound edges are written exactly as for clean inputs, and the
  SignOff/judgement/MergeAudit transition completes.
* INV-7 — the poison row is durably dead-lettered (``IngestDeadLetter`` at stage
  ``'signoff-poison'``, ≤16 chars, replayable) rather than silently swallowed; the clean-path
  ``entities_written``/``edges_written`` and audit transitions are identical to a no-poison run.

Pre-fix the unguarded ``make_entity`` in ``_outbound_edges`` raises on the poison row, so
``approve``/``reject`` raise and never complete. Real sign-off against ephemeral Neo4j +
Postgres (testcontainers); ``integration``-marked.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    ErQueueItem,
    IngestDeadLetter,
    MergeAudit,
    ResolverJudgement,
    SignOff,
)
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import signoff
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration

_POISON_ID = "poison-unrelated"
_POISON_SOURCE = "s3://landing/poison-unrelated.json"


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-21T00:00:00Z",
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


def _poison_row() -> ErQueueItem:
    """An UNRELATED queue row whose raw_entity ``make_entity`` cannot parse (unknown schema).

    Stored verbatim (NOT built through ``make_entity`` at seed time, since it would raise).
    Marked ``pending_review`` so it sits in the tenant queue both scans walk — but its id is
    NOT one of the parked merge's source_ids, so it must never be treated as a member/edge.
    """
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity={
            "id": _POISON_ID,
            "schema": "NotARealSchema",
            "properties": {"name": ["x"]},
            "datasets": ["t"],
        },
        source_record=_POISON_SOURCE,
        status="pending_review",
    )


def _sanctioned(member_id: str, *, flag: bool = True) -> dict[str, object]:
    """A person record; ``flag`` puts a sanction topic on it so the merge trips the guard."""
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


def _company(entity_id: str) -> dict[str, object]:
    return {
        "id": entity_id,
        "schema": "Company",
        "properties": {"name": ["Acme Holdings"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }


def _person_ids(neo4j: Neo4jClient) -> list[str]:
    return [
        row["id"] for row in neo4j.execute_read("MATCH (n:Person) RETURN n.id AS id ORDER BY n.id")
    ]


def _park_sensitive_merge(
    sessions: sessionmaker[Session],
    neo4j: Neo4jClient,
    *,
    with_edge: bool,
) -> str:
    """Seed a sensitive duplicate Person pair (p1 owns acme if ``with_edge``), park it, and
    return the parked canonical id."""
    with sessions() as session:
        session.add(_queue_item(_sanctioned("p1"), source="p1"))
        session.add(_queue_item(_sanctioned("p2", flag=False), source="p2"))
        if with_edge:
            session.add(_queue_item(_company("acme"), source="acme"))
            session.add(_queue_item(_ownership("own-p1", "p1", "acme"), source="own-p1"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=neo4j, guard_mode="block")
    assert stats.review == 1, "the sanctioned pair must park for review"
    with sessions() as session:
        return session.execute(
            select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
        ).scalar_one()


def _poison_dead_letters(sessions: sessionmaker[Session]) -> list[IngestDeadLetter]:
    with sessions() as session:
        return list(
            session.execute(
                select(IngestDeadLetter).where(
                    IngestDeadLetter.stage == "signoff-poison",
                )
            ).scalars()
        )


def _assert_poison_dead_lettered(sessions: sessionmaker[Session]) -> None:
    """INV-7: the poison row is durably recorded at stage 'signoff-poison' (replayable)."""
    dls = _poison_dead_letters(sessions)
    assert dls, "the poison row must be durably dead-lettered, not silently swallowed"
    assert all(len(d.stage) <= 16 for d in dls), "stage must fit String(16)"
    assert any(d.source_record == _POISON_SOURCE for d in dls), (
        "the dead-letter carries the poison row's source_record (replayable)"
    )


def test_approve_succeeds_despite_unrelated_poison_row(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """INV-6/INV-7: approve a valid parked merge (with an outbound edge) past a poison row."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    canonical_id = _park_sensitive_merge(sessions, clean_graph, with_edge=True)
    # Inject the UNRELATED poison row into the same tenant's queue.
    with sessions() as session:
        session.add(_poison_row())
        session.commit()

    # Must NOT raise (pre-fix the unguarded make_entity in _outbound_edges raises on the poison).
    with sessions() as session:
        result = signoff.approve(
            session,
            clean_graph,
            canonical_id=canonical_id,
            approver="bob",
            reason="confirmed same person",
        )

    # INV-6: clean-path behavior identical to a no-poison approve.
    assert result.decision == "approved"
    assert result.entities_written == 1, "the canonical entity is written"
    assert result.edges_written == 1, "the outbound ownership edge is promoted (poison ignored)"
    assert _person_ids(clean_graph) == [canonical_id], "one canonical person"
    owns = clean_graph.execute_read(
        "MATCH (p:Person)-[r:OWNS]->(c:Company {id: 'acme'}) RETURN p.id AS owner",
    )
    assert len(owns) == 1
    assert owns[0]["owner"] == canonical_id, "the outbound edge is promoted onto the canonical"

    with sessions() as session:
        assert session.execute(select(SignOff.decision)).scalar_one() == "approved"
        judgements = list(session.execute(select(ResolverJudgement)).scalars())
        assert [j.judgement for j in judgements] == ["positive"]
        assert (
            session.execute(
                select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_id)
            ).scalar_one()
            == "merged"
        )
        # The poison row is NOT promoted to a node and stays out of the member set.
        assert (
            session.execute(
                "MATCH (n {id: $id}) RETURN count(n) AS n"  # type: ignore[arg-type]
            )
            if False
            else None
        ) is None
    poison_nodes = clean_graph.execute_read(
        "MATCH (n {id: $id}) RETURN count(n) AS n", id=_POISON_ID
    )
    assert poison_nodes[0]["n"] == 0, "the poison row must never become a graph node"

    # INV-7: durably dead-lettered, not silently swallowed.
    _assert_poison_dead_lettered(sessions)
    engine.dispose()


def test_reject_succeeds_despite_unrelated_poison_row(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """INV-6/INV-7: reject a valid parked merge past a poison row; members written separately."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    canonical_id = _park_sensitive_merge(sessions, clean_graph, with_edge=False)
    with sessions() as session:
        session.add(_poison_row())
        session.commit()

    # Must NOT raise (pre-fix the unguarded make_entity in _outbound_edges/_member_rows wedges).
    with sessions() as session:
        result = signoff.reject(
            session,
            clean_graph,
            canonical_id=canonical_id,
            approver="alice",
            reason="distinct individuals",
        )

    # INV-6: clean-path behavior identical to a no-poison reject — members written separately.
    assert result.decision == "rejected"
    assert result.entities_written == 2, "both members are written as their own entities"
    assert _person_ids(clean_graph) == ["p1", "p2"], "members written separately"

    with sessions() as session:
        assert session.execute(select(SignOff.decision)).scalar_one() == "rejected"
        judgements = list(session.execute(select(ResolverJudgement)).scalars())
        assert [j.judgement for j in judgements] == ["negative"]
        assert (
            session.execute(
                select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_id)
            ).scalar_one()
            == "rejected"
        )
        # The poison row was NOT treated as a member: only p1/p2 rows are resolved by the reject.
        resolved = session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.status == "resolved")
        ).scalar_one()
        assert resolved == 2, "exactly the two parked members are resolved (poison untouched)"

    poison_nodes = clean_graph.execute_read(
        "MATCH (n {id: $id}) RETURN count(n) AS n", id=_POISON_ID
    )
    assert poison_nodes[0]["n"] == 0, "the poison row must never become a graph node"

    # INV-7: durably dead-lettered, not silently swallowed.
    _assert_poison_dead_lettered(sessions)
    engine.dispose()
