"""Consumption dashboard SPA is served, public, and CDN-free (ADR 0115, Slice D).

The vendored single-page app is mounted at ``/app`` (public per the Slice-A carve-out) and must load
only self-hosted assets — a strict no-CDN posture (data sovereignty + offline-capable), matching how
``htmx.min.js`` is already vendored.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from worldmonitor.api.main import create_app
from worldmonitor.settings import Settings


class _FakeNeo4j:
    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("the SPA serve tests never touch the graph")


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
    app = create_app(settings=_settings(), verifier=None, neo4j_client=_FakeNeo4j())  # type: ignore[arg-type]
    return TestClient(app, raise_server_exceptions=False)


def test_spa_index_served_publicly() -> None:
    resp = _client().get("/app/")
    assert resp.status_code == 200, resp.text
    assert "WORLD" in resp.text.upper()
    assert "app.js" in resp.text  # loads the vendored app bundle


def test_spa_assets_are_public() -> None:
    client = _client()
    for path in (
        "/app/app.js",
        "/app/app.css",
        "/app/vendor/globe.gl.min.js",
        "/app/vendor/force-graph.min.js",
        "/app/vendor/countries.geojson",  # Natural Earth 110m outlines (public domain, vendored)
    ):
        assert client.get(path).status_code == 200, f"{path} must be served publicly"


def test_spa_entry_points_are_cdn_free() -> None:
    """index.html and app.js reference no external host — only vendored + relative + our API."""
    client = _client()
    for path in ("/app/", "/app/app.js"):
        body = client.get(path).text.lower()
        for banned in ("http://", "https://", "unpkg", "jsdelivr", "cdn."):
            assert banned not in body, f"{path} must be CDN-free but contains {banned!r}"
