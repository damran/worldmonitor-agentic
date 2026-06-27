"""Property: ``resolution.merge.cluster_and_merge`` is order-independent, idempotent, and lossless.

ER is the spine — a merge that depends on INPUT ORDER is non-deterministic identity resolution (the
same batch resolves differently on re-run / crash-retry), and a merge that DROPS a member's property
values silently loses sourced facts. These are metamorphic invariants the same-distribution
example-tests (``tests/unit/test_resolution_merge_incompat.py``) never sweep.
"""

from __future__ import annotations

import random

import strategies as wm
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.resolution.merge import cluster_and_merge

_SETTINGS = settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])


@given(data=wm.multi_component_cluster(), seed=st.integers(min_value=0, max_value=10_000))
@_SETTINGS
def test_resolution_invariant_under_permutation(data: tuple[list, list, list], seed: int) -> None:
    """Permuting BOTH the entity list and the pair list must yield the IDENTICAL set of clusters
    (canonical ids + member sets + scores + property maps). Order-dependence here would be
    non-deterministic identity resolution."""
    entities, pairs, _ = data
    rng = random.Random(seed)
    perm_entities = list(entities)
    perm_pairs = list(pairs)
    rng.shuffle(perm_entities)
    rng.shuffle(perm_pairs)

    base = wm.signatures(cluster_and_merge(entities, pairs))
    permuted = wm.signatures(cluster_and_merge(perm_entities, perm_pairs))
    assert base == permuted, (
        "cluster_and_merge is order-dependent: a permutation of the same entities/pairs produced "
        f"a different result.\n  base={base}\n  perm={permuted}"
    )


@given(data=wm.multi_component_cluster())
@_SETTINGS
def test_resolution_is_idempotent_on_rerun(data: tuple[list, list, list]) -> None:
    """Re-resolving the SAME batch re-derives the SAME clusters (B-1 / ADR 0036 crash-retry)."""
    entities, pairs, _ = data
    first = wm.signatures(cluster_and_merge(entities, pairs))
    second = wm.signatures(cluster_and_merge(entities, pairs))
    assert first == second


@given(data=wm.multi_component_cluster())
@_SETTINGS
def test_grouping_matches_expected_partition(data: tuple[list, list, list]) -> None:
    """Below-threshold inter-group pairs must NOT fuse the groups: the member partition equals the
    constructed disjoint groups (each high-wired component is its own cluster)."""
    entities, pairs, expected_groups = data
    clusters = cluster_and_merge(entities, pairs)
    assert wm.member_partition(clusters) == frozenset(expected_groups)


@given(data=wm.connected_cluster())
@_SETTINGS
def test_fully_connected_collapses_to_one_cluster(data: tuple[list, list]) -> None:
    """A connected component above threshold collapses to EXACTLY ONE merged cluster whose
    member_ids are ALL input ids (no member is left behind, no second cluster is minted)."""
    entities, pairs = data
    all_ids = tuple(sorted(e.id for e in entities))
    clusters = cluster_and_merge(entities, pairs)
    merges = [c for c in clusters if c.is_merge]
    assert len(merges) == 1, f"expected one merged cluster, got {len(merges)}: {clusters}"
    assert tuple(sorted(merges[0].member_ids)) == all_ids
    assert merges[0].canonical_id.startswith("wmc-")


@given(data=wm.connected_cluster())
@_SETTINGS
def test_merged_values_are_lossless_union(data: tuple[list, list]) -> None:
    """The merged entity's value set per property is the LOSSLESS UNION of its members' values.

    Both sides are read via FtM ``.get`` (already cleaned), so cleaning is applied identically on
    each side — any mismatch is a genuinely dropped or invented value, not a cleaning artefact."""
    entities, pairs = data
    clusters = cluster_and_merge(entities, pairs)
    merged = next(c for c in clusters if c.is_merge).entity

    expected: dict[str, set[str]] = {}
    for entity in entities:
        for prop in entity.properties:
            expected.setdefault(prop, set()).update(str(v) for v in entity.get(prop))

    for prop, values in expected.items():
        got = {str(v) for v in merged.get(prop)}
        assert got == values, (
            f"property {prop!r} not a lossless union after merge: "
            f"members={sorted(values)} merged={sorted(got)}"
        )
