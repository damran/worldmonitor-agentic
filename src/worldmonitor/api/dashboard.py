"""Dashboard read API (ADR 0115, Slice C) — the JSON surface driving the consumption dashboard.

Public, read-only, bounded routes over the resolved graph (the ``/api/dashboard`` subtree is opened
in ``AuthMiddleware`` by ``create_app``; ADR 0115, Slice A). They wrap the ``graph/queries.py``
dashboard helpers, which read Neo4j via parameterized Cypher with a hard row cap
(``read_guards.DASHBOARD_RESULT_LIMIT``). Nothing here writes the graph.

The data served is public-source (news feeds + OpenSanctions) on a single-tenant, self-hosted
deploy — the write/operator surface stays auth-gated. Coordinates come from precise
``latitude``/``longitude`` (GeoNames, and Slice-B Events) or, failing that, a coarse country
centroid (``geo_precision="country"``) so the globe has geo before precise extraction lands.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from worldmonitor.api.deps import get_llm_gateway, get_neo4j
from worldmonitor.graph import queries
from worldmonitor.graph.geo import country_centroid
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.queries import get_entity, get_provenance
from worldmonitor.graph.read_guards import DASHBOARD_RESULT_LIMIT, ID_PATTERN
from worldmonitor.llm.gateway import LLMGateway, LLMGatewayError

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

EntityId = Annotated[str, Path(pattern=ID_PATTERN)]
Limit = Annotated[int, Query(ge=1, le=DASHBOARD_RESULT_LIMIT)]


def _to_float(value: object) -> float | None:
    """Best-effort parse of an FtM coordinate string to a float, else ``None``."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _schema(labels: list[str]) -> str | None:
    """The FtM schema is the node label that is not the shared ``Entity`` label."""
    return next((label for label in labels if label != "Entity"), None)


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    """Parse ``minLon,minLat,maxLon,maxLat`` into a tuple, or ``None``; 422 on a malformed value."""
    if not bbox:
        return None
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(status_code=422, detail="bbox must be minLon,minLat,maxLon,maxLat")
    try:
        min_lon, min_lat, max_lon, max_lat = (float(p) for p in parts)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="bbox values must be numbers") from exc
    return min_lon, min_lat, max_lon, max_lat


def _in_bbox(lat: float, lon: float, box: tuple[float, float, float, float]) -> bool:
    min_lon, min_lat, max_lon, max_lat = box
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


@router.get("/stats")
def stats(client: Annotated[Neo4jClient, Depends(get_neo4j)]) -> dict[str, int]:
    """Coarse graph counts (nodes / edges / articles) — the dashboard's 'is it alive' pulse."""
    return queries.graph_stats(client)


@router.get("/feed")
def feed(
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
    limit: Limit = 100,
) -> dict[str, list[dict[str, Any]]]:
    """Most recent Article nodes (newest first) — the live feed rail."""
    return {"articles": queries.recent_articles(client, limit=limit)}


@router.get("/points")
def points(
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
    limit: Limit = 300,
    bbox: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Geo-located nodes for the globe.

    Each node resolves to a precise point (``geo_precision="point"`` from ``latitude``/
    ``longitude``) or a coarse country-centroid point (``geo_precision="country"``); a node with
    neither is dropped. An optional ``bbox`` (``minLon,minLat,maxLon,maxLat``) filters the result.
    """
    box = _parse_bbox(bbox)
    resolved: list[dict[str, Any]] = []
    for row in queries.geo_candidates(client, limit=limit):
        lat, lon = _to_float(row.get("lat_raw")), _to_float(row.get("lon_raw"))
        precision = "point"
        if lat is None or lon is None:
            centroid = country_centroid(row.get("country"))
            if centroid is None:
                continue
            lat, lon = centroid
            precision = "country"
        if box is not None and not _in_bbox(lat, lon, box):
            continue
        resolved.append(
            {
                "id": row["id"],
                "label": row.get("label"),
                "schema": _schema(row.get("labels") or []),
                "lat": lat,
                "lon": lon,
                "geo_precision": precision,
                "country": row.get("country"),
                "summary": row.get("summary"),
                "url": row.get("url"),
                "time": row.get("time"),
            }
        )
    return {"points": resolved}


@router.get("/search")
def search(
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Limit = 25,
) -> dict[str, list[dict[str, Any]]]:
    """Case-insensitive name/title substring search over resolved entities."""
    return {"results": queries.search_entities(client, term=q, limit=limit)}


@router.get("/entity/{entity_id}")
def entity(
    entity_id: EntityId,
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
    limit: Limit = 100,
) -> dict[str, Any]:
    """One entity's node, its immediate neighbourhood (nodes + typed links), and its provenance.

    Shaped for the click-through graph panel: ``nodes`` + ``links`` feed a force-graph directly,
    and ``provenance`` is the entity's receipts (where each fact came from). 404 if absent.
    """
    center = get_entity(client, entity_id=entity_id)
    if center is None:
        raise HTTPException(status_code=404, detail="Entity not found")

    center_label = _next_str(center.get("name")) or _next_str(center.get("title")) or entity_id
    nodes: list[dict[str, Any]] = [
        {"id": entity_id, "label": center_label, "schema": None, "center": True}
    ]
    links: list[dict[str, Any]] = []
    for row in queries.neighborhood(client, entity_id=entity_id, limit=limit):
        nodes.append(
            {
                "id": row["id"],
                "label": row.get("label"),
                "schema": _schema(row.get("labels") or []),
                "center": False,
            }
        )
        links.append({"source": entity_id, "target": row["id"], "rel": row.get("rel")})

    return {
        "id": entity_id,
        "properties": center,
        "nodes": nodes,
        "links": links,
        "provenance": get_provenance(client, entity_id=entity_id),
    }


# AI brief: served from a short in-process TTL cache so a public, unauthenticated endpoint can't
# be spammed into one LLM call per request (the brief changes slowly; freshness to the minute is
# irrelevant). LOCAL/Ollama egress is free, but this bounds cost under any mode + request rate.
_BRIEF_TTL_SECONDS = 300.0


def _build_brief_prompt(events: list[dict[str, Any]], articles: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    if events:
        lines.append("Recent geo-located events:")
        lines += [
            f"- {e.get('name')}" + (f" [{e['country']}]" if e.get("country") else "")
            for e in events[:20]
        ]
    if articles:
        lines.append("\nRecent headlines:")
        lines += [f"- {a.get('title')}" for a in articles[:20]]
    return (
        "You are an OSINT analyst. Write a concise 3-4 sentence situational brief of the current "
        "world state from the events and headlines below. Be factual and neutral; do NOT speculate "
        "beyond what is listed. Plain prose, no markdown.\n\n" + "\n".join(lines)
    )


def _brief_content(response: Any) -> str:
    """Defensively read the completion text (mirrors ``api/llm.py``'s ModelResponse handling)."""
    choices: Any = getattr(response, "choices", None)
    if choices and len(choices) > 0:
        message: Any = getattr(choices[0], "message", None)
        if message is not None:
            content: Any = getattr(message, "content", "")
            return str(content) if content is not None else ""
    return ""


def _brief_sources(
    events: list[dict[str, Any]], articles: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The receipts behind the brief: deduped source titles + urls."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        url = event.get("source_url")
        if isinstance(url, str) and url not in seen:
            seen.add(url)
            out.append({"title": event.get("source_title") or event.get("name"), "url": url})
    for article in articles:
        url = article.get("url")
        if isinstance(url, str) and url not in seen:
            seen.add(url)
            out.append({"title": article.get("title"), "url": url})
    return out[:12]


@router.get("/brief")
def brief(
    request: Request,
    client: Annotated[Neo4jClient, Depends(get_neo4j)],
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
) -> dict[str, Any]:
    """An AI-synthesized situational brief with citation receipts (server-controlled egress).

    The prompt is built from recent events + headlines (public data) — never from caller input — so
    this stays public-read-safe. Cached for ``_BRIEF_TTL_SECONDS`` to bound LLM calls.
    """
    state = request.app.state
    cache: dict[str, Any] = getattr(state, "brief_cache", None) or {}
    state.brief_cache = cache
    now = time.monotonic()
    hit = cache.get("brief")
    if hit is not None and now - hit["at"] < _BRIEF_TTL_SECONDS:
        return cast("dict[str, Any]", hit["body"])

    events = queries.recent_events(client, limit=20)
    articles = queries.recent_articles(client, limit=20)
    if not events and not articles:
        # Not cached — recompute as soon as the driver has ingested something.
        return {
            "brief": "No activity yet — the driver is still ingesting curated feeds.",
            "sources": [],
        }

    try:
        response = gateway.chat(
            [{"role": "user", "content": _build_brief_prompt(events, articles)}],
            caller_tag="dashboard-brief",
        )
    except LLMGatewayError as exc:
        raise HTTPException(
            status_code=502, detail="AI brief unavailable (LLM gateway error)"
        ) from exc

    body = {"brief": _brief_content(response), "sources": _brief_sources(events, articles)}
    cache["brief"] = {"at": now, "body": body}
    return body


def _next_str(value: object) -> str | None:
    """First string from an FtM array property (or the scalar itself), else ``None``."""
    if isinstance(value, list):
        for item in cast("list[object]", value):
            if isinstance(item, str):
                return item
        return None
    return value if isinstance(value, str) else None
