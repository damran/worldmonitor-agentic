"""FastAPI dependencies for reading the authenticated principal."""

from __future__ import annotations

from starlette.requests import Request

from worldmonitor.authz.oidc import Principal
from worldmonitor.graph.neo4j_client import Neo4jClient


def get_neo4j(request: Request) -> Neo4jClient:
    """Return the Neo4j read client injected onto ``app.state`` by ``create_app``.

    The client is stored once at app construction (ADR 0062 DI-for-testability);
    routes read it from here so tests can inject a fake or a testcontainer client.
    """
    return request.app.state.neo4j_client


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
