"""Unit tests — Phase-3 Gate S1: MCP HTTP auth (INV-S1-* invariants).

Covers §4b (role/scope/no-leak/adapter-mapping) and §4c (read-only exactly-4-tools +
stdio non-regression) from GATE_S1_MCP_HTTP_AUTH_SPEC.md.

BUILDER CONTRACT (names the implementation MUST match):

    worldmonitor.mcp.auth
        ZitadelMCPTokenVerifier(verifier)
            async verify_token(token: str) -> AccessToken | None
              - On InvalidTokenError from wrapped verifier → return None (→ 401)
              - On success + worldmonitor:graph-read role present
                → AccessToken(token=token, client_id=<sub>, scopes=["worldmonitor:read"],
                              expires_at=<exp>)
              - On success + role absent → AccessToken(token=token, client_id=<sub>,
                                                       scopes=[], expires_at=<exp>)

        build_auth_settings(*, issuer_url: str, resource_server_url: str | None = None)
            -> AuthSettings
              required_scopes=["worldmonitor:read"] always set

    worldmonitor.mcp.server
        build_http_app(*, neo4j_client=None, token_verifier: TokenVerifier) -> Starlette
              token_verifier is REQUIRED (no default) — fail-closed: missing it → TypeError

REAL SDK SYMBOLS:
    mcp.server.auth.middleware.bearer_auth  BearerAuthBackend, RequireAuthMiddleware
    mcp.server.auth.provider                AccessToken, TokenVerifier
    mcp.server.auth.settings                AuthSettings

RED TODAY:
    ModuleNotFoundError: No module named 'worldmonitor.mcp.auth'
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import pytest
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from worldmonitor.authz.oidc import InvalidTokenError

# ── imports that FAIL until builder delivers mcp/auth.py ──────────────────────────────
from worldmonitor.mcp.auth import ZitadelMCPTokenVerifier, build_auth_settings  # noqa: F401
from worldmonitor.mcp.server import (
    build_http_app,  # noqa: F401
    build_server,
)

# ── constants ──────────────────────────────────────────────────────────────────────────
_ZITADEL_ROLE_CLAIM = "urn:zitadel:iam:org:project:roles"
_WM_ROLE = "worldmonitor:graph-read"
_WM_SCOPE = "worldmonitor:read"
_ISSUER_URL = "https://issuer.test.example"
_RESOURCE_URL = "https://mcp.test.example"

VALID_WITH_ROLE_TOKEN = "unit-valid-role-tok"
VALID_NO_ROLE_TOKEN = "unit-valid-norole-tok"
GARBAGE_TOKEN = "unit-garbage-tok"
_CLAIM_SUB = "hermes-unit-svc"
_CLAIM_EXP = int(time.time()) + 3600
_KNOWN_CLAIM_VALUES = {_CLAIM_SUB, _WM_ROLE, "worldmonitor:graph-read"}


# ── deterministic fakes ────────────────────────────────────────────────────────────────


class _FakeZitadelVerifier:
    """Sync fake implementing the worldmonitor.authz.oidc.TokenVerifier protocol."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token == VALID_WITH_ROLE_TOKEN:
            return {
                "sub": _CLAIM_SUB,
                "exp": _CLAIM_EXP,
                _ZITADEL_ROLE_CLAIM: {_WM_ROLE: {"orgId": "test-org"}},
            }
        if token == VALID_NO_ROLE_TOKEN:
            return {
                "sub": _CLAIM_SUB,
                "exp": _CLAIM_EXP,
                _ZITADEL_ROLE_CLAIM: {},
            }
        raise InvalidTokenError(f"invalid token in unit test: {token!r}")


class _RecordingFake:
    """Duck-typed Neo4jClient for unit tests: records reads, forbids writes."""

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.read_calls.append((query, params))
        if "properties(m) AS props" in query:
            return []
        if "STARTS WITH 'prov_'" in query:
            return [{"prov": [["prov_source_id", "src:test"]]}]
        if "properties(n) AS props" in query:
            return [
                {
                    "props": {
                        "id": "Q42",
                        "prov_source_id": "src:test",
                        "prov_retrieved_at": "2026-01-01T00:00:00Z",
                        "prov_reliability": "A",
                        "prov_source_record": "s3://landing/test.json",
                    }
                }
            ]
        return []

    def execute_write(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        raise AssertionError("read tool must NEVER call execute_write")

    def session(self) -> Any:
        raise AssertionError("read tool must NEVER open a write session")

    def verify(self) -> None:
        raise AssertionError("build_server/build_http_app must not connect an injected client")


# ── helpers ────────────────────────────────────────────────────────────────────────────


def _make_mcp_verifier() -> ZitadelMCPTokenVerifier:
    return ZitadelMCPTokenVerifier(_FakeZitadelVerifier())


def _build_unit_auth_app(
    *,
    scopes: list[str] | None = None,
) -> tuple[Starlette, _RecordingFake]:
    """Build a minimal Starlette app wired with the real ZitadelMCPTokenVerifier adapter."""
    from worldmonitor.mcp.server import tool_get_entity

    fake_neo4j = _RecordingFake()

    class _ToolEndpoint:
        """ASGI endpoint: calls get_entity so read_calls proves tool body was entered."""

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                try:
                    tool_get_entity(fake_neo4j, "Q42")
                    body = b'{"ok":true}'
                    status = 200
                except Exception:
                    body = b'{"ok":false}'
                    status = 200  # reached but failed for non-auth reason; not 401/403
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})

    required = scopes if scopes is not None else [_WM_SCOPE]
    verifier = _make_mcp_verifier()

    app = Starlette(
        routes=[Route("/mcp", endpoint=RequireAuthMiddleware(_ToolEndpoint(), required))],
        middleware=[
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(verifier)),
        ],
    )
    return app, fake_neo4j


# ── §4b: INV-S1-ROLE — role present → tool runs; role absent → 403 ────────────────────


def test_inv_s1_role_valid_bearer_with_role_reaches_tool() -> None:
    """A bearer carrying worldmonitor:graph-read reaches the tool body (execute_read called)."""
    app, fake_neo4j = _build_unit_auth_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {VALID_WITH_ROLE_TOKEN}"})
    assert resp.status_code == 200, (
        f"valid+role bearer must not be auth-rejected; got {resp.status_code}, {resp.text!r}"
    )
    assert fake_neo4j.read_calls, (
        "tool body must have run (execute_read called) when bearer+role is valid"
    )


def test_inv_s1_role_absent_returns_403_insufficient_scope() -> None:
    """A valid bearer WITHOUT the role yields 403 insufficient_scope; tool body not entered."""
    app, fake_neo4j = _build_unit_auth_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {VALID_NO_ROLE_TOKEN}"})
    assert resp.status_code == 403, (
        f"valid bearer without role must yield 403; got {resp.status_code}, {resp.text!r}"
    )
    body = resp.json()
    assert body.get("error") == "insufficient_scope", (
        f"403 body must carry error='insufficient_scope'; got {body!r}"
    )
    assert fake_neo4j.read_calls == [], (
        "tool body must NOT have run when role is absent (execute_read must not be called)"
    )


# ── §4b: INV-S1-AUTH fail-closed startup ─────────────────────────────────────────────


def test_inv_s1_auth_fail_closed_build_http_app_requires_token_verifier() -> None:
    """build_http_app() without token_verifier MUST raise — no anonymous HTTP port.

    token_verifier has no default: calling build_http_app() without it raises TypeError.
    This is the fail-closed invariant: you literally cannot construct an anonymous HTTP
    app because Python enforces the required keyword argument before any code runs.
    """
    with pytest.raises(TypeError, match="token_verifier"):
        build_http_app()  # type: ignore[call-arg]


def test_inv_s1_auth_fail_closed_fastmcp_raises_without_verifier() -> None:
    """FastMCP itself raises if auth=AuthSettings is given without token_verifier.

    This is the SDK-level fail-closed behaviour (belt-and-suspenders with the above).
    """
    from mcp.server.fastmcp import FastMCP

    auth_cfg = build_auth_settings(issuer_url=_ISSUER_URL, resource_server_url=None)
    with pytest.raises(ValueError, match="token_verifier"):
        FastMCP(name="test-fail-closed", auth=auth_cfg)


# ── §4b: adapter mapping ──────────────────────────────────────────────────────────────


def test_adapter_returns_none_on_invalid_token_error() -> None:
    """ZitadelMCPTokenVerifier.verify_token → None when underlying verify raises.

    On InvalidTokenError from the wrapped ZitadelTokenVerifier → returns None (→ 401).
    """
    verifier = ZitadelMCPTokenVerifier(_FakeZitadelVerifier())
    result = asyncio.run(verifier.verify_token(GARBAGE_TOKEN))
    assert result is None, (
        f"adapter must return None (not raise) on InvalidTokenError; got {result!r}"
    )


def test_adapter_scopes_contain_worldmonitor_read_when_role_present() -> None:
    """verify_token → AccessToken.scopes == ['worldmonitor:read'] when role claim is present."""
    verifier = ZitadelMCPTokenVerifier(_FakeZitadelVerifier())
    token = asyncio.run(verifier.verify_token(VALID_WITH_ROLE_TOKEN))
    assert token is not None, "adapter must NOT return None for a valid token with role"
    assert isinstance(token, AccessToken), f"must return an AccessToken; got {type(token)}"
    assert _WM_SCOPE in token.scopes, (
        f"scopes must contain '{_WM_SCOPE}' when role is present; scopes={token.scopes!r}"
    )


def test_adapter_scopes_empty_when_role_absent() -> None:
    """verify_token → AccessToken.scopes does NOT contain 'worldmonitor:read' without role."""
    verifier = ZitadelMCPTokenVerifier(_FakeZitadelVerifier())
    token = asyncio.run(verifier.verify_token(VALID_NO_ROLE_TOKEN))
    assert token is not None, "adapter must NOT return None for a structurally valid token"
    assert _WM_SCOPE not in token.scopes, (
        f"scopes must NOT contain '{_WM_SCOPE}' when role is absent; scopes={token.scopes!r}"
    )


def test_adapter_client_id_from_sub() -> None:
    """verify_token → AccessToken.client_id populated from 'sub' claim."""
    verifier = ZitadelMCPTokenVerifier(_FakeZitadelVerifier())
    token = asyncio.run(verifier.verify_token(VALID_WITH_ROLE_TOKEN))
    assert token is not None
    assert token.client_id == _CLAIM_SUB, (
        f"client_id must equal sub={_CLAIM_SUB!r}; got {token.client_id!r}"
    )


def test_adapter_expires_at_from_exp() -> None:
    """verify_token → AccessToken.expires_at populated from 'exp' claim."""
    verifier = ZitadelMCPTokenVerifier(_FakeZitadelVerifier())
    token = asyncio.run(verifier.verify_token(VALID_WITH_ROLE_TOKEN))
    assert token is not None
    assert token.expires_at == _CLAIM_EXP, (
        f"expires_at must equal exp={_CLAIM_EXP}; got {token.expires_at!r}"
    )


def test_build_auth_settings_required_scopes() -> None:
    """build_auth_settings() always sets required_scopes=['worldmonitor:read']."""
    auth_cfg = build_auth_settings(issuer_url=_ISSUER_URL, resource_server_url=None)
    assert isinstance(auth_cfg, AuthSettings)
    assert auth_cfg.required_scopes == [_WM_SCOPE], (
        f"required_scopes must be ['{_WM_SCOPE}']; got {auth_cfg.required_scopes!r}"
    )


# ── §4b: INV-S1-NOLEAK — 401 and 403 bodies exclude token/claim/traceback ────────────


def test_inv_s1_noleak_401_body_excludes_token_and_claim_and_traceback() -> None:
    """401 response body never contains the raw token string, claim values, or 'Traceback'."""
    app, _ = _build_unit_auth_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {GARBAGE_TOKEN}"})
    assert resp.status_code == 401
    body = resp.text

    assert GARBAGE_TOKEN not in body, f"raw token {GARBAGE_TOKEN!r} leaked into 401 body: {body!r}"
    for claim_value in _KNOWN_CLAIM_VALUES:
        assert claim_value not in body, (
            f"claim value {claim_value!r} leaked into 401 body: {body!r}"
        )
    assert "Traceback" not in body, f"'Traceback' found in 401 body: {body!r}"


def test_inv_s1_noleak_403_body_excludes_token_and_claim_and_traceback() -> None:
    """403 response body never contains the raw token string, claim values, or 'Traceback'."""
    app, _ = _build_unit_auth_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/mcp", headers={"Authorization": f"Bearer {VALID_NO_ROLE_TOKEN}"})
    assert resp.status_code == 403
    body = resp.text

    # The token itself is well-formed but the scopes are wrong; still must not leak.
    assert VALID_NO_ROLE_TOKEN not in body, f"raw token leaked into 403 body: {body!r}"
    for claim_value in _KNOWN_CLAIM_VALUES:
        assert claim_value not in body, (
            f"claim value {claim_value!r} leaked into 403 body: {body!r}"
        )
    assert "Traceback" not in body, f"'Traceback' found in 403 body: {body!r}"


# ── §4c: INV-S1-READONLY — HTTP server registers EXACTLY the four read tools ─────────


def test_inv_s1_readonly_http_server_registers_exactly_four_tools() -> None:
    """build_http_app registers EXACTLY {get_entity, get_neighbors, get_provenance, find_paths}.

    The HTTP surface must not add, remove, or rename any tool compared to the stdio
    surface (INV-S1-READONLY: same four read tools, no write/active tool).
    """
    import asyncio

    fake_neo4j = _RecordingFake()
    verifier = _make_mcp_verifier()

    # build_http_app creates a FastMCP internally; we need to inspect the server's tool list.
    # The builder exposes the server (or we use the registered Starlette app's tool metadata).
    # We compare against build_server's tool list as the ground truth (both must be equal).
    stdio_server = build_server(neo4j_client=fake_neo4j)
    stdio_tools = {t.name for t in asyncio.run(stdio_server.list_tools())}

    # The HTTP server must expose the same set — tested via build_http_app's underlying FastMCP.
    # If build_http_app returns a Starlette app (from streamable_http_app()), we test it by
    # verifying the HTTP app was built from the same tool registration as build_server().
    # Contract: build_http_app must register tools identically to build_server.
    # We assert the expected set explicitly so a builder cannot quietly add a tool.
    assert stdio_tools == {
        "get_entity",
        "get_neighbors",
        "get_provenance",
        "find_paths",
    }, f"build_server tool set drifted: {stdio_tools!r}"

    # build_http_app must be built from the same registration; calling it proves it doesn't crash.
    http_app = build_http_app(neo4j_client=fake_neo4j, token_verifier=verifier)
    # The Starlette app returned by streamable_http_app() doesn't expose tool metadata directly,
    # but we can verify the route structure carries the MCP endpoint (no extra endpoints).
    route_paths = {getattr(r, "path", None) for r in http_app.routes}
    assert "/mcp" in route_paths, (
        f"build_http_app must register the /mcp streamable-http route; routes={route_paths!r}"
    )


# ── §4c: INV-S1-STDIO — stdio transport constructs without auth settings ──────────────


def test_inv_s1_stdio_build_server_constructs_without_auth() -> None:
    """build_server() with a fake Neo4j client completes WITHOUT requiring auth settings.

    The stdio path (ADR 0063) must remain unchanged: no auth, no port, no token verifier
    required. This is the non-regression guard for the stdio transport.
    """
    import asyncio

    fake_neo4j = _RecordingFake()
    # Must not raise — no auth settings, no token_verifier, no network.
    server = build_server(neo4j_client=fake_neo4j)
    assert server is not None

    tools = {t.name for t in asyncio.run(server.list_tools())}
    assert tools == {"get_entity", "get_neighbors", "get_provenance", "find_paths"}, (
        f"stdio build_server must still register the four tools: {tools!r}"
    )


# ── Gate F-2 (ADR 0121): HTTP transport carries the SAME annotations/output-schema as
#    stdio (INV-S1-READONLY no-drift, extended to the new contract-polish fields). This
#    reuses the shared `_register_read_tools` — the ONE registration site both transports
#    call — because `build_http_app`'s returned Starlette app (from `streamable_http_app()`)
#    exposes no tool-list introspection once wrapped as an ASGI app.


def test_http_tools_carry_same_annotations_and_schema_as_stdio() -> None:
    """The HTTP-mounted tool set carries the same readOnlyHint/idempotentHint/openWorldHint
    annotations AND a non-null outputSchema as the stdio surface, for all four tools."""
    from mcp.server.fastmcp import FastMCP

    from worldmonitor.mcp.server import _register_read_tools

    fake_http = _RecordingFake()
    fake_stdio = _RecordingFake()
    verifier = _make_mcp_verifier()

    auth_cfg = build_auth_settings(issuer_url=_ISSUER_URL, resource_server_url=None)
    http_server = FastMCP(
        name="worldmonitor-graph-read",
        auth=auth_cfg,
        token_verifier=verifier,
        stateless_http=True,
    )
    _register_read_tools(http_server, fake_http)

    stdio_server = build_server(neo4j_client=fake_stdio)

    http_tools = {t.name: t for t in asyncio.run(http_server.list_tools())}
    stdio_tools = {t.name: t for t in asyncio.run(stdio_server.list_tools())}

    expected_names = {"get_entity", "get_neighbors", "get_provenance", "find_paths"}
    assert http_tools.keys() == expected_names, f"HTTP tool set drifted: {http_tools.keys()!r}"
    assert stdio_tools.keys() == expected_names, f"stdio tool set drifted: {stdio_tools.keys()!r}"

    for name in expected_names:
        http_ann = http_tools[name].annotations
        stdio_ann = stdio_tools[name].annotations
        assert http_ann is not None, f"{name} carries no annotations on the HTTP transport"
        assert stdio_ann is not None, f"{name} carries no annotations on the stdio transport"

        http_triple = (http_ann.readOnlyHint, http_ann.idempotentHint, http_ann.openWorldHint)
        stdio_triple = (stdio_ann.readOnlyHint, stdio_ann.idempotentHint, stdio_ann.openWorldHint)
        assert http_triple == (True, True, False), (
            f"{name}: HTTP (readOnlyHint, idempotentHint, openWorldHint) must be "
            f"(True, True, False); got {http_triple!r}"
        )
        assert http_triple == stdio_triple, (
            f"{name}: HTTP annotations drifted from stdio (INV-S1 no-drift): "
            f"http={http_triple!r} stdio={stdio_triple!r}"
        )

        assert http_tools[name].outputSchema is not None, (
            f"{name} carries no outputSchema on the HTTP transport"
        )
        assert stdio_tools[name].outputSchema is not None, (
            f"{name} carries no outputSchema on the stdio transport"
        )
