"""Integration test for GDS degree centrality (needs the GDS plugin)."""

from __future__ import annotations

import pytest

from worldmonitor.graph.gds import degree_centrality
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import make_entity

pytestmark = pytest.mark.integration


def test_degree_centrality_flags_central_sanctioned_node(
    neo4j_gds_client: Neo4jClient,
) -> None:
    client = neo4j_gds_client
    client.execute_write("MATCH (n) DETACH DELETE n")

    # A sanctioned hub owning three companies => highest degree centrality.
    hub = make_entity(
        {
            "id": "hub",
            "schema": "Company",
            "properties": {"name": ["Hub Holdings"], "topics": ["sanction"]},
            "datasets": ["t"],
        }
    )
    subsidiaries = [
        make_entity(
            {
                "id": f"sub{i}",
                "schema": "Company",
                "properties": {"name": [f"Sub {i}"]},
                "datasets": ["t"],
            }
        )
        for i in range(3)
    ]
    ownerships = [
        make_entity(
            {
                "id": f"own{i}",
                "schema": "Ownership",
                "properties": {"owner": ["hub"], "asset": [f"sub{i}"]},
                "datasets": ["t"],
            }
        )
        for i in range(3)
    ]
    write_entities(client, [hub, *subsidiaries, *ownerships])

    results = degree_centrality(client, top=10)

    assert results, "expected degree-centrality results"
    top = results[0]
    assert top.entity_id == "hub"
    assert top.is_sanctioned
    assert top.score >= 3.0
