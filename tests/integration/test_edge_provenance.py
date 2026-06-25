"""Integration tests: relationships carry the provenance of the assertion that
created them — audit gap **G1** ("provenance on every node *and edge*").

The writer's invariant is that an edge's provenance is the provenance of the FtM
entity that *asserts* the edge (the Ownership/Directorship entity, or the
property-holder for an entity-reference link), **not** either endpoint node's.
These tests prove that distinction holds, against an ephemeral Neo4j
(testcontainers). Marked ``integration`` so they are excluded from the default
quality run and gated by the dedicated CI job.
"""

from __future__ import annotations

import pytest

from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

pytestmark = pytest.mark.integration


def _stamped(data: dict[str, object], provenance: Provenance) -> FtmEntity:
    return stamp(make_entity(data), provenance)


def _edge_by_id(client: Neo4jClient, edge_id: str) -> dict[str, object] | None:
    """Read the provenance an edge (matched by its FtM id) carries, or None."""
    rows = client.execute_read(
        "MATCH ()-[r]->() WHERE r.id = $id "
        "RETURN r.prov_source_id AS source_id, r.prov_retrieved_at AS retrieved_at, "
        "r.prov_reliability AS reliability, r.prov_source_record AS source_record",
        id=edge_id,
    )
    return rows[0] if rows else None


def test_first_class_edges_carry_assertion_provenance(
    clean_graph: Neo4jClient,
) -> None:
    """An Ownership edge and a second edge type (Directorship) land with prov_*
    traceable to their s3:// landing record, and EVERY relationship carries
    provenance (the precise guarantee G1 found broken)."""
    ensure_constraints(clean_graph)

    person_prov = Provenance(
        source_id="opensanctions:people",
        retrieved_at="2026-06-21T00:00:00Z",
        reliability="A",
        source_record="s3://landing/test-tenant/opensanctions/person.json",
    )
    company_prov = Provenance(
        source_id="opencorporates:companies",
        retrieved_at="2026-06-21T00:00:00Z",
        reliability="B",
        source_record="s3://landing/test-tenant/opencorporates/company.json",
    )
    ownership_prov = Provenance(
        source_id="opencorporates:ownership",
        retrieved_at="2026-06-21T01:00:00Z",
        reliability="B",
        source_record="s3://landing/test-tenant/opencorporates/ownership.json",
    )
    directorship_prov = Provenance(
        source_id="opencorporates:officers",
        retrieved_at="2026-06-21T02:00:00Z",
        reliability="C",
        source_record="s3://landing/test-tenant/opencorporates/directorship.json",
    )

    person = _stamped(
        {"id": "p-1", "schema": "Person", "properties": {"name": ["Alice"]}, "datasets": ["t"]},
        person_prov,
    )
    company = _stamped(
        {"id": "c-1", "schema": "Company", "properties": {"name": ["ACME"]}, "datasets": ["t"]},
        company_prov,
    )
    # Two first-class, distinctly-typed edges over the SAME two endpoints, each
    # asserted by its own source.
    ownership = _stamped(
        {
            "id": "own-1",
            "schema": "Ownership",
            "properties": {"owner": ["p-1"], "asset": ["c-1"]},
            "datasets": ["t"],
        },
        ownership_prov,
    )
    directorship = _stamped(
        {
            "id": "dir-1",
            "schema": "Directorship",
            "properties": {"director": ["p-1"], "organization": ["c-1"]},
            "datasets": ["t"],
        },
        directorship_prov,
    )

    write_entities(clean_graph, [person, company, ownership, directorship])

    # The Ownership edge traces back to the ownership assertion's landing record.
    own_edge = _edge_by_id(clean_graph, "own-1")
    assert own_edge is not None, "the Ownership relationship must be written"
    assert own_edge["source_id"] == ownership_prov.source_id
    assert own_edge["source_record"] == ownership_prov.source_record
    assert own_edge["reliability"] == ownership_prov.reliability
    assert own_edge["retrieved_at"] == ownership_prov.retrieved_at

    # The second edge type carries its own assertion's provenance.
    dir_edge = _edge_by_id(clean_graph, "dir-1")
    assert dir_edge is not None, "the Directorship relationship must be written"
    assert dir_edge["source_id"] == directorship_prov.source_id
    assert dir_edge["source_record"] == directorship_prov.source_record

    # Provenance is present on EVERY relationship — not just the ones we looked up.
    all_edges = clean_graph.execute_read(
        "MATCH ()-[r]->() "
        "RETURN r.prov_source_id AS source_id, r.prov_source_record AS source_record"
    )
    assert len(all_edges) >= 2, "expected at least the Ownership and Directorship edges"
    assert all(e["source_id"] for e in all_edges), "every relationship must carry prov_source_id"
    assert all(
        isinstance(e["source_record"], str) and e["source_record"].startswith("s3://")
        for e in all_edges
    ), "every relationship must point at its s3:// landing record"


def test_edge_provenance_is_the_asserting_source_not_an_endpoint(
    clean_graph: Neo4jClient,
) -> None:
    """When the edge's source differs from BOTH endpoints' sources, the edge must
    reflect the asserting entity's provenance — never an endpoint's. This is the
    "do not shortcut" requirement: copying an endpoint's prov would be wrong data
    dressed as compliance."""
    ensure_constraints(clean_graph)

    person_prov = Provenance(
        source_id="src:person-registry",
        retrieved_at="2026-06-21T00:00:00Z",
        reliability="A",
        source_record="s3://landing/test-tenant/persons/p-2.json",
    )
    company_prov = Provenance(
        source_id="src:company-registry",
        retrieved_at="2026-06-21T00:00:00Z",
        reliability="A",
        source_record="s3://landing/test-tenant/companies/c-2.json",
    )
    # The ownership ASSERTION is from a third, independent source — different from
    # both the person's source and the company's source.
    ownership_prov = Provenance(
        source_id="src:leaked-ownership-filing",
        retrieved_at="2026-06-21T03:00:00Z",
        reliability="D",
        source_record="s3://landing/test-tenant/filings/own-2.json",
    )

    person = _stamped(
        {"id": "p-2", "schema": "Person", "properties": {"name": ["Bob"]}, "datasets": ["t"]},
        person_prov,
    )
    company = _stamped(
        {"id": "c-2", "schema": "Company", "properties": {"name": ["Globex"]}, "datasets": ["t"]},
        company_prov,
    )
    ownership = _stamped(
        {
            "id": "own-2",
            "schema": "Ownership",
            "properties": {"owner": ["p-2"], "asset": ["c-2"]},
            "datasets": ["t"],
        },
        ownership_prov,
    )

    write_entities(clean_graph, [person, company, ownership])

    edge = _edge_by_id(clean_graph, "own-2")
    assert edge is not None, "the Ownership relationship must be written"
    # The edge reflects the asserting source...
    assert edge["source_id"] == ownership_prov.source_id
    assert edge["source_record"] == ownership_prov.source_record
    # ...and NOT either endpoint's source.
    assert edge["source_id"] not in {person_prov.source_id, company_prov.source_id}
    assert edge["source_record"] not in {person_prov.source_record, company_prov.source_record}

    # Cross-check: the endpoint NODES still carry their own, different provenance,
    # proving the edge did not simply inherit a neighbour node's.
    nodes = clean_graph.execute_read(
        "MATCH (n:Entity) WHERE n.id IN ['p-2', 'c-2'] "
        "RETURN n.id AS id, n.prov_source_id AS source_id ORDER BY id",
    )
    by_id = {n["id"]: n["source_id"] for n in nodes}
    assert by_id["p-2"] == person_prov.source_id
    assert by_id["c-2"] == company_prov.source_id
