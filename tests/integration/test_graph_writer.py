"""Integration tests: constraints get created and the writer stamps tenant_id.

Runs against an ephemeral Neo4j (testcontainers). Marked ``integration`` so it is
excluded from the default quality run and gated by the dedicated CI job.
"""

from __future__ import annotations

import pytest

from worldmonitor.graph.constraints import CANONICAL_ID_PROPERTIES, ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

pytestmark = pytest.mark.integration


def _stamped(data: dict[str, object], source: str) -> FtmEntity:
    """Build an entity stamped with provenance whose ids trace to ``source``."""
    return stamp(
        make_entity(data),
        Provenance(
            source_id=f"src:{source}",
            retrieved_at="2026-06-21T00:00:00Z",
            reliability="A",
            source_record=f"s3://landing/{source}.json",
        ),
    )


def test_constraints_created(neo4j_client: Neo4jClient) -> None:
    ensure_constraints(neo4j_client)
    rows = neo4j_client.execute_read("SHOW CONSTRAINTS YIELD name RETURN collect(name) AS names")
    names = rows[0]["names"]
    for prop in CANONICAL_ID_PROPERTIES:
        assert f"entity_tenant_{prop}" in names


def test_writer_stamps_tenant_id_on_nodes_and_edges(
    clean_graph: Neo4jClient, tenant_id: str
) -> None:
    ensure_constraints(clean_graph)
    person = make_entity(
        {"id": "p-1", "schema": "Person", "properties": {"name": ["Alice"]}, "datasets": ["t"]}
    )
    company = make_entity(
        {"id": "c-1", "schema": "Company", "properties": {"name": ["ACME"]}, "datasets": ["t"]}
    )
    ownership = make_entity(
        {
            "id": "o-1",
            "schema": "Ownership",
            "properties": {"owner": ["p-1"], "asset": ["c-1"]},
            "datasets": ["t"],
        }
    )

    write_entities(clean_graph, [person, company, ownership], tenant_id=tenant_id)

    node_rows = clean_graph.execute_read(
        "MATCH (n:Entity) RETURN n.id AS id, n.tenant_id AS tenant ORDER BY id"
    )
    assert {row["id"] for row in node_rows} >= {"c-1", "p-1"}
    assert node_rows, "expected entity nodes to be written"
    assert all(row["tenant"] == tenant_id for row in node_rows)

    edge_rows = clean_graph.execute_read("MATCH ()-[r]->() RETURN r.tenant_id AS tenant")
    assert edge_rows, "expected the ownership relationship to be written"
    assert all(row["tenant"] == tenant_id for row in edge_rows)


def test_writer_stamps_provenance_on_every_edge(clean_graph: Neo4jClient, tenant_id: str) -> None:
    """Every relationship carries prov_* traceable to its asserting entity's s3 record.

    Proves the G1 fix (PR #17): an Ownership edge and a second edge type
    (Directorship) must both land with the provenance of the *assertion* that
    created them, not just tenant_id.
    """
    ensure_constraints(clean_graph)
    person = _stamped(
        {"id": "p-1", "schema": "Person", "properties": {"name": ["Alice"]}, "datasets": ["t"]},
        "person",
    )
    company = _stamped(
        {"id": "c-1", "schema": "Company", "properties": {"name": ["ACME"]}, "datasets": ["t"]},
        "company",
    )
    ownership = _stamped(
        {
            "id": "o-1",
            "schema": "Ownership",
            "properties": {"owner": ["p-1"], "asset": ["c-1"]},
            "datasets": ["t"],
        },
        "ownership",
    )
    directorship = _stamped(
        {
            "id": "d-1",
            "schema": "Directorship",
            "properties": {"director": ["p-1"], "organization": ["c-1"]},
            "datasets": ["t"],
        },
        "directorship",
    )

    write_entities(clean_graph, [person, company, ownership, directorship], tenant_id=tenant_id)

    rows = clean_graph.execute_read(
        "MATCH ()-[r]->() "
        "RETURN type(r) AS type, r.prov_source_id AS source_id, "
        "r.prov_source_record AS source_record"
    )
    assert rows, "expected the Ownership + Directorship relationships to be written"

    # Provenance present on EVERY relationship — the invariant, not just two edges.
    assert all(row["source_id"] for row in rows), "every edge must carry prov_source_id"
    assert all(
        row["source_record"] and row["source_record"].startswith("s3://landing/") for row in rows
    ), "every edge must carry a prov_source_record pointing at the landing zone"

    by_type = {row["type"]: row for row in rows}
    # Ownership edge -> the Ownership assertion's provenance, traceable to its raw record.
    assert by_type["OWNS"]["source_id"] == "src:ownership"
    assert by_type["OWNS"]["source_record"] == "s3://landing/ownership.json"
    # A second, independent edge type carries its own assertion's provenance.
    assert by_type["DIRECTS"]["source_id"] == "src:directorship"
    assert by_type["DIRECTS"]["source_record"] == "s3://landing/directorship.json"


def test_edge_provenance_is_the_asserting_source_not_an_endpoint(
    clean_graph: Neo4jClient, tenant_id: str
) -> None:
    """An edge reflects the source that ASSERTED it, even when its endpoints differ.

    The Ownership edge is asserted by a third source (a registry), distinct from the
    sources of the owner (person) and the asset (company). The edge must carry the
    registry's provenance — copying an endpoint node's provenance would be wrong data
    dressed as compliance.
    """
    ensure_constraints(clean_graph)
    person = _stamped(
        {"id": "p-9", "schema": "Person", "properties": {"name": ["Bob"]}, "datasets": ["t"]},
        "person-source",
    )
    company = _stamped(
        {"id": "c-9", "schema": "Company", "properties": {"name": ["Globex"]}, "datasets": ["t"]},
        "company-source",
    )
    ownership = _stamped(
        {
            "id": "o-9",
            "schema": "Ownership",
            "properties": {"owner": ["p-9"], "asset": ["c-9"]},
            "datasets": ["t"],
        },
        "registry-source",
    )

    write_entities(clean_graph, [person, company, ownership], tenant_id=tenant_id)

    edge = clean_graph.execute_read(
        "MATCH (:Person {id: $p})-[r:OWNS]->(:Company {id: $c}) "
        "RETURN r.prov_source_id AS source_id, r.prov_source_record AS source_record",
        p="p-9",
        c="c-9",
    )[0]
    # The edge carries the ASSERTING source, not either endpoint's source.
    assert edge["source_id"] == "src:registry-source"
    assert edge["source_record"] == "s3://landing/registry-source.json"
    assert edge["source_id"] != "src:person-source"
    assert edge["source_id"] != "src:company-source"

    # Sanity: the endpoint nodes still carry their own (different) provenance.
    nodes = clean_graph.execute_read(
        "MATCH (n:Entity) WHERE n.id IN ['p-9', 'c-9'] "
        "RETURN n.id AS id, n.prov_source_id AS source_id ORDER BY id"
    )
    node_sources = {row["id"]: row["source_id"] for row in nodes}
    assert node_sources == {"c-9": "src:company-source", "p-9": "src:person-source"}


def test_writer_requires_tenant(clean_graph: Neo4jClient) -> None:
    from worldmonitor.graph.writer import WriterError

    person = make_entity(
        {"id": "p-9", "schema": "Person", "properties": {"name": ["Bob"]}, "datasets": ["t"]}
    )
    with pytest.raises(WriterError):
        write_entities(clean_graph, [person], tenant_id="")
