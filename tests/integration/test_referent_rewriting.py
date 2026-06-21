"""Integration tests: referent rewriting (G2) — no dangling edges after a merge.

When ER collapses source entities into a canonical, any edge that referenced a
merged-away source id must be rewritten to the canonical id before the graph
write. ftmg materialises an edge with ``MATCH (endpoint {id: ...})`` — so if the
referenced id was merged away and never written, the MATCH fails and the **whole
edge is silently dropped**. Referent rewriting is what keeps those edges alive
and pointing at the surviving canonical node ("resolve to canonical IDs" applied
to edges; closes ADR 0023 item 1 / audit gap G2).

These run the real ER pipeline (Splink score -> nomenklatura merge -> guard ->
write) against an ephemeral Neo4j + Postgres (testcontainers). Marked
``integration`` so they are excluded from the default quality run and gated by
the dedicated CI job.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, MergeAlert, MergeAudit
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.queries import get_neighbors
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


def _queue_item(tenant_id: str, data: dict[str, object], *, source: str) -> ErQueueItem:
    """An ER-queue row whose mapped entity carries provenance (so edges get prov_*)."""
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


def _petrov(member_id: str) -> dict[str, object]:
    """One of two deliberate duplicate companies (merge into one canonical node)."""
    return {
        "id": member_id,
        "schema": "Company",
        "properties": {"name": ["Petrov Holdings Ltd"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }


def _ownership(edge_id: str, owner: str, asset: str) -> dict[str, object]:
    return {
        "id": edge_id,
        "schema": "Ownership",
        "properties": {"owner": [owner], "asset": [asset]},
        "datasets": ["t"],
    }


def test_edge_to_merged_member_is_rewritten_to_canonical(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """An Ownership edge that referenced a merged-away company id lands on the canonical node.

    ``ivan`` owns ``petrov-a`` and ``petrov-b``, which are duplicates that merge to
    a single minted canonical id (neither member id). Both ownership edges
    reference merged-away ids; after rewriting they must both connect ``ivan`` to
    the one canonical company — with their own (edge) provenance intact (G1).
    """
    tenant_id = "g2-core-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    ivan = {
        "id": "ivan",
        "schema": "Person",
        "properties": {"name": ["Ivan Owner"], "nationality": ["ru"]},
        "datasets": ["t"],
    }
    rows = [
        (ivan, "ivan"),
        (_petrov("petrov-a"), "petrov-a"),
        (_petrov("petrov-b"), "petrov-b"),
        (_ownership("own-a", "ivan", "petrov-a"), "own-a"),
        (_ownership("own-b", "ivan", "petrov-b"), "own-b"),
    ]
    with sessions() as session:
        for data, source in rows:
            session.add(_queue_item(tenant_id, data, source=source))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_id)

    # {petrov-a, petrov-b} merged; ivan, own-a, own-b stay singletons -> 4 clusters.
    assert stats.clusters == 4
    assert stats.review == 0
    assert stats.alerts == 0

    # Exactly one canonical company node; the merged-away member ids have no node.
    companies = clean_graph.execute_read(
        "MATCH (n:Company {tenant_id: $t}) RETURN n.id AS id", t=tenant_id
    )
    assert len(companies) == 1, "the duplicate companies must collapse to one node"
    canonical_id = companies[0]["id"]
    assert canonical_id not in {"petrov-a", "petrov-b"}, "canonical is a minted id, not a member"
    for member in ("petrov-a", "petrov-b"):
        orphan = clean_graph.execute_read(
            "MATCH (n {tenant_id: $t, id: $id}) RETURN count(n) AS n", t=tenant_id, id=member
        )[0]["n"]
        assert orphan == 0, f"merged-away id {member} must not survive as a node"

    # Both ownership edges were rewritten: they connect ivan -> the canonical node.
    # (Without rewriting, the MATCH on petrov-a/petrov-b would fail and BOTH edges
    # would be dropped, leaving ivan with zero OWNS edges.)
    owns = clean_graph.execute_read(
        "MATCH (o:Entity {tenant_id: $t, id: 'ivan'})-[r:OWNS]->(m:Entity {tenant_id: $t}) "
        "RETURN m.id AS target, r.prov_source_id AS source_id",
        t=tenant_id,
    )
    assert len(owns) == 2, "both ownership edges must survive the merge (rewritten, not dropped)"
    assert {row["target"] for row in owns} == {canonical_id}
    # Edge provenance preserved through the rewrite (G1): every edge keeps its source.
    assert all(row["source_id"] == "opensanctions:test" for row in owns)

    # Neighbour traversal — the headline read the API exposes — now finds the merge.
    neighbours = {
        n["id"] for n in get_neighbors(clean_graph, tenant_id=tenant_id, entity_id="ivan")
    }
    assert neighbours == {canonical_id}

    engine.dispose()


def test_alert_mode_rewrites_referents_of_a_flagged_merge(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A sanctioned (guard-flagged) merge in alert mode is written AND its edges rewrite.

    The two sanctioned duplicates merge into a flagged cluster; under alert mode
    (ADR 0024) it is promoted, so it joins the referent map and the ownership edge
    that named ``p1`` is rewritten onto the canonical person node.
    """
    tenant_id = "g2-alert-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    p1 = {
        "id": "p1",
        "schema": "Person",
        "properties": {
            "name": ["Vladimir Example"],
            "nationality": ["ru"],
            "birthDate": ["1960-01-01"],
            "topics": ["sanction"],
        },
        "datasets": ["t"],
    }
    p2 = {
        "id": "p2",
        "schema": "Person",
        "properties": {
            "name": ["Vladimir Example"],
            "nationality": ["ru"],
            "birthDate": ["1960-01-01"],
        },
        "datasets": ["t"],
    }
    acme = {
        "id": "acme",
        "schema": "Company",
        "properties": {"name": ["Acme Holdings"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    rows = [(p1, "p1"), (p2, "p2"), (acme, "acme"), (_ownership("own-p1", "p1", "acme"), "own-p1")]
    with sessions() as session:
        for data, source in rows:
            session.add(_queue_item(tenant_id, data, source=source))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, guard_mode="alert"
        )

    assert stats.alerts == 1
    assert stats.review == 0

    # The flagged sanctioned merge was written as one canonical person node.
    persons = clean_graph.execute_read(
        "MATCH (n:Person {tenant_id: $t}) RETURN n.id AS id", t=tenant_id
    )
    assert len(persons) == 1
    canonical_person = persons[0]["id"]
    assert canonical_person not in {"p1", "p2"}

    # The ownership edge that named the merged-away p1 now connects the canonical
    # person to acme (without rewriting, the MATCH on p1 fails and the edge drops).
    owns = clean_graph.execute_read(
        "MATCH (p:Person {tenant_id: $t})-[r:OWNS]->(c:Company {tenant_id: $t}) "
        "RETURN p.id AS owner, c.id AS asset, r.prov_source_id AS source_id",
        t=tenant_id,
    )
    assert len(owns) == 1
    assert owns[0]["owner"] == canonical_person
    assert owns[0]["asset"] == "acme"
    assert owns[0]["source_id"] == "opensanctions:test"

    # The durable alert trail exists for the flagged-but-merged cluster.
    with sessions() as session:
        alert = session.execute(
            select(MergeAlert).where(MergeAlert.tenant_id == tenant_id)
        ).scalar_one()
        assert sorted(alert.source_ids) == ["p1", "p2"]

    engine.dispose()


def test_block_mode_parks_flagged_cluster_and_does_not_materialise_its_edge(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """Block mode parks the sanctioned merge; its members never enter the referent map.

    Because the {p1,p2} cluster is parked (never written, never promoted), it
    contributes nothing to rewriting: the ownership edge that named ``p1`` is left
    untouched, p1 has no node, and the edge is therefore not materialised — the
    sensitive entity is not resurrected through a back-door rewrite.
    """
    tenant_id = "g2-block-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    p1 = {
        "id": "p1",
        "schema": "Person",
        "properties": {
            "name": ["Vladimir Example"],
            "nationality": ["ru"],
            "birthDate": ["1960-01-01"],
            "topics": ["sanction"],
        },
        "datasets": ["t"],
    }
    p2 = {
        "id": "p2",
        "schema": "Person",
        "properties": {
            "name": ["Vladimir Example"],
            "nationality": ["ru"],
            "birthDate": ["1960-01-01"],
        },
        "datasets": ["t"],
    }
    acme = {
        "id": "acme",
        "schema": "Company",
        "properties": {"name": ["Acme Holdings"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    rows = [(p1, "p1"), (p2, "p2"), (acme, "acme"), (_ownership("own-p1", "p1", "acme"), "own-p1")]
    with sessions() as session:
        for data, source in rows:
            session.add(_queue_item(tenant_id, data, source=source))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, guard_mode="block"
        )

    assert stats.review == 1
    assert stats.alerts == 0

    # The sanctioned cluster was parked: no person node, and the audit shows review.
    persons = clean_graph.execute_read(
        "MATCH (n:Person {tenant_id: $t}) RETURN count(n) AS n", t=tenant_id
    )
    assert persons[0]["n"] == 0
    with sessions() as session:
        review = session.execute(
            select(MergeAudit).where(
                MergeAudit.tenant_id == tenant_id, MergeAudit.decision == "pending_review"
            )
        ).scalar_one()
        assert sorted(review.source_ids) == ["p1", "p2"]

    # The edge to the parked member is not materialised (its endpoint has no node)
    # and was NOT rewritten to any canonical — acme has no ownership relationship.
    owns = clean_graph.execute_read(
        "MATCH (:Company {tenant_id: $t, id: 'acme'})<-[r:OWNS]-() RETURN count(r) AS n",
        t=tenant_id,
    )
    assert owns[0]["n"] == 0, "a parked cluster must not be reachable via a rewritten edge"

    engine.dispose()


def test_rerun_is_idempotent_noop(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    """Re-running resolve_pending after everything is resolved changes nothing."""
    tenant_id = "g2-idempotent-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    rows = [
        (
            {
                "id": "ivan",
                "schema": "Person",
                "properties": {"name": ["Ivan Owner"]},
                "datasets": ["t"],
            },
            "ivan",
        ),
        (_petrov("petrov-a"), "petrov-a"),
        (_petrov("petrov-b"), "petrov-b"),
        (_ownership("own-a", "ivan", "petrov-a"), "own-a"),
    ]
    with sessions() as session:
        for data, source in rows:
            session.add(_queue_item(tenant_id, data, source=source))
        session.commit()

    with sessions() as session:
        first = resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_id)
    assert first.promoted == 3  # ivan, canonical petrov, own-a

    def _counts() -> tuple[int, int]:
        nodes = clean_graph.execute_read(
            "MATCH (n:Entity {tenant_id: $t}) RETURN count(n) AS n", t=tenant_id
        )[0]["n"]
        edges = clean_graph.execute_read(
            "MATCH ({tenant_id: $t})-[r]->() WHERE r.tenant_id = $t RETURN count(r) AS n",
            t=tenant_id,
        )[0]["n"]
        return nodes, edges

    before = _counts()

    with sessions() as session:
        second = resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_id)

    # Nothing pending the second time: a pure no-op, graph unchanged.
    assert second.pending == 0
    assert second.clusters == 0
    assert second.promoted == 0
    assert _counts() == before

    # And the queue carries no leftover pending rows.
    with sessions() as session:
        pending = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending")
        ).scalar_one()
        assert pending == 0

    engine.dispose()
