"""Dashboard AI-brief endpoint (ADR 0115, Slice E).

Public, server-controlled-egress brief with citation receipts + a TTL cache so a public endpoint
can't be spammed into one LLM call per request. No Ollama — an injected fake gateway.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from worldmonitor.api import dashboard as dash_mod
from worldmonitor.api.main import create_app
from worldmonitor.llm.gateway import LLMGatewayError
from worldmonitor.settings import Settings


class _FakeNeo4j:
    def execute_read(self, *a: Any, **k: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("query helpers are patched in these tests")


class _FakeGateway:
    def __init__(self, content: str = "The situation is calm.", error: bool = False) -> None:
        self.content = content
        self.error = error
        self.calls = 0

    def chat(self, messages: Any, *, caller_tag: str = "gateway", **kw: Any) -> Any:
        self.calls += 1
        if self.error:
            raise LLMGatewayError("boom")
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = self.content
        return resp


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        zitadel_domain="",
        zitadel_client_id="",
        zitadel_client_secret="",
        app_base_url="",
    )  # type: ignore[arg-type]


def _client(gateway: _FakeGateway) -> TestClient:
    app = create_app(
        settings=_settings(),
        verifier=None,
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
        llm_gateway=gateway,  # type: ignore[arg-type]
    )
    return TestClient(app, raise_server_exceptions=False)


def _patch(monkeypatch: pytest.MonkeyPatch, *, events: list[dict], articles: list[dict]) -> None:
    monkeypatch.setattr(dash_mod.queries, "recent_events", lambda client, *, limit: events)
    monkeypatch.setattr(dash_mod.queries, "recent_articles", lambda client, *, limit: articles)


def test_brief_returns_text_and_citation_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(
        monkeypatch,
        events=[
            {
                "name": "Quake",
                "country": "tr",
                "source_title": "Quake hits",
                "source_url": "https://n/e1",
            }
        ],
        articles=[{"title": "Markets fall", "url": "https://n/a1"}],
    )
    gw = _FakeGateway(content="A quake struck Turkey; markets fell.")
    resp = _client(gw).get("/api/dashboard/brief")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["brief"] == "A quake struck Turkey; markets fell."
    urls = {s["url"] for s in body["sources"]}
    assert urls == {"https://n/e1", "https://n/a1"}  # receipts from both events + articles


def test_brief_empty_graph_short_circuits_without_calling_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, events=[], articles=[])
    gw = _FakeGateway()
    body = _client(gw).get("/api/dashboard/brief").json()
    assert "No activity" in body["brief"]
    assert gw.calls == 0, "an empty graph must not spend an LLM call"


def test_brief_gateway_error_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, events=[], articles=[{"title": "x", "url": "https://n/a"}])
    resp = _client(_FakeGateway(error=True)).get("/api/dashboard/brief")
    assert resp.status_code == 502


def test_brief_is_cached_across_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, events=[], articles=[{"title": "x", "url": "https://n/a"}])
    gw = _FakeGateway()
    client = _client(gw)
    first = client.get("/api/dashboard/brief").json()
    second = client.get("/api/dashboard/brief").json()
    assert first == second
    assert gw.calls == 1, (
        "a public brief endpoint must serve from cache, not call the LLM per request"
    )
