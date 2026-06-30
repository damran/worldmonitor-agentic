"""Bearer-auth adapter bridging Zitadel OIDC to the MCP SDK (Phase-3 Gate S1, ADR 0090).

The MCP server's HTTP (`streamable-http`) transport authenticates every tool call with a
Zitadel bearer, using the SAME verification REST already uses (`authz.oidc`). The SDK speaks
its own async ``TokenVerifier`` protocol (``verify_token(token) -> AccessToken | None``); this
module adapts our sync :class:`~worldmonitor.authz.oidc.ZitadelTokenVerifier` to it and maps the
Zitadel project-role claim to the SDK scope the server requires.

Invariants (GATE_S1_MCP_HTTP_AUTH_SPEC.md §3):
- INV-S1-AUTH — an unverifiable token yields ``None`` → the SDK rejects with 401 before any
  tool body runs.
- INV-S1-ROLE — only a token whose ``urn:zitadel:iam:org:project:roles`` carries
  ``worldmonitor:graph-read`` is granted the ``worldmonitor:read`` scope; the SDK's
  ``RequireAuthMiddleware`` then 403s anything lacking it.
- INV-S1-NOLEAK — this module never logs the token or claim values.

The role→scope map lives HERE, in one place, by design.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from worldmonitor.authz.oidc import InvalidTokenError, TokenVerifier

# Zitadel surfaces granted project roles under this reserved claim (a mapping
# role-name -> {orgId: ...}); see ADR 0090 §7.
ZITADEL_ROLE_CLAIM = "urn:zitadel:iam:org:project:roles"

# The role a service principal (Hermes) must hold to read the graph over MCP, and the
# single SDK scope it maps to. The HTTP server requires this scope (see build_auth_settings).
WM_GRAPH_READ_ROLE = "worldmonitor:graph-read"
WM_READ_SCOPE = "worldmonitor:read"


class ZitadelMCPTokenVerifier:
    """Adapt a sync :class:`worldmonitor.authz.oidc.TokenVerifier` to the SDK async protocol.

    Implements the SDK ``TokenVerifier`` protocol structurally (``async verify_token``); it is
    handed to ``BearerAuthBackend``. The wrapped verifier does the real RS256/JWKS + issuer +
    audience check; this adapter only translates the result and projects the role claim onto a
    scope. The inbound token is treated as hostile: never logged, never echoed.
    """

    def __init__(self, verifier: TokenVerifier) -> None:
        self._verifier = verifier

    async def verify_token(self, token: str) -> AccessToken | None:
        """Return an :class:`AccessToken` for a valid bearer, else ``None`` (→ 401).

        Scopes carry ``worldmonitor:read`` IFF the verified claims grant the
        ``worldmonitor:graph-read`` project role; otherwise an empty scope list (→ the SDK
        403s on the required scope). ``client_id``/``expires_at`` come from ``sub``/``exp``.
        """
        try:
            claims = self._verifier.verify(token)
        except InvalidTokenError:
            return None

        scopes = [WM_READ_SCOPE] if _has_graph_read_role(claims) else []
        exp = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(claims.get("sub", "")),
            scopes=scopes,
            expires_at=int(exp) if exp is not None else None,
        )


def _has_graph_read_role(claims: Mapping[str, Any]) -> bool:
    """True iff the Zitadel project-role claim grants ``worldmonitor:graph-read``."""
    roles = claims.get(ZITADEL_ROLE_CLAIM)
    return isinstance(roles, Mapping) and WM_GRAPH_READ_ROLE in roles


def build_auth_settings(*, issuer_url: str, resource_server_url: str | None = None) -> AuthSettings:
    """Build the SDK :class:`AuthSettings` requiring the ``worldmonitor:read`` scope.

    ``issuer_url`` is the OIDC issuer Zitadel signs tokens with (advertised in the server's
    protected-resource metadata). ``resource_server_url`` is this MCP server's own public URL
    (RFC 9728); ``None`` is permitted.
    """
    return AuthSettings(
        issuer_url=AnyHttpUrl(issuer_url),
        required_scopes=[WM_READ_SCOPE],
        resource_server_url=AnyHttpUrl(resource_server_url) if resource_server_url else None,
    )
