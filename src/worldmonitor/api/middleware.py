"""Auth middleware.

Every request to a non-public path must carry a valid Zitadel bearer token.
On success the verified :class:`Principal` is attached to ``request.state`` so
routes and downstream layers can read the authenticated caller. The platform is
single-tenant (D1, ADR 0042).
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from worldmonitor.authz.oidc import InvalidTokenError, Principal, TokenVerifier

# Paths reachable without authentication.
DEFAULT_PUBLIC_PATHS: frozenset[str] = frozenset({"/health", "/docs", "/redoc", "/openapi.json"})


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=401)


class AuthMiddleware:
    """Pure-ASGI middleware enforcing OIDC auth."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        verifier: TokenVerifier | None,
        public_paths: Iterable[str] = DEFAULT_PUBLIC_PATHS,
    ) -> None:
        self._app = app
        self._verifier = verifier
        self._public_paths = frozenset(public_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        error = self._authenticate(request)
        if error is not None:
            await error(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _authenticate(self, request: Request) -> Response | None:
        """Return an error response, or ``None`` to let the request through."""
        if request.url.path in self._public_paths:
            return None

        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return _unauthorized("Not authenticated")

        if self._verifier is None:
            return _unauthorized("Authentication is not configured")

        try:
            claims = self._verifier.verify(token.strip())
        except InvalidTokenError:
            return _unauthorized("Invalid token")

        state: dict[str, object] = request.scope.setdefault("state", {})
        state["principal"] = Principal.from_claims(claims)
        return None
