"""Integration tests: return-to-block sign-off + durable judgements (ADR 0031).

Under ``MERGE_GUARD_MODE="block"`` the guard parks a flagged (sensitive) merge as
``pending_review``. An operator then **approves** it (promote the canonical entity +
its outbound edges) or **rejects** it (write the members as separate entities). Both
persist a DURABLE, TENANT-SCOPED resolver judgement that every later batch's ephemeral
resolver loads — so the decision sticks and the cluster never re-parks. These tests
prove the resolution path actually consumes those judgements (the D1 guardrail), that
the judgement is tenant-scoped (G4), and the full approve/reject flows.

Run against an ephemeral Neo4j + Postgres (testcontainers); ``integration``-marked.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, MergeAudit, ResolverJudgement, SignOff
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import signoff
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


def _queue_item(tenant_id: str, data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-21T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )
    entity = stamp(make_entity(data), provenance)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        connector_id="opensanctions",
        raw_entity=entity.to_dict(),
        source_record=provenance.source_record,
        status="pending",
    )


def _judgement(tenant_id: str, left: str, right: str, verdict: str) -> ResolverJudgement:
    low, high = sorted((left, right))
    return ResolverJudgement(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        left_id=low,
        right_id=high,
        judgement=verdict,
        source="signoff",
    )


def _petrov(member_id: str) -> dict[str, object]:
    """One of two deliberate duplicate companies (non-sensitive; merge by Splink)."""
    return {
        "id": member_id,
        "schema": "Company",
        "properties": {"name": ["Petrov Holdings Ltd"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }


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


def _person_ids(neo4j: Neo4jClient, tenant_id: str) -> list[str]:
    return [
        row["id"]
        for row in neo4j.execute_read(
            "MATCH (n:Person {tenant_id: $t}) RETURN n.id AS id ORDER BY n.id", t=tenant_id
        )
    ]


def test_negative_judgement_is_consumed_and_tenant_scoped(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A persisted NEGATIVE judgement keeps duplicates apart — for THAT tenant only.

    Tenant A has a ``negative`` judgement on (petrov-a, petrov-b): the resolution path
    must load it and refuse the merge Splink would otherwise make. Tenant B has the
    identical records and NO judgement, so they merge — proving A's judgement cannot
    bind B's resolution (G4).
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    tenant_a = "judgement-tenant-a"
    with sessions() as session:
        session.add(_queue_item(tenant_a, _petrov("petrov-a"), source="petrov-a"))
        session.add(_queue_item(tenant_a, _petrov("petrov-b"), source="petrov-b"))
        session.add(_judgement(tenant_a, "petrov-a", "petrov-b", "negative"))
        session.commit()
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_a)

    a_nodes = clean_graph.execute_read(
        "MATCH (n:Company {tenant_id: $t}) RETURN n.id AS id", t=tenant_a
    )
    assert {row["id"] for row in a_nodes} == {"petrov-a", "petrov-b"}, (
        "the negative judgement must be consumed and prevent the merge"
    )

    tenant_b = "judgement-tenant-b"
    with sessions() as session:
        session.add(_queue_item(tenant_b, _petrov("petrov-a"), source="petrov-a"))
        session.add(_queue_item(tenant_b, _petrov("petrov-b"), source="petrov-b"))
        session.commit()
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_b)

    b_nodes = clean_graph.execute_read(
        "MATCH (n:Company {tenant_id: $t}) RETURN count(n) AS n", t=tenant_b
    )
    assert b_nodes[0]["n"] == 1, (
        "tenant B has no judgement, so the duplicates merge — A's judgement must not bind B"
    )
    engine.dispose()


def test_reject_splits_members_and_blocks_remerge(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """Reject a parked merge: members are written separately and never re-merge.

    A sanctioned duplicate pair parks under block mode. ``reject`` writes p1 and p2 as
    their own entities, records a negative judgement + a sign_off row, and flips the
    audit to ``rejected``. A re-ingest of the same pair stays split and never re-parks.
    """
    tenant_id = "reject-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    with sessions() as session:
        session.add(_queue_item(tenant_id, _sanctioned("p1"), source="p1"))
        session.add(_queue_item(tenant_id, _sanctioned("p2", flag=False), source="p2"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, guard_mode="block"
        )
    assert stats.review == 1
    assert _person_ids(clean_graph, tenant_id) == [], "parked merge writes nothing"

    with sessions() as session:
        canonical_id = session.execute(
            select(MergeAudit.canonical_id).where(
                MergeAudit.tenant_id == tenant_id, MergeAudit.decision == "pending_review"
            )
        ).scalar_one()

    with sessions() as session:
        result = signoff.reject(
            session,
            clean_graph,
            tenant_id=tenant_id,
            canonical_id=canonical_id,
            approver="alice",
            reason="distinct individuals",
        )
    assert result.decision == "rejected"
    assert result.entities_written == 2
    assert _person_ids(clean_graph, tenant_id) == ["p1", "p2"], "members written separately"

    with sessions() as session:
        signoffs = list(
            session.execute(select(SignOff).where(SignOff.tenant_id == tenant_id)).scalars()
        )
        assert len(signoffs) == 1
        assert signoffs[0].decision == "rejected"
        assert signoffs[0].approver == "alice"
        judgements = list(
            session.execute(
                select(ResolverJudgement).where(ResolverJudgement.tenant_id == tenant_id)
            ).scalars()
        )
        assert [j.judgement for j in judgements] == ["negative"]
        audit_decision = session.execute(
            select(MergeAudit.decision).where(
                MergeAudit.tenant_id == tenant_id, MergeAudit.canonical_id == canonical_id
            )
        ).scalar_one()
        assert audit_decision == "rejected"
        parked = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending_review")
        ).scalar_one()
        assert parked == 0, "the parked rows are resolved by the sign-off"

    # Re-ingest the same pair: the negative judgement keeps them split and never re-parks.
    with sessions() as session:
        session.add(_queue_item(tenant_id, _sanctioned("p1"), source="p1-reingest"))
        session.add(_queue_item(tenant_id, _sanctioned("p2", flag=False), source="p2-reingest"))
        session.commit()
    with sessions() as session:
        stats2 = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, guard_mode="block"
        )
    assert stats2.review == 0, "a rejected pair must never re-park"
    assert _person_ids(clean_graph, tenant_id) == ["p1", "p2"], "still two separate persons"
    engine.dispose()


def test_approve_promotes_with_outbound_edge_and_does_not_repark(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """Approve a parked merge: the canonical entity + its outbound edge are promoted.

    A sanctioned duplicate pair (p1 owns acme) parks under block mode — acme is written
    but the ownership edge is dropped (its endpoint is parked). ``approve`` promotes the
    canonical person, rewrites the ownership edge onto it, records a positive judgement +
    a sign_off row, and flips the audit to ``merged``. A re-ingest re-merges them and is
    NOT re-parked (a fresh canonical id is minted — deferred cross-batch dedup, Gate B —
    but the operator is never asked to review it again).
    """
    tenant_id = "approve-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    acme = {
        "id": "acme",
        "schema": "Company",
        "properties": {"name": ["Acme Holdings"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    with sessions() as session:
        session.add(_queue_item(tenant_id, _sanctioned("p1"), source="p1"))
        session.add(_queue_item(tenant_id, _sanctioned("p2", flag=False), source="p2"))
        session.add(_queue_item(tenant_id, acme, source="acme"))
        session.add(_queue_item(tenant_id, _ownership("own-p1", "p1", "acme"), source="own-p1"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, guard_mode="block"
        )
    assert stats.review == 1
    assert _person_ids(clean_graph, tenant_id) == []
    dropped = clean_graph.execute_read(
        "MATCH (:Company {tenant_id: $t, id: 'acme'})<-[r:OWNS]-() RETURN count(r) AS n",
        t=tenant_id,
    )
    assert dropped[0]["n"] == 0, "the edge to the parked member is dropped while parked"

    with sessions() as session:
        canonical_id = session.execute(
            select(MergeAudit.canonical_id).where(
                MergeAudit.tenant_id == tenant_id, MergeAudit.decision == "pending_review"
            )
        ).scalar_one()

    with sessions() as session:
        result = signoff.approve(
            session,
            clean_graph,
            tenant_id=tenant_id,
            canonical_id=canonical_id,
            approver="bob",
            reason="confirmed same person",
        )
    assert result.decision == "approved"
    assert result.edges_written == 1
    assert _person_ids(clean_graph, tenant_id) == [canonical_id], "one canonical person, not p1/p2"
    owns = clean_graph.execute_read(
        "MATCH (p:Person {tenant_id: $t})-[r:OWNS]->(c:Company {tenant_id: $t, id: 'acme'}) "
        "RETURN p.id AS owner",
        t=tenant_id,
    )
    assert len(owns) == 1
    assert owns[0]["owner"] == canonical_id, "the outbound edge is promoted onto the canonical"

    with sessions() as session:
        judgements = list(
            session.execute(
                select(ResolverJudgement).where(ResolverJudgement.tenant_id == tenant_id)
            ).scalars()
        )
        assert [j.judgement for j in judgements] == ["positive"]
        assert (
            session.execute(
                select(SignOff.decision).where(SignOff.tenant_id == tenant_id)
            ).scalar_one()
            == "approved"
        )
        assert (
            session.execute(
                select(MergeAudit.decision).where(
                    MergeAudit.tenant_id == tenant_id, MergeAudit.canonical_id == canonical_id
                )
            ).scalar_one()
            == "merged"
        )

    # Re-ingest the same pair: the positive judgement re-merges them and the guard does
    # NOT re-park (no operator re-review), even though a fresh canonical id is minted.
    with sessions() as session:
        session.add(_queue_item(tenant_id, _sanctioned("p1"), source="p1-reingest"))
        session.add(_queue_item(tenant_id, _sanctioned("p2", flag=False), source="p2-reingest"))
        session.commit()
    with sessions() as session:
        stats2 = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, guard_mode="block"
        )
    assert stats2.review == 0, "an approved pair must never re-park"
    assert stats2.promoted == 1, "the approved merge is promoted, not parked"
    leftover = clean_graph.execute_read(
        "MATCH (n:Person {tenant_id: $t}) WHERE n.id IN ['p1', 'p2'] RETURN count(n) AS n",
        t=tenant_id,
    )
    assert leftover[0]["n"] == 0, "p1/p2 always merge — they never survive as their own nodes"
    engine.dispose()
