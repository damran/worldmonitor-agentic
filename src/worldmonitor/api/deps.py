"""FastAPI dependencies for reading the authenticated principal."""

from __future__ import annotations

from starlette.requests import Request

from worldmonitor.authz.oidc import Principal


def get_principal(request: Request) -> Principal:
    """Return the principal set by :class:`AuthMiddleware`.

    Routes that depend on this are guaranteed to run only after the middleware
    has authenticated the request, so the principal is always present.
    """
    principal = request.scope.get("state", {}).get("principal")
    if not isinstance(principal, Principal):  # pragma: no cover - defensive
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="Not authenticated")
    return principal
