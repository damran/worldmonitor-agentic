"""Integration tests for graph read queries (single-tenant, D1 / ADR 0042)."""

from __future__ import annotations

import pytest

from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.queries import get_entity, get_neighbors, get_provenance
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

pytestmark = pytest.mark.integration

_PROV = Provenance(
    source_id="opensanctions:us_ofac_sdn",
    retrieved_at="2026-06-21T00:00:00Z",
    reliability="A",
    source_record="s3://landing/test-tenant/opensanctions/p1.json",
)


def _stamped(data: dict[str, object]) -> FtmEntity:
    return stamp(make_entity(data), _PROV)


def test_get_entity_neighbors_and_provenance(clean_graph: Neo4jClient) -> None:
    ensure_constraints(clean_graph)
    person = _stamped(
        {
            "id": "p1",
            "schema": "Person",
            "properties": {"name": ["Jane Target"], "topics": ["sanction"]},
            "datasets": ["t"],
        }
    )
    set_anchor(person, "wikidata_id", "Q777")
    company = _stamped(
        {"id": "c1", "schema": "Company", "properties": {"name": ["Shell Co"]}, "datasets": ["t"]}
    )
    ownership = make_entity(
        {
            "id": "o1",
            "schema": "Ownership",
            "properties": {"owner": ["p1"], "asset": ["c1"]},
            "datasets": ["t"],
        }
    )
    write_entities(clean_graph, [person, company, ownership])

    # get_entity returns the resolved node with its name and anchor.
    node = get_entity(clean_graph, entity_id="p1")
    assert node is not None
    assert "Jane Target" in node["name"]
    assert node["wikidata_id"] == "Q777"

    # get_provenance returns where the fact came from (the raw landing pointer).
    provenance = get_provenance(clean_graph, entity_id="p1")
    assert provenance["prov_source_id"] == _PROV.source_id
    assert provenance["prov_source_record"] == _PROV.source_record

    # get_neighbors returns everyone linked to the entity.
    neighbor_ids = {n["id"] for n in get_neighbors(clean_graph, entity_id="p1")}
    assert "c1" in neighbor_ids

    # A missing id returns nothing.
    assert get_entity(clean_graph, entity_id="does-not-exist") is None
