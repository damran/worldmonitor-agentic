"""Deterministic canonical id (B-1, ADR 0036).

A merged cluster's canonical id must be a deterministic function of its membership so a
crash+retry re-resolves to the SAME id (the graph MERGE then converges on one node instead
of minting a duplicate). These unit tests pin the id properties without a graph: same
membership -> same id, distinct membership -> distinct id, singletons keep their own id,
and the merged entity + referent map agree on the id (so edge rewriting targets the right
node). The end-to-end crash-window proof is the integration test
``test_b1_crash_recovery.py``.
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.referents import build_referent_map
from worldmonitor.resolution.splink_model import ScoredPair


def _company(entity_id: str, name: str = "Acme Corporation Ltd", country: str = "us") -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [name], "jurisdiction": [country]},
            "datasets": ["t"],
        }
    )


def _merge_of(entities: list[FtmEntity], pairs: list[ScoredPair]):
    """The single merged cluster produced from ``entities`` given ``pairs``."""
    return next(c for c in cluster_and_merge(entities, pairs) if c.is_merge)


def test_same_membership_yields_same_canonical_id() -> None:
    """The crux of B-1: re-resolving the same member set re-derives the same id."""
    first = _merge_of([_company("c1"), _company("c2")], [ScoredPair("c1", "c2", 0.99)])
    second = _merge_of([_company("c1"), _company("c2")], [ScoredPair("c1", "c2", 0.99)])
    assert first.canonical_id == second.canonical_id


def test_distinct_membership_yields_distinct_canonical_id() -> None:
    """Genuinely distinct clusters must not collide on the content-addressed id."""
    ab = _merge_of([_company("c1"), _company("c2")], [ScoredPair("c1", "c2", 0.99)]).canonical_id
    ac = _merge_of([_company("c1"), _company("c3")], [ScoredPair("c1", "c3", 0.99)]).canonical_id
    cd = _merge_of([_company("c3"), _company("c4")], [ScoredPair("c3", "c4", 0.99)]).canonical_id
    assert len({ab, ac, cd}) == 3


def test_canonical_id_is_order_independent() -> None:
    """Input/pair ordering must not change the id (members are sorted before hashing)."""
    fwd = _merge_of([_company("c1"), _company("c2")], [ScoredPair("c1", "c2", 0.99)]).canonical_id
    rev = _merge_of([_company("c2"), _company("c1")], [ScoredPair("c2", "c1", 0.99)]).canonical_id
    assert fwd == rev


def test_three_member_transitive_cluster_id_is_stable() -> None:
    """A transitively-formed 3-member cluster is content-addressed by its full membership."""
    first = _merge_of(
        [_company("c1"), _company("c2"), _company("c3")],
        [ScoredPair("c1", "c2", 0.99), ScoredPair("c2", "c3", 0.99)],
    )
    second = _merge_of(
        [_company("c3"), _company("c1"), _company("c2")],
        [ScoredPair("c3", "c2", 0.99), ScoredPair("c1", "c2", 0.99)],
    )
    assert set(first.member_ids) == {"c1", "c2", "c3"}
    assert first.canonical_id == second.canonical_id


def test_singleton_keeps_its_own_id() -> None:
    """A singleton is NOT content-hashed — its node id and inbound edges must be unchanged."""
    clusters = cluster_and_merge([_company("solo")], [])
    assert len(clusters) == 1
    assert clusters[0].is_merge is False
    assert clusters[0].canonical_id == "solo"


def test_merged_entity_and_referent_map_agree_on_canonical_id() -> None:
    """Referent rewriting must target the same id the canonical node is written under."""
    merge = _merge_of([_company("c1"), _company("c2")], [ScoredPair("c1", "c2", 0.99)])
    # A minted id, not one of the members (matches the referent-rewriting contract).
    assert merge.canonical_id not in merge.member_ids
    assert merge.canonical_id.startswith("wmc-")
    # The node is written under the canonical id, and every member edge rewrites onto it.
    assert merge.entity.id == merge.canonical_id
    assert build_referent_map([merge]) == {"c1": merge.canonical_id, "c2": merge.canonical_id}
