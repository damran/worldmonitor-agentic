"""Gate B-4a slice-1 — the Tier-1-aware Neo4j prune (``graph/ops.py:erase_source_graph``).

FAILING-FIRST oracle for the GRAPH half of cross-store GDPR source erasure (spec
``docs/reviews/GATE_B4A_ERASURE_SPEC.md`` §4.3/§9, ADR 0049). B's provenance is **Tier-1 only**
(verified in ``graph/writer.py`` + ``provenance/model.py`` + ``resolution/merge.py``): every node
carries ``prov_*`` (G1, single-source = ``source[0]``) and a fused node additionally carries a
``prov_witnesses`` JSON ``{prop: [datasets…]}`` map. There is no Tier-2 (`:Statement`/`:Source`),
so the graph erase works purely on ``prov_witnesses`` + ``prov_*`` + node/edge deletion.

These tests pin the EXACT post-erase graph state — a sole-source node (+ its value + its edges) is
DETACH DELETEd (value-complete, fixing A's lineage-only B-1 gap); a multi-source survivor is pruned
of the erased dataset (witness set, X-only props, ``prov_*`` rebuild) while keeping every other
source's data; the precise JSON decision never lets ``"ofac"`` sweep ``"ofac-eu"`` (the CONTAINS
substring trap); and the merge is never un-done. RED today: ``worldmonitor.graph.ops`` does not
exist (the symbol is imported lazily inside each test so the file still collects).

Real writer + ER fusion against ephemeral Neo4j (testcontainers); ``integration``-marked.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import _merge_entities

pytestmark = pytest.mark.integration

_RETRIEVED = "2026-06-25T00:00:00Z"


def _entity(entity_id: str, source_id: str, schema: str, props: dict[str, list[str]]) -> FtmEntity:
    """A single-source FtM entity stamped with provenance tracing to ``source_id``."""
    entity = make_entity(
        {"id": entity_id, "schema": schema, "properties": props, "datasets": [source_id]}
    )
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=_RETRIEVED,
            reliability="A",
            source_record=f"s3://landing/{source_id}/{entity_id}.json",
        ),
    )


def _person(entity_id: str, source_id: str, props: dict[str, list[str]]) -> FtmEntity:
    return _entity(entity_id, source_id, "Person", props)


def _node(client: Neo4jClient, node_id: str) -> dict[str, Any] | None:
    """Read every property of the node keyed by ``node_id`` (or ``None`` if it is gone)."""
    rows = client.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN properties(n) AS props", id=node_id
    )
    return rows[0]["props"] if rows else None


def _name_hits(client: Neo4jClient, value: str) -> int:
    """How many nodes still carry ``value`` in their (list-valued) ``name`` property."""
    return client.execute_read(
        "MATCH (n) WHERE $v IN coalesce(n.name, []) RETURN count(n) AS n", v=value
    )[0]["n"]


def _edge_ids(client: Neo4jClient) -> set[str]:
    """The FtM ids of every relationship carrying one."""
    rows = client.execute_read("MATCH ()-[r]->() WHERE r.id IS NOT NULL RETURN r.id AS id")
    return {row["id"] for row in rows}


# --------------------------------------------------------------------------- T1


def test_t1_multi_source_partial_erase_prunes_a_keeps_b(clean_graph: Neo4jClient) -> None:
    """THE crux. The SAME entity from src-A + src-B fuses to one node; erasing src-A leaves the
    node alive, drops src-A from every witness set, REMOVEs the prop src-A solely supplied, keeps
    the shared values, and rebuilds ``prov_source_id`` onto the surviving source."""
    ensure_constraints(clean_graph)
    by_id = {
        "e-a": _person(
            "e-a",
            "src-A",
            {"name": ["Vladimir Example"], "nationality": ["ru"], "passportNumber": ["P-9-SECRET"]},
        ),
        "e-b": _person("e-b", "src-B", {"name": ["Vladimir Example"], "nationality": ["ru"]}),
    }
    merged, dropped = _merge_entities("wmc-vlad", ("e-a", "e-b"), by_id)
    assert dropped == ()
    write_entities(clean_graph, [merged])

    # PRECONDITION — the fused node exists with both sources + the src-A-only passport value.
    before = _node(clean_graph, "wmc-vlad")
    assert before is not None, "the fused node must be written"
    assert before["name"] == ["Vladimir Example"]
    assert before["passportNumber"] == ["P-9-SECRET"], (
        "src-A's sole value must be present pre-erase"
    )
    assert before["prov_source_id"] == "src-A", "source[0] is src-A (sorted-first member)"
    w_before = json.loads(before["prov_witnesses"])
    assert "src-A" in w_before["name"]
    assert w_before["passportNumber"] == ["src-A"]

    from worldmonitor.graph.ops import erase_source_graph

    erase_source_graph(clean_graph, "src-A")

    # POSTCONDITION — node SURVIVES, src-A scrubbed, src-B fully retained.
    after = _node(clean_graph, "wmc-vlad")
    assert after is not None, "a multi-source node must SURVIVE erasure of one of its sources"
    w_after = json.loads(after["prov_witnesses"])
    assert "src-A" not in json.dumps(w_after), (
        "no witness set may still reference the erased source"
    )
    assert w_after["name"] == ["src-B"]
    assert w_after["nationality"] == ["src-B"]
    assert "passportNumber" not in w_after, "the src-A-only prop must be dropped from the map"
    # shared values intact (src-B still witnesses them) …
    assert after["name"] == ["Vladimir Example"]
    assert after["nationality"] == ["ru"]
    # … but the src-A-only VALUE is gone from the node (value-retraction, not just lineage).
    assert after.get("passportNumber") is None, "the prop src-A solely supplied must be REMOVEd"
    # G1: prov_source_id rebuilt onto a surviving witness (never left pointing at the erased src).
    assert after["prov_source_id"] == "src-B"


# -------------------------------------------------------------------------- T1b


def test_t1b_prov_star_rebuilt_clears_dangling_source_record(clean_graph: Neo4jClient) -> None:
    """When the erased source IS ``source[0]``, the survivor's ``prov_*`` is rebuilt onto a
    surviving dataset and the now-dangling ``prov_source_record`` (a pointer to the deleted raw
    record) is cleared — while ``prov_source_id`` always remains (G1)."""
    ensure_constraints(clean_graph)
    by_id = {
        "e-a": _person("e-a", "src-A", {"name": ["Olga Example"]}),
        "e-b": _person("e-b", "src-B", {"name": ["Olga Example"]}),
    }
    merged, _ = _merge_entities("wmc-olga", ("e-a", "e-b"), by_id)
    write_entities(clean_graph, [merged])

    before = _node(clean_graph, "wmc-olga")
    assert before is not None
    assert before["prov_source_id"] == "src-A"
    assert before["prov_source_record"] == "s3://landing/src-A/e-a.json", (
        "the soon-dangling pointer"
    )

    from worldmonitor.graph.ops import erase_source_graph

    erase_source_graph(clean_graph, "src-A")

    after = _node(clean_graph, "wmc-olga")
    assert after is not None
    assert after["prov_source_id"] == "src-B", (
        "G1: prov_source_id rebuilt onto the surviving source"
    )
    assert after.get("prov_source_record", "") == "", (
        "no dangling pointer to the erased source's deleted raw record may remain"
    )
    assert "src-A" not in json.dumps(json.loads(after["prov_witnesses"]))


def test_survivor_prov_kept_when_erased_source_is_not_source0(clean_graph: Neo4jClient) -> None:
    """A survivor whose ``source[0]`` is NOT the erased source keeps its ``prov_*`` untouched —
    only the erased dataset is pruned from the witness map (no gratuitous rebuild)."""
    ensure_constraints(clean_graph)
    by_id = {
        "e-a": _person("e-a", "src-A", {"name": ["Keep Prov"], "nationality": ["ru"]}),
        "e-b": _person("e-b", "src-B", {"name": ["Keep Prov"]}),
    }
    merged, _ = _merge_entities("wmc-keep", ("e-a", "e-b"), by_id)
    write_entities(clean_graph, [merged])

    before = _node(clean_graph, "wmc-keep")
    assert before is not None
    assert before["prov_source_id"] == "src-A"

    from worldmonitor.graph.ops import erase_source_graph

    erase_source_graph(clean_graph, "src-B")  # erase a NON-source[0] source

    after = _node(clean_graph, "wmc-keep")
    assert after is not None
    # prov_* is NOT rebuilt (source[0] survives) — the original pointer is intact.
    assert after["prov_source_id"] == "src-A"
    assert after["prov_source_record"] == "s3://landing/src-A/e-a.json"
    # the src-A-only nationality is untouched; src-B is pruned from the shared name's witnesses.
    assert after["nationality"] == ["ru"]
    w = json.loads(after["prov_witnesses"])
    assert "src-B" not in json.dumps(w)
    assert w["name"] == ["src-A"]
    assert w["nationality"] == ["src-A"]


# --------------------------------------------------------------------------- T2


def test_t2_sole_source_node_edge_and_value_detach_deleted(clean_graph: Neo4jClient) -> None:
    """A sole-source entity (+ its edge) is DETACH DELETEd: the node, its incident edge, AND its
    property VALUE all disappear (value-complete erase — fixes A's lineage-only B-1 gap)."""
    ensure_constraints(clean_graph)
    pii = "Soledad Unique Target"
    person = _person("p-sole", "src-A", {"name": [pii]})
    company = _entity("c-sole", "src-A", "Company", {"name": ["SoleCo Unique"]})
    ownership = _entity("o-sole", "src-A", "Ownership", {"owner": ["p-sole"], "asset": ["c-sole"]})
    write_entities(clean_graph, [person, company, ownership])

    # PRECONDITION — node, edge, and value all present.
    assert _node(clean_graph, "p-sole") is not None
    assert clean_graph.execute_read("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"] >= 1
    assert _name_hits(clean_graph, pii) == 1

    from worldmonitor.graph.ops import erase_source_graph

    erase_source_graph(clean_graph, "src-A")

    # POSTCONDITION — everything the sole source contributed is gone.
    assert _node(clean_graph, "p-sole") is None, "the sole-source node must be DETACH DELETEd"
    assert _node(clean_graph, "c-sole") is None
    assert clean_graph.execute_read("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"] == 0
    assert clean_graph.execute_read("MATCH (n) RETURN count(n) AS n")[0]["n"] == 0
    assert _name_hits(clean_graph, pii) == 0, "the personal-data VALUE must be gone from the graph"


# -------------------------------------------------------------------------- T5b


def test_t5b_prefix_collision_ofac_does_not_touch_ofac_eu(clean_graph: Neo4jClient) -> None:
    """Erasing dataset ``"ofac"`` must NOT touch a node witnessed only by ``"ofac-eu"`` — proving
    the precise JSON decision, not a ``CONTAINS "ofac"`` substring pre-filter, decides it."""
    ensure_constraints(clean_graph)
    write_entities(
        clean_graph,
        [
            _person("p-ofac", "ofac", {"name": ["Ofac Person"]}),
            _person("p-ofac-eu", "ofac-eu", {"name": ["Ofac EU Person"]}),
        ],
    )

    # PRECONDITION — both nodes exist; the EU node is witnessed by the prefix-colliding dataset.
    assert _node(clean_graph, "p-ofac") is not None
    eu_before = _node(clean_graph, "p-ofac-eu")
    assert eu_before is not None
    assert eu_before["prov_source_id"] == "ofac-eu"
    assert json.loads(eu_before["prov_witnesses"])["name"] == ["ofac-eu"]

    from worldmonitor.graph.ops import erase_source_graph

    erase_source_graph(clean_graph, "ofac")

    # The exact-match (sole-source) node is gone …
    assert _node(clean_graph, "p-ofac") is None
    # … but the prefix-collision node SURVIVES, byte-for-byte untouched.
    eu_after = _node(clean_graph, "p-ofac-eu")
    assert eu_after is not None, (
        "erasing 'ofac' must NOT delete the 'ofac-eu' node (substring trap)"
    )
    assert eu_after["name"] == ["Ofac EU Person"]
    assert eu_after["prov_source_id"] == "ofac-eu"
    assert json.loads(eu_after["prov_witnesses"])["name"] == ["ofac-eu"], (
        "the surviving source's witness map must be untouched"
    )


# --------------------------------------------------------------------------- T6


def test_t6_erase_does_not_unmerge_and_keeps_surviving_edges(clean_graph: Neo4jClient) -> None:
    """A 3-source merged canonical survives erasing ONE source with its durable id intact — the
    merged-away members are NOT resurrected, an edge asserted by a SURVIVING source stays, and an
    edge asserted by the ERASED source is deleted."""
    ensure_constraints(clean_graph)
    durable = "wm-anchor-qid-Q777"
    by_id = {
        "e-a": _person("e-a", "src-A", {"name": ["Canonical Person"], "nationality": ["ru"]}),
        "e-b": _person("e-b", "src-B", {"name": ["Canonical Person"], "nationality": ["ru"]}),
        "e-c": _person("e-c", "src-C", {"name": ["Canonical Person"], "nationality": ["ru"]}),
    }
    merged, dropped = _merge_entities(durable, ("e-a", "e-b", "e-c"), by_id)
    assert dropped == ()
    company = _entity("c-keep", "src-B", "Company", {"name": ["KeepCo"]})
    edge_keep = _entity("o-keep", "src-B", "Ownership", {"owner": [durable], "asset": ["c-keep"]})
    edge_erase = _entity("o-erase", "src-A", "Ownership", {"owner": [durable], "asset": ["c-keep"]})
    write_entities(clean_graph, [merged, company, edge_keep, edge_erase])

    # PRECONDITION — ONE merged Person node (members not separately materialised) + both edges.
    assert _node(clean_graph, durable) is not None
    people_before = [
        r["id"] for r in clean_graph.execute_read("MATCH (n:Person) RETURN n.id AS id ORDER BY id")
    ]
    assert people_before == [durable], "the cluster is one merged node before erasure"
    assert {"o-keep", "o-erase"} <= _edge_ids(clean_graph)

    from worldmonitor.graph.ops import erase_source_graph

    erase_source_graph(clean_graph, "src-A")

    # POSTCONDITION — survives, NOT un-merged, no resurrection of e-a/e-b/e-c.
    after = _node(clean_graph, durable)
    assert after is not None, "the merged canonical must survive erasing one contributing source"
    people_after = [
        r["id"] for r in clean_graph.execute_read("MATCH (n:Person) RETURN n.id AS id ORDER BY id")
    ]
    assert people_after == [durable], "erase must NOT un-merge / resurrect merged-away members"
    # the surviving sources' data is fully retained …
    w = json.loads(after["prov_witnesses"])
    assert "src-A" not in json.dumps(w)
    assert set(w["name"]) == {"src-B", "src-C"}
    assert after["name"] == ["Canonical Person"]
    # … the surviving-source edge stays, the erased-source edge is deleted, the neighbour survives.
    edges_after = _edge_ids(clean_graph)
    assert "o-keep" in edges_after, "an edge asserted by a surviving source must remain"
    assert "o-erase" not in edges_after, "an edge asserted by the erased source must be deleted"
    assert _node(clean_graph, "c-keep") is not None
