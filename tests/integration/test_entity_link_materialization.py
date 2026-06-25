"""Integration test: concrete-range AND abstract-range entity links both materialize.

ftmg's ``generate_entity_links`` keys each endpoint by an ``entity:``-prefixed id
(``registry.entity.node_id``), while nodes are written with the **raw** FtM id — so a
concrete-range entity-typed link (e.g. ``Person.addressEntity -> Address``) silently
MATCH-missed and never materialized (review **H3**). The Ownership *fixture* hid this
because an edge-**schema** uses raw endpoint ids; a real entity-typed *property* does not.

This pins the H3 fix through the REAL ``resolve_pending`` -> referent-rewrite -> write
path with real-FtM-shaped entities (an entity-typed property, concrete range). Gate D
then INVERTS the former G3 boundary: the abstract-``Thing``-range case (``Sanction.entity``,
range = the abstract base ``Thing``) must now ALSO materialize a real
``(:Sanction)-[:ENTITY]->(:Organization)`` edge — closing audit gap G3 (ADR 0046). The H3
concrete-range assertion stays FROZEN; the two paths are pinned together so fixing one
cannot silently alter the other.
"""

from __future__ import annotations

import uuid

import pytest

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-22T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )
    entity = stamp(make_entity(data), provenance)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity=entity.to_dict(),
        source_record=provenance.source_record,
        status="pending",
    )


def test_concrete_range_entity_link_materializes_abstract_range_stays_dropped(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    rows = [
        # H3: a CONCRETE-range entity-typed property link (Person.addressEntity -> Address).
        (
            {
                "id": "person-1",
                "schema": "Person",
                "properties": {"name": ["Jane Roe"], "addressEntity": ["addr-1"]},
                "datasets": ["t"],
            },
            "person-1",
        ),
        (
            {
                "id": "addr-1",
                "schema": "Address",
                "properties": {"full": ["1 Main St"], "country": ["us"]},
                "datasets": ["t"],
            },
            "addr-1",
        ),
        # G3 (now FIXED by Gate D / ADR 0046): an ABSTRACT-Thing-range link
        # (Sanction.entity -> Organization) must now ALSO materialize.
        (
            {
                "id": "san-1",
                "schema": "Sanction",
                "properties": {"entity": ["org-1"]},
                "datasets": ["t"],
            },
            "san-1",
        ),
        (
            {
                "id": "org-1",
                "schema": "Organization",
                "properties": {"name": ["Acme Holdings"]},
                "datasets": ["t"],
            },
            "org-1",
        ),
    ]
    with sessions() as session:
        for data, source in rows:
            session.add(_queue_item(data, source=source))
        session.commit()
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    # All four entities are written as nodes (distinct singletons).
    nodes = clean_graph.execute_read("MATCH (n:Entity) RETURN count(n) AS n")[0]["n"]
    assert nodes == 4

    # H3 FIXED: the concrete-range entity link materializes Person -> Address. Before the
    # fix the link MATCHed an 'entity:'-prefixed id no node carried, so 0 was created.
    links = clean_graph.execute_read(
        "MATCH (p:Person {id: 'person-1'})-[r]->(a:Address {id: 'addr-1'}) RETURN type(r) AS rel",
    )
    assert len(links) == 1, "concrete-range entity link (addressEntity) must materialize (H3 fixed)"

    # G3 INVERTED (Gate D / ADR 0046): the abstract-Thing-range Sanction.entity link must now
    # materialize a real (:Sanction)-[:ENTITY]->(:Organization) edge. ftmg dropped it at the
    # abstract-range lookup; the ftmg_fork override re-keys on prop.type == registry.entity
    # with the ENTITY_LABEL fallback. FAILS pre-fix (the edge does not exist).
    sanction_edges = clean_graph.execute_read(
        "MATCH (s:Sanction {id: 'san-1'})-[r:ENTITY]->(t:Organization {id: 'org-1'}) "
        "RETURN count(r) AS n"
    )[0]["n"]
    assert sanction_edges >= 1, (
        "abstract-Thing-range link (Sanction.entity) must now materialize (G3 closed / ADR 0046)"
    )

    engine.dispose()
