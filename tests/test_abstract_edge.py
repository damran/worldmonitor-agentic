"""Gate D — abstract ``Thing``-range entity-link materialization (the OFAC crux).

ftmg 0.1.0 keys every entity-link's target lookup on the **range SCHEMA**
(``config.nodes.schemata.get(prop.range.name)``, ``transform.py:227-229`` for
``generate_entity_links``; ``317-322`` for ``generate_edge_entity``). The abstract base
``Thing`` has no ``config.nodes.schemata`` entry (``config.py:67-70`` registers only
``not schema.edge and not schema.abstract``; ``config.py:73`` *raises* if you try to
register an abstract schema), so EVERY entity-link whose property range is the abstract
``Thing`` is silently dropped — verified against installed FtM:

    Sanction.entity        -> Thing  (abstract)  drop site 1 (non-edge schema)   THE CRUX
    UnknownLink.subject    -> Thing  (abstract)  drop site 2 (edge schema)
    UnknownLink.object     -> Thing  (abstract)  drop site 2 (edge schema)

    Ownership.owner/asset  -> LegalEntity/Asset  (concrete) — MUST stay contracting
    Person.addressEntity   -> Address            (concrete) — the H3 frozen line

This is the failing-test-first oracle for the ``graph/ftmg_fork/`` thin override (spec §11
/ §13). Each materialization case **FAILS on the current tree** because the edge never
exists, and PASSES once the fork re-keys the target lookup on
``prop.type == registry.entity`` with the ``ENTITY_LABEL="Entity"`` fallback. The
ghost / idempotency / G1 / contraction cases pin the surrounding invariants.

Real Neo4j integration (testcontainers): mirrors the ``clean_graph`` convention in
``tests/integration/test_graph_writer.py`` / ``test_edge_provenance.py``. The write entry
point is ``graph.writer.write_entities`` (the spec-named fork entry point), built from
``make_entity`` / ``stamp`` / ``Provenance``.
"""

from __future__ import annotations

import pytest

from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.canonical import pick_anchor

pytestmark = pytest.mark.integration


def _prov(source: str) -> Provenance:
    return Provenance(
        source_id=f"opensanctions:{source}",
        retrieved_at="2026-06-25T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )


def _stamped(data: dict[str, object], source: str) -> FtmEntity:
    """Build an FtM entity stamped with provenance traceable to ``source``."""
    return stamp(make_entity(data), _prov(source))


def _organization(entity_id: str, name: str, source: str) -> FtmEntity:
    return _stamped(
        {
            "id": entity_id,
            "schema": "Organization",
            "properties": {"name": [name]},
            "datasets": ["t"],
        },
        source,
    )


def _sanction(entity_id: str, targets: list[str], source: str) -> FtmEntity:
    return _stamped(
        {
            "id": entity_id,
            "schema": "Sanction",
            "properties": {"entity": targets},
            "datasets": ["t"],
        },
        source,
    )


# --------------------------------------------------------------------------------------
# 1. The crux — a real Sanction.entity -> Organization edge materializes (drop site 1).
# --------------------------------------------------------------------------------------
def test_sanction_entity_edge_materializes(clean_graph: Neo4jClient) -> None:
    """``Sanction.entity`` (abstract ``Thing`` range) must materialize a real edge.

    The headline OFAC failure: a ``Sanction`` asserting ``entity: ["org-1"]`` against a
    concrete ``Organization`` ``org-1``. ftmg drops it at the abstract-range lookup
    (``generate_entity_links`` line 227-229 — ``config.nodes.schemata.get("Thing")`` is
    ``None``), so this edge does NOT exist on the current tree. Asserts the SPECIFIC
    ``(:Sanction {id:'san-1'})-[:ENTITY]->(:Organization {id:'org-1'})`` edge — not merely
    "some edge" — so the builder cannot pass it by materializing the wrong shape.
    """
    ensure_constraints(clean_graph)

    write_entities(
        clean_graph,
        [
            _sanction("san-1", ["org-1"], "ofac-sanction"),
            _organization("org-1", "Acme Holdings", "registry"),
        ],
    )

    edges = clean_graph.execute_read(
        "MATCH (s:Sanction {id: 'san-1'})-[r:ENTITY]->(t:Organization {id: 'org-1'}) "
        "RETURN type(r) AS rel, t.id AS target"
    )
    assert len(edges) == 1, (
        "the abstract-Thing-range Sanction.entity link must materialize a real "
        "(:Sanction)-[:ENTITY]->(:Organization) edge (drop site 1 / spec §11 crux)"
    )
    assert edges[0]["rel"] == "ENTITY"
    assert edges[0]["target"] == "org-1"

    # And no spurious extra outgoing edges from the Sanction (exactly one assertion).
    out = clean_graph.execute_read("MATCH (s:Sanction {id: 'san-1'})-[r]->() RETURN count(r) AS n")[
        0
    ]["n"]
    assert out == 1, "exactly one entity-link edge from the single Sanction.entity assertion"


# --------------------------------------------------------------------------------------
# 2. Every Sanction carrying an entity-prop (with a present target) gets >=1 edge.
# --------------------------------------------------------------------------------------
def test_every_sanction_with_entity_prop_gets_an_edge(clean_graph: Neo4jClient) -> None:
    """No silent drop: every Sanction with a present entity target materializes >=1 edge.

    Three independent Sanctions, three concrete Organization targets. On the current
    tree ALL three are dropped (0 edges); post-fix every Sanction has >=1 outgoing edge.
    Asserts the per-node count so a partial fix (one edge for three assertions) fails.
    """
    ensure_constraints(clean_graph)

    sanction_ids = ["san-a", "san-b", "san-c"]
    org_ids = ["org-a", "org-b", "org-c"]
    entities: list[FtmEntity] = []
    for san_id, org_id in zip(sanction_ids, org_ids, strict=True):
        entities.append(_sanction(san_id, [org_id], f"src-{san_id}"))
        entities.append(_organization(org_id, f"Org {org_id}", f"src-{org_id}"))

    write_entities(clean_graph, entities)

    rows = clean_graph.execute_read(
        "MATCH (s:Sanction)-[r:ENTITY]->(t:Organization) "
        "RETURN s.id AS san, count(r) AS n ORDER BY san"
    )
    by_san = {row["san"]: row["n"] for row in rows}
    for san_id in sanction_ids:
        assert by_san.get(san_id, 0) >= 1, (
            f"Sanction {san_id} with a present entity target must get >=1 materialized edge"
        )
    assert sum(by_san.values()) == 3, "exactly three Sanction->Organization edges, one each"


# --------------------------------------------------------------------------------------
# 3. :Ghost dangling target — never-ingested target id (THE adversarial target).
# --------------------------------------------------------------------------------------
def test_ghost_target_tagged_and_excluded(clean_graph: Neo4jClient) -> None:
    """A Sanction -> target whose target was NEVER ingested mints a tagged ``:Ghost``.

    The adversarial case (spec §7.2 / §13, judge-heaviest). ``ghost-1`` is referenced by
    the Sanction's ``entity`` prop but is never written as a concrete entity. The fork
    MUST MERGE the target node, label it ``:Ghost``, and keep the edge — a typed
    traversal-only endpoint — rather than silently MATCH-miss and drop the assertion again.

    The ghost is structurally inert to resolution:
      * it carries NO canonical-anchor property (``wikidata_id`` / ``geonames_id`` / ``lei``
        / ``opencorporates_id``), so it can never anchor a durable canonical id; and
      * it was minted at write-time, AFTER clustering/merge, so it is never a cluster
        member — ``pick_anchor`` over any member set that does NOT contain it (it cannot,
        because a ghost is never a member) returns no ghost id.
    """
    ensure_constraints(clean_graph)

    write_entities(clean_graph, [_sanction("san-9", ["ghost-1"], "ofac-sanction")])

    # The target node exists and is tagged :Ghost.
    ghost = clean_graph.execute_read(
        "MATCH (g {id: 'ghost-1'}) RETURN labels(g) AS labels, "
        "g.wikidata_id AS wikidata_id, g.geonames_id AS geonames_id, "
        "g.lei AS lei, g.opencorporates_id AS opencorporates_id"
    )
    assert len(ghost) == 1, "the never-ingested target id must be MERGEd as a node"
    assert "Ghost" in ghost[0]["labels"], (
        "a never-ingested Sanction target must be tagged :Ghost (spec §6)"
    )

    # The edge exists — the assertion is preserved (traversable), not dropped.
    edge = clean_graph.execute_read(
        "MATCH (s:Sanction {id: 'san-9'})-[r:ENTITY]->(g:Ghost {id: 'ghost-1'}) "
        "RETURN count(r) AS n"
    )[0]["n"]
    assert edge == 1, "the Sanction->ghost edge must be preserved (traversal-only endpoint)"

    # Forward-looking: the ghost has NO anchor property, so it can never anchor a canonical
    # id (the uniqueness constraints are on exactly these properties).
    assert ghost[0]["wikidata_id"] is None
    assert ghost[0]["geonames_id"] is None
    assert ghost[0]["lei"] is None
    assert ghost[0]["opencorporates_id"] is None

    # Structural exclusion from anchoring: a ghost is never a cluster member, so the
    # anchor-derivation over the (concrete) member set never sees it and never returns it.
    members = [
        _organization("org-real", "Real Co", "registry"),
    ]
    members[0].context["wm_anchor_lei"] = ["5493001KJTIIGC8Y1R12"]
    anchor = pick_anchor(members)
    assert anchor == "wm-anchor-lei-5493001KJTIIGC8Y1R12", "real members anchor on their durable id"
    assert anchor is not None and "ghost" not in anchor.lower(), (
        "a :Ghost is never a cluster member, so pick_anchor never derives a ghost-backed id "
        "(D-GHOST: a ghost must never anchor)"
    )


# --------------------------------------------------------------------------------------
# 4. Idempotent re-projection — writing the same Sanction twice -> no duplicate edge.
# --------------------------------------------------------------------------------------
def test_reprojection_idempotent(clean_graph: Neo4jClient) -> None:
    """Re-projecting the same Sanction.entity assertion creates NO duplicate edge.

    MERGE is keyed on (source durable id, target durable id, rel-type), so a second write
    of the identical entities is a no-op on the edge (ADR 0036 crash-retry idempotency).
    """
    ensure_constraints(clean_graph)

    entities = [
        _sanction("san-2", ["org-2"], "ofac-sanction"),
        _organization("org-2", "Beta Corp", "registry"),
    ]
    write_entities(clean_graph, entities)
    write_entities(clean_graph, entities)  # re-project the identical assertion

    count = clean_graph.execute_read(
        "MATCH (:Sanction {id: 'san-2'})-[r:ENTITY]->(:Organization {id: 'org-2'}) "
        "RETURN count(r) AS n"
    )[0]["n"]
    assert count == 1, "re-projection must not duplicate the edge (MERGE idempotency)"


# --------------------------------------------------------------------------------------
# 5. Second drop site — UnknownLink (edge schema, subject/object range Thing).
# --------------------------------------------------------------------------------------
def test_unknownlink_second_site_materializes(clean_graph: Neo4jClient) -> None:
    """``UnknownLink.subject/object`` (abstract ``Thing`` range) materializes (drop site 2).

    ``UnknownLink`` is an EDGE schema whose ``subject``/``object`` both range over the
    abstract ``Thing``. ftmg's ``generate_edge_entity`` drops it at the abstract source/
    target range lookup (line 317-322) — 0 edges on the current tree. Post-fix the
    subject->object edge materializes between the two concrete endpoints.
    """
    ensure_constraints(clean_graph)

    unknown = _stamped(
        {
            "id": "ul-1",
            "schema": "UnknownLink",
            "properties": {"subject": ["org-s"], "object": ["org-o"]},
            "datasets": ["t"],
        },
        "leak",
    )
    write_entities(
        clean_graph,
        [
            unknown,
            _organization("org-s", "Subject Co", "registry"),
            _organization("org-o", "Object Co", "registry"),
        ],
    )

    # The UnknownLink edge connects its two concrete endpoints (drop site 2 fixed).
    rows = clean_graph.execute_read(
        "MATCH (s {id: 'org-s'})-[r]->(o {id: 'org-o'}) RETURN count(r) AS n"
    )
    assert rows[0]["n"] >= 1, (
        "UnknownLink.subject/object (abstract Thing range) must materialize the "
        "subject->object edge (drop site 2 / generate_edge_entity)"
    )


# --------------------------------------------------------------------------------------
# 6. G1 — the new materialized edge carries the asserting Sanction's prov_*.
# --------------------------------------------------------------------------------------
def test_edge_carries_asserting_prov(clean_graph: Neo4jClient) -> None:
    """The new Sanction->target edge carries the asserting entity's ``prov_*`` (G1).

    Every edge carries the provenance of the assertion that created it — here the
    ``Sanction`` (the property-holder), NOT either endpoint node's. Distinct sources for
    the Sanction vs the Organization prove the edge took the asserting source, never an
    endpoint's.
    """
    ensure_constraints(clean_graph)

    write_entities(
        clean_graph,
        [
            _sanction("san-3", ["org-3"], "ofac-sanction"),
            _organization("org-3", "Gamma Ltd", "company-registry"),
        ],
    )

    edge = clean_graph.execute_read(
        "MATCH (:Sanction {id: 'san-3'})-[r:ENTITY]->(:Organization {id: 'org-3'}) "
        "RETURN r.prov_source_id AS source_id, r.prov_retrieved_at AS retrieved_at, "
        "r.prov_reliability AS reliability, r.prov_source_record AS source_record"
    )
    assert len(edge) == 1, "the Sanction->Organization edge must exist to carry provenance"
    row = edge[0]
    # G1: prov_* present and traceable to the ASSERTING Sanction's landing record.
    assert row["source_id"] == "opensanctions:ofac-sanction"
    assert row["source_record"] == "s3://landing/ofac-sanction.json"
    assert row["reliability"] == "A"
    assert row["retrieved_at"] == "2026-06-25T00:00:00Z"
    # ...and NOT the Organization endpoint's source.
    assert row["source_id"] != "opensanctions:company-registry"
    assert row["source_record"] != "s3://landing/company-registry.json"


# --------------------------------------------------------------------------------------
# 7. Regression guard — concrete-range edges still CONTRACT (must stay green).
# --------------------------------------------------------------------------------------
def test_concrete_range_still_contracts(clean_graph: Neo4jClient) -> None:
    """Ownership/Directorship (concrete range) still materialize — regression unbroken.

    The fork must distinguish abstract from concrete: a concrete range keeps its schema
    label; only an abstract range falls back to ``:Entity``. This is green TODAY (ftmg
    contracts concrete-range edge schemata) and MUST stay green (spec §7.3 / D-FROZEN).
    """
    ensure_constraints(clean_graph)

    write_entities(
        clean_graph,
        [
            _stamped(
                {
                    "id": "p-1",
                    "schema": "Person",
                    "properties": {"name": ["Alice"]},
                    "datasets": ["t"],
                },
                "person",
            ),
            _stamped(
                {
                    "id": "c-1",
                    "schema": "Company",
                    "properties": {"name": ["ACME"]},
                    "datasets": ["t"],
                },
                "company",
            ),
            _stamped(
                {
                    "id": "o-1",
                    "schema": "Ownership",
                    "properties": {"owner": ["p-1"], "asset": ["c-1"]},
                    "datasets": ["t"],
                },
                "ownership",
            ),
            _stamped(
                {
                    "id": "d-1",
                    "schema": "Directorship",
                    "properties": {"director": ["p-1"], "organization": ["c-1"]},
                    "datasets": ["t"],
                },
                "directorship",
            ),
        ],
    )

    owns = clean_graph.execute_read(
        "MATCH (:Person {id: 'p-1'})-[r:OWNS]->(:Company {id: 'c-1'}) RETURN count(r) AS n"
    )[0]["n"]
    directs = clean_graph.execute_read(
        "MATCH (:Person {id: 'p-1'})-[r:DIRECTS]->(:Company {id: 'c-1'}) RETURN count(r) AS n"
    )[0]["n"]
    assert owns == 1, "concrete-range Ownership.owner/asset must still contract (regression)"
    assert directs == 1, "concrete-range Directorship must still contract (regression)"
    # Neither endpoint is a ghost: both were ingested as concrete entities.
    ghosts = clean_graph.execute_read("MATCH (g:Ghost) RETURN count(g) AS n")[0]["n"]
    assert ghosts == 0, "concrete, ingested endpoints are never ghosts"
