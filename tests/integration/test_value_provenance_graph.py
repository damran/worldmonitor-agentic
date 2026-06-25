"""Gate C — Tier-1 witness map on the LIVE Neo4j node (spec §5 Tier-1; A1/A3/A7 Tier-1 half).

The repo-root oracle (`tests/test_provenance_merge.py`) proves the in-memory fusion + witness-map
derivation; this integration test proves the END-TO-END wiring the slice plan leaves to the builder:
a fused multi-source entity's Tier-1 ``prov_witnesses`` JSON map LANDS on the written node, G1's
per-node ``prov_*`` COEXISTS (additive — never replaced), and the witness map keys on the DURABLE
canonical id so an alias-on-read lookup of a superseded member id reaches the same node (A7 Tier-1).

Real ER pipeline + writer against ephemeral Neo4j + Postgres (testcontainers); ``integration`` mark.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, ResolverJudgement
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import resolve_node_id, write_entities
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import _merge_entities
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration

_Q_ACME = "Q42"
_DURABLE = f"qid:{_Q_ACME}"


def _source_entity(entity_id: str, source_id: str, props: dict[str, list[str]]) -> object:
    entity = make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": [source_id]}
    )
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at="2026-06-25T00:00:00Z",
            reliability="A",
            source_record=f"s3://landing/{source_id}/{entity_id}.json",
        ),
    )


def test_tier1_witness_map_lands_on_node_with_g1_prov_coexisting(clean_graph: Neo4jClient) -> None:
    """A 3-source fused node carries ``prov_witnesses`` (all 3 datasets) AND keeps G1 ``prov_*``."""
    ensure_constraints(clean_graph)
    by_id = {
        "e-a": _source_entity(
            "e-a", "src-A", {"name": ["Vladimir Example"], "nationality": ["ru"]}
        ),
        "e-b": _source_entity(
            "e-b", "src-B", {"name": ["Vladimir Example"], "nationality": ["ru"]}
        ),
        "e-c": _source_entity(
            "e-c",
            "src-C",
            {"name": ["Vladimir Example"], "nationality": ["ru"], "passportNumber": ["P-9"]},
        ),
    }
    merged, dropped = _merge_entities("wmc-person", ("e-a", "e-b", "e-c"), by_id)
    assert dropped == ()

    write_entities(clean_graph, [merged])

    rows = clean_graph.execute_read(
        "MATCH (n:Entity {id: $id}) "
        "RETURN n.prov_witnesses AS witnesses, n.prov_source_id AS source_id, "
        "n.prov_source_record AS source_record",
        id="wmc-person",
    )
    assert rows, "the fused node must be written"
    node = rows[0]

    # G1 PRESERVED (additive): single-source prov_* still on the node.
    assert node["source_id"], "G1: the node must still carry prov_source_id"
    assert node["source_record"], "G1: the node must still carry prov_source_record"

    # Tier-1: the witness map landed as a JSON string and reflects ALL three datasets.
    assert node["witnesses"], "the Tier-1 prov_witnesses map must land on the node"
    witnesses = json.loads(node["witnesses"])
    assert witnesses["name"] == ["src-A", "src-B", "src-C"]
    assert witnesses["nationality"] == ["src-A", "src-B", "src-C"]
    # The single-source value is witnessed by exactly its one source (the adversarial target).
    assert witnesses["passportNumber"] == ["src-C"]


def _company(entity_id: str) -> dict[str, object]:
    return {
        "id": entity_id,
        "schema": "Company",
        "properties": {
            "name": ["Acme Corporation Ltd"],
            "jurisdiction": ["us"],
            "wikidataId": [_Q_ACME],
        },
        "datasets": ["t"],
    }


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id=f"src-{source}",
        retrieved_at="2026-06-25T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )
    entity = stamp(make_entity(data), provenance)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        entity_id=entity.id,
        raw_entity=entity.to_dict(),
        source_record=provenance.source_record,
        status="pending",
    )


def _judgement(left: str, right: str) -> ResolverJudgement:
    low, high = sorted((left, right))
    return ResolverJudgement(
        id=str(uuid.uuid4()), left_id=low, right_id=high, judgement="positive", source="signoff"
    )


def test_tier1_witness_map_keys_on_durable_id_through_pipeline(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A1/A7 (Tier-1): an anchored 2-source merge written through the pipeline carries the witness
    map on the DURABLE node; alias-on-read of a superseded member id reaches that same node."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions: sessionmaker[Session] = session_factory(engine)
    ensure_constraints(clean_graph)

    with sessions() as session:
        session.add(_queue_item(_company("m-a"), source="A"))
        session.add(_queue_item(_company("m-b"), source="B"))
        session.add(_judgement("m-a", "m-b"))
        session.commit()
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    # The merge is keyed on the durable QID node; the Tier-1 witness map covers BOTH sources.
    rows = clean_graph.execute_read(
        "MATCH (n:Company {id: $id}) RETURN n.prov_witnesses AS witnesses, n.prov_source_id AS sid",
        id=_DURABLE,
    )
    assert rows, "the anchored merge must be keyed on the durable QID node"
    assert rows[0]["sid"], "G1: the durable node must still carry prov_source_id"
    witnesses = json.loads(rows[0]["witnesses"])
    assert witnesses["name"] == ["src-A", "src-B"], (
        "both sources must witness the corroborated name"
    )

    # A7 (Tier-1): alias-on-read of a superseded member id resolves to the durable node.
    with sessions() as session:
        assert resolve_node_id(session, "m-a") == _DURABLE
        assert resolve_node_id(session, "m-b") == _DURABLE
    engine.dispose()
