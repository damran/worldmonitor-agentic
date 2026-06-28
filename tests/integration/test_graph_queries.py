"""Integration tests for graph read queries (single-tenant, D1 / ADR 0042)."""

from __future__ import annotations

import pytest

from worldmonitor.graph import read_guards
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
    # ADR 0055 (fail-closed edge provenance): the Ownership *edge* entity is the assertion
    # that creates the relationship, so it must carry provenance — route it through the same
    # `_stamped` helper as its endpoints. Stamping only; the neighbor/provenance assertions
    # below are unchanged.
    ownership = _stamped(
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


# ======================================================================================
# find_paths (ADR 0062, slice 2a) — bounded relationship paths between two entities.
# `find_paths` is imported locally so this module still collects (RED via AttributeError)
# while the existing get_entity/get_neighbors/get_provenance test above stays green.
# ======================================================================================
def _seed_owns_chain(client: Neo4jClient) -> None:
    """p1 -OWNS-> c1 -OWNS-> c2: p1..c1 is one hop, p1..c2 is two."""
    ensure_constraints(client)
    person = _stamped(
        {"id": "p1", "schema": "Person", "properties": {"name": ["Jane Target"]}, "datasets": ["t"]}
    )
    company1 = _stamped(
        {"id": "c1", "schema": "Company", "properties": {"name": ["Shell Co"]}, "datasets": ["t"]}
    )
    company2 = _stamped(
        {"id": "c2", "schema": "Company", "properties": {"name": ["Sub Co"]}, "datasets": ["t"]}
    )
    own1 = _stamped(
        {
            "id": "o1",
            "schema": "Ownership",
            "properties": {"owner": ["p1"], "asset": ["c1"]},
            "datasets": ["t"],
        }
    )
    own2 = _stamped(
        {
            "id": "o2",
            "schema": "Ownership",
            "properties": {"owner": ["c1"], "asset": ["c2"]},
            "datasets": ["t"],
        }
    )
    write_entities(client, [person, company1, company2, own1, own2])


def test_find_paths_returns_bounded_node_ids_and_rel_types(clean_graph: Neo4jClient) -> None:
    from worldmonitor.graph.queries import find_paths

    _seed_owns_chain(clean_graph)
    paths = find_paths(clean_graph, from_id="p1", to_id="c2", max_hops=3)
    assert paths, "expected at least one path from p1 to c2 within 3 hops"

    path = paths[0]
    # Each path carries the node ids it traverses + the relationship types between them.
    assert path["nodes"][0] == "p1"
    assert path["nodes"][-1] == "c2"
    assert "c1" in path["nodes"]
    assert path["relationships"], "a path must list its relationship types"
    assert all(rel == "OWNS" for rel in path["relationships"])
    # node-count == rel-count + 1 for a simple path.
    assert len(path["nodes"]) == len(path["relationships"]) + 1


def test_find_paths_respects_max_hops(clean_graph: Neo4jClient) -> None:
    from worldmonitor.graph.queries import find_paths

    _seed_owns_chain(clean_graph)
    # p1..c1 is one hop -> reachable at max_hops=1.
    near = find_paths(clean_graph, from_id="p1", to_id="c1", max_hops=1)
    assert near, "p1 -> c1 is a single hop and must be found at max_hops=1"
    assert near[0]["nodes"][0] == "p1"
    assert near[0]["nodes"][-1] == "c1"

    # p1..c2 is two hops -> NOT reachable when bounded to a single hop.
    far = find_paths(clean_graph, from_id="p1", to_id="c2", max_hops=1)
    assert far == [], "p1 -> c2 is two hops and must not be returned at max_hops=1"


def test_find_paths_is_read_only_and_parameterized(clean_graph: Neo4jClient) -> None:
    from worldmonitor.graph.queries import find_paths

    _seed_owns_chain(clean_graph)
    before = clean_graph.execute_read("MATCH (n) RETURN count(n) AS n")[0]["n"]

    # An injection-shaped id is a bound parameter, not interpolated Cypher: it simply
    # matches nothing (no path, no error, no mutation).
    hostile = find_paths(clean_graph, from_id='p1") DETACH DELETE n //', to_id="c2", max_hops=3)
    assert hostile == []

    after = clean_graph.execute_read("MATCH (n) RETURN count(n) AS n")[0]["n"]
    assert after == before, "find_paths must be read-only — the graph was mutated"


# ======================================================================================
# get_neighbors result-count LIMIT (ADR 0064) — end-to-end truncation over a real graph.
#
# A genuinely high-degree node must not return its WHOLE neighbourhood: with
# read_guards.NEIGHBOR_RESULT_LIMIT monkeypatched well below the neighbour count, the cap
# is observable end-to-end. raising=False so the monkeypatch is RED-correct on the current
# base (the constant does not exist yet) AND green once the builder adds + reads it.
#
# RED today: get_neighbors carries NO LIMIT, so the four neighbours all come back (4 != 2).
# ======================================================================================
def test_get_neighbors_truncates_to_result_limit(
    clean_graph: Neo4jClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(read_guards, "NEIGHBOR_RESULT_LIMIT", 2, raising=False)

    ensure_constraints(clean_graph)
    hub = _stamped(
        {"id": "hub", "schema": "Person", "properties": {"name": ["Hub Node"]}, "datasets": ["t"]}
    )
    # FOUR distinct one-hop neighbours of `hub` — strictly more than the cap of 2. Each
    # Ownership is an EDGE-schema entity, so it materializes as a relationship (hub -OWNS-> nN),
    # NOT an intermediate node: hub therefore has exactly four 1-hop Entity neighbours.
    neighbours = [
        _stamped(
            {
                "id": f"n{i}",
                "schema": "Company",
                "properties": {"name": [f"Neighbour Co {i}"]},
                "datasets": ["t"],
            }
        )
        for i in range(1, 5)
    ]
    edges = [
        _stamped(
            {
                "id": f"o{i}",
                "schema": "Ownership",
                "properties": {"owner": ["hub"], "asset": [f"n{i}"]},
                "datasets": ["t"],
            }
        )
        for i in range(1, 5)
    ]
    write_entities(clean_graph, [hub, *neighbours, *edges])

    result = get_neighbors(clean_graph, entity_id="hub", hops=1)
    # End-to-end truncation: hub has FOUR neighbours but the cap is 2 — exactly the cap
    # comes back (an arbitrary bounded subset; ADR 0064 ships LIMIT without ORDER BY).
    assert len(result) == 2, (
        f"get_neighbors must truncate to read_guards.NEIGHBOR_RESULT_LIMIT (2); "
        f"got {len(result)} rows (the whole neighbourhood?)"
    )
