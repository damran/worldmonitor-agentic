"""Integration test for GDS degree centrality (needs the GDS plugin)."""

from __future__ import annotations

import pytest

from worldmonitor.graph.gds import degree_centrality
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp

pytestmark = pytest.mark.integration

# ADR 0055 (fail-closed edge provenance): an Ownership *edge* entity is a relationship
# assertion and must carry provenance, else `write_entities` refuses it. The endpoint
# Company nodes carry no entity-typed properties (so they yield no relationship batch and
# are never refused), but the edges must be stamped — stamping only; the degree-centrality
# / sanction-flag assertions are unaffected by provenance.
_PROV = Provenance(
    source_id="src:gds-test",
    retrieved_at="2026-06-21T00:00:00Z",
    reliability="A",
    source_record="s3://landing/gds-test.json",
)


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
        stamp(
            make_entity(
                {
                    "id": f"own{i}",
                    "schema": "Ownership",
                    "properties": {"owner": ["hub"], "asset": [f"sub{i}"]},
                    "datasets": ["t"],
                }
            ),
            _PROV,
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
