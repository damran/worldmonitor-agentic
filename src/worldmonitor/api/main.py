"""FastAPI application factory.

The app boots with an unauthenticated ``/health`` probe; every other route is
gated by Zitadel OIDC and carries tenant context (see :mod:`.middleware`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI

from worldmonitor.api.deps import get_principal
from worldmonitor.api.middleware import AuthMiddleware
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
) -> FastAPI:
    """Construct the WorldMonitor API.

    ``verifier`` can be injected (tests / custom auth); otherwise it is built
    from ``settings`` when Zitadel is configured.
    """
    settings = settings or get_settings()
    if verifier is None:
        verifier = _build_verifier(settings)

    app = FastAPI(title="WorldMonitor API", version="0.0.1")
    app.add_middleware(AuthMiddleware, verifier=verifier)

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe — unauthenticated."""
        return {"status": "ok", "environment": settings.environment}

    @app.get("/me", tags=["system"])
    async def me(  # pyright: ignore[reportUnusedFunction]
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> dict[str, str]:
        """Echo the authenticated principal and its tenant — auth-gated."""
        return {"subject": principal.subject, "tenant_id": principal.tenant_id}

    return app
