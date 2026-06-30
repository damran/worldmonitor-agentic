"""Integration tests — Phase-3 Gate S1: MCP HTTP transport end-to-end (§4d).

Stand up ``streamable_http_app()`` with the real ``ZitadelMCPTokenVerifier`` adapter
over an in-proc fake verifier (no live Zitadel, no network).  Send real HTTP
``tools/call`` requests and verify the full wiring:

    BearerAuthBackend → RequireAuthMiddleware → StreamableHTTPASGIApp → tool → result

Tests:
    (i)  no Authorization header → HTTP 401 before any tool body runs
    (ii) valid Bearer + worldmonitor:graph-read role → HTTP 200, result carries prov_*
    (iii) garbage Bearer token → HTTP 401 before tool body runs
    (iv) valid Bearer WITHOUT role → HTTP 403 insufficient_scope before tool runs

Docker/local HTTP is available in this environment; all tests run locally, not CI-only.

BUILDER CONTRACT (names implementation MUST match):
    worldmonitor.mcp.auth.ZitadelMCPTokenVerifier(verifier)
    worldmonitor.mcp.auth.build_auth_settings(*, issuer_url, resource_server_url=None)
    worldmonitor.mcp.server.build_http_app(*, neo4j_client=None, token_verifier) -> Starlette

REAL SDK SYMBOLS:
    mcp.server.auth.settings                AuthSettings
    starlette.testclient                    TestClient

RED TODAY:
    ModuleNotFoundError: No module named 'worldmonitor.mcp.auth'
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Mapping
from typing import Any

import pytest
from starlette.testclient import TestClient

from worldmonitor.authz.oidc import InvalidTokenError

# ── imports that FAIL until builder delivers mcp/auth.py + server.build_http_app ─────
from worldmonitor.mcp.auth import ZitadelMCPTokenVerifier, build_auth_settings  # noqa: F401
from worldmonitor.mcp.server import build_http_app  # noqa: F401

pytestmark = pytest.mark.integration

# ── constants ──────────────────────────────────────────────────────────────────────────
_ZITADEL_ROLE_CLAIM = "urn:zitadel:iam:org:project:roles"
_WM_ROLE = "worldmonitor:graph-read"
_WM_SCOPE = "worldmonitor:read"
_ISSUER_URL = "https://issuer.integration.test"
_RESOURCE_URL = "https://mcp.integration.test"

VALID_WITH_ROLE_TOKEN = "integ-valid-role-tok"
VALID_NO_ROLE_TOKEN = "integ-valid-norole-tok"
GARBAGE_TOKEN = "integ-garbage-tok"

# The entity returned by the recording fake — must include prov_* fields.
_ENTITY_WITH_PROV = {
    "id": "Q42",
    "name": ["Integration Test Entity"],
    "prov_source_id": "opensanctions:test",
    "prov_retrieved_at": "2026-01-01T00:00:00Z",
    "prov_reliability": "A",
    "prov_source_record": "s3://landing/integration/test.json",
}


# ── deterministic in-proc fake verifier (no network) ─────────────────────────────────


class _FakeZitadelVerifier:
    """Sync fake implementing worldmonitor.authz.oidc.TokenVerifier for integration tests."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token == VALID_WITH_ROLE_TOKEN:
            return {
                "sub": "hermes-integ-svc",
                "exp": int(time.time()) + 3600,
                _ZITADEL_ROLE_CLAIM: {_WM_ROLE: {"orgId": "integ-org"}},
            }
        if token == VALID_NO_ROLE_TOKEN:
            return {
                "sub": "hermes-integ-svc",
                "exp": int(time.time()) + 3600,
                _ZITADEL_ROLE_CLAIM: {},
            }
        raise InvalidTokenError(f"unknown token in integration test: {token!r}")


# ── recording Neo4j fake ──────────────────────────────────────────────────────────────


class _RecordingFake:
    """Duck-typed Neo4jClient: records reads, returns fixture entity, forbids writes."""

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.read_calls.append((query, params))
        # get_entity — return the fixture entity with prov_*
        if "properties(n) AS props" in query and "properties(m) AS props" not in query:
            return [{"props": dict(_ENTITY_WITH_PROV)}]
        # get_neighbors — empty result
        if "properties(m) AS props" in query:
            return []
        # get_provenance
        if "STARTS WITH 'prov_'" in query:
            return [
                {"prov": [[k, v] for k, v in _ENTITY_WITH_PROV.items() if k.startswith("prov_")]}
            ]
        return []

    def execute_write(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        raise AssertionError("read tool must NEVER call execute_write")

    def session(self) -> Any:
        raise AssertionError("read tool must NEVER open a write session")

    def verify(self) -> None:
        raise AssertionError("build_http_app must not connect an injected client")


# ── shared fixture ────────────────────────────────────────────────────────────────────


@pytest.fixture
def http_app_and_fake() -> tuple[Any, _RecordingFake]:
    """Build the real streamable_http_app() with the ZitadelMCPTokenVerifier adapter.

    Function-scoped (a fresh app per test): a streamable-HTTP ``StreamableHTTPSessionManager``
    may only run once per instance, and each test enters its own ``TestClient`` lifespan — so a
    shared module-scoped app would fail the second test's lifespan startup. A fresh app also
    gives each test a clean ``read_calls`` ledger (no cross-test contamination).
    """
    fake_neo4j = _RecordingFake()
    mcp_verifier = ZitadelMCPTokenVerifier(_FakeZitadelVerifier())
    app = build_http_app(neo4j_client=fake_neo4j, token_verifier=mcp_verifier)
    return app, fake_neo4j


def _parse_mcp_result(resp_text: str) -> list[dict[str, Any]]:
    """Parse MCP HTTP response (SSE format: 'event: message\\ndata: {JSON}\\n\\n')."""
    results = []
    for line in resp_text.splitlines():
        if line.startswith("data: "):
            with contextlib.suppress(ValueError, KeyError):
                results.append(json.loads(line[6:]))
    return results


def _call_tool(
    client: TestClient,
    *,
    tool_name: str,
    arguments: dict[str, Any],
    bearer_token: str | None,
) -> Any:
    """Send a tools/call MCP request over HTTP and return the raw response."""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if bearer_token is not None:
        headers["Authorization"] = f"Bearer {bearer_token}"
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        headers=headers,
    )


# ── integration tests ─────────────────────────────────────────────────────────────────


def test_no_auth_header_returns_401(http_app_and_fake: tuple[Any, _RecordingFake]) -> None:
    """(i) No Authorization header → HTTP 401 before any tool body runs (INV-S1-AUTH).

    The auth middleware rejects the request entirely; the tool body (and therefore
    Neo4j execute_read) must NEVER be reached.  This is the unauthenticated-port guard.
    """
    app, fake_neo4j = http_app_and_fake
    fake_neo4j.read_calls.clear()

    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8000") as client:
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "get_entity", "arguments": {"entity_id": "Q42"}},
            },
            headers={"Content-Type": "application/json"},
        )

    assert resp.status_code == 401, (
        f"no Authorization header must yield 401; got {resp.status_code}, {resp.text!r}"
    )
    body = resp.json()
    assert "error" in body, f"401 body must carry 'error' key; got {body!r}"
    # Tool body was never reached — no Neo4j call.
    assert fake_neo4j.read_calls == [], "execute_read must NOT be called when auth rejects at 401"


def test_garbage_token_returns_401_before_tool_runs(
    http_app_and_fake: tuple[Any, _RecordingFake],
) -> None:
    """A garbage Bearer token → 401; tool body not entered (INV-S1-AUTH)."""
    app, fake_neo4j = http_app_and_fake
    fake_neo4j.read_calls.clear()

    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8000") as client:
        resp = _call_tool(
            client,
            tool_name="get_entity",
            arguments={"entity_id": "Q42"},
            bearer_token=GARBAGE_TOKEN,
        )

    assert resp.status_code == 401, (
        f"garbage bearer must yield 401; got {resp.status_code}, {resp.text!r}"
    )
    body_text = resp.text
    # No-leak: garbage token bytes must not appear in the response body.
    assert GARBAGE_TOKEN not in body_text, f"garbage token leaked into 401 body: {body_text!r}"
    assert fake_neo4j.read_calls == [], (
        "execute_read must NOT be called when bearer token is invalid"
    )


def test_valid_bearer_without_role_returns_403(
    http_app_and_fake: tuple[Any, _RecordingFake],
) -> None:
    """Valid bearer WITHOUT worldmonitor:graph-read → 403 insufficient_scope (INV-S1-ROLE).

    The verifier authenticates the principal but RequireAuthMiddleware blocks it because
    the scope mapping found no worldmonitor:read scope for this token.
    """
    app, fake_neo4j = http_app_and_fake
    fake_neo4j.read_calls.clear()

    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8000") as client:
        resp = _call_tool(
            client,
            tool_name="get_entity",
            arguments={"entity_id": "Q42"},
            bearer_token=VALID_NO_ROLE_TOKEN,
        )

    assert resp.status_code == 403, (
        f"valid bearer without role must yield 403; got {resp.status_code}, {resp.text!r}"
    )
    body = resp.json()
    assert body.get("error") == "insufficient_scope", (
        f"403 error field must be 'insufficient_scope'; got {body!r}"
    )
    # No-leak: the token itself and claim values must not appear in the 403 body.
    body_text = resp.text
    assert VALID_NO_ROLE_TOKEN not in body_text, (
        f"no-role token leaked into 403 body: {body_text!r}"
    )
    assert "Traceback" not in body_text, f"traceback in 403 body: {body_text!r}"
    assert fake_neo4j.read_calls == [], "execute_read must NOT be called when role is absent (403)"


def test_valid_bearer_with_role_returns_tool_result_with_prov_star(
    http_app_and_fake: tuple[Any, _RecordingFake],
) -> None:
    """(ii) Valid Bearer + worldmonitor:graph-read → 200 + get_entity carries prov_*.

    This is the end-to-end wiring confirmation:
        ZitadelMCPTokenVerifier → BearerAuthBackend → RequireAuthMiddleware
        → StreamableHTTPASGIApp → tool_get_entity → recording fake → result

    The result must carry the prov_* provenance fields (G1 carried invariant: provenance
    on every node the read surface returns).
    """
    app, fake_neo4j = http_app_and_fake
    fake_neo4j.read_calls.clear()

    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8000") as client:
        resp = _call_tool(
            client,
            tool_name="get_entity",
            arguments={"entity_id": "Q42"},
            bearer_token=VALID_WITH_ROLE_TOKEN,
        )

    assert resp.status_code == 200, (
        f"valid+role bearer must yield 200; got {resp.status_code}, body={resp.text[:300]!r}"
    )

    # The MCP HTTP response body is SSE-formatted: parse the JSON-RPC result frame.
    frames = _parse_mcp_result(resp.text)
    assert frames, f"no MCP response frames parsed from: {resp.text!r}"

    # Find the result frame (id=1).
    result_frame = next((f for f in frames if f.get("id") == 1), None)
    assert result_frame is not None, f"no frame with id=1 in {frames!r}"
    assert "error" not in result_frame, (
        f"tool/call must not return a top-level JSON-RPC error for a valid entity: {result_frame!r}"
    )

    # Extract text content blocks from the MCP result and decode JSON payloads.
    result = result_frame.get("result", {})
    content_blocks = result.get("content", [])
    decoded_payloads: list[dict[str, Any]] = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            with contextlib.suppress(ValueError, KeyError):
                decoded_payloads.append(json.loads(block["text"]))

    # At least one payload must carry prov_source_id (G1 provenance invariant).
    has_prov = any(
        isinstance(payload, dict) and "prov_source_id" in payload for payload in decoded_payloads
    )
    assert has_prov, (
        f"get_entity result must carry prov_source_id (G1 provenance-on-every-node); "
        f"decoded payloads: {decoded_payloads!r}, raw body: {resp.text[:500]!r}"
    )

    # Also verify prov_retrieved_at is present (full provenance set, not just source_id).
    has_retrieved_at = any(
        isinstance(payload, dict) and "prov_retrieved_at" in payload for payload in decoded_payloads
    )
    assert has_retrieved_at, (
        f"get_entity result must carry prov_retrieved_at; payloads: {decoded_payloads!r}"
    )

    # The recording fake's execute_read was called → tool body was entered.
    assert fake_neo4j.read_calls, "execute_read must have been called for a successful tool/call"


def test_www_authenticate_header_present_on_401(
    http_app_and_fake: tuple[Any, _RecordingFake],
) -> None:
    """401 response carries WWW-Authenticate: Bearer header (RFC 6750 / INV-S1-NOLEAK).

    The header directs clients to use Bearer auth; it must not echo the invalid token.
    """
    app, _ = http_app_and_fake

    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1:8000") as client:
        resp = client.post(
            "/mcp",
            headers={"Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert resp.status_code == 401
    www_auth = resp.headers.get("www-authenticate", "")
    assert www_auth.lower().startswith("bearer"), (
        f"401 must carry 'WWW-Authenticate: Bearer ...' header; got: {www_auth!r}"
    )
    # The WWW-Authenticate header itself must not echo the (absent) token.
    assert "Traceback" not in www_auth
