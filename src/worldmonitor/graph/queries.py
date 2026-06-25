"""Graph read queries.

Internal read helpers over the resolved graph — ``get_entity`` / ``get_neighbors``
/ ``get_provenance``. The platform is single-tenant (D1, ADR 0042); the public
API/MCP surface (Phase 2) builds on these. This answers the Phase-1 done-when:
"show this entity, everyone linked to it, and where each fact came from".
"""

from __future__ import annotations

from typing import Any

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
