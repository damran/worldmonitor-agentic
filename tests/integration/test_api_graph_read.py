"""Integration tests for the Gate-2a graph-read REST routes (ADR 0062).

End-to-end: a real ``Neo4jClient`` (testcontainer) injected into ``create_app``,
a tiny resolved graph seeded via the ``clean_graph`` fixture, and the four read
routes exercised over HTTP with a valid bearer token.

Injection mechanism assumed (must match the builder):
    ``create_app(*, settings, verifier, readiness, neo4j_client=<Neo4jClient>)``.
Response shapes assumed: see ``tests/unit/test_api_graph.py`` module docstring.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.settings import Settings

pytestmark = pytest.mark.integration

_PROV = Provenance(
    source_id="opensanctions:us_ofac_sdn",
    retrieved_at="2026-06-21T00:00:00Z",
    reliability="A",
    source_record="s3://landing/test/ofac/p1.json",
)


class _FakeVerifier:
    """Accepts the token ``"good"``; rejects everything else."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


def _stamped(data: dict[str, object]) -> FtmEntity:
    return stamp(make_entity(data), _PROV)


def _seed_chain(client: Neo4jClient) -> None:
    """p1 -OWNS-> c1 -OWNS-> c2 (so p1..c2 is two hops; p1..c1 is one)."""
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


def _client(neo4j_client: Neo4jClient) -> TestClient:
    app = create_app(
        settings=Settings(environment="test"),
        verifier=_FakeVerifier(),
        neo4j_client=neo4j_client,  # type: ignore[call-arg]
    )
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer good"}


def _as_list(body: Any, key: str) -> list[Any]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        value = body.get(key, body.get("results"))
        if isinstance(value, list):
            return value
    return []


def _as_entity(body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and isinstance(body.get("entity"), dict):
        return body["entity"]
    return body if isinstance(body, dict) else {}


def _as_prov(body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and isinstance(body.get("provenance"), dict):
        return body["provenance"]
    return body if isinstance(body, dict) else {}


def test_entity_route_returns_node_and_provenance(clean_graph: Neo4jClient) -> None:
    _seed_chain(clean_graph)
    client = _client(clean_graph)

    resp = client.get("/entities/p1", headers=_auth())
    assert resp.status_code == 200
    entity = _as_entity(resp.json())
    assert "Jane Target" in entity["name"]
    assert entity["prov_source_id"] == _PROV.source_id

    # A missing id is a 404, not an empty 200.
    assert client.get("/entities/nope", headers=_auth()).status_code == 404


def test_provenance_route_traces_to_landing(clean_graph: Neo4jClient) -> None:
    _seed_chain(clean_graph)
    resp = _client(clean_graph).get("/entities/p1/provenance", headers=_auth())
    assert resp.status_code == 200
    prov = _as_prov(resp.json())
    assert prov["prov_source_id"] == _PROV.source_id
    assert prov["prov_source_record"] == _PROV.source_record


def test_neighbors_route_returns_linked_entities(clean_graph: Neo4jClient) -> None:
    _seed_chain(clean_graph)
    resp = _client(clean_graph).get("/entities/p1/neighbors?hops=1", headers=_auth())
    assert resp.status_code == 200
    neighbors = _as_list(resp.json(), "neighbors")
    ids = {n.get("id") for n in neighbors}
    assert "c1" in ids
    # G1: provenance rides in neighbor responses (ADR 0062 line 33). The seeded
    # nodes are prov-stamped, so the returned neighbor carries its prov_* keys.
    # Purely additive — does not weaken the existing id assertion above.
    c1 = next(n for n in neighbors if n.get("id") == "c1")
    assert c1["prov_source_id"] == _PROV.source_id


def test_paths_route_connects_two_seeded_entities(clean_graph: Neo4jClient) -> None:
    _seed_chain(clean_graph)
    client = _client(clean_graph)

    # p1 -> c2 is reachable within 3 hops; the path lists node ids + rel types.
    resp = client.get("/paths?from=p1&to=c2&max_hops=3", headers=_auth())
    assert resp.status_code == 200
    paths = _as_list(resp.json(), "paths")
    assert paths, "expected at least one path from p1 to c2 within 3 hops"
    nodes = paths[0]["nodes"]
    assert nodes[0] == "p1" and nodes[-1] == "c2"
    assert "OWNS" in paths[0]["relationships"]

    # The same pair is NOT reachable within a single hop -> no path returned.
    bounded = client.get("/paths?from=p1&to=c2&max_hops=1", headers=_auth())
    assert bounded.status_code == 200
    assert _as_list(bounded.json(), "paths") == []
