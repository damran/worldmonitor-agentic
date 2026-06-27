"""Property: provenance is the non-negotiable invariant — it survives merge and round-trips.

CLAUDE.md: *provenance on every node and edge*; it doubles as the GDPR/audit log. So a merge must
NOT silently drop a contributing source from the witness map (G1 / Gate C, ADR 0045), and the
single-source stamp must round-trip exactly. A dropped source is an un-auditable fact.
"""

from __future__ import annotations

import strategies as wm
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.provenance.model import (
    Provenance,
    get_provenance,
    provenance_node_properties,
    stamp,
    witness_map,
)
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.splink_model import ScoredPair


def _tagged(entity_id: str, source_id: str, name: str):  # noqa: ANN202 - FtmEntity
    from worldmonitor.ontology.ftm import make_entity

    entity = make_entity({"id": entity_id, "schema": "Company", "properties": {"name": [name]}})
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at="2026-01-01T00:00:00Z",
            reliability="B",
            source_record=f"s3://landing/{entity_id}.json",
        ),
    )


_SETTINGS = settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])


@given(
    src_a=st.sampled_from(wm.SOURCE_POOL),
    src_b=st.sampled_from(wm.SOURCE_POOL),
    name=st.text(alphabet="ABCDEFabcdef", min_size=2, max_size=8),
)
@_SETTINGS
def test_merge_keeps_both_sources_in_witness_map(src_a: str, src_b: str, name: str) -> None:
    """An entity merged from TWO distinct sources names BOTH in its witness map — no source silently
    dropped (Gate C, ADR 0045). Both members share the ``name`` property, so its witness set must be
    exactly the two contributing sources."""
    if src_a == src_b:
        return  # same source is not a two-source merge; the distinct case is what we pin
    a = _tagged("a", src_a, name)
    b = _tagged("b", src_b, name)
    clusters = cluster_and_merge([a, b], [ScoredPair("a", "b", 0.99)])
    merged = next(c for c in clusters if c.is_merge).entity

    wmap = witness_map(merged)
    all_sources = set().union(*wmap.values()) if wmap else set()
    assert {src_a, src_b} <= all_sources, (
        f"a source was dropped from the witness map: expected both {src_a!r},{src_b!r}, "
        f"witness_map={wmap}"
    )
    assert wmap.get("name") == {src_a, src_b}, (
        f"name witnesses should be exactly both sources, got {wmap.get('name')}"
    )


@given(
    src_a=st.sampled_from(wm.SOURCE_POOL),
    src_b=st.sampled_from(wm.SOURCE_POOL),
)
@_SETTINGS
def test_merged_node_carries_g1_prov_source_id(src_a: str, src_b: str) -> None:
    """G1: every merged node carries a ``prov_source_id`` property (provenance on every node)."""
    a = _tagged("a", src_a, "Acme")
    b = _tagged("b", src_b, "Acme")
    clusters = cluster_and_merge([a, b], [ScoredPair("a", "b", 0.99)])
    merged = next(c for c in clusters if c.is_merge).entity
    node_props = provenance_node_properties(merged)
    assert node_props.get("prov_source_id"), f"merged node has no G1 prov_source_id: {node_props}"


@given(
    source_id=st.text(alphabet="abcdefABCDEF-", min_size=1, max_size=10),
    retrieved=st.text(alphabet="0123456789T:-Z", min_size=1, max_size=20),
    reliability=st.sampled_from(["A", "B", "C", "D", "E", "F"]),
    record=st.text(alphabet="abc/.:0123", min_size=1, max_size=12),
)
@_SETTINGS
def test_stamp_get_provenance_roundtrip(
    source_id: str, retrieved: str, reliability: str, record: str
) -> None:
    """``stamp`` then ``get_provenance`` round-trips every field exactly, and a single-source
    entity's witness map is ``{prop: {source_id}}`` for each value-bearing property."""
    from worldmonitor.ontology.ftm import make_entity

    prov = Provenance(
        source_id=source_id,
        retrieved_at=retrieved,
        reliability=reliability,
        source_record=record,
    )
    entity = make_entity({"id": "x", "schema": "Company", "properties": {"name": ["Acme"]}})
    stamp(entity, prov)

    got = get_provenance(entity)
    assert got == prov, f"provenance did not round-trip: {got!r} != {prov!r}"

    wmap = witness_map(entity)
    assert wmap.get("name") == {source_id}, (
        f"single-source witness map should be {{name: {{{source_id!r}}}}}, got {wmap}"
    )
