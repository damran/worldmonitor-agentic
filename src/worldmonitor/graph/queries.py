"""Graph read queries.

Internal read helpers over the resolved graph — ``get_entity`` / ``get_neighbors``
/ ``get_provenance``. The platform is single-tenant (D1, ADR 0042); the public
API/MCP surface (Phase 2) builds on these. This answers the Phase-1 done-when:
"show this entity, everyone linked to it, and where each fact came from".
"""

from __future__ import annotations

from typing import Any

from worldmonitor.graph import read_guards
from worldmonitor.graph.neo4j_client import Neo4jClient


def get_entity(client: Neo4jClient, *, entity_id: str) -> dict[str, Any] | None:
    """Return a resolved entity's node properties, or ``None`` if not found."""
    rows = client.execute_read(
        "MATCH (n:Entity {id: $entity_id}) RETURN properties(n) AS props",
        entity_id=entity_id,
    )
    return rows[0]["props"] if rows else None


def get_neighbors(client: Neo4jClient, *, entity_id: str, hops: int = 1) -> list[dict[str, Any]]:
    """Return entities linked to ``entity_id`` within ``hops``."""
    depth = max(1, int(hops))
    query = (
        f"MATCH (n:Entity {{id: $entity_id}})"
        f"-[*1..{depth}]-(m:Entity) "
        "WHERE m.id <> $entity_id RETURN DISTINCT properties(m) AS props"
    )
    rows = client.execute_read(query, entity_id=entity_id)
    return [row["props"] for row in rows]


def get_provenance(client: Neo4jClient, *, entity_id: str) -> dict[str, str]:
    """Return the provenance (``prov_*``) properties recorded on an entity's node."""
    rows = client.execute_read(
        "MATCH (n:Entity {id: $entity_id}) "
        "RETURN [k IN keys(n) WHERE k STARTS WITH 'prov_' | [k, n[k]]] AS prov",
        entity_id=entity_id,
    )
    if not rows:
        return {}
    return {str(key): str(value) for key, value in rows[0]["prov"]}


# Cap on the number of paths returned, so a result can never blow up unbounded.
# The path-traversal depth ceiling lives in the shared ``read_guards.HOP_CAP`` (ADR
# 0063: one cap, one place) — ``find_paths`` clamps against it below.
_PATH_RESULT_LIMIT = 50


def find_paths(
    client: Neo4jClient, *, from_id: str, to_id: str, max_hops: int
) -> list[dict[str, Any]]:
    """Return bounded relationship paths between two entities (ADR 0062, slice 2a).

    Read-only ``shortestPath`` between ``from_id`` and ``to_id``, undirected and
    anchored so each returned path starts at ``from_id`` and ends at ``to_id``.
    ``max_hops`` is clamped to a hard ceiling (the variable-length bound is a
    literal in the Cypher string — Cypher cannot parameterize it); ``from_id`` /
    ``to_id`` are BOUND parameters, never string-interpolated, so an
    injection-shaped id simply matches nothing and returns ``[]`` (no mutation).

    Each path is ``{"nodes": [id, ...], "relationships": [rel_type, ...]}``.
    """
    depth = max(1, min(int(max_hops), read_guards.HOP_CAP))
    query = (
        "MATCH p = shortestPath("
        f"(a:Entity {{id: $from_id}})-[*1..{depth}]-(b:Entity {{id: $to_id}})) "
        "RETURN [n IN nodes(p) | n.id] AS nodes, "
        "[r IN relationships(p) | type(r)] AS relationships "
        f"LIMIT {_PATH_RESULT_LIMIT}"
    )
    rows = client.execute_read(query, from_id=from_id, to_id=to_id)
    return [{"nodes": row["nodes"], "relationships": row["relationships"]} for row in rows]
