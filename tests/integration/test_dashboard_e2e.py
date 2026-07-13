"""End-to-end product test: the whole dashboard over real stores (ADR 0115).

Every slice has its own tests; this proves the INTEGRATED product works together. It seeds a
realistic graph (OpenSanctions-shaped sanctioned entities + an ownership network + curated-feed
articles), runs the real extraction→resolution path to add news Events, then drives EVERY
`/api/dashboard` endpoint through the real FastAPI app (real Neo4j + Postgres, a stubbed gateway)
and asserts coherent data flows to the globe, feed, graph panel, search, and brief.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from worldmonitor.api.main import create_app
from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.runner.extraction import extract_cycle
from worldmonitor.settings import Settings

pytestmark = pytest.mark.integration

_PROV = Provenance(
    source_id="opensanctions:us_ofac_sdn",
    retrieved_at="2026-07-13T00:00:00Z",
    reliability="B",
    source_record="s3://landing/os/1.json",
)


def _stamped(data: dict[str, object]) -> FtmEntity:
    return stamp(make_entity(data), _PROV)


def _seed_base_graph(neo4j: Neo4jClient) -> None:
    """A realistic first-boot-ish graph: a sanctioned person owning a company, + feed articles."""
    ensure_constraints(neo4j)
    person = _stamped(
        {
            "id": "os-p1",
            "schema": "Person",
            "properties": {"name": ["Ivan Petrov"], "country": ["ru"], "topics": ["sanction"]},
            "datasets": ["opensanctions"],
        }
    )
    company = _stamped(
        {
            "id": "os-c1",
            "schema": "Company",
            "properties": {"name": ["Rosneft"], "country": ["ru"]},
            "datasets": ["opensanctions"],
        }
    )
    ownership = _stamped(
        {
            "id": "os-o1",
            "schema": "Ownership",
            "properties": {"owner": ["os-p1"], "asset": ["os-c1"]},
            "datasets": ["opensanctions"],
        }
    )
    articles = [
        _stamped(
            {
                "id": f"feed-{i}",
                "schema": "Article",
                "properties": {"title": [title], "sourceUrl": [f"https://news.example/{i}"]},
                "datasets": ["feeds"],
            }
        )
        for i, title in enumerate(["Explosion reported in Kyiv", "Sanctions package expanded"])
    ]
    write_entities(neo4j, [person, company, ownership, *articles])


class _StubGateway:
    """One gateway for both the extraction pass and the brief endpoint (keyed on caller_tag)."""

    def chat(self, messages: Any, *, caller_tag: str = "gateway", **kw: Any) -> Any:
        resp = MagicMock()
        resp.choices = [MagicMock()]
        if caller_tag == "extraction":
            resp.choices[0].message.content = (
                '{"is_event": true, "event_type": "conflict", "summary": "Blast hits Kyiv", '
                '"country": "ua", "place": "Kyiv", '
                '"actors": [{"name": "State Emergency Service", "kind": "organization"}]}'
            )
        else:
            resp.choices[0].message.content = "Elevated activity across Eastern Europe overnight."
        return resp


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        zitadel_domain="",
        zitadel_client_id="",
        zitadel_client_secret="",
        app_base_url="",
    )  # type: ignore[arg-type]


def test_dashboard_product_end_to_end(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    # --- seed a realistic graph + run the real extraction→resolution path for news Events ---
    _seed_base_graph(clean_graph)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    gateway = _StubGateway()
    extract_cycle(
        neo4j=clean_graph,
        sessions=sessions,
        gateway=gateway,
        max_articles=10,
        retrieved_at="2026-07-13T01:00:00Z",
    )
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # --- drive the REAL app (public dashboard; injected real stores + stub gateway) ---
    app = create_app(
        settings=_settings(),
        verifier=None,
        neo4j_client=clean_graph,
        db_sessions=sessions,
        llm_gateway=gateway,  # type: ignore[arg-type]
    )
    client = TestClient(app, raise_server_exceptions=False)

    # 1. stats — the graph is alive.
    stats = client.get("/api/dashboard/stats").json()
    assert stats["articles"] >= 2 and stats["nodes"] >= 5 and stats["edges"] >= 1

    # 2. feed — the headlines are there.
    titles = {a["title"] for a in client.get("/api/dashboard/feed").json()["articles"]}
    assert {"Explosion reported in Kyiv", "Sanctions package expanded"} <= titles

    # 3. points — the globe has geo: sanctioned entities at RU + the derived Event at UA.
    points = client.get("/api/dashboard/points").json()["points"]
    countries = {p["country"] for p in points}
    assert "ru" in countries, f"sanctioned entities must plot at their country; got {countries}"
    assert "ua" in countries, f"the derived Event must plot at its country; got {countries}"
    assert any(p["schema"] == "Event" for p in points), "an Event should be on the globe"
    assert all(-90 <= p["lat"] <= 90 and -180 <= p["lon"] <= 180 for p in points)

    # 4. search — find the sanctioned person.
    results = client.get("/api/dashboard/search", params={"q": "petrov"}).json()["results"]
    assert any(r["id"] == "os-p1" for r in results)

    # 5. entity graph panel — the ownership neighbour + provenance receipts.
    entity = client.get("/api/dashboard/entity/os-p1").json()
    assert {n["id"] for n in entity["nodes"]} >= {"os-p1", "os-c1"}
    assert entity["links"], "the ownership relationship must surface as a link"
    assert entity["provenance"].get("prov_source_id"), "receipts must carry the source"

    # 6. brief — an AI synthesis with citation receipts.
    brief = client.get("/api/dashboard/brief").json()
    assert brief["brief"] == "Elevated activity across Eastern Europe overnight."
    assert brief["sources"], "the brief must carry citation receipts"

    engine.dispose()
