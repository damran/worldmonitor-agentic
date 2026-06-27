"""Integration tests for reference anchoring (Wikidata + GeoNames)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.anchors import get_anchors, set_anchor
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.plugins.connectors.geonames import GeoNamesConnector
from worldmonitor.plugins.enrichers.wikidata import WikidataEnricher
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.settings import get_settings
from worldmonitor.storage.landing import LandingStore

pytestmark = pytest.mark.integration

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "geonames"
_FIXTURE = str(_FIXTURES_DIR / "VA.txt")

# ADR 0060 (fail-closed node provenance): a non-edge entity reaching the writer with NO
# provenance is refused (the node half of G1). Real ingest stamps every entity before queueing
# (run_ingest -> connector.map(provenance=...)), so these node fixtures must be stamped too —
# stamping only; the anchor-projection assertions are unaffected by provenance.
_PROV = Provenance(
    source_id="src:anchor-test",
    retrieved_at="2026-06-21T00:00:00Z",
    reliability="A",
    source_record="s3://landing/anchor-test.json",
)


@pytest.fixture
def _allow_geonames_fixtures(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Allowlist wiring ONLY (Gate H-6/H-7, ADR 0052 §9): the local ``path`` override is
    default-deny, so point ``geonames_allowed_path_dir`` at the fixtures dir for the in-tree fixture
    ingest below (env + ``get_settings.cache_clear()``, the suite-wide pattern). No asserts change.
    """
    monkeypatch.setenv("GEONAMES_ALLOWED_PATH_DIR", str(_FIXTURES_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_wikidata_live_lookup_anchors_known_org() -> None:
    """A known org gets its Q-number via live SPARQL (data-source dependent)."""
    entity = make_entity(
        {
            "id": "icc",
            "schema": "Organization",
            "properties": {"name": ["International Criminal Court"]},
            "datasets": ["t"],
        }
    )
    WikidataEnricher().enrich(entity)
    qid = get_anchors(entity).get("wikidata_id")
    if qid is None:
        pytest.skip("Wikidata SPARQL endpoint unreachable")
    assert qid == "Q47488"


def test_writer_projects_anchor_onto_node(clean_graph: Neo4jClient) -> None:
    ensure_constraints(clean_graph)
    entity = stamp(
        make_entity(
            {
                "id": "org-1",
                "schema": "Organization",
                "properties": {"name": ["Acme"]},
                "datasets": ["t"],
            }
        ),
        _PROV,
    )
    set_anchor(entity, "wikidata_id", "Q12345")
    write_entities(clean_graph, [entity])

    rows = clean_graph.execute_read("MATCH (n:Entity {id: 'org-1'}) RETURN n.wikidata_id AS wd")
    assert rows[0]["wd"] == "Q12345"


def test_geonames_ingest_via_fixture(
    minio: tuple[str, str, str], postgres_dsn: str, _allow_geonames_fixtures: None
) -> None:
    endpoint, access_key, secret_key = minio
    landing = LandingStore.connect(
        endpoint=endpoint, access_key=access_key, secret_key=secret_key, bucket="landing"
    )
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    from worldmonitor.runner.ingest import run_ingest

    with sessions() as session:
        stats = run_ingest(
            GeoNamesConnector(),
            {"country": "VA", "path": _FIXTURE},
            landing=landing,
            session=session,
            reliability="A",
        )
    assert stats.queued >= 100

    with sessions() as session:
        rows = list(session.execute(select(ErQueueItem)).scalars())
        by_id = {row.raw_entity["id"]: row for row in rows}
        assert "geonames-3164670" in by_id
        assert by_id["geonames-3164670"].raw_entity["wm_anchor_geonames_id"] == ["3164670"]

    engine.dispose()


def test_pipeline_anchors_resolved_entity(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    """End-to-end: a resolved entity carrying wikidataId is anchored on its node."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    raw = stamp(
        make_entity(
            {
                "id": "org-q",
                "schema": "Organization",
                "properties": {"name": ["Anchored Org"], "wikidataId": ["Q98765"]},
                "datasets": ["t"],
            }
        ),
        _PROV,
    ).to_dict()
    with sessions() as session:
        session.add(
            ErQueueItem(
                id=str(uuid.uuid4()),
                connector_id="opensanctions",
                raw_entity=raw,
                source_record="s3://landing/org-q.json",
                status="pending",
            )
        )
        session.commit()

    with sessions() as session:
        resolve_pending(
            session=session,
            neo4j=clean_graph,
            enrich=WikidataEnricher(lookup=False).enrich,
        )

    rows = clean_graph.execute_read("MATCH (n:Entity) RETURN n.wikidata_id AS wd")
    assert any(row["wd"] == "Q98765" for row in rows)
    engine.dispose()
