"""FastAPI dependencies for reading the authenticated principal."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from worldmonitor.authz.oidc import Principal
from worldmonitor.authz.roles import WM_LLM_ROLE, principal_has_role
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.llm.gateway import LLMGateway


def get_db(request: Request) -> Iterator[Session]:
    """Yield a SQLAlchemy session from the injected ``app.state.db_sessions`` factory.

    ``create_app`` stores a ``sessionmaker`` on ``app.state.db_sessions`` (ADR 0069
    DI-for-testability; tests inject a testcontainer factory). A request gets its own
    session which is closed after the response — a standard generator dependency.
    """
    db: Session = request.app.state.db_sessions()
    try:
        yield db
    finally:
        db.close()


def get_neo4j(request: Request) -> Neo4jClient:
    """Return the Neo4j read client injected onto ``app.state`` by ``create_app``.

    The client is stored once at app construction (ADR 0062 DI-for-testability);
    routes read it from here so tests can inject a fake or a testcontainer client.
    """
    return request.app.state.neo4j_client


def get_llm_gateway(request: Request) -> LLMGateway:
    """Return the LLM gateway injected onto ``app.state`` by ``create_app``.

    The gateway is stored once at app construction (ADR 0092 DI-for-testability);
    routes read it from here so tests can inject a spy/fake gateway and assert
    the route always delegates to it (mirrors :func:`get_neo4j` exactly).
    """
    return request.app.state.llm_gateway  # type: ignore[no-any-return]


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


def require_llm_role(request: Request) -> Principal:
    """Return the authenticated principal, requiring the ``worldmonitor:llm`` project role.

    Gate L1-b (ADR 0104 item 5). Resolves the principal via :func:`get_principal` first, so
    an unauthenticated request 401s exactly as it always has (unauthenticated != forbidden) —
    ``get_principal`` itself is untouched. Only once a principal is present is the
    ``worldmonitor:llm`` role checked; missing it is a 403, not a silent 401 swallow.
    """
    principal = get_principal(request)
    if not principal_has_role(principal, WM_LLM_ROLE):
        raise HTTPException(status_code=403, detail=f"Missing required role: {WM_LLM_ROLE}")
    return principal
