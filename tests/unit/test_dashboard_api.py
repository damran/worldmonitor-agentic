"""Dashboard read API — router logic (ADR 0115, Slice C).

Fast unit tests over the router's OWN logic (geo resolution: precise vs country-centroid vs drop;
bbox filtering; entity nodes/links assembly; public access; limit cap), with the ``graph/queries``
helpers monkeypatched so no Neo4j is needed. The Cypher itself is pinned against a real graph in
``tests/integration/test_dashboard_api.py``.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from worldmonitor.api import dashboard as dash_mod
from worldmonitor.api.main import create_app
from worldmonitor.settings import Settings


class _DummyNeo4j:
    """Placeholder client; the query helpers are patched, so it is never called."""

    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("query helpers must be patched in the router unit tests")


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        zitadel_domain="",
        zitadel_client_id="",
        zitadel_client_secret="",
        app_base_url="",
    )  # type: ignore[arg-type]


def _client() -> TestClient:
    app = create_app(settings=_settings(), verifier=None, neo4j_client=_DummyNeo4j())  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


def test_dashboard_stats_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard read surface answers WITHOUT auth (the ADR 0115 carve-out)."""
    monkeypatch.setattr(
        dash_mod.queries, "graph_stats", lambda client: {"nodes": 3, "edges": 1, "articles": 2}
    )
    resp = _client().get("/api/dashboard/stats")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"nodes": 3, "edges": 1, "articles": 2}


def test_points_resolves_precise_country_and_drops_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": "addr1",
            "labels": ["Entity", "Address"],
            "label": "Port",
            "lat_raw": "51.5",
            "lon_raw": "-0.1",
            "country": "gb",
            "summary": None,
            "url": None,
            "time": "t",
        },
        {
            "id": "p1",
            "labels": ["Entity", "Person"],
            "label": "Jane",
            "lat_raw": None,
            "lon_raw": None,
            "country": "ru",
            "summary": None,
            "url": None,
            "time": "t",
        },
        {
            "id": "x1",
            "labels": ["Entity", "Person"],
            "label": "NoGeo",
            "lat_raw": None,
            "lon_raw": None,
            "country": "zz",  # unknown code -> dropped
            "summary": None,
            "url": None,
            "time": "t",
        },
    ]
    monkeypatch.setattr(dash_mod.queries, "geo_candidates", lambda client, *, limit: rows)
    body = _client().get("/api/dashboard/points").json()
    points = {p["id"]: p for p in body["points"]}

    assert points["addr1"]["geo_precision"] == "point"
    assert points["addr1"]["lat"] == 51.5 and points["addr1"]["lon"] == -0.1
    assert points["addr1"]["schema"] == "Address"
    assert points["p1"]["geo_precision"] == "country"  # ru -> centroid
    assert "x1" not in points  # unknown country dropped, never plotted wrongly


def test_points_bbox_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "id": "eu",
            "labels": ["Entity", "Address"],
            "label": "London",
            "lat_raw": "51.5",
            "lon_raw": "-0.1",
            "country": None,
            "summary": None,
            "url": None,
            "time": "t",
        },
        {
            "id": "au",
            "labels": ["Entity", "Address"],
            "label": "Sydney",
            "lat_raw": "-33.9",
            "lon_raw": "151.2",
            "country": None,
            "summary": None,
            "url": None,
            "time": "t",
        },
    ]
    monkeypatch.setattr(dash_mod.queries, "geo_candidates", lambda client, *, limit: rows)
    body = _client().get("/api/dashboard/points", params={"bbox": "-10,40,10,60"}).json()
    assert {p["id"] for p in body["points"]} == {"eu"}  # Sydney is outside the box


def test_points_bbox_malformed_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash_mod.queries, "geo_candidates", lambda client, *, limit: [])
    assert _client().get("/api/dashboard/points", params={"bbox": "1,2,3"}).status_code == 422


def test_entity_404_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dash_mod, "get_entity", lambda client, *, entity_id: None)
    assert _client().get("/api/dashboard/entity/nope").status_code == 404


def test_entity_assembles_nodes_links_and_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dash_mod, "get_entity", lambda client, *, entity_id: {"name": ["Jane"], "country": ["ru"]}
    )
    monkeypatch.setattr(
        dash_mod, "get_provenance", lambda client, *, entity_id: {"prov_source_id": "os"}
    )
    monkeypatch.setattr(
        dash_mod.queries,
        "neighborhood",
        lambda client, *, entity_id, limit: [
            {"id": "c1", "label": "Shell Co", "labels": ["Entity", "Company"], "rel": "OWNERSHIP"}
        ],
    )
    body = _client().get("/api/dashboard/entity/p1").json()

    assert body["id"] == "p1"
    assert {n["id"] for n in body["nodes"]} == {"p1", "c1"}
    center = next(n for n in body["nodes"] if n["id"] == "p1")
    assert center["center"] is True and center["label"] == "Jane"
    assert body["links"] == [{"source": "p1", "target": "c1", "rel": "OWNERSHIP"}]
    assert body["provenance"] == {"prov_source_id": "os"}


def test_feed_and_search_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        dash_mod.queries, "recent_articles", lambda client, *, limit: [{"id": "a1", "title": "T"}]
    )
    monkeypatch.setattr(
        dash_mod.queries,
        "search_entities",
        lambda client, *, term, limit: [{"id": "p1", "label": "Jane", "labels": ["Entity"]}],
    )
    client = _client()
    assert client.get("/api/dashboard/feed").json()["articles"][0]["title"] == "T"
    assert (
        client.get("/api/dashboard/search", params={"q": "jane"}).json()["results"][0]["id"] == "p1"
    )


def test_limit_over_cap_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row cap (DASHBOARD_RESULT_LIMIT) is enforced at the API boundary."""
    monkeypatch.setattr(dash_mod.queries, "recent_articles", lambda client, *, limit: [])
    assert _client().get("/api/dashboard/feed", params={"limit": 10_000}).status_code == 422


def test_search_requires_query() -> None:
    """``q`` is required and non-empty (min_length=1)."""
    assert _client().get("/api/dashboard/search").status_code == 422
