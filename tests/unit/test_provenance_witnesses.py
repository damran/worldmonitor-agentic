"""Gate C — Tier-1 witness-map derivation (`provenance.model.witness_map`).

Unit-level (Docker-free) proof that the per-property witness map is derived faithfully from a fused
``StatementEntity`` — covering the cases the repo-root oracle (`tests/test_provenance_merge.py`)
leaves to the builder: a single-source entity yields singleton sets, a fused entity reflects ALL
contributing datasets, the ``id`` pseudo-property is excluded, an unstamped/un-provenanced entity
yields the empty map, and the witness map round-trips through ``stamp_witness_map`` and an FtM
``to_dict`` serialization (so it survives ``rekey_cluster``). Spec §3/§5; proves A1/A2 (Tier-1).
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import (
    WITNESSES_NODE_PROPERTY,
    Provenance,
    stamp,
    stamp_witness_map,
    witness_map,
    witness_node_properties,
)
from worldmonitor.resolution.merge import _merge_entities


def _source_entity(entity_id: str, source_id: str, props: dict[str, list[str]]) -> object:
    entity = make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": [source_id]}
    )
    stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at="2026-06-25T00:00:00Z",
            reliability="A",
            source_record=f"s3://landing/{source_id}/{entity_id}.json",
        ),
    )
    return entity


def test_single_source_entity_yields_singleton_sets() -> None:
    """A single-source entity (no fusion) witnesses each prop by exactly its one source."""
    entity = _source_entity("e-1", "src-A", {"name": ["Acme"], "nationality": ["us"]})
    assert witness_map(entity) == {"name": {"src-A"}, "nationality": {"src-A"}}


def test_unstamped_entity_yields_empty_map() -> None:
    """An entity with no provenance and no stamped witness map has no witnesses."""
    entity = make_entity(
        {"id": "x", "schema": "Person", "properties": {"name": ["Z"]}, "datasets": ["t"]}
    )
    assert witness_map(entity) == {}


def test_id_pseudo_property_is_excluded() -> None:
    """The ``id`` pseudo-property is never a witnessed value."""
    entity = _source_entity("e-1", "src-A", {"name": ["Acme"]})
    assert "id" not in witness_map(entity)


def test_fused_entity_reflects_all_contributing_datasets() -> None:
    """A 3-source fusion's witness map covers every contributing dataset per prop."""
    by_id = {
        "e-a": _source_entity("e-a", "src-A", {"name": ["Acme"], "nationality": ["us"]}),
        "e-b": _source_entity("e-b", "src-B", {"name": ["Acme"], "nationality": ["us"]}),
        "e-c": _source_entity(
            "e-c", "src-C", {"name": ["Acme"], "nationality": ["us"], "passportNumber": ["P-9"]}
        ),
    }
    merged, dropped = _merge_entities("wmc-x", ("e-a", "e-b", "e-c"), by_id)
    assert dropped == ()
    witnesses = witness_map(merged)
    assert witnesses["name"] == {"src-A", "src-B", "src-C"}
    assert witnesses["nationality"] == {"src-A", "src-B", "src-C"}
    # The single-source value is witnessed by exactly its one asserting dataset.
    assert witnesses["passportNumber"] == {"src-C"}


def test_witness_map_survives_serialization_roundtrip() -> None:
    """A stamped witness map travels through ``to_dict``/``from_dict`` (rekey_cluster path)."""
    entity = make_entity(
        {"id": "e-1", "schema": "Person", "properties": {"name": ["Acme"]}, "datasets": ["t"]}
    )
    stamp_witness_map(entity, {"name": {"src-A", "src-B"}})
    rekeyed = make_entity({**entity.to_dict(), "id": "wmc-new"})
    assert witness_map(rekeyed) == {"name": {"src-A", "src-B"}}


def test_witness_node_properties_encodes_single_json_property() -> None:
    """The Neo4j projection is one JSON-string property recovering the per-prop sets on parse."""
    import json

    by_id = {
        "e-a": _source_entity("e-a", "src-A", {"name": ["Acme"]}),
        "e-c": _source_entity("e-c", "src-C", {"name": ["Acme"], "passportNumber": ["P-9"]}),
    }
    merged, _ = _merge_entities("wmc-x", ("e-a", "e-c"), by_id)
    node_props = witness_node_properties(merged)
    assert set(node_props) == {WITNESSES_NODE_PROPERTY}
    decoded = json.loads(node_props[WITNESSES_NODE_PROPERTY])
    assert decoded["name"] == ["src-A", "src-C"]
    assert decoded["passportNumber"] == ["src-C"]


def test_empty_entity_has_no_witness_node_property() -> None:
    """A value-less entity projects no witness node property (clean node)."""
    entity = make_entity({"id": "x", "schema": "Person", "properties": {}, "datasets": ["t"]})
    assert witness_node_properties(entity) == {}
