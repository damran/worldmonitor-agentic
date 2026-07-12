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
    depth = max(1, min(int(hops), read_guards.HOP_CAP))
    query = (
        f"MATCH (n:Entity {{id: $entity_id}})"
        f"-[*1..{depth}]-(m:Entity) "
        "WHERE m.id <> $entity_id "
        f"RETURN DISTINCT properties(m) AS props LIMIT {read_guards.NEIGHBOR_RESULT_LIMIT}"
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
        f"LIMIT {read_guards.PATH_RESULT_LIMIT}"
    )
    rows = client.execute_read(query, from_id=from_id, to_id=to_id)
    return [{"nodes": row["nodes"], "relationships": row["relationships"]} for row in rows]


# ================================================================================================
# Dashboard read helpers (ADR 0115, Slice C) — bounded reads driving the consumption surface.
#
# FtM properties land in Neo4j as ARRAYS (``head(...)`` takes the first value); ``prov_*`` and
# canonical anchors are scalars; the FtM schema is a node LABEL (``'Article' IN labels(n)``). Every
# query floors its LIMIT at :data:`read_guards.DASHBOARD_RESULT_LIMIT` (the API also validates it),
# and interpolates ONLY that integer — all caller values are bound parameters.
# ================================================================================================
def _dash_limit(limit: int) -> int:
    return max(1, min(int(limit), read_guards.DASHBOARD_RESULT_LIMIT))


def recent_articles(client: Neo4jClient, *, limit: int) -> list[dict[str, Any]]:
    """Return the most recent Article nodes (newest first) for the feed rail."""
    query = (
        "MATCH (n:Entity) WHERE 'Article' IN labels(n) "
        "RETURN n.id AS id, head(n.title) AS title, head(n.sourceUrl) AS url, "
        "head(n.summary) AS summary, head(n.publisher) AS publisher, "
        "coalesce(head(n.publishedAt), head(n.date)) AS published, "
        "n.prov_source_id AS source, n.prov_retrieved_at AS retrieved_at "
        "ORDER BY coalesce(head(n.publishedAt), head(n.date), n.prov_retrieved_at) DESC "
        f"LIMIT {_dash_limit(limit)}"
    )
    return client.execute_read(query)


def geo_candidates(client: Neo4jClient, *, limit: int) -> list[dict[str, Any]]:
    """Return nodes that carry precise coordinates OR a country, for globe plotting.

    Returns raw rows (coordinates as strings, country as a code); the caller resolves each into a
    precise point (``latitude``/``longitude``) or a coarse country-centroid point.
    """
    query = (
        "MATCH (n:Entity) WHERE n.latitude IS NOT NULL OR n.country IS NOT NULL "
        "RETURN n.id AS id, labels(n) AS labels, "
        "coalesce(head(n.name), head(n.title), n.id) AS label, "
        "head(n.latitude) AS lat_raw, head(n.longitude) AS lon_raw, "
        "head(n.country) AS country, head(n.summary) AS summary, head(n.sourceUrl) AS url, "
        "coalesce(head(n.publishedAt), head(n.date), n.prov_retrieved_at) AS time "
        f"LIMIT {_dash_limit(limit)}"
    )
    return client.execute_read(query)


def neighborhood(client: Neo4jClient, *, entity_id: str, limit: int) -> list[dict[str, Any]]:
    """Return the immediate neighbours of ``entity_id`` WITH the relationship type (graph panel)."""
    query = (
        "MATCH (n:Entity {id: $entity_id})-[r]-(m:Entity) "
        "RETURN m.id AS id, coalesce(head(m.name), head(m.title), m.id) AS label, "
        "labels(m) AS labels, type(r) AS rel "
        f"LIMIT {_dash_limit(limit)}"
    )
    return client.execute_read(query, entity_id=entity_id)


def search_entities(client: Neo4jClient, *, term: str, limit: int) -> list[dict[str, Any]]:
    """Return entities whose name or title contains ``term`` (case-insensitive substring)."""
    query = (
        "MATCH (n:Entity) "
        "WHERE any(v IN coalesce(n.name, []) WHERE toLower(v) CONTAINS toLower($term)) "
        "OR any(v IN coalesce(n.title, []) WHERE toLower(v) CONTAINS toLower($term)) "
        "RETURN n.id AS id, coalesce(head(n.name), head(n.title), n.id) AS label, "
        "labels(n) AS labels "
        f"LIMIT {_dash_limit(limit)}"
    )
    return client.execute_read(query, term=term)


def graph_stats(client: Neo4jClient) -> dict[str, int]:
    """Return coarse graph counts (nodes, edges, articles) for the dashboard status bar."""
    nodes = client.execute_read("MATCH (n:Entity) RETURN count(n) AS c")
    edges = client.execute_read("MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c")
    articles = client.execute_read(
        "MATCH (n:Entity) WHERE 'Article' IN labels(n) RETURN count(n) AS c"
    )
    return {
        "nodes": int(nodes[0]["c"]) if nodes else 0,
        "edges": int(edges[0]["c"]) if edges else 0,
        "articles": int(articles[0]["c"]) if articles else 0,
    }
