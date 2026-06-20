"""Integration tests: constraints get created and the writer stamps tenant_id.

Runs against an ephemeral Neo4j (testcontainers). Marked ``integration`` so it is
excluded from the default quality run and gated by the dedicated CI job.
"""

from __future__ import annotations

import pytest

from worldmonitor.graph.constraints import CANONICAL_ID_PROPERTIES, ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import make_entity

pytestmark = pytest.mark.integration


def test_constraints_created(neo4j_client: Neo4jClient) -> None:
    ensure_constraints(neo4j_client)
    rows = neo4j_client.execute_read("SHOW CONSTRAINTS YIELD name RETURN collect(name) AS names")
    names = rows[0]["names"]
    for prop in CANONICAL_ID_PROPERTIES:
        assert f"entity_tenant_{prop}" in names


def test_writer_stamps_tenant_id_on_nodes_and_edges(
    clean_graph: Neo4jClient, tenant_id: str
) -> None:
    ensure_constraints(clean_graph)
    person = make_entity(
        {"id": "p-1", "schema": "Person", "properties": {"name": ["Alice"]}, "datasets": ["t"]}
    )
    company = make_entity(
        {"id": "c-1", "schema": "Company", "properties": {"name": ["ACME"]}, "datasets": ["t"]}
    )
    ownership = make_entity(
        {
            "id": "o-1",
            "schema": "Ownership",
            "properties": {"owner": ["p-1"], "asset": ["c-1"]},
            "datasets": ["t"],
        }
    )

    write_entities(clean_graph, [person, company, ownership], tenant_id=tenant_id)

    node_rows = clean_graph.execute_read(
        "MATCH (n:Entity) RETURN n.id AS id, n.tenant_id AS tenant ORDER BY id"
    )
    assert {row["id"] for row in node_rows} >= {"c-1", "p-1"}
    assert node_rows, "expected entity nodes to be written"
    assert all(row["tenant"] == tenant_id for row in node_rows)

    edge_rows = clean_graph.execute_read("MATCH ()-[r]->() RETURN r.tenant_id AS tenant")
    assert edge_rows, "expected the ownership relationship to be written"
    assert all(row["tenant"] == tenant_id for row in edge_rows)


def test_writer_requires_tenant(clean_graph: Neo4jClient) -> None:
    from worldmonitor.graph.writer import WriterError

    person = make_entity(
        {"id": "p-9", "schema": "Person", "properties": {"name": ["Bob"]}, "datasets": ["t"]}
    )
    with pytest.raises(WriterError):
        write_entities(clean_graph, [person], tenant_id="")
