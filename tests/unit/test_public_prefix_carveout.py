"""Open-read carve-out: the consumption dashboard subtree is public; everything else stays gated
(ADR 0115, Slice A).

The dashboard (JSON read API ``/api/dashboard/*`` + the vendored SPA ``/app`` + its ``/static``
assets) must be reachable WITHOUT Zitadel login, while the write/operator surface (``/me``,
``/integrations``, ``/v1/chat/completions``) stays auth-gated. These tests pin both halves: a
public prefix opens ONLY its own subtree (matched at a path-segment boundary, so ``/api/dashboard``
never opens ``/api/dashboardX``), and the gated surface is untouched.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from worldmonitor.api.main import create_app
from worldmonitor.api.middleware import AuthMiddleware
from worldmonitor.settings import Settings


class _FakeNeo4j:
    """Placeholder read client; the carve-out tests never touch the graph."""

    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("the graph client must not be used by the carve-out tests")


def _settings() -> Settings:
    # Auth UNCONFIGURED — the default self-hosted posture the dashboard ships in: the dashboard is
    # public, the gated surface fails closed. ``_env_file=None`` so a local ``.env`` cannot flip
    # auth on under our feet.
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


# --- Unit: the segment-boundary matcher itself --------------------------------------------------
@pytest.mark.parametrize(
    ("path", "public"),
    [
        ("/api/dashboard", True),
        ("/api/dashboard/events", True),
        ("/app", True),
        ("/app/index.html", True),
        ("/static/app.css", True),
        ("/api/dashboardX", False),  # no segment boundary -> must NOT be opened
        ("/api/dashboardevil/x", False),
        ("/appXYZ", False),
        ("/me", False),
        ("/integrations", False),
        ("/v1/chat/completions", False),
    ],
)
def test_is_public_anchors_at_segment_boundary(path: str, public: bool) -> None:
    middleware = AuthMiddleware(
        app=lambda *a, **k: None,  # unused by _is_public
        verifier=None,
        public_paths=frozenset({"/health"}),
        public_prefixes=frozenset({"/api/dashboard", "/app", "/static"}),
    )
    assert middleware._is_public(path) is public


# --- End-to-end: gated stays gated, public opens (via create_app) -------------------------------
def test_dashboard_prefix_bypasses_auth_when_unconfigured() -> None:
    """A request under a public prefix is NOT auth-blocked: with no route mounted yet it reaches
    routing and 404s (proving it passed the middleware) rather than 401."""
    resp = _client().get("/api/dashboard/events", headers={"Accept": "application/json"})
    assert resp.status_code != 401, "the dashboard read path must not be auth-gated"
    assert resp.status_code == 404, "no route is mounted in Slice A; the point is it wasn't blocked"


def test_static_assets_are_public() -> None:
    resp = _client().get("/static/app.css")
    assert resp.status_code == 200, "the SPA's static assets must be public"


def test_dashboard_lookalike_sibling_is_still_gated() -> None:
    """A look-alike sibling (no segment boundary) must NOT be opened by the prefix."""
    resp = _client().get(
        "/api/dashboardX", headers={"Accept": "application/json"}, follow_redirects=False
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("path", ["/me", "/integrations", "/v1/chat/completions"])
def test_operator_surface_stays_gated(path: str) -> None:
    resp = _client().get(path, headers={"Accept": "application/json"}, follow_redirects=False)
    assert resp.status_code == 401, f"{path} must remain auth-gated"
