"""FastAPI application factory.

The app boots with an unauthenticated ``/health`` probe; every other route is
gated by Zitadel OIDC (see :mod:`.middleware`). The platform is single-tenant
(D1, ADR 0042).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse

from worldmonitor.api.deps import get_principal
from worldmonitor.api.middleware import AuthMiddleware
from worldmonitor.api.readiness import ReadinessResult, build_default_readiness
from worldmonitor.authz.oidc import Principal, TokenVerifier, ZitadelTokenVerifier
from worldmonitor.settings import Settings, get_settings


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
) -> FastAPI:
    """Construct the WorldMonitor API.

    ``verifier`` can be injected (tests / custom auth); otherwise it is built
    from ``settings`` when Zitadel is configured. ``readiness`` (the zero-arg
    store-reachability sweep behind ``/ready``) can be injected with fakes in
    tests; otherwise it is built from ``settings`` + the real store clients.
    """
    settings = settings or get_settings()
    # Fail closed: a non-development boot with a placeholder secret halts loud here, before any
    # client is built (ADR 0061). Development is unaffected (placeholders allowed locally).
    settings.validate_production_secrets()
    if verifier is None:
        verifier = _build_verifier(settings)
    if readiness is None:
        readiness = build_default_readiness(settings)
    check_readiness = readiness

    app = FastAPI(title="WorldMonitor API", version="0.0.1")
    app.add_middleware(AuthMiddleware, verifier=verifier)

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

    return app
