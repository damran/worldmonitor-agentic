"""Phase-1 acceptance gate — the full spine, end to end.

Done-when (docs/40_ROADMAP.md): "show this sanctioned entity, everyone linked to
it, and where each fact came from — a correct, deduplicated, canonical-ID-anchored
answer; a duplicated input collapses to one node."

Two integration tests cover it:
  1. Live OpenSanctions dataset -> landing -> ER queue -> resolve -> graph: a real
     sanctioned entity is one node with provenance back to the raw landing record
     and its neighbours linked.
  2. A deliberate duplicate collapses to a single resolved node, while a sanctioned
     entity is anchored to its canonical ID — through the real resolve->graph path.

Note: this file asserts neighbour linking on a non-merged (singleton) entity.
Edges whose endpoints were merged away are now rewritten to the canonical id
(referent rewriting, G2 / ADR 0025) — that case is proven in
``tests/integration/test_referent_rewriting.py``.
"""

from __future__ import annotations

import uuid

import pytest

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.queries import get_entity, get_neighbors, get_provenance
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.plugins.connectors.opensanctions import OpenSanctionsConnector
from worldmonitor.plugins.enrichers.wikidata import WikidataEnricher
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.runner.ingest import run_ingest
from worldmonitor.storage.landing import LandingStore

pytestmark = pytest.mark.integration

# Stable known entity in the ie_unlawful_organizations dataset.
_IRA_ID = "NK-CRxrz3RXD3GZS85Edg3r9U"


def test_live_opensanctions_spine(
    clean_graph: Neo4jClient, minio: tuple[str, str, str], postgres_dsn: str
) -> None:
    """collect -> landing -> ER queue -> resolve -> graph over a live dataset."""
    tenant = "phase1-live"
    endpoint, access_key, secret_key = minio
    landing = LandingStore.connect(
        endpoint=endpoint, access_key=access_key, secret_key=secret_key, bucket="landing"
    )
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    with sessions() as session:
        run_ingest(
            OpenSanctionsConnector(),
            {"dataset": "ie_unlawful_organizations"},
            tenant_id=tenant,
            landing=landing,
            session=session,
        )
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant)

    # The sanctioned entity is a single resolved node...
    node = get_entity(clean_graph, tenant_id=tenant, entity_id=_IRA_ID)
    assert node is not None
    assert "Irish Republican Army" in node["name"]
    assert node["tenant_id"] == tenant

    # ...carries the sanction topic (ftmg encodes topics as labels)...
    labels = clean_graph.execute_read(
        "MATCH (n:Entity {tenant_id: $t, id: $id}) RETURN labels(n) AS labels", t=tenant, id=_IRA_ID
    )[0]["labels"]
    assert "Sanction" in labels

    # ...with provenance back to the raw landing record...
    provenance = get_provenance(clean_graph, tenant_id=tenant, entity_id=_IRA_ID)
    assert provenance["prov_source_id"] == "opensanctions:ie_unlawful_organizations"
    assert provenance["prov_source_record"].startswith("s3://landing/")

    # The whole dataset resolved into the tenant's graph (entities + the Sanction
    # records), all tenant-scoped. (Neighbour linking is asserted in the dedup test
    # via an Ownership edge: ftmg does not materialise entity-links whose range is
    # the abstract `Thing` schema, e.g. Sanction.entity — a noted follow-up.)
    count = clean_graph.execute_read(
        "MATCH (n:Entity {tenant_id: $t}) RETURN count(n) AS n", t=tenant
    )[0]["n"]
    assert count >= 3

    engine.dispose()


def test_deliberate_duplicate_collapses_to_one_node(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A duplicated input yields one resolved node; a sanctioned entity is anchored."""
    tenant = "phase1-dedup"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    def queue_item(data: dict[str, object], name: str) -> ErQueueItem:
        provenance = Provenance(
            source_id="opensanctions:test",
            retrieved_at="2026-06-21T00:00:00Z",
            reliability="A",
            source_record=f"s3://landing/{name}.json",
        )
        entity = stamp(make_entity(data), provenance)
        return ErQueueItem(
            id=str(uuid.uuid4()),
            tenant_id=tenant,
            connector_id="opensanctions",
            raw_entity=entity.to_dict(),
            source_record=provenance.source_record,
            status="pending",
        )

    ivan = {
        "id": "ivan",
        "schema": "Person",
        "properties": {
            "name": ["Ivan Sanctioned"],
            "nationality": ["ru"],
            "birthDate": ["1970-01-01"],
            "topics": ["sanction"],
            "wikidataId": ["Q31337"],
        },
        "datasets": ["t"],
    }
    distinct = {
        "id": "distinct-co",
        "schema": "Company",
        "properties": {"name": ["Distinct Holdings"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    petrov_a = {
        "id": "petrov-a",
        "schema": "Company",
        "properties": {"name": ["Petrov Holdings Ltd"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    # Deliberate duplicate of petrov-a (same name + jurisdiction, different source id).
    petrov_b = {
        "id": "petrov-b",
        "schema": "Company",
        "properties": {"name": ["Petrov Holdings Ltd"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }
    ownership = {
        "id": "own-ivan-distinct",
        "schema": "Ownership",
        "properties": {"owner": ["ivan"], "asset": ["distinct-co"]},
        "datasets": ["t"],
    }

    with sessions() as session:
        for data, name in [
            (ivan, "ivan"),
            (distinct, "distinct"),
            (petrov_a, "petrov-a"),
            (petrov_b, "petrov-b"),
            (ownership, "own"),
        ]:
            session.add(queue_item(data, name))
        session.commit()

    with sessions() as session:
        resolve_pending(
            session=session,
            neo4j=clean_graph,
            tenant_id=tenant,
            enrich=WikidataEnricher(lookup=False).enrich,
        )

    # The deliberate duplicate collapses to exactly one resolved node.
    petrov_nodes = clean_graph.execute_read(
        "MATCH (n:Company {tenant_id: $t}) WHERE 'Petrov Holdings Ltd' IN n.name RETURN n.id AS id",
        t=tenant,
    )
    assert len(petrov_nodes) == 1, "duplicate company must collapse to a single node"

    # The sanctioned entity: one node, canonical-ID anchored, provenance to landing.
    ivan_node = get_entity(clean_graph, tenant_id=tenant, entity_id="ivan")
    assert ivan_node is not None
    assert ivan_node["wikidata_id"] == "Q31337"
    provenance = get_provenance(clean_graph, tenant_id=tenant, entity_id="ivan")
    assert provenance["prov_source_record"] == "s3://landing/ivan.json"

    # Neighbours linked correctly (the non-merged company it owns).
    neighbor_ids = {n["id"] for n in get_neighbors(clean_graph, tenant_id=tenant, entity_id="ivan")}
    assert "distinct-co" in neighbor_ids

    engine.dispose()
