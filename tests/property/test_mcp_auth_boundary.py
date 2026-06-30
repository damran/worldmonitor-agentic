"""PRIMARY property test — Phase-3 Gate S1: MCP HTTP auth boundary.

This is the ``@given`` oracle for INV-S1-AUTH + INV-S1-ROLE + INV-S1-NOLEAK +
clamp-survives-auth (GATE_S1_MCP_HTTP_AUTH_SPEC.md §4a).

BUILDER CONTRACT — the implementation MUST match these names exactly:

    worldmonitor.mcp.auth
        ZitadelMCPTokenVerifier(verifier)
            Wraps a ``worldmonitor.authz.oidc.TokenVerifier`` (sync ``verify(token)``)
            and adapts it to the SDK async protocol:
                async verify_token(token: str) -> AccessToken | None
            On ``InvalidTokenError`` → return None (→ 401).
            On success with ``urn:zitadel:iam:org:project:roles`` containing
            ``worldmonitor:graph-read`` → AccessToken(scopes=["worldmonitor:read"]).
            On success without that role → AccessToken(scopes=[]).
            Populates ``client_id`` from ``sub``, ``expires_at`` from ``exp``.

        build_auth_settings(*, issuer_url: str, resource_server_url: str | None = None)
            -> mcp.server.auth.settings.AuthSettings
            Returns AuthSettings with required_scopes=["worldmonitor:read"].

    worldmonitor.mcp.server
        build_http_app(*, neo4j_client=None, token_verifier: TokenVerifier) -> Starlette
            ``token_verifier`` has NO default — missing it raises TypeError (fail-closed).
            Returns server.streamable_http_app() wired with BearerAuthBackend +
            RequireAuthMiddleware. Registers exactly the same four read tools as
            build_server().

REAL SDK SYMBOLS USED (mcp==1.28.1):
    mcp.server.auth.middleware.bearer_auth  BearerAuthBackend, RequireAuthMiddleware
    mcp.server.auth.provider                AccessToken, TokenVerifier
    mcp.server.auth.settings                AuthSettings
    starlette.middleware.authentication     AuthenticationMiddleware

RED TODAY:
    ``ModuleNotFoundError: No module named 'worldmonitor.mcp.auth'``
    (mcp/auth.py and the HTTP transport entrypoint do not exist yet)
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Mapping
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.routing import Route
from starlette.testclient import TestClient

from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.graph.read_guards import HOP_CAP

# ── imports that MUST fail until the builder delivers mcp/auth.py + server entrypoint ──
from worldmonitor.mcp.auth import ZitadelMCPTokenVerifier, build_auth_settings  # noqa: F401
from worldmonitor.mcp.server import (
    build_http_app,  # noqa: F401
    tool_find_paths,
    tool_get_neighbors,
)

# ── constants ──────────────────────────────────────────────────────────────────────────
# Exactly two known-good tokens; the fake verifier returns None for everything else.
VALID_WITH_ROLE_TOKEN = "valid-with-role-sentinel-xyz"
VALID_NO_ROLE_TOKEN = "valid-no-role-sentinel-abc"

# Zitadel project-role claim path (from ADR 0090 and the spec §7).
_ZITADEL_ROLE_CLAIM = "urn:zitadel:iam:org:project:roles"
_WM_ROLE = "worldmonitor:graph-read"
_WM_SCOPE = "worldmonitor:read"

# Claim values that must NEVER appear verbatim in error responses (no-leak).
_CLAIM_SUB = "hermes-test-svc"
_TRAVERSAL_BOUND = re.compile(r"\.\.(\d+)")

_SETTINGS = settings(
    max_examples=200,
    deadline=None,  # per repo convention: setup can be slow on a busy runner
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

_CLAMP_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ── deterministic fake verifier (no network, no JWKS) ─────────────────────────────────


class _FakeZitadelVerifier:
    """Sync ``worldmonitor.authz.oidc.TokenVerifier``: maps known tokens, rejects others."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token == VALID_WITH_ROLE_TOKEN:
            return {
                "sub": _CLAIM_SUB,
                "exp": int(time.time()) + 3600,
                _ZITADEL_ROLE_CLAIM: {_WM_ROLE: {"orgId": "test-org-1"}},
            }
        if token == VALID_NO_ROLE_TOKEN:
            return {
                "sub": _CLAIM_SUB,
                "exp": int(time.time()) + 3600,
                _ZITADEL_ROLE_CLAIM: {},
            }
        raise InvalidTokenError(f"unknown/invalid token for test: {token!r}")


# ── spy ASGI app ───────────────────────────────────────────────────────────────────────


class _SpyASGI:
    """Minimal ASGI callable: returns HTTP 200.  If reached, auth passed the chain."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            body = b'{"reached":true}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})


# ── recording Neo4j fake (spy for clamp test) ─────────────────────────────────────────


class _RecordingFake:
    def __init__(self) -> None:
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.read_calls.append((query, params))
        if "properties(m) AS props" in query:
            return []  # get_neighbors: empty neighbour set
        return []  # find_paths: no paths

    def execute_write(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        raise AssertionError("a read tool must NEVER call execute_write")

    def session(self) -> Any:
        raise AssertionError("a read tool must NEVER open a write session")


# ── shared test app (module-level, reused across all property examples) ────────────────
# Built at import time so the ModuleNotFoundError (missing worldmonitor.mcp.auth)
# surfaces at collection time, not deferred until first @given execution.


def _build_auth_boundary_app() -> Starlette:
    """BearerAuthBackend(ZitadelMCPTokenVerifier(fake)) → RequireAuthMiddleware → spy."""
    fake = _FakeZitadelVerifier()
    mcp_verifier = ZitadelMCPTokenVerifier(fake)
    spy = _SpyASGI()
    return Starlette(
        routes=[Route("/mcp", endpoint=RequireAuthMiddleware(spy, [_WM_SCOPE]))],
        middleware=[
            Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(mcp_verifier)),
        ],
    )


_APP = _build_auth_boundary_app()
_CLIENT = TestClient(_APP, raise_server_exceptions=False)


# ── Hypothesis strategies ──────────────────────────────────────────────────────────────

# The named permutations from the spec §4a (finite, always exercised).
_NAMED_HEADERS = st.sampled_from(
    [
        None,  # absent — no Authorization header
        "",  # present but empty string
        "Bearer",  # "Bearer" keyword with no token
        "Basic dXNlcjpwYXNz",  # non-Bearer scheme
        "Bearer wrong-issuer-jwt",  # wrong issuer → fake raises → None → 401
        "Bearer wrong-audience-jwt",  # wrong audience → same
        "Bearer expired-jwt",  # expired → same
        f"Bearer {VALID_WITH_ROLE_TOKEN}",  # valid with role → spy REACHED
        f"Bearer {VALID_NO_ROLE_TOKEN}",  # valid without role → 403
    ]
)

# Arbitrary garbage Bearer tokens (fake returns None for all of them → 401).
# Alphabet restricted to printable ASCII (33–126): HTTP header field-values are ASCII
# (RFC 7230) and an HTTP client cannot transmit a non-ASCII header value, so a non-ASCII
# token is outside the input domain this TestClient-based boundary test models — not a gap
# in the auth check. Excludes space (32) to avoid leading/trailing-whitespace header trimming.
#
# Every garbage token is prefixed with ``~`` — a tchar that never appears in the SDK's static
# auth-error bodies ({"error":..,"error_description":..}). This keeps the no-leak substring
# assertion meaningful: any string containing ``~`` cannot be a coincidental substring of a
# ~-free static body, so ``raw not in body`` fails ONLY if the server actually reflects the
# token (the real invariant) — not because a 1-char token like ``_`` collides with
# "error_description". The verifier still rejects these as invalid (→ 401).
_GARBAGE_BEARER = (
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1,
        max_size=80,
    )
    .map(lambda t: f"~{t}")
    .filter(lambda t: t not in (VALID_WITH_ROLE_TOKEN, VALID_NO_ROLE_TOKEN))
    .map(lambda t: f"Bearer {t}")
)

_AUTH_HEADERS = st.one_of(_NAMED_HEADERS, _GARBAGE_BEARER)

_HOPS = st.integers(min_value=-100, max_value=100_000)


# ── helpers ────────────────────────────────────────────────────────────────────────────


def _send(auth_header: str | None) -> Any:
    """POST to /mcp with the given Authorization header value (None = absent)."""
    hdrs: dict[str, str] = {}
    if auth_header is not None:
        hdrs["Authorization"] = auth_header
    return _CLIENT.post("/mcp", headers=hdrs)


def _is_valid_with_role(auth_header: str | None) -> bool:
    return auth_header == f"Bearer {VALID_WITH_ROLE_TOKEN}"


def _raw_token(auth_header: str) -> str | None:
    """Extract the raw token from a Bearer header, or None if not Bearer."""
    if auth_header.startswith("Bearer ") and len(auth_header) > 7:
        return auth_header[7:]
    return None


# ── PROPERTY 1: no request lacking valid-bearer-with-role reaches the tool body ───────


@given(auth_header=_AUTH_HEADERS)
@_SETTINGS
def test_no_unauthorized_request_reaches_tool_body(auth_header: str | None) -> None:
    """INV-S1-AUTH + INV-S1-ROLE: spy NEVER reached unless bearer carries the role.

    Non-(valid+role) inputs yield 401 (no/invalid token) or 403 (valid, no role).
    Only the one valid+role token causes the spy to return 200.

    The spy is a stateless sentinel: 200 iff reached, 401/403 iff rejected.
    Token variants that map to None (wrong-issuer, wrong-audience, expired, arbitrary
    garbage) all → 401.  Valid-no-role → 403.  Valid-with-role → 200.

    A tautology cannot pass this: any implementation that leaks a 401/403 case through
    to the spy returns 200 instead, which fails the ``not is_valid_with_role`` branch.
    """
    resp = _send(auth_header)

    if _is_valid_with_role(auth_header):
        assert resp.status_code == 200, (
            f"valid bearer+role must reach the spy tool (got {resp.status_code}); "
            f"header={auth_header!r}, body={resp.text!r}"
        )
    else:
        assert resp.status_code in (401, 403), (
            f"non-(valid+role) bearer must be rejected with 401 or 403 "
            f"(got {resp.status_code}); header={auth_header!r}, body={resp.text!r}"
        )


# ── PROPERTY 2: no-leak metamorphic ────────────────────────────────────────────────────


@given(auth_header=_AUTH_HEADERS)
@_SETTINGS
def test_noleak_on_rejection(auth_header: str | None) -> None:
    """INV-S1-NOLEAK (metamorphic): every rejected body excludes token/claim/Traceback.

    Token bytes in ⇒ token bytes NEVER out.  The SDK's RequireAuthMiddleware emits
    only {"error": ..., "error_description": ...} — but we assert explicitly so any
    hand-rolled middleware that echoes the token fails this test.
    """
    if _is_valid_with_role(auth_header):
        return  # not a rejection; no-leak applies to rejected requests only

    resp = _send(auth_header)
    assert resp.status_code in (401, 403)  # guard: must be a rejection
    body = resp.text

    # Raw token bytes must not appear in the response.
    if auth_header is not None:
        raw = _raw_token(auth_header)
        if raw:
            assert raw not in body, (
                f"raw token leaked into error response: token={raw!r} in {body!r}"
            )

    # Known claim values must not leak.
    assert _CLAIM_SUB not in body, (
        f"claim value {_CLAIM_SUB!r} (sub) leaked into error response: {body!r}"
    )
    assert _WM_ROLE not in body, (
        f"claim value {_WM_ROLE!r} (role) leaked into error response: {body!r}"
    )

    # No Python tracebacks (server-side exception bleed-through).
    assert "Traceback" not in body, f"'Traceback' found in rejection response body: {body!r}"


# ── PROPERTY 3: clamp survives auth ───────────────────────────────────────────────────


@given(hops=_HOPS)
@_CLAMP_SETTINGS
def test_clamp_survives_auth(hops: int) -> None:
    """INV-S1-READONLY (clamp clause): auth does NOT bypass the hop guard.

    The same tool functions that build_http_app registers apply read_guards.clamp_hops.
    We drive tool_get_neighbors + tool_find_paths directly (the same path the authorized
    HTTP handler executes) and assert every traversal bound in the issued query is
    <= HOP_CAP.  Together with test_no_unauthorized_request_reaches_tool_body this
    proves: auth → tool body → clamp; the auth layer cannot elevate hop access.
    """
    fake = _RecordingFake()

    # get_neighbors
    with contextlib.suppress(Exception):
        # ToolError on bad id is fine; we care about the query bound
        tool_get_neighbors(fake, "Q42", hops)

    for query, _ in fake.read_calls:
        for bound in _TRAVERSAL_BOUND.findall(query):
            assert int(bound) <= HOP_CAP, (
                f"tool_get_neighbors issued bound {bound} > HOP_CAP={HOP_CAP} "
                f"for hops={hops!r}; build_http_app uses the same function so "
                f"this proves clamp is preserved regardless of auth outcome"
            )

    fake.read_calls.clear()

    # find_paths
    with contextlib.suppress(Exception):
        tool_find_paths(fake, "Q42", "Q99", hops)

    for query, _ in fake.read_calls:
        for bound in _TRAVERSAL_BOUND.findall(query):
            assert int(bound) <= HOP_CAP, (
                f"tool_find_paths issued bound {bound} > HOP_CAP={HOP_CAP} for max_hops={hops!r}"
            )
