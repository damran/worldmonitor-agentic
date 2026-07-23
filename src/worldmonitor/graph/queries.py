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


def get_entity_dossier(
    client: Neo4jClient, *, entity_id: str, hops: int = 1
) -> dict[str, Any] | None:
    """Assemble a deterministic entity dossier (Gate F-3 slice 1, ADR 0122).

    Composes the three EXISTING read helpers — :func:`get_entity`, :func:`get_neighbors`,
    :func:`get_provenance` — into one fixed-shape object: ``{entity, neighbors, provenance,
    merge_history}``. No new Cypher, no write, no ``Session``.

    Returns ``None`` iff ``get_entity`` is ``None`` (absent) — WITHOUT calling
    ``get_neighbors``/``get_provenance``, so an absent entity costs exactly one read, not
    three (the short-circuit).

    ``hops`` is clamped to ``read_guards.HOP_CAP`` before it reaches ``get_neighbors``
    (defense-in-depth: ``get_neighbors`` clamps again internally); the neighbours list
    inherits ``get_neighbors``' existing ``NEIGHBOR_RESULT_LIMIT`` bound (ADR 0064) — no
    new limit is introduced.

    ``merge_history`` is a fixed, machine-readable "recorded absence" sentinel (ADR 0122
    D3): the merge audit trail lives in Postgres and its readers need a SQLAlchemy
    ``Session``, which the stdio MCP surface does not have — populating it is a later gate.
    """
    entity = get_entity(client, entity_id=entity_id)
    if entity is None:
        return None
    neighbors = get_neighbors(client, entity_id=entity_id, hops=read_guards.clamp_hops(hops))
    provenance = get_provenance(client, entity_id=entity_id)
    return {
        "entity": entity,
        "neighbors": neighbors,
        "provenance": provenance,
        "merge_history": {"status": "not_assembled", "available": False},
    }


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

    When the geo node is an ``Address`` that an ``Event`` pins via ``ADDRESS_ENTITY`` (Slice F
    precise geo), the row is re-attributed to that **Event** — its id/label/schema — plotted at the
    Address's coords, so a precise dot represents the event (clickable straight to it), not a bare
    place. A plain Address (e.g. a GeoNames place) or a country-fallback Event represents itself.
    """
    query = (
        "MATCH (n:Entity) WHERE n.latitude IS NOT NULL OR n.country IS NOT NULL "
        "OPTIONAL MATCH (evt:Event)-[:ADDRESS_ENTITY]->(n) "
        "WITH n, evt "
        "RETURN coalesce(evt.id, n.id) AS id, coalesce(labels(evt), labels(n)) AS labels, "
        "coalesce(head(evt.name), head(n.name), head(n.title), head(n.full), n.id) AS label, "
        "head(n.latitude) AS lat_raw, head(n.longitude) AS lon_raw, "
        "coalesce(head(evt.country), head(n.country)) AS country, "
        "coalesce(head(evt.summary), head(n.summary)) AS summary, head(n.sourceUrl) AS url, "
        "coalesce(head(evt.date), head(n.publishedAt), head(n.date), n.prov_retrieved_at) AS time "
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


def recent_events(client: Neo4jClient, *, limit: int) -> list[dict[str, Any]]:
    """Recent derived Event nodes (newest first) with their source-article citation (ADR 0115).

    Each row: the Event's display name + country, and the ``PROOF``-linked source Article's title +
    URL (the receipt) — the AI brief's substrate. Empty until the Slice-B extraction pass runs.
    """
    query = (
        "MATCH (e:Event) "
        "OPTIONAL MATCH (e)-[:PROOF]->(a) "
        "RETURN e.id AS id, head(e.name) AS name, head(e.country) AS country, "
        "head(a.title) AS source_title, head(a.sourceUrl) AS source_url "
        "ORDER BY coalesce(head(e.date), e.prov_retrieved_at) DESC "
        f"LIMIT {_dash_limit(limit)}"
    )
    return client.execute_read(query)


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
