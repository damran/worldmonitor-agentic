"""Unit tests for the Gate-2a graph-read REST routes (ADR 0062).

These exercise route *behaviour* against a FAKE injected Neo4j client — no real
Neo4j connection is opened. The four read routes wrap the existing
``graph/queries.py`` helpers (``get_entity`` / ``get_neighbors`` /
``get_provenance``) plus the new ``find_paths``.

Injection mechanism assumed (the builder must match this exactly):
    ``create_app(*, settings, verifier, readiness, neo4j_client=...)`` — a new
    keyword-only ``neo4j_client`` parameter on the app factory, mirroring the
    existing ``readiness=`` / ``verifier=`` injection points (ADR 0062
    "DI for testability: the Neo4j read client is injectable into create_app").
    Default in production is ``Neo4jClient.from_settings(settings)``; tests inject
    the fake below. The routes call ``get_entity(neo4j_client, entity_id=...)``
    etc., so the fake duck-types ``Neo4jClient.execute_read``.

Response-shape contract assumed (the builder must match):
    - ``GET /entities/{id}``           -> the entity props dict (incl. ``prov_*``)
    - ``GET /entities/{id}/neighbors`` -> a list of neighbor props dicts
    - ``GET /entities/{id}/provenance``-> the node's ``prov_*`` dict
    - ``GET /paths``                   -> a list of paths, each a mapping with
      ``"nodes"`` (list of node ids) + ``"relationships"`` (list of rel-type str)
    List endpoints are read tolerantly (bare list or ``{"<key>": [...]}``) so the
    load-bearing assertions are the *contents* (ids / rel types / clamp), not the
    envelope.

Hop-cap assumed: ``EXPECTED_HOP_CAP = 4`` (ADR 0062 "default cap, e.g. 4").
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from fastapi.testclient import TestClient

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.settings import Settings

EXPECTED_HOP_CAP = 4

# Query fragments that uniquely identify the three existing helpers' Cypher, so the
# fake can dispatch canned rows and the test can find the per-helper recorded call.
_ENTITY_FRAGMENT = "RETURN properties(n) AS props"
_NEIGHBORS_FRAGMENT = "properties(m) AS props"
_PROVENANCE_FRAGMENT = "STARTS WITH 'prov_'"


class _FakeVerifier:
    """Accepts the token ``"good"``; rejects everything else (mirrors test_api_health)."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


class _FakeNeo4jClient:
    """Duck-types ``Neo4jClient.execute_read`` with canned, per-helper rows.

    Records every ``(query, params)`` so tests can assert (a) that input validation
    short-circuits BEFORE any query runs and (b) that ``hops`` / ``max_hops`` reach
    the query layer CLAMPED (the bound is a literal in the query string — Cypher
    cannot parameterize a variable-length bound).
    """

    def __init__(
        self,
        *,
        entity: dict[str, Any] | None = None,
        neighbors: list[dict[str, Any]] | None = None,
        provenance: dict[str, str] | None = None,
        paths: list[dict[str, Any]] | None = None,
    ) -> None:
        self.entity = entity
        self.neighbors = neighbors or []
        self.provenance = provenance
        self.paths = paths or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if _ENTITY_FRAGMENT in query and _NEIGHBORS_FRAGMENT not in query:
            return [{"props": self.entity}] if self.entity is not None else []
        if _NEIGHBORS_FRAGMENT in query:
            return [{"props": n} for n in self.neighbors]
        if _PROVENANCE_FRAGMENT in query:
            if self.provenance is None:
                return []
            return [{"prov": [[k, v] for k, v in self.provenance.items()]}]
        # Fallback: the new find_paths helper (its exact Cypher is the builder's choice).
        return [{"nodes": p["nodes"], "relationships": p["relationships"]} for p in self.paths]

    # --- helpers for assertions -------------------------------------------------
    def neighbors_call(self) -> tuple[str, dict[str, Any]] | None:
        for query, params in self.calls:
            if _NEIGHBORS_FRAGMENT in query:
                return query, params
        return None

    def paths_call(self) -> tuple[str, dict[str, Any]] | None:
        for query, params in self.calls:
            if (
                _NEIGHBORS_FRAGMENT not in query
                and _PROVENANCE_FRAGMENT not in query
                and _ENTITY_FRAGMENT not in query
            ):
                return query, params
        return None


def _client(verifier: object | None, neo4j_client: object) -> TestClient:
    app = create_app(
        settings=Settings(environment="test"),
        verifier=verifier,  # type: ignore[arg-type]
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


def _entity_fixture() -> dict[str, Any]:
    return {
        "id": "A",
        "name": ["Acme Holdings"],
        "prov_source_id": "src:test",
        "prov_source_record": "s3://landing/test/a.json",
        "prov_retrieved_at": "2026-06-21T00:00:00Z",
        "prov_reliability": "A",
    }


# ======================================================================================
# Auth-gating: every route is behind get_principal (401 without a valid token).
# ======================================================================================
def test_all_graph_routes_require_a_token() -> None:
    fake = _FakeNeo4jClient(entity=_entity_fixture())
    client = _client(_FakeVerifier(), fake)
    routes = [
        "/entities/A",
        "/entities/A/neighbors?hops=1",
        "/entities/A/provenance",
        "/paths?from=A&to=B&max_hops=1",
        "/entities/A/dossier",  # Gate F-3 (ADR 0122) — same auth gate as the sibling routes
    ]
    for route in routes:
        resp = client.get(route)
        assert resp.status_code == 401, f"{route} must be auth-gated (got {resp.status_code})"
    # No query should have run for unauthenticated requests.
    assert fake.calls == []


def test_all_graph_routes_accept_a_valid_token() -> None:
    fake = _FakeNeo4jClient(
        entity=_entity_fixture(),
        neighbors=[{"id": "B", "name": ["Beta"]}],
        provenance={"prov_source_id": "src:test"},
        paths=[{"nodes": ["A", "B"], "relationships": ["OWNS"]}],
    )
    client = _client(_FakeVerifier(), fake)
    for route in [
        "/entities/A",
        "/entities/A/neighbors?hops=1",
        "/entities/A/provenance",
        "/paths?from=A&to=B&max_hops=1",
        "/entities/A/dossier",  # Gate F-3 (ADR 0122) — same auth gate as the sibling routes
    ]:
        resp = client.get(route, headers=_auth())
        assert resp.status_code == 200, f"{route} -> {resp.status_code}: {resp.text}"


# ======================================================================================
# GET /entities/{id} -> get_entity (200 incl prov_*, 404 if absent).
# ======================================================================================
def test_get_entity_returns_payload_with_provenance() -> None:
    fake = _FakeNeo4jClient(entity=_entity_fixture())
    resp = _client(_FakeVerifier(), fake).get("/entities/A", headers=_auth())
    assert resp.status_code == 200
    entity = _as_entity(resp.json())
    assert entity["id"] == "A"
    # The entity payload carries its provenance (ADR 0062 / docs/60 §2).
    assert entity["prov_source_id"] == "src:test"
    assert entity["prov_source_record"] == "s3://landing/test/a.json"


def test_get_entity_missing_id_is_404() -> None:
    fake = _FakeNeo4jClient(entity=None)
    resp = _client(_FakeVerifier(), fake).get("/entities/does-not-exist", headers=_auth())
    assert resp.status_code == 404


# ======================================================================================
# GET /entities/{id}/neighbors?hops=N -> get_neighbors, N clamped to the hard cap.
# ======================================================================================
def test_neighbors_clamps_hops_to_the_cap() -> None:
    fake = _FakeNeo4jClient(neighbors=[{"id": "B", "name": ["Beta"]}])
    resp = _client(_FakeVerifier(), fake).get("/entities/A/neighbors?hops=99", headers=_auth())
    assert resp.status_code == 200
    neighbors = _as_list(resp.json(), "neighbors")
    assert any(n.get("id") == "B" for n in neighbors)

    call = fake.neighbors_call()
    assert call is not None, "the neighbors helper was never invoked"
    query, _ = call
    match = re.search(r"\*1\.\.(\d+)", query)
    assert match is not None, f"could not read the traversal depth from: {query!r}"
    depth = int(match.group(1))
    # The requested 99 must have been clamped DOWN to the cap before the query ran.
    assert depth == EXPECTED_HOP_CAP, f"hops not clamped: traversal bound was {depth}, want 4"
    assert "99" not in query, "the unclamped hop count leaked into the query"


# ======================================================================================
# GET /entities/{id}/provenance -> get_provenance (the node's prov_*).
# ======================================================================================
def test_provenance_route_returns_prov_star() -> None:
    fake = _FakeNeo4jClient(
        provenance={
            "prov_source_id": "src:test",
            "prov_source_record": "s3://landing/test/a.json",
        }
    )
    resp = _client(_FakeVerifier(), fake).get("/entities/A/provenance", headers=_auth())
    assert resp.status_code == 200
    prov = _as_prov(resp.json())
    assert prov["prov_source_id"] == "src:test"
    assert prov["prov_source_record"] == "s3://landing/test/a.json"


# ======================================================================================
# GET /paths?from=&to=&max_hops=N -> find_paths, max_hops clamped, bounded paths.
# ======================================================================================
def test_paths_clamps_max_hops_and_returns_bounded_paths() -> None:
    fake = _FakeNeo4jClient(
        paths=[{"nodes": ["A", "M", "B"], "relationships": ["OWNS", "DIRECTS"]}]
    )
    resp = _client(_FakeVerifier(), fake).get("/paths?from=A&to=B&max_hops=99", headers=_auth())
    assert resp.status_code == 200
    paths = _as_list(resp.json(), "paths")
    assert len(paths) == 1
    path = paths[0]
    assert path["nodes"] == ["A", "M", "B"]
    assert path["relationships"] == ["OWNS", "DIRECTS"]

    call = fake.paths_call()
    assert call is not None, "the find_paths helper was never invoked"
    query, _ = call
    # max_hops is a variable-length bound -> it is a literal in the query; the
    # unclamped 99 must NOT appear, and any bound that does appear must be <= the cap.
    assert "99" not in query, "max_hops not clamped: 99 leaked into the find_paths query"
    for bound in re.findall(r"\.\.(\d+)", query):
        assert int(bound) <= EXPECTED_HOP_CAP, f"find_paths bound {bound} exceeds the cap"


# ======================================================================================
# Input validation: a malformed id is rejected BEFORE any query runs.
# ======================================================================================
def test_malformed_entity_id_rejected_before_query() -> None:
    fake = _FakeNeo4jClient(entity=_entity_fixture())
    payload = quote('") DETACH DELETE n //', safe="")
    resp = _client(_FakeVerifier(), fake).get(f"/entities/{payload}", headers=_auth())
    assert resp.status_code in (400, 422), (
        f"injection-shaped id must be rejected: {resp.status_code}"
    )
    assert fake.calls == [], "validation must short-circuit before the Neo4j client is touched"


def test_blank_path_endpoint_rejected_before_query() -> None:
    fake = _FakeNeo4jClient(paths=[{"nodes": ["A", "B"], "relationships": ["OWNS"]}])
    resp = _client(_FakeVerifier(), fake).get("/paths?from=&to=B&max_hops=1", headers=_auth())
    assert resp.status_code in (400, 422), f"blank 'from' must be rejected: {resp.status_code}"
    assert fake.calls == [], "validation must short-circuit before the Neo4j client is touched"


# ======================================================================================
# REGRESSION-LOCK additions (gate 2a completeness critic, 2026-06-28).
#
# These assert STATED invariants of ADR 0062 that were previously unasserted. They use
# DIRECT, precise envelope assertions (NOT the tolerant _as_list / _as_entity / _as_prov
# helpers) so a future payload-shape refactor fails loudly here. Do not route these
# through the tolerant readers — the exact top-level shape is the thing being locked.
# ======================================================================================


def _neighbor_with_prov() -> dict[str, Any]:
    # A neighbor row that, like a real prov-stamped node, carries its own prov_* keys.
    return {
        "id": "B",
        "name": ["Beta"],
        "prov_source_id": "src:test",
        "prov_source_record": "s3://landing/b.json",
    }


# G1 — provenance rides in neighbor responses (ADR 0062 line 33: "entity/neighbor
# payloads carry their prov_*"). Locks get_neighbors against a payload-trimming refactor
# that would silently drop provenance from neighbour objects.
def test_neighbors_payload_carries_provenance() -> None:
    fake = _FakeNeo4jClient(neighbors=[_neighbor_with_prov()])
    resp = _client(_FakeVerifier(), fake).get("/entities/A/neighbors", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict) and isinstance(body.get("neighbors"), list)
    neighbor = next(n for n in body["neighbors"] if n.get("id") == "B")
    # The neighbour object CARRIES its provenance straight through the route.
    assert neighbor["prov_source_id"] == "src:test"
    assert neighbor["prov_source_record"] == "s3://landing/b.json"


# G2 — provenance of an ABSENT entity must be 404, not a fail-open 200 {}.
# RED until read_provenance raises 404 when the entity does not exist (current code
# returns 200 with an empty body). This is the failing-test-first behavioural fix the
# builder must make; the fake has entity=None AND provenance=None, so get_provenance
# returns {} and (if the builder gates on get_entity) get_entity returns None — 404
# either way once fixed.
def test_provenance_404_when_entity_absent() -> None:
    fake = _FakeNeo4jClient()  # entity=None, provenance=None -> get_provenance returns {}
    resp = _client(_FakeVerifier(), fake).get("/entities/zzz-absent/provenance", headers=_auth())
    assert resp.status_code == 404, (
        f"absent-entity provenance must 404, not fail-open 200: got {resp.status_code} {resp.text}"
    )


# G3 — exact response envelopes. Slice 2b / MCP consume these verbatim, so the precise
# top-level JSON shape of each route is pinned directly (no tolerant normalization).
def test_entity_envelope_is_bare_dict_with_id() -> None:
    fake = _FakeNeo4jClient(entity=_entity_fixture())
    resp = _client(_FakeVerifier(), fake).get("/entities/A", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict), f"entity body must be a bare dict, got {type(body).__name__}"
    assert body["id"] == "A"
    # NOT wrapped: the props are top-level, there is no envelope key around them.
    assert "entity" not in body, "entity payload must not be wrapped under an 'entity' key"


def test_provenance_envelope_is_bare_prov_map() -> None:
    fake = _FakeNeo4jClient(provenance={"prov_source_id": "src:test"})
    resp = _client(_FakeVerifier(), fake).get("/entities/A/provenance", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict), f"provenance body must be a bare dict, got {type(body).__name__}"
    assert body["prov_source_id"] == "src:test"
    # NOT wrapped under a 'provenance' key.
    assert "provenance" not in body, "prov map must not be wrapped under a 'provenance' key"


def test_neighbors_envelope_is_neighbors_list() -> None:
    fake = _FakeNeo4jClient(neighbors=[{"id": "B", "name": ["Beta"]}])
    resp = _client(_FakeVerifier(), fake).get("/entities/A/neighbors", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert list(body.keys()) == ["neighbors"], (
        f"neighbors envelope must be exactly {{'neighbors': [...]}}, got keys {list(body.keys())}"
    )
    assert isinstance(body["neighbors"], list)


def test_paths_envelope_is_paths_list() -> None:
    fake = _FakeNeo4jClient(paths=[{"nodes": ["A", "B"], "relationships": ["OWNS"]}])
    resp = _client(_FakeVerifier(), fake).get("/paths?from=A&to=B&max_hops=1", headers=_auth())
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)
    assert list(body.keys()) == ["paths"], (
        f"paths envelope must be exactly {{'paths': [...]}}, got keys {list(body.keys())}"
    )
    assert isinstance(body["paths"], list)


# G4a — /paths with from == to: the self-path request is allowed (200) and reaches the
# find_paths helper with from_id == to_id == the same id (lock the self-path behaviour).
def test_paths_allows_from_equals_to() -> None:
    fake = _FakeNeo4jClient(paths=[{"nodes": ["A"], "relationships": []}])
    resp = _client(_FakeVerifier(), fake).get("/paths?from=A&to=A&max_hops=2", headers=_auth())
    assert resp.status_code == 200, f"self-path must be allowed: {resp.status_code} {resp.text}"
    call = fake.paths_call()
    assert call is not None, "find_paths was never invoked"
    _, params = call
    assert params.get("from_id") == "A", f"from_id not passed through to find_paths: {params}"
    assert params.get("to_id") == "A", f"to_id not passed through to find_paths: {params}"


# ======================================================================================
# Gate F-3 slice 1 — GET /entities/{id}/dossier (ADR 0122). The route doesn't exist yet on
# the current tree, so every request below 404s (FastAPI's default "route not found"
# handler) regardless of id validity — a NAIVE 404-only assertion would pass vacuously for
# the wrong reason. So the 404 test below additionally pins the explicit ``detail`` body
# ``read_entity`` uses (the route's OWN not-found idiom, spec §4.1), which only a REAL
# implementation can produce; a route-not-found 404 carries FastAPI's generic
# ``{"detail": "Not Found"}`` instead. No import of a not-yet-existing symbol is needed
# here — the route is exercised purely over HTTP, so this section is fail-soft by
# construction (assertions fail; the module still collects and the rest of the file
# stays green).
# ======================================================================================


def test_dossier_returns_assembled_sections() -> None:
    fake = _FakeNeo4jClient(
        entity=_entity_fixture(),
        neighbors=[_neighbor_with_prov()],
        provenance={
            "prov_source_id": "src:test",
            "prov_source_record": "s3://landing/test/a.json",
        },
    )
    resp = _client(_FakeVerifier(), fake).get("/entities/A/dossier", headers=_auth())
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert isinstance(body, dict)
    assert set(body.keys()) == {"entity", "neighbors", "provenance", "merge_history"}, (
        f"dossier body must have exactly the four top-level keys (§4); got {list(body.keys())}"
    )
    assert body["entity"]["id"] == "A"
    assert body["entity"]["prov_source_id"] == "src:test"
    neighbor = next(n for n in body["neighbors"] if n.get("id") == "B")
    assert neighbor["prov_source_id"] == "src:test"
    assert body["provenance"]["prov_source_id"] == "src:test"
    assert body["provenance"]["prov_source_record"] == "s3://landing/test/a.json"
    assert body["merge_history"] == {"status": "not_assembled", "available": False}


def test_dossier_absent_entity_404() -> None:
    fake = _FakeNeo4jClient(entity=None)
    resp = _client(_FakeVerifier(), fake).get("/entities/zzz-absent/dossier", headers=_auth())
    assert resp.status_code == 404, f"{resp.status_code}: {resp.text}"
    # Pins the ROUTE's OWN not-found idiom (mirrors read_entity's HTTPException detail) so
    # this can't pass merely because the route doesn't exist yet.
    assert resp.json().get("detail") == "Entity not found", (
        f"dossier 404 must carry the route's own 'Entity not found' detail (not a generic "
        f"route-not-found 404); got {resp.json()!r}"
    )


def test_dossier_rejects_injection_id_422() -> None:
    fake = _FakeNeo4jClient(entity=_entity_fixture())
    # NOT a trailing "//" — that quotes to a literal path separator (%2F%2F), so
    # "/entities/<...>///dossier" matches NO route and 404s at the router before
    # Path(pattern=...) validation ever runs. "--" keeps the same injection intent
    # (a Cypher comment terminator) without introducing a path-separator byte.
    payload = quote('") DETACH DELETE n --', safe="")
    resp = _client(_FakeVerifier(), fake).get(f"/entities/{payload}/dossier", headers=_auth())
    assert resp.status_code in (400, 422), (
        f"injection-shaped id must be rejected: {resp.status_code}"
    )
    assert fake.calls == [], "validation must short-circuit before the Neo4j client is touched"


def test_dossier_hops_clamped() -> None:
    # entity= is REQUIRED here (unlike the sibling /neighbors clamp test): the dossier's
    # mandatory short-circuit 404s before get_neighbors ever runs when get_entity is None.
    fake = _FakeNeo4jClient(entity=_entity_fixture(), neighbors=[_neighbor_with_prov()])
    resp = _client(_FakeVerifier(), fake).get("/entities/A/dossier?hops=99", headers=_auth())
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    call = fake.neighbors_call()
    assert call is not None, "the neighbors helper was never invoked by the dossier route"
    query, _ = call
    match = re.search(r"\*1\.\.(\d+)", query)
    assert match is not None, f"could not read the traversal depth from: {query!r}"
    depth = int(match.group(1))
    assert depth == EXPECTED_HOP_CAP, f"hops not clamped: traversal bound was {depth}, want 4"
    assert "99" not in query, "the unclamped hop count leaked into the query"


# ======================================================================================
# AC-5 — REST <-> MCP lockstep parity (spec §3.2 / §6.4): ONE shared recording fake drives
# BOTH thin surfaces for the SAME entity_id/hops; the decoded REST body must deep-equal the
# MCP tool's returned dict. ``tool_get_entity_dossier`` is imported LOCALLY (fail-soft: a
# missing symbol fails only this test, not the whole module).
# ======================================================================================
def test_dossier_rest_mcp_parity() -> None:
    from worldmonitor.mcp.server import tool_get_entity_dossier

    fake = _FakeNeo4jClient(
        entity=_entity_fixture(),
        neighbors=[_neighbor_with_prov()],
        provenance={
            "prov_source_id": "src:test",
            "prov_source_record": "s3://landing/test/a.json",
        },
    )
    rest_resp = _client(_FakeVerifier(), fake).get("/entities/A/dossier?hops=1", headers=_auth())
    assert rest_resp.status_code == 200, f"{rest_resp.status_code}: {rest_resp.text}"
    rest_body = rest_resp.json()

    mcp_result = tool_get_entity_dossier(fake, "A", hops=1)

    assert rest_body == mcp_result, (
        "REST body and MCP tool result must be byte-identical (as decoded JSON) for the same "
        f"seeded input (AC-5 lockstep): rest={rest_body!r} mcp={mcp_result!r}"
    )


# ======================================================================================
# Gate F-5 (`summary` context-budget flag, ADR 0124) — `?summary=true` on
# /entities/{id}/neighbors and /paths returns `{count, sample}` in place of the full list;
# `summary` absent/false stays BYTE-IDENTICAL to today. Today both routes silently IGNORE
# an unrecognized `summary` query param (FastAPI drops unknown params), so the summary-mode
# assertions below are RED for the load-bearing reason: no {count, sample} envelope is
# shaped yet — not merely "the parameter is unrecognized".
# ======================================================================================


def _labeled_neighbor(node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "name": [f"Node {node_id}"],
        "prov_source_id": "src:test",
        "prov_source_record": f"s3://landing/{node_id}.json",
    }


def _labeled_path(*nodes: str) -> dict[str, Any]:
    return {"nodes": list(nodes), "relationships": ["OWNS"] * (len(nodes) - 1)}


def test_neighbors_summary_shape() -> None:
    """AC-2 / spec §6.4: `?summary=true` -> exactly {count, sample}; count == len(seeded);
    sample capped at 3; each sample element carries its own prov_* (G1, not laundered)."""
    fake = _FakeNeo4jClient(
        neighbors=[
            _labeled_neighbor("D"),
            _labeled_neighbor("B"),
            _labeled_neighbor("A"),
            _labeled_neighbor("C"),
        ]
    )
    resp = _client(_FakeVerifier(), fake).get("/entities/A/neighbors?summary=true", headers=_auth())
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert set(body.keys()) == {"count", "sample"}, (
        f"summary envelope must be exactly {{count, sample}}; got keys {list(body.keys())}"
    )
    assert body["count"] == 4
    assert len(body["sample"]) == 3
    for item in body["sample"]:
        assert item.get("prov_source_id") == "src:test", (
            f"a sample element must carry its own provenance verbatim (G1); got {item!r}"
        )
    # Canonical-sort determinism (§3.4): ascending by the shared "id" key -> A, B, C.
    assert [item["id"] for item in body["sample"]] == ["A", "B", "C"]


def test_paths_summary_shape() -> None:
    fake = _FakeNeo4jClient(
        paths=[
            _labeled_path("A", "D"),
            _labeled_path("A", "B"),
            _labeled_path("A", "C"),
            _labeled_path("A", "Z"),
        ]
    )
    resp = _client(_FakeVerifier(), fake).get(
        "/paths?from=A&to=B&max_hops=1&summary=true", headers=_auth()
    )
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert set(body.keys()) == {"count", "sample"}, (
        f"summary envelope must be exactly {{count, sample}}; got keys {list(body.keys())}"
    )
    assert body["count"] == 4
    assert len(body["sample"]) == 3
    # Canonical sort orders by the "nodes" key (alphabetically first) -> B, C, D before Z.
    assert [item["nodes"] for item in body["sample"]] == [["A", "B"], ["A", "C"], ["A", "D"]]


def test_neighbors_summary_false_matches_absent_byte_parity() -> None:
    """AC-2: `summary=false` (explicit) must be BYTE-IDENTICAL to `summary` omitted entirely
    — the widened `dict[str, Any]` return annotation must not change a single normal-mode
    byte. (Currently GREEN: today `summary` is simply an ignored, unrecognized query param,
    so both requests already coincide; this pins that widening the annotation may never
    change that.)"""
    neighbor = _labeled_neighbor("B")
    resp_absent = _client(_FakeVerifier(), _FakeNeo4jClient(neighbors=[neighbor])).get(
        "/entities/A/neighbors", headers=_auth()
    )
    resp_false = _client(_FakeVerifier(), _FakeNeo4jClient(neighbors=[neighbor])).get(
        "/entities/A/neighbors?summary=false", headers=_auth()
    )
    assert resp_absent.status_code == resp_false.status_code == 200
    assert resp_absent.json() == resp_false.json() == {"neighbors": [neighbor]}


def test_paths_summary_false_matches_absent_byte_parity() -> None:
    path = _labeled_path("A", "B")
    resp_absent = _client(_FakeVerifier(), _FakeNeo4jClient(paths=[path])).get(
        "/paths?from=A&to=B&max_hops=1", headers=_auth()
    )
    resp_false = _client(_FakeVerifier(), _FakeNeo4jClient(paths=[path])).get(
        "/paths?from=A&to=B&max_hops=1&summary=false", headers=_auth()
    )
    assert resp_absent.status_code == resp_false.status_code == 200
    assert resp_absent.json() == resp_false.json() == {"paths": [path]}


def test_neighbors_summary_invalid_value_is_422() -> None:
    """A non-boolean `summary` value is rejected by FastAPI's typed `Query(bool)` coercion
    (empirically verified against this repo's installed FastAPI/pydantic: "banana" ->
    422 bool_parsing) BEFORE any query runs — an unparseable flag must fail closed, never
    silently coerce to False."""
    fake = _FakeNeo4jClient(neighbors=[_labeled_neighbor("B")])
    resp = _client(_FakeVerifier(), fake).get(
        "/entities/A/neighbors?summary=banana", headers=_auth()
    )
    assert resp.status_code == 422, (
        f"non-boolean summary must 422; got {resp.status_code}: {resp.text}"
    )
    assert fake.calls == [], "a rejected summary value must short-circuit before any query runs"


def test_paths_summary_invalid_value_is_422() -> None:
    fake = _FakeNeo4jClient(paths=[_labeled_path("A", "B")])
    resp = _client(_FakeVerifier(), fake).get(
        "/paths?from=A&to=B&max_hops=1&summary=banana", headers=_auth()
    )
    assert resp.status_code == 422, (
        f"non-boolean summary must 422; got {resp.status_code}: {resp.text}"
    )
    assert fake.calls == [], "a rejected summary value must short-circuit before any query runs"


# ======================================================================================
# AC-4 lockstep parity (spec §6.4) — ONE shared recording fake drives BOTH surfaces for the
# SAME seeded input; the REST summary body must deep-equal the MCP tool's summary return.
# tool_get_neighbors / tool_find_paths are imported LOCALLY (fail-soft, the F-3 idiom) —
# they exist today but lack the `summary` kwarg, so calling with it raises TypeError until
# the builder adds it.
# ======================================================================================
def test_neighbors_summary_rest_mcp_parity() -> None:
    from worldmonitor.mcp.server import tool_get_neighbors

    fake = _FakeNeo4jClient(
        neighbors=[
            _labeled_neighbor("D"),
            _labeled_neighbor("B"),
            _labeled_neighbor("A"),
            _labeled_neighbor("C"),
        ]
    )
    rest_resp = _client(_FakeVerifier(), fake).get(
        "/entities/A/neighbors?summary=true", headers=_auth()
    )
    assert rest_resp.status_code == 200, f"{rest_resp.status_code}: {rest_resp.text}"
    rest_body = rest_resp.json()

    mcp_result = tool_get_neighbors(fake, "A", summary=True)

    expected = {
        "count": 4,
        "sample": [_labeled_neighbor("A"), _labeled_neighbor("B"), _labeled_neighbor("C")],
    }
    assert rest_body == mcp_result == expected, (
        "REST body and MCP tool result must be byte-identical (AC-4 lockstep) for the same "
        f"seeded input: rest={rest_body!r} mcp={mcp_result!r}"
    )


def test_paths_summary_rest_mcp_parity() -> None:
    from worldmonitor.mcp.server import tool_find_paths

    fake = _FakeNeo4jClient(
        paths=[
            _labeled_path("A", "D"),
            _labeled_path("A", "B"),
            _labeled_path("A", "C"),
            _labeled_path("A", "Z"),
        ]
    )
    rest_resp = _client(_FakeVerifier(), fake).get(
        "/paths?from=A&to=B&max_hops=1&summary=true", headers=_auth()
    )
    assert rest_resp.status_code == 200, f"{rest_resp.status_code}: {rest_resp.text}"
    rest_body = rest_resp.json()

    mcp_result = tool_find_paths(fake, "A", "B", max_hops=1, summary=True)

    expected = {
        "count": 4,
        "sample": [_labeled_path("A", "B"), _labeled_path("A", "C"), _labeled_path("A", "D")],
    }
    assert rest_body == mcp_result == expected, (
        "REST body and MCP tool result must be byte-identical (AC-4 lockstep) for the same "
        f"seeded input: rest={rest_body!r} mcp={mcp_result!r}"
    )
