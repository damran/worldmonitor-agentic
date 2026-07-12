"""FastAPI application factory.

The app boots with an unauthenticated ``/health`` probe; every other route is
gated by Zitadel OIDC (see :mod:`.middleware`). The platform is single-tenant
(D1, ADR 0042).
"""

from __future__ import annotations

import importlib
import pkgutil
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.sessions import SessionMiddleware

from worldmonitor.api import auth_web
from worldmonitor.api.auth_web import build_oauth
from worldmonitor.api.dashboard import router as dashboard_router
from worldmonitor.api.deps import get_principal
from worldmonitor.api.graph import router as graph_router
from worldmonitor.api.integrations import router as integrations_router
from worldmonitor.api.llm import router as llm_router
from worldmonitor.api.middleware import (
    DEFAULT_PUBLIC_PATHS,
    DEFAULT_PUBLIC_PREFIXES,
    AuthMiddleware,
)
from worldmonitor.api.readiness import ReadinessResult, build_default_readiness
from worldmonitor.api.review import router as review_router
from worldmonitor.authz.oidc import Principal, TokenVerifier, ZitadelTokenVerifier
from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.llm.gateway import LLMGateway
from worldmonitor.plugins.registry import Registry
from worldmonitor.settings import Settings, get_settings

# Browser OIDC routes are PUBLIC so an unauthenticated browser can complete the login flow.
_AUTH_WEB_PUBLIC_PATHS: frozenset[str] = frozenset({"/login", "/auth/callback", "/logout"})

# The read-only consumption dashboard is public (ADR 0115): its JSON read API (``/api/dashboard/*``,
# Slice C), the vendored single-page app (``/app``, Slice D), and the static assets it loads
# (``/static/*``). Read-only, public news data, single-tenant self-hosted — the write/operator
# surface (integrations, review, ``/v1/chat/completions``) is deliberately NOT listed and stays
# auth-gated.
_DASHBOARD_PUBLIC_PREFIXES: frozenset[str] = frozenset({"/api/dashboard", "/app", "/static"})

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
# The vendored single-page consumption dashboard (ADR 0115, Slice D); served at /app (public).
_APP_DIR = _STATIC_DIR / "app"


def _build_verifier(settings: Settings) -> TokenVerifier | None:
    """A Zitadel verifier once auth is configured, else ``None``."""
    if not settings.auth_configured:
        return None
    return ZitadelTokenVerifier(
        issuer=settings.oidc_issuer,
        jwks_uri=settings.oidc_jwks_uri,
        audience=settings.zitadel_client_id,
    )


def _discover_registry() -> Registry:
    """A registry of every connector AND notifier (the Integrations catalog, ADR 0069).

    Mirrors ``runner.driver.discover_connectors`` but also walks the notifier package — plugins
    live two levels down (``<family>/<name>/<impl>.py``), so walk each package recursively.
    """
    registry = Registry()
    for pkg_name in ("worldmonitor.plugins.connectors", "worldmonitor.plugins.notifiers"):
        package = importlib.import_module(pkg_name)
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
            registry.discover_module(importlib.import_module(info.name))
    return registry


def create_app(
    *,
    settings: Settings | None = None,
    verifier: TokenVerifier | None = None,
    readiness: Callable[[], ReadinessResult] | None = None,
    neo4j_client: Neo4jClient | None = None,
    oauth: OAuth | None = None,
    db_sessions: sessionmaker[Session] | None = None,
    registry: Registry | None = None,
    llm_gateway: LLMGateway | None = None,
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
    routes 503). ``db_sessions`` (the Postgres ``sessionmaker`` behind the
    Integrations UI, ADR 0069) and ``registry`` (the plugin catalog) can be
    injected with a testcontainer factory + a fake registry; when ``None`` they are
    built from ``settings`` + a discovered Registry (connectors + notifiers).
    ``llm_gateway`` (the sovereignty choke point behind ``POST /v1/chat/completions``,
    ADR 0092) can be injected with a spy/fake in tests; when ``None`` it is built
    from ``settings`` (the S2 ``LLMGateway`` with its egress-audit + confidential
    selector).
    """
    settings = settings or get_settings()
    # Fail closed: a non-development boot with a placeholder secret halts loud here, before any
    # client is built (ADR 0061 / 0068). Development is unaffected (placeholders allowed locally).
    settings.validate_production_secrets()
    # Warn loudly (never silently) if any safety guard is disabled (ADR 0109).
    settings.log_enforcement_status()
    if verifier is None:
        verifier = _build_verifier(settings)
    if readiness is None:
        readiness = build_default_readiness(settings)
    if neo4j_client is None:
        neo4j_client = Neo4jClient.from_settings(settings)
    if oauth is None and settings.auth_configured:
        oauth = build_oauth(settings)
    if db_sessions is None:
        db_sessions = session_factory(engine_from_settings(settings))
    if registry is None:
        registry = _discover_registry()
    if llm_gateway is None:
        llm_gateway = LLMGateway(settings, session_factory=db_sessions)
    check_readiness = readiness

    app = FastAPI(title="WorldMonitor API", version="0.0.1")
    app.state.settings = settings
    app.state.neo4j_client = neo4j_client
    app.state.oauth = oauth
    app.state.db_sessions = db_sessions
    app.state.registry = registry
    app.state.llm_gateway = llm_gateway
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # Middleware order is load-bearing (ADR 0068 §3): Starlette runs the LAST-ADDED middleware
    # OUTERMOST, so SessionMiddleware (added AFTER AuthMiddleware) wraps it and populates
    # ``request.session`` BEFORE AuthMiddleware reads it for the session auth path.
    app.add_middleware(
        AuthMiddleware,
        verifier=verifier,
        public_paths=DEFAULT_PUBLIC_PATHS | _AUTH_WEB_PUBLIC_PATHS,
        public_prefixes=DEFAULT_PUBLIC_PREFIXES | _DASHBOARD_PUBLIC_PREFIXES,
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

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    # The consumption dashboard SPA (ADR 0115). ``html=True`` serves index.html at /app/; the
    # /app prefix is public (see ``_DASHBOARD_PUBLIC_PREFIXES``). It calls only /api/dashboard.
    app.mount("/app", StaticFiles(directory=str(_APP_DIR), html=True), name="dashboard-app")

    app.include_router(auth_web.router)
    app.include_router(graph_router)
    app.include_router(integrations_router)
    app.include_router(llm_router)
    app.include_router(review_router)
    app.include_router(dashboard_router)  # public read surface (ADR 0115); prefix /api/dashboard
    return app
