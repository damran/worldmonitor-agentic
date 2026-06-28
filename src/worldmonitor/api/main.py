"""FastAPI application factory.

The app boots with an unauthenticated ``/health`` probe; every other route is
gated by Zitadel OIDC (see :mod:`.middleware`). The platform is single-tenant
(D1, ADR 0042).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Annotated

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from worldmonitor.api import auth_web
from worldmonitor.api.auth_web import build_oauth
from worldmonitor.api.deps import get_principal
from worldmonitor.api.graph import router as graph_router
from worldmonitor.api.middleware import DEFAULT_PUBLIC_PATHS, AuthMiddleware
from worldmonitor.api.readiness import ReadinessResult, build_default_readiness
from worldmonitor.authz.oidc import Principal, TokenVerifier, ZitadelTokenVerifier
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.settings import Settings, get_settings

# Browser OIDC routes are PUBLIC so an unauthenticated browser can complete the login flow.
_AUTH_WEB_PUBLIC_PATHS: frozenset[str] = frozenset({"/login", "/auth/callback", "/logout"})


def _build_verifier(settings: Settings) -> TokenVerifier | None:
    """A Zitadel verifier once auth is configured, else ``None``."""
    if not settings.auth_configured:
        return None
    return ZitadelTokenVerifier(
        issuer=settings.oidc_issuer,
        jwks_uri=settings.oidc_jwks_uri,
        audience=settings.zitadel_client_id,
    )


def create_app(
    *,
    settings: Settings | None = None,
    verifier: TokenVerifier | None = None,
    readiness: Callable[[], ReadinessResult] | None = None,
    neo4j_client: Neo4jClient | None = None,
    oauth: OAuth | None = None,
) -> FastAPI:
    """Construct the WorldMonitor API.

    ``verifier`` can be injected (tests / custom auth); otherwise it is built
    from ``settings`` when Zitadel is configured. ``readiness`` (the zero-arg
    store-reachability sweep behind ``/ready``) can be injected with fakes in
    tests; otherwise it is built from ``settings`` + the real store clients.
    ``neo4j_client`` (the read client behind the graph routes, ADR 0062) can be
    injected with a fake / testcontainer client; when injected it is used verbatim
    (no real connection is opened). Otherwise it is built lazily from ``settings``.
    ``oauth`` (the Authlib registry behind the browser OIDC routes, ADR 0068) can
    be injected with a fake (no live Zitadel / no network); when ``None`` it is
    built from ``settings`` once auth is configured (else left unset and the login
    routes 503).
    """
    settings = settings or get_settings()
    # Fail closed: a non-development boot with a placeholder secret halts loud here, before any
    # client is built (ADR 0061 / 0068). Development is unaffected (placeholders allowed locally).
    settings.validate_production_secrets()
    if verifier is None:
        verifier = _build_verifier(settings)
    if readiness is None:
        readiness = build_default_readiness(settings)
    if neo4j_client is None:
        neo4j_client = Neo4jClient.from_settings(settings)
    if oauth is None and settings.auth_configured:
        oauth = build_oauth(settings)
    check_readiness = readiness

    app = FastAPI(title="WorldMonitor API", version="0.0.1")
    app.state.settings = settings
    app.state.neo4j_client = neo4j_client
    app.state.oauth = oauth

    # Middleware order is load-bearing (ADR 0068 §3): Starlette runs the LAST-ADDED middleware
    # OUTERMOST, so SessionMiddleware (added AFTER AuthMiddleware) wraps it and populates
    # ``request.session`` BEFORE AuthMiddleware reads it for the session auth path.
    app.add_middleware(
        AuthMiddleware,
        verifier=verifier,
        public_paths=DEFAULT_PUBLIC_PATHS | _AUTH_WEB_PUBLIC_PATHS,
    )
    # The session-cookie signing key. ``validate_production_secrets`` (called above) already raised
    # in any non-{development,test} boot with an empty ``session_secret_key``, so the random
    # fallback below ONLY ever runs in dev/test. It is generated PER create_app CALL (each app gets
    # its OWN random key) — never a published shared constant — so a cookie minted under one process
    # cannot cross-validate against another (ADR 0068 security fix).
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_key or secrets.token_urlsafe(32),
        same_site="lax",
        https_only=settings.environment not in {"development", "test"},
    )

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe — unauthenticated."""
        return {"status": "ok", "environment": settings.environment}

    @app.get("/ready", tags=["system"])
    async def ready() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        """Readiness probe — unauthenticated; fail-closed store reachability.

        200 + ``{"ready": true, "checks": {...}}`` IFF every store is reachable;
        503 + the per-component body naming the down store(s) otherwise.
        """
        result = check_readiness()
        return JSONResponse(
            {"ready": result.ready, "checks": result.checks},
            status_code=200 if result.ready else 503,
        )

    @app.get("/me", tags=["system"])
    async def me(  # pyright: ignore[reportUnusedFunction]
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> dict[str, str]:
        """Echo the authenticated principal — auth-gated."""
        return {"subject": principal.subject}

    app.include_router(auth_web.router)
    app.include_router(graph_router)
    return app
