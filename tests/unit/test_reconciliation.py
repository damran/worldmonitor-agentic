"""Unit tests for Gate 3b reconciliation instruments (ADR 0114 D-5).

Concrete, hand-built examples exercising each instrument's happy path plus the specific red-team
scenarios from ``docs/fable-review/82_GATE_3B_CUTOVER_PLAN.md`` §4 (and its §7 red-team findings).
See ``tests/property/test_prop_reconciliation.py``'s module docstring for the full dataclass/field
contract these tests (and the mandatory ``@given`` suite) both pin.

Docker-free throughout: pure in-memory ``GraphSnapshot``/``NodeSnapshot``/``EdgeSnapshot``
dataclasses (imported from ``worldmonitor.resolution.divergence``, reused not redefined), no
testcontainers, no Neo4j client, no DB session.

RED at collection time: ``worldmonitor.resolution.reconciliation`` does not exist yet — the
module-level import below fails with ``ImportError``. That is the correct, intended TDD failure
mode (the Gate 3a-i / 3a-ii-B precedent).
"""

from __future__ import annotations

import json

from worldmonitor.resolution.divergence import EdgeSnapshot, GraphSnapshot, NodeSnapshot
from worldmonitor.resolution.reconciliation import (  # gate import: RED until builder lands
    CountReconciliation,
    ErasedResidue,
    FoldSideExtras,
    LabelParity,
    compare_labels,
    enumerate_fold_side_extras,
    find_copresent_value_divergence,
    find_erased_source_residue,
    reconcile_counts,
)


def _identity(token: str) -> str:
    return token


# ===========================================================================================
# enumerate_fold_side_extras — happy path + E1 consolidation + true extras (R7)
# ===========================================================================================


def test_enumerate_fold_side_extras_identical_graphs_has_no_extras() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset({"Thing"}), props={}),),
        edges=(EdgeSnapshot(type="OWNS", src="s1", dst="s1", props={}),),
    )
    live = fold
    result = enumerate_fold_side_extras(live, fold, _identity)
    assert isinstance(result, FoldSideExtras)
    assert result.nodes == ()
    assert result.edges == ()


def test_enumerate_fold_side_extras_legit_e1_consolidation_is_not_extra() -> None:
    """A live alias L with survivor_of(L.id)==S, S a fold node, must NOT be reported as extra."""

    def survivor_of(token: str) -> str:
        return {"old-alias": "survivor-1"}.get(token, token)

    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="survivor-1", labels=frozenset(), props={}),), edges=()
    )
    live = GraphSnapshot(
        nodes=(NodeSnapshot(id="old-alias", labels=frozenset(), props={}),), edges=()
    )
    result = enumerate_fold_side_extras(live, fold, survivor_of)
    assert result.nodes == (), (
        "a legitimate E1 consolidation (live alias resolving to a fold survivor) must not be "
        "reported as a fold-side extra"
    )


def test_enumerate_fold_side_extras_finds_node_and_edge_with_no_live_preimage() -> None:
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(id="survivor-1", labels=frozenset(), props={}),
            NodeSnapshot(id="ghost-node", labels=frozenset(), props={}),
        ),
        edges=(
            EdgeSnapshot(type="OWNS", src="survivor-1", dst="survivor-1", props={}),
            EdgeSnapshot(type="GHOST_REL", src="survivor-1", dst="ghost-node", props={}),
        ),
    )
    live = GraphSnapshot(
        nodes=(NodeSnapshot(id="survivor-1", labels=frozenset(), props={}),),
        edges=(EdgeSnapshot(type="OWNS", src="survivor-1", dst="survivor-1", props={}),),
    )
    result = enumerate_fold_side_extras(live, fold, _identity)
    assert [n.id for n in result.nodes] == ["ghost-node"]
    assert [(e.type, e.src, e.dst) for e in result.edges] == [
        ("GHOST_REL", "survivor-1", "ghost-node")
    ]


# ===========================================================================================
# find_copresent_value_divergence — happy path + wrong-but-logged catastrophic merge (R9c)
# ===========================================================================================


def test_find_copresent_value_divergence_identical_props_has_no_findings() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={"traits": frozenset({"a"})}),),
        edges=(),
    )
    live = fold
    assert find_copresent_value_divergence(live, fold, _identity) == []


def test_find_copresent_value_divergence_wrong_but_logged_merge_still_surfaces_genuine_extra() -> (
    None
):
    """82 §4 R9c red-team scenario: a wrong-but-logged catastrophic merge replayed faithfully — the
    fold node's props are a SUPERSET of the live node's (so R4/measure_divergence's live->fold
    subset direction PASSES, total==0, blind to this) — but R9c still surfaces the genuine
    fold-only non-excluded value with no live counterpart."""
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="wrong-survivor",
                labels=frozenset(),
                # 'traits' superset-subsumes live's value AND carries one genuinely fold-only value
                # (from the wrongly-merged-in member) that live never had.
                props={"traits": frozenset({"shared-value", "wrongly-merged-in-value"})},
            ),
        ),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="wrong-survivor",
                labels=frozenset(),
                props={"traits": frozenset({"shared-value"})},
            ),
        ),
        edges=(),
    )
    result = find_copresent_value_divergence(live, fold, _identity)
    findings = [(e.node_id, e.prop, e.value) for e in result]
    assert findings == [("wrong-survivor", "traits", "wrongly-merged-in-value")], (
        "R9c must surface the fold-only value even though the live->fold subset direction (R4) "
        f"would pass cleanly for this exact node; got {findings!r}"
    )


def test_find_copresent_value_divergence_excluded_axis_never_flagged_with_divergence() -> None:
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1",
                labels=frozenset(),
                props={
                    "traits": frozenset({"shared", "genuinely-extra"}),
                    "wikidata_id": frozenset({"Q999"}),
                    "datasets": frozenset({"fold-batch"}),
                    "prov_source_id": frozenset({"src:fold"}),
                    "caption": frozenset({"Fold Caption"}),
                },
            ),
        ),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1",
                labels=frozenset(),
                props={
                    "traits": frozenset({"shared"}),
                    "datasets": frozenset({"live-batch"}),
                    "prov_source_id": frozenset({"src:live"}),
                    "caption": frozenset({"Live Caption"}),
                },
            ),
        ),
        edges=(),
    )
    result = find_copresent_value_divergence(live, fold, _identity)
    findings = [(e.node_id, e.prop, e.value) for e in result]
    assert findings == [("s1", "traits", "genuinely-extra")], (
        f"only the non-excluded 'traits' divergence may surface; got {findings!r} (the missing "
        "live 'wikidata_id'/differing 'datasets'/differing 'prov_source_id'/differing 'caption' "
        "must all stay excluded)"
    )


# ===========================================================================================
# reconcile_counts — happy path + same-id duplicate carrying an un-logged anchor (R11b)
# ===========================================================================================


def test_reconcile_counts_balanced_has_zero_residual() -> None:
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(id="s1", labels=frozenset(), props={}),
            NodeSnapshot(id="s2", labels=frozenset(), props={}),
        ),
        edges=(EdgeSnapshot(type="OWNS", src="s1", dst="s2", props={}),),
    )
    live = fold
    result = reconcile_counts(live, fold, _identity)
    assert isinstance(result, CountReconciliation)
    assert result.live_nodes == 2
    assert result.fold_nodes == 2
    assert result.distinct_live_node_survivors == 2
    assert result.duplicate_live_node_ids == 0
    assert result.fold_side_extra_nodes == 0
    assert result.node_residual == 0
    assert result.live_edges == 1
    assert result.fold_edges == 1
    assert result.distinct_live_edge_survivors == 1
    assert result.duplicate_live_edge_ids == 0
    assert result.fold_side_extra_edges == 0
    assert result.edge_residual == 0


def test_reconcile_counts_same_id_duplicate_with_unlogged_anchor_does_not_vanish_count_clean() -> (
    None
):
    """82 §4 R11b red-team scenario: two live NodeSnapshots share ONE id (no n.id UNIQUE
    constraint in this domain); the duplicate carries a bare CANONICAL_ID_FIELDS anchor
    (``wikidata_id``) the ORIGINAL row never logged. The duplicate must show up in
    duplicate_live_node_ids and node_residual must NOT silently read 0."""
    fold = GraphSnapshot(nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={}),), edges=())
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(id="s1", labels=frozenset(), props={}),
            NodeSnapshot(
                id="s1",  # SAME raw id as the row above -- a materialised duplicate
                labels=frozenset(),
                props={"wikidata_id": frozenset({"Q-UNLOGGED"})},
            ),
        ),
        edges=(),
    )
    result = reconcile_counts(live, fold, _identity)
    assert result.live_nodes == 2
    assert result.fold_nodes == 1
    assert result.distinct_live_node_survivors == 1, (
        "both duplicate rows share the same id, so survivor_of collapses them to ONE distinct "
        "survivor target — this is exactly why a separate multiplicity term is required"
    )
    assert result.duplicate_live_node_ids == 1, (
        "the same-id duplicate MUST be counted in duplicate_live_node_ids"
    )
    assert result.node_residual != 0, (
        "INV-RECON-MULTIPLICITY VIOLATED: node_residual must NOT silently balance to 0 while a "
        f"same-id duplicate (carrying an un-logged anchor) exists; got {result.node_residual}"
    )
    assert result.node_residual == 1


# ===========================================================================================
# compare_labels — happy path + dropped :Sanction topic label (§3.1 LOSS direction)
# ===========================================================================================


def test_compare_labels_identical_labels_has_no_findings() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset({"Thing", "Company"}), props={}),), edges=()
    )
    live = fold
    result = compare_labels(live, fold, _identity)
    assert isinstance(result, LabelParity)
    assert result.missing_in_fold == ()
    assert result.extra_in_fold == ()


def test_compare_labels_dropped_sanction_topic_label_surfaces_in_missing_in_fold() -> None:
    """§3.1: the dropped-label detecting direction is live_labels ⊆ fold_labels (LOSS) — a naive
    fold_labels ⊆ live_labels check stays TRUE exactly when this bug is present, so it must be
    caught the OTHER way."""
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="entity-1", labels=frozenset({"Person"}), props={}),), edges=()
    )
    live = GraphSnapshot(
        nodes=(NodeSnapshot(id="entity-1", labels=frozenset({"Person", "Sanction"}), props={}),),
        edges=(),
    )
    result = compare_labels(live, fold, _identity)
    missing = {(e.node_id, e.label) for e in result.missing_in_fold}
    assert missing == {("entity-1", "Sanction")}, (
        f"a dropped :Sanction topic label present on live but absent from the co-present fold "
        f"node must appear in missing_in_fold; got {missing!r}"
    )
    assert result.extra_in_fold == ()


def test_compare_labels_fold_invented_label_surfaces_in_extra_in_fold() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="entity-1", labels=frozenset({"Person", "Invented"}), props={}),),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(NodeSnapshot(id="entity-1", labels=frozenset({"Person"}), props={}),), edges=()
    )
    result = compare_labels(live, fold, _identity)
    extra = {(e.node_id, e.label) for e in result.extra_in_fold}
    assert extra == {("entity-1", "Invented")}
    assert result.missing_in_fold == ()


# ===========================================================================================
# find_erased_source_residue — happy path + erased value co-present on a multi-source node (R9b)
# ===========================================================================================


def test_find_erased_source_residue_clean_fold_has_no_findings() -> None:
    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id="s1", labels=frozenset(), props={"datasets": frozenset({"ok"})}),),
        edges=(),
    )
    assert find_erased_source_residue(fold, frozenset({"erased-source"})) == []


def test_find_erased_source_residue_flags_fold_extra_node_via_datasets() -> None:
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="ghost-node",
                labels=frozenset(),
                props={"datasets": frozenset({"erased-source", "ok-source"})},
            ),
        ),
        edges=(),
    )
    result = find_erased_source_residue(fold, frozenset({"erased-source"}))
    findings = {(r.node_id, r.source_id) for r in result}
    assert isinstance(result[0], ErasedResidue)
    assert findings == {("ghost-node", "erased-source")}


def test_find_erased_source_residue_flags_copresent_multi_source_node_via_prov_witnesses() -> None:
    """82 §4 R9b red-team CRITICAL: a multi-source node value-pruned in place (``ops.py``'s
    surviving-node prune keeps the node, dropping only the erased source's contribution) whose
    ``datasets`` no longer mentions the erased source, but whose ``prov_witnesses`` map still
    references it — this is the co-present resurrection signal R9 (node-count-only) misses."""
    witnesses_json = json.dumps({"traits": ["ok-source", "erased-source"]})
    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="multi-source-survivor",
                labels=frozenset(),
                props={
                    "datasets": frozenset({"ok-source"}),  # already scrubbed clean
                    "prov_witnesses": frozenset({witnesses_json}),  # residue still present
                },
            ),
        ),
        edges=(),
    )
    result = find_erased_source_residue(fold, frozenset({"erased-source"}))
    findings = {(r.node_id, r.source_id) for r in result}
    assert findings == {("multi-source-survivor", "erased-source")}, (
        "a co-present multi-source node whose datasets set was already pruned but whose "
        "prov_witnesses map still references the erased source MUST be flagged — this is the "
        "resurrected-value signal R9b exists to catch"
    )
