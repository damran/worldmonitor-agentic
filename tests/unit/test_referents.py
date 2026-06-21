"""Unit tests for referent rewriting (G2).

Pure-logic coverage for the two primitives the resolution pipeline uses to keep
edges from dangling after a merge: building the member->canonical map (only from
promoted clusters) and rewriting an entity's entity-typed references through it.
No Neo4j/Postgres — this is deliberately backing-service-free.
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, get_provenance, stamp
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.referents import build_referent_map, rewrite_referents


def _cluster(canonical_id: str, member_ids: tuple[str, ...]) -> ResolvedCluster:
    return ResolvedCluster(
        canonical_id=canonical_id,
        member_ids=member_ids,
        entity=make_entity({"id": canonical_id, "schema": "Company"}),
        score=1.0,
    )


# --------------------------------------------------------------------------- #
# build_referent_map
# --------------------------------------------------------------------------- #
def test_singleton_maps_to_itself() -> None:
    referents = build_referent_map([_cluster("a", ("a",))])
    assert referents == {"a": "a"}


def test_merge_maps_every_member_to_canonical() -> None:
    referents = build_referent_map([_cluster("CANON", ("petrov-a", "petrov-b"))])
    assert referents == {"petrov-a": "CANON", "petrov-b": "CANON"}


def test_multiple_clusters_union() -> None:
    referents = build_referent_map(
        [_cluster("CANON", ("petrov-a", "petrov-b")), _cluster("ivan", ("ivan",))]
    )
    assert referents == {"petrov-a": "CANON", "petrov-b": "CANON", "ivan": "ivan"}


def test_empty_clusters_give_empty_map() -> None:
    assert build_referent_map([]) == {}


# --------------------------------------------------------------------------- #
# rewrite_referents
# --------------------------------------------------------------------------- #
def test_edge_endpoint_rewritten_to_canonical() -> None:
    own = make_entity(
        {
            "id": "own1",
            "schema": "Ownership",
            "properties": {"owner": ["ivan"], "asset": ["petrov-b"]},
        }
    )
    rewrite_referents(own, {"petrov-b": "CANON", "ivan": "ivan"})
    assert own.get("owner") == ["ivan"]  # singleton, unchanged
    assert own.get("asset") == ["CANON"]  # merged-away id redirected


def test_reference_absent_from_map_is_unchanged() -> None:
    """A reference to a parked / out-of-batch id stays put (block-mode rule)."""
    own = make_entity(
        {"id": "own1", "schema": "Ownership", "properties": {"owner": ["p1"], "asset": ["c1"]}}
    )
    rewrite_referents(own, {"c1": "CANON-C"})  # p1 (a parked member) not in the map
    assert own.get("owner") == ["p1"]  # untouched
    assert own.get("asset") == ["CANON-C"]


def test_non_entity_properties_untouched() -> None:
    own = make_entity(
        {
            "id": "own1",
            "schema": "Ownership",
            "properties": {"owner": ["a"], "asset": ["b"], "role": ["shareholder"]},
        }
    )
    rewrite_referents(own, {"a": "CANON-A", "b": "CANON-B"})
    assert own.get("role") == ["shareholder"]  # literal property survives


def test_non_edge_entity_reference_rewritten() -> None:
    """Entity-typed props on non-edge schemata (e.g. Membership.member) rewrite too."""
    mem = make_entity(
        {
            "id": "m1",
            "schema": "Membership",
            "properties": {"member": ["p1"], "organization": ["o1"]},
        }
    )
    rewrite_referents(mem, {"p1": "CANON-P"})
    assert mem.get("member") == ["CANON-P"]
    assert mem.get("organization") == ["o1"]


def test_multivalued_reference_partially_rewritten() -> None:
    san = make_entity({"id": "s1", "schema": "Sanction", "properties": {"entity": ["x", "y", "z"]}})
    rewrite_referents(san, {"x": "CANON-X", "z": "CANON-Z"})  # y unmapped
    assert san.get("entity") == ["CANON-X", "y", "CANON-Z"]


def test_empty_map_is_a_noop() -> None:
    own = make_entity(
        {"id": "own1", "schema": "Ownership", "properties": {"owner": ["a"], "asset": ["b"]}}
    )
    rewrite_referents(own, {})
    assert own.get("owner") == ["a"]
    assert own.get("asset") == ["b"]


def test_provenance_preserved_through_rewrite() -> None:
    """G1: rewriting an endpoint must not strip the edge's asserting provenance."""
    own = make_entity(
        {"id": "own1", "schema": "Ownership", "properties": {"owner": ["a"], "asset": ["petrov-b"]}}
    )
    stamp(
        own,
        Provenance(
            source_id="opensanctions:test",
            retrieved_at="2026-06-21T00:00:00Z",
            reliability="A",
            source_record="s3://landing/own1.json",
        ),
    )
    rewrite_referents(own, {"petrov-b": "CANON"})
    prov = get_provenance(own)
    assert prov is not None
    assert prov.source_id == "opensanctions:test"
    assert prov.source_record == "s3://landing/own1.json"
    assert own.get("asset") == ["CANON"]


def test_returns_same_entity_instance() -> None:
    own = make_entity({"id": "own1", "schema": "Ownership", "properties": {"owner": ["a"]}})
    assert rewrite_referents(own, {"a": "CANON"}) is own
