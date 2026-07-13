"""News→event extraction, end-to-end over real stores (ADR 0115, Slice B).

Seeds a curated-feed Article node, runs one extraction cycle (with a stubbed gateway — no Ollama),
and lets the REAL resolver drain the derived candidates into Neo4j. Pins that the derived Event
lands as a node with its country literal (for the globe) and its INVOLVED/PROOF relationships, and
that the merge guard + fail-closed provenance-on-write accept the derived entities.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.runner.extraction import extract_cycle

pytestmark = pytest.mark.integration

_EXTRACTION_JSON = (
    '{"is_event": true, "event_type": "protest", '
    '"summary": "Workers strike at Company X in Kyiv", '
    '"country": "ua", "place": "Kyiv", '
    '"actors": [{"name": "Company X", "kind": "organization"}]}'
)


def _stub_gateway(reply: str) -> MagicMock:
    gw = MagicMock()
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = reply
    gw.chat.return_value = resp
    return gw


def _seed_article(neo4j: Neo4jClient) -> None:
    ensure_constraints(neo4j)
    article = stamp(
        make_entity(
            {
                "id": "feed-int1",
                "schema": "Article",
                "properties": {"title": ["Strike hits Kharkiv"], "publishedAt": ["2026-07-12"]},
                "datasets": ["feeds"],
            }
        ),
        Provenance(
            source_id="feeds",
            retrieved_at="2026-07-12T08:00:00Z",
            reliability="B",
            source_record="s3://landing/feeds/int1.json",
        ),
    )
    write_entities(neo4j, [article])


def test_extraction_pass_writes_event_with_links(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    _seed_article(clean_graph)

    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    # 1. Extraction cycle: select the feed Article, "call" the (stubbed) LLM, enqueue candidates.
    stats = extract_cycle(
        neo4j=clean_graph,
        sessions=sessions,
        gateway=_stub_gateway(_EXTRACTION_JSON),
        max_articles=10,
        retrieved_at="2026-07-12T09:00:00Z",
    )
    assert stats.extracted == 1 and stats.events == 1 and stats.actors == 1

    # Candidates: the Event + the actor + a precise Address (place "Kyiv" matched the gazetteer).
    with sessions() as session:
        pending = session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.status == "pending")
        ).scalar_one()
        assert pending == 3

    # 2. The REAL resolver drains them into the graph (merge guard + provenance-on-write apply).
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # 3. The derived Event node exists (descriptive text in `name` — ftmg drops long text-type
    #    props like `summary`). Its geo is a PRECISE pin: a linked Address at Kyiv, so the Event
    #    itself carries no country (no country-centroid duplicate).
    events = clean_graph.execute_read(
        "MATCH (e:Event) RETURN e.id AS id, head(e.country) AS country, head(e.name) AS name"
    )
    assert len(events) == 1
    assert events[0]["country"] is None, (
        "a city-matched Event carries a precise Address, not country"
    )
    assert "Kyiv" in events[0]["name"]

    address = clean_graph.execute_read(
        "MATCH (:Event)-[:ADDRESS_ENTITY]->(a:Address) "
        "RETURN head(a.full) AS place, toFloat(head(a.latitude)) AS lat, "
        "toFloat(head(a.longitude)) AS lon"
    )
    assert len(address) == 1 and address[0]["place"] == "Kyiv"
    assert abs(address[0]["lat"] - 50.45) < 0.01 and abs(address[0]["lon"] - 30.52) < 0.01

    involved = clean_graph.execute_read(
        "MATCH (:Event)-[:INVOLVED]->(a) RETURN head(a.name) AS name"
    )
    assert any(row["name"] == "Company X" for row in involved), f"missing INVOLVED edge: {involved}"

    proof = clean_graph.execute_read("MATCH (:Event)-[:PROOF]->(d) RETURN d.id AS id")
    assert any(row["id"] == "feed-int1" for row in proof), f"missing PROOF receipt edge: {proof}"

    engine.dispose()


def test_extraction_pass_is_idempotent(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    """Re-running after the Event is in the graph re-calls nothing and enqueues nothing new."""
    _seed_article(clean_graph)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    gw = _stub_gateway(_EXTRACTION_JSON)
    extract_cycle(
        neo4j=clean_graph, sessions=sessions, gateway=gw, max_articles=10, retrieved_at="t"
    )
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # Second cycle: the Event now exists, so the article is skipped before any LLM call.
    stats2 = extract_cycle(
        neo4j=clean_graph, sessions=sessions, gateway=gw, max_articles=10, retrieved_at="t"
    )
    assert stats2.scanned == 0
    assert gw.chat.call_count == 1  # only the first cycle called the model

    engine.dispose()
