"""Graph-read REST routes (ADR 0062, slice 2a).

Auth-gated, read-only, bounded routes wrapping the existing ``graph/queries.py``
helpers (``get_entity`` / ``get_neighbors`` / ``get_provenance``) plus the new
``find_paths``. Every route is behind :func:`get_principal` (the same gate as
``/me``; 401 without a valid token) and reads the injected Neo4j client from
``app.state`` via :func:`get_neo4j`.

Safety (ADR 0062): read-only only; entity ids are validated by shape BEFORE any
query runs (injection-shaped ids are rejected 422); ``hops`` / ``max_hops`` are
clamped to a hard ceiling before reaching the query layer; reads are
parameterized (ids are bound params, never string-interpolated). Single-tenant
(D1, ADR 0042) — auth-gated, no tenant scoping.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from worldmonitor.api.deps import get_neo4j, get_principal
from worldmonitor.authz.oidc import Principal
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.queries import find_paths, get_entity, get_neighbors, get_provenance

# Read guards (hop cap / hop clamp / id alphabet) live in one shared module so the REST
# and MCP read surfaces can never drift (ADR 0063: one cap, one place). ``HOP_CAP`` is
# re-exported here for callers/tests that read ``api.graph.HOP_CAP``.
from worldmonitor.graph.read_guards import HOP_CAP, ID_PATTERN, clamp_hops

__all__ = ["HOP_CAP", "router"]

router = APIRouter(tags=["graph"])

EntityId = Annotated[str, Path(pattern=ID_PATTERN)]


@router.get("/entities/{entity_id}")
def read_entity(
    entity_id: EntityId,
    _principal: Annotated[Principal, Depends(get_principal)],
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
) -> dict[str, Any]:
    """Return a resolved entity's properties (incl. its ``prov_*``); 404 if absent."""
    entity = get_entity(client, entity_id=entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return entity


@router.get("/entities/{entity_id}/neighbors")
def read_neighbors(
    entity_id: EntityId,
    _principal: Annotated[Principal, Depends(get_principal)],
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
    hops: Annotated[int, Query(ge=1)] = 1,
) -> dict[str, list[dict[str, Any]]]:
    """Return entities linked to ``entity_id`` within ``hops`` (clamped to the cap)."""
    neighbors = get_neighbors(client, entity_id=entity_id, hops=clamp_hops(hops))
    return {"neighbors": neighbors}


@router.get("/entities/{entity_id}/provenance")
def read_provenance(
    entity_id: EntityId,
    _principal: Annotated[Principal, Depends(get_principal)],
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
) -> dict[str, str]:
    """Return the node's provenance (``prov_*``) — where each fact came from.

    404 if absent: the resolved graph guarantees provenance on every present node
    (ADR 0060 fail-closed writes), so an empty provenance map means the node is
    absent (or invariant-breaching) — 404 either way, consistent with ``GET
    /entities/{id}``.
    """
    prov = get_provenance(client, entity_id=entity_id)
    if not prov:
        raise HTTPException(status_code=404, detail="Entity not found")
    return prov


@router.get("/paths")
def read_paths(
    _principal: Annotated[Principal, Depends(get_principal)],
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
    from_id: Annotated[str, Query(alias="from", min_length=1, pattern=ID_PATTERN)],
    to_id: Annotated[str, Query(alias="to", min_length=1, pattern=ID_PATTERN)],
    max_hops: Annotated[int, Query(ge=1)] = 1,
) -> dict[str, list[dict[str, Any]]]:
    """Return bounded paths between two entities (``max_hops`` clamped to the cap)."""
    paths = find_paths(client, from_id=from_id, to_id=to_id, max_hops=clamp_hops(max_hops))
    return {"paths": paths}
