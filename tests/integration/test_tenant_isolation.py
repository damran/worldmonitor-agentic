"""Integration test: two tenants holding the SAME canonical ID get two distinct,
mutually-isolated nodes — audit gap **G4**.

The isolation is correct *by construction*: ``tenant_id`` participates both in the
writer's MERGE node key (rewritten to ``{id, tenant_id}``) and in the composite
``(tenant_id, anchor)`` uniqueness constraint, so ``(tenant-A, X)`` and
``(tenant-B, X)`` are distinct keys that can never collide. Phase 1 shipped that
design but had no test naming the headline isolation property; this proves it
before the Phase 2 multi-tenant read surface relies on it. Real Neo4j
(testcontainers); marked ``integration`` so it is gated by the dedicated CI job.
"""

from __future__ import annotations

import pytest

from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.queries import get_entity
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity

pytestmark = pytest.mark.integration

# One real-world canonical identifier (an LEI), the SAME FtM id, asserted under
# two tenants — exercising both isolation mechanisms (MERGE key + constraint).
_SHARED_LEI = "5493001KJTIIGC8Y1R12"
_SHARED_ID = "shared-co"
_TENANT_A = "tenant-alpha"
_TENANT_B = "tenant-beta"


def _company(name: str) -> FtmEntity:
    entity = make_entity(
        {"id": _SHARED_ID, "schema": "Company", "properties": {"name": [name]}, "datasets": ["t"]}
    )
    set_anchor(entity, "lei", _SHARED_LEI)
    return entity


def test_two_tenants_same_canonical_id_are_isolated(clean_graph: Neo4jClient) -> None:
    """Same FtM id + same LEI under two tenants -> two distinct nodes, mutually
    read-isolated, and the composite ``(tenant_id, lei)`` constraint permits both."""
    ensure_constraints(clean_graph)

    # Two independent tenant ingests assert the same real-world company.
    write_entities(clean_graph, [_company("Shared Co - tenant A view")], tenant_id=_TENANT_A)
    write_entities(clean_graph, [_company("Shared Co - tenant B view")], tenant_id=_TENANT_B)

    # TWO distinct nodes carry the shared LEI — one per tenant. A single,
    # non-composite uniqueness constraint on `lei` would have rejected the second
    # write; both coexisting proves the constraint is composite (tenant_id, lei).
    rows = clean_graph.execute_read(
        "MATCH (n:Entity) WHERE n.lei = $lei "
        "RETURN elementId(n) AS eid, n.tenant_id AS tenant ORDER BY tenant",
        lei=_SHARED_LEI,
    )
    assert len(rows) == 2, "expected exactly one node per tenant for the shared LEI"
    assert len({r["eid"] for r in rows}) == 2, "the two tenants must be distinct nodes"
    assert [r["tenant"] for r in rows] == [_TENANT_A, _TENANT_B]

    # The composite uniqueness constraint is actually present and enforced.
    names = clean_graph.execute_read("SHOW CONSTRAINTS YIELD name RETURN collect(name) AS names")[
        0
    ]["names"]
    assert "entity_tenant_lei" in names

    # Read isolation: each tenant's scoped read returns ONLY its own node.
    a_node = get_entity(clean_graph, tenant_id=_TENANT_A, entity_id=_SHARED_ID)
    b_node = get_entity(clean_graph, tenant_id=_TENANT_B, entity_id=_SHARED_ID)
    assert a_node is not None and b_node is not None
    assert a_node["tenant_id"] == _TENANT_A
    assert b_node["tenant_id"] == _TENANT_B

    # A's read never surfaces B's node, and vice versa.
    assert "Shared Co - tenant A view" in a_node["name"]
    assert "Shared Co - tenant B view" not in a_node["name"]
    assert "Shared Co - tenant B view" in b_node["name"]
    assert "Shared Co - tenant A view" not in b_node["name"]

    # A third, unrelated tenant sees neither node.
    assert get_entity(clean_graph, tenant_id="tenant-gamma", entity_id=_SHARED_ID) is None
