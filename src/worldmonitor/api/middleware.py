"""Auth middleware.

Every request to a non-public path must carry an authenticated identity. There
are two paths into one :class:`Principal` (ADR 0068):

* **Bearer** — an API/agent caller sends ``Authorization: Bearer <jwt>``; the
  existing :class:`TokenVerifier` validates it against Zitadel JWKS. This is the
  original :meth:`AuthMiddleware._authenticate` path, preserved unchanged (an
  invalid / missing-verifier bearer still 401s, even for ``Accept: text/html``).
* **Session** — a browser logged in via the OIDC authorization-code flow carries
  a signed session cookie; ``request.session["principal"]`` (set at the callback)
  is reconstructed into a :class:`Principal`. ``SessionMiddleware`` runs OUTER of
  this middleware (see ``create_app``) so ``request.session`` is already populated.

:meth:`_authorize` dispatches: an ``Authorization`` header → the bearer path
(which OWNS the outcome); no header → the session path, then a browser redirect
to ``/login`` (``Accept: text/html``) or the frozen 401 for an API/JSON caller.
On success the verified :class:`Principal` is attached to ``request.state``. The
platform is single-tenant (D1, ADR 0042).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from worldmonitor.authz.oidc import InvalidTokenError, Principal, TokenVerifier

# Paths reachable without authentication. The browser OIDC routes (/login, /auth/callback, /logout)
# are added by ``create_app`` so an unauthenticated browser can complete the login flow.
DEFAULT_PUBLIC_PATHS: frozenset[str] = frozenset(
    {"/health", "/ready", "/docs", "/redoc", "/openapi.json"}
)


def _unauthorized(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=401)


class AuthMiddleware:
    """Pure-ASGI middleware enforcing dual-path (bearer OR session) OIDC auth."""

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
        error = self._authorize(request)
        if error is not None:
            await error(scope, receive, send)
            return
        await self._app(scope, receive, send)

    def _authorize(self, request: Request) -> Response | None:
        """Dispatch to the bearer or the session/browser path; ``None`` lets the request through.

        Public paths are open. When an ``Authorization`` header is present the bearer path OWNS the
        outcome (a present-but-invalid bearer 401s — it must NOT fall through to a login redirect
        that would mask a failed token behind a login screen, even for an ``Accept: text/html``
        caller). With no header we try the signed session, then redirect a browser to ``/login`` /
        401 an API caller.
        """
        if request.url.path in self._public_paths:
            return None
        if request.headers.get("Authorization"):
            return self._authenticate(request)
        return self._authenticate_via_session(request)

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

    def _authenticate_via_session(self, request: Request) -> Response | None:
        """No Authorization header: authenticate via the signed session, else redirect/401.

        A browser carrying a valid session cookie (set at ``/auth/callback``) is authenticated as
        the IdP subject. An unauthenticated browser (``Accept: text/html``) is bounced into the
        login flow; an API/JSON caller still gets the frozen 401 contract (a tokenless API request
        must 401, never redirect).

        FAIL CLOSED (ADR 0068): when no ``verifier`` is configured (auth is not wired up) the
        session path must NOT authenticate at all — mirroring the bearer path's "Authentication is
        not configured" 401. Otherwise a validly signed cookie (minted under a fallback key) forges
        a principal on an app that has no auth configured.
        """
        if self._verifier is not None:
            session_principal = self._session_principal(request)
            if session_principal is not None:
                state: dict[str, object] = request.scope.setdefault("state", {})
                state["principal"] = Principal.from_claims(session_principal["claims"])
                return None

        if "text/html" in request.headers.get("accept", ""):
            destination = "/login?" + urlencode({"next": request.url.path})
            return RedirectResponse(destination, status_code=302)
        return _unauthorized("Not authenticated")

    @staticmethod
    def _session_principal(request: Request) -> dict[str, Any] | None:
        """Return a valid session principal dict, or ``None``.

        ``request.session`` is populated by ``SessionMiddleware`` (OUTER of this middleware). A
        tampered cookie fails the HMAC and ``SessionMiddleware`` silently yields an empty session,
        so a forged cookie produces no principal. Defensive: if ``SessionMiddleware`` is not
        installed, ``request.session`` raises ``AssertionError`` — treat that as no session.
        """
        try:
            session: dict[str, Any] = request.session
        except (AssertionError, KeyError):  # SessionMiddleware not installed
            return None
        raw = session.get("principal")
        if not isinstance(raw, dict):
            return None
        principal = cast("dict[str, Any]", raw)
        if not principal.get("subject"):
            return None
        if not isinstance(principal.get("claims"), dict):
            return None
        return principal
