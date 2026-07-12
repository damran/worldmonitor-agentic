"""Dashboard read helpers over a real graph (ADR 0115, Slice C).

Pins the Cypher + node-shape assumptions the dashboard relies on (schema-as-label,
FtM-props-as-arrays, ``prov_*`` scalars) by writing entities through the real ftmg writer and
reading them back through the ``graph/queries.py`` dashboard helpers.
"""

from __future__ import annotations

import pytest

from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.queries import (
    geo_candidates,
    graph_stats,
    neighborhood,
    recent_articles,
    search_entities,
)
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

pytestmark = pytest.mark.integration

_PROV = Provenance(
    source_id="feeds:bbc",
    retrieved_at="2026-07-10T00:00:00Z",
    reliability="B",
    source_record="s3://landing/feeds/a1.json",
)


def _stamped(data: dict[str, object]) -> FtmEntity:
    return stamp(make_entity(data), _PROV)


def _seed(client: Neo4jClient) -> None:
    ensure_constraints(client)
    article = _stamped(
        {
            "id": "a1",
            "schema": "Article",
            "properties": {
                "title": ["Quake hits the port city"],
                "sourceUrl": ["https://news.example/1"],
                "summary": ["A magnitude 6 event."],
                "publisher": ["BBC"],
                "publishedAt": ["2026-07-10"],
            },
            "datasets": ["feeds"],
        }
    )
    person = _stamped(
        {
            "id": "p1",
            "schema": "Person",
            "properties": {"name": ["Jane Target"], "country": ["ru"]},
            "datasets": ["opensanctions"],
        }
    )
    company = _stamped(
        {
            "id": "c1",
            "schema": "Company",
            "properties": {"name": ["Shell Co"], "country": ["gb"]},
            "datasets": ["opensanctions"],
        }
    )
    address = _stamped(
        {
            "id": "addr1",
            "schema": "Address",
            "properties": {
                "full": ["Port of Example"],
                "latitude": ["51.5"],
                "longitude": ["-0.1"],
                "country": ["gb"],
            },
            "datasets": ["geonames"],
        }
    )
    ownership = _stamped(
        {
            "id": "o1",
            "schema": "Ownership",
            "properties": {"owner": ["p1"], "asset": ["c1"]},
            "datasets": ["opensanctions"],
        }
    )
    write_entities(client, [article, person, company, address, ownership])


def test_recent_articles_reads_article_nodes(clean_graph: Neo4jClient) -> None:
    _seed(clean_graph)
    articles = recent_articles(clean_graph, limit=20)
    match = next((a for a in articles if a["id"] == "a1"), None)
    assert match is not None, f"the Article node must be read as an article; got {articles}"
    assert match["title"] == "Quake hits the port city"
    assert match["url"] == "https://news.example/1"
    assert match["publisher"] == "BBC"


def test_geo_candidates_precise_and_country(clean_graph: Neo4jClient) -> None:
    _seed(clean_graph)
    by_id = {row["id"]: row for row in geo_candidates(clean_graph, limit=100)}
    # Address carries precise coordinates.
    assert by_id["addr1"]["lat_raw"] == "51.5"
    assert by_id["addr1"]["lon_raw"] == "-0.1"
    # Person/Company carry a country but no coordinates.
    assert by_id["p1"]["country"] == "ru"
    assert by_id["p1"]["lat_raw"] is None


def test_search_and_neighborhood(clean_graph: Neo4jClient) -> None:
    _seed(clean_graph)
    results = search_entities(clean_graph, term="jane", limit=10)
    assert any(r["id"] == "p1" for r in results), f"search must find Jane; got {results}"

    neighbours = neighborhood(clean_graph, entity_id="p1", limit=10)
    neighbour_ids = {n["id"] for n in neighbours}
    assert "c1" in neighbour_ids, f"p1 must be linked to c1; got {neighbours}"
    assert all(n.get("rel") for n in neighbours), (
        "each neighbour edge must carry a relationship type"
    )


def test_graph_stats_counts(clean_graph: Neo4jClient) -> None:
    _seed(clean_graph)
    stats = graph_stats(clean_graph)
    assert stats["articles"] >= 1
    assert stats["nodes"] >= 4  # article + person + company + address (edge is a relationship)
    assert stats["edges"] >= 1  # the ownership relationship
