"""Property/metamorphic tests for Gate 3b reconciliation instruments (ADR 0114 D-5).

``worldmonitor.resolution.reconciliation`` (NEW, does not exist yet) mirrors
``worldmonitor.resolution.divergence`` (ADR 0102): pure, Neo4j-free, ``@given``-tested functions
over ``NodeSnapshot``/``EdgeSnapshot``/``GraphSnapshot`` (imported, not redefined) that answer the
one-time Gate 3b cutover reconciliation runbook (``docs/fable-review/82_GATE_3B_CUTOVER_PLAN.md``
§4). Each test class below pins ONE ``INV-RECON-*`` invariant from ``.claude/gate.scope``.

RED at collection time: ``worldmonitor.resolution.reconciliation`` does not exist yet — importing
``FoldSideExtras``/``CoPresentValueDivergence``/``ErasedResidue``/``CountReconciliation``/
``LabelParity``/``enumerate_fold_side_extras``/``find_copresent_value_divergence``/
``find_erased_source_residue``/``reconcile_counts``/``compare_labels`` fails with ``ImportError``.
That is the correct, intended TDD failure mode (the Gate 3a-i / 3a-ii-B precedent).

NOT marked ``@pytest.mark.integration`` — this file is pure and Docker-free throughout (no
testcontainers, no engine, no Neo4j driver anywhere in this file or the module under test).

=====================================================================================================
THE CONTRACT this suite pins for the builder (``.claude/gate.scope`` gives function names + a
partial field list; every choice below that the spec leaves open is resolved HERE and MUST be
matched byte-for-name by the implementation)
=====================================================================================================

Reused, NOT redefined, from ``worldmonitor.resolution.divergence``: ``NodeSnapshot``,
``EdgeSnapshot``, ``GraphSnapshot``, ``_excluded`` (the exclusion predicate — id / caption /
``CANONICAL_ID_FIELDS`` bare anchor keys / ``datasets`` / ``prov_*``). Do NOT reimplement a second,
driftable copy of ``_excluded`` — that duplication is the exact hazard ``build_survivor_of``/ADR
0102 already avoids once.

``FoldSideExtras`` (frozen dataclass) — result of ``enumerate_fold_side_extras(live, fold,
survivor_of) -> FoldSideExtras``:
    nodes: tuple[NodeSnapshot, ...]   # fold nodes with NO live node L s.t. survivor_of(L.id)==F.id
    edges: tuple[EdgeSnapshot, ...]   # fold edges with no live edge under the same
                                      # (type, survivor_of(src), survivor_of(dst)) key
    Both tuples are DETERMINISTICALLY SORTED: nodes by ``.id`` ascending, edges by
    ``(.type, .src, .dst)`` ascending (my choice — gate.scope only says "sorted outputs").

``CoPresentValueDivergence`` (frozen dataclass) — one row per (co-present node, non-excluded prop,
offending value) triple, flat ``list[CoPresentValueDivergence]`` from
``find_copresent_value_divergence(live, fold, survivor_of)``:
    node_id: str   # the CO-PRESENT node id (== the fold node id == survivor_of(some live id))
    prop: str      # the non-excluded property name (never id/caption/anchor/datasets/prov_*)
    value: str     # the survivor_of-NORMALISED fold value with no live counterpart on that prop
    A "co-present" node is a fold node F for which some live node L has survivor_of(L.id)==F.id.
    Normalise BOTH sides via survivor_of exactly as ``_props_subset`` does (fold -> live
    direction, reversed from ``measure_divergence``'s live -> fold). List sorted by
    ``(node_id, prop, value)``.

``ErasedResidue`` (frozen dataclass) — one row per DISTINCT ``(node_id, source_id)`` pair (a fold
node referencing an erased source de-duplicates to ONE row per erased id, regardless of how many
props/witness-keys reference it), from
``find_erased_source_residue(fold, erased_source_ids) -> list[ErasedResidue]``:
    node_id: str
    source_id: str   # the ERASED source_id this fold node still references
    Fires when EITHER the node's ``datasets`` prop value-set contains an erased source_id, OR the
    decoded ``prov_witnesses`` JSON map (``{prop: [datasets...]}``, see ``graph/ops.py
    ::_decode_witnesses``) has an erased source_id in any prop's dataset list. Applies uniformly to
    every fold node — fold-side-extra AND co-present alike (this function takes only ``fold`` +
    ``erased_source_ids``, no ``live``/``survivor_of``, exactly per gate.scope's signature). List
    sorted by ``(node_id, source_id)``.

``CountReconciliation`` (frozen dataclass) — result of ``reconcile_counts(live, fold,
survivor_of) -> CountReconciliation``. gate.scope spells out the node-side field names literally
(``live_nodes, fold_nodes, distinct_live_survivors, duplicate_live_ids, fold_side_extra_nodes,
residual``) and says "same shape for edges" without spelling the edge names. My resolution: a FLAT
dataclass, node fields used VERBATIM where gate.scope already names them, the three ambiguous ones
(``distinct_live_survivors``, ``duplicate_live_ids``, ``residual``) disambiguated with an explicit
``_node_``/``_edge_`` infix so a single flat object can hold both sides unambiguously:
    live_nodes: int
    fold_nodes: int
    distinct_live_node_survivors: int   # |{survivor_of(n.id) for n in live.nodes}|
    duplicate_live_node_ids: int        # live_nodes - |{n.id for n in live.nodes}| (RAW ids,
                                        # un-normalised -- the "no n.id UNIQUE" multiplicity term)
    fold_side_extra_nodes: int          # == len(enumerate_fold_side_extras(...).nodes)
    node_residual: int
    live_edges: int
    fold_edges: int
    distinct_live_edge_survivors: int   # edge keys (type, survivor_of(src), survivor_of(dst))
    duplicate_live_edge_ids: int        # live_edges - |{(type,src,dst) for e in live}| (RAW)
    fold_side_extra_edges: int          # == len(enumerate_fold_side_extras(...).edges)
    edge_residual: int

    RESIDUAL FORMULA (the load-bearing part of INV-RECON-MULTIPLICITY — pinned here as the
    contract; a same-id duplicate's ``survivor_of`` is IDENTICAL to the original row's, so
    ``distinct_live_node_survivors`` alone is BLIND to it — that blindness is exactly the
    "absorbed into alias_collapse" hazard §4 R11b names. The fix is that ``duplicate_live_node_ids``
    is counted on RAW ids (never survivor_of-normalised) and enters the residual as its OWN
    additive term, so it can never be silently cancelled by the alias-collapse arithmetic):

        node_residual = duplicate_live_node_ids
                        + (fold_nodes - distinct_live_node_survivors - fold_side_extra_nodes)
        edge_residual = duplicate_live_edge_ids
                        + (fold_edges - distinct_live_edge_survivors - fold_side_extra_edges)

    ``node_residual == 0`` / ``edge_residual == 0`` when perfectly balanced (no duplicate, every
    live survivor reflected once in the fold, every fold-side extra accounted for).

``LabelParity`` (frozen dataclass) — result of ``compare_labels(live, fold, survivor_of) ->
LabelParity``, using gate.scope's own two literal field names as the top-level fields:
    missing_in_fold: tuple[LabelDivergence, ...]   # LOSS direction: live label absent from the
                                                    # co-present fold node's label set
    extra_in_fold: tuple[LabelDivergence, ...]     # fold-invented: fold label absent from live
``LabelDivergence`` (frozen dataclass, my own helper name for one entry — not named in gate.scope):
    node_id: str
    label: str
Both tuples sorted by ``(node_id, label)``.

INV-RECON-PURE: a static/import-time check (no ``@given``, no Docker) that
``worldmonitor.resolution.reconciliation``'s own source imports no ``neo4j``/``sqlalchemy``/
``psycopg``/``asyncpg`` module and none of ``worldmonitor.db``/``worldmonitor.graph.neo4j_client``/
``worldmonitor.graph.writer``/``worldmonitor.graph.ops``/``worldmonitor.runner`` — mirroring how
``divergence.py``'s purity is relied on (module docstring, ADR 0102 D9).
"""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS
from worldmonitor.resolution.divergence import EdgeSnapshot, GraphSnapshot, NodeSnapshot
from worldmonitor.resolution.reconciliation import (  # gate import: RED until builder lands
    CoPresentValueDivergence,
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

_SETTINGS = settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _identity(token: str) -> str:
    return token


def _draw_pool(draw: st.DrawFn, size: int) -> list[str]:
    """A pairwise-DISTINCT pool of ``size`` short tokens (structural uniqueness via a permutation
    of a fixed integer range — the house idiom from ``test_prop_projection_divergence.py``)."""
    perm = draw(st.permutations(list(range(size))))
    return [f"tok{i}" for i in perm]


def _taker(pool: list[str]) -> Callable[[int], list[str]]:
    """A stateful ``take(n)`` cursor over ``pool`` — successive calls return disjoint slices."""
    state = {"cursor": 0}

    def take(n: int) -> list[str]:
        cursor = state["cursor"]
        assert cursor + n <= len(pool), "token pool exhausted — increase the pool size"
        out = pool[cursor : cursor + n]
        state["cursor"] = cursor + n
        return out

    return take


# ===========================================================================================
# INV-RECON-FOLD-EXTRA — enumerate_fold_side_extras
# ===========================================================================================


@dataclass(frozen=True)
class _FoldExtraScenario:
    fold: GraphSnapshot
    live: GraphSnapshot
    survivor_of: Callable[[str], str]
    extra_node_ids: frozenset[str]
    extra_edges: tuple[EdgeSnapshot, ...]
    survivor_ids: frozenset[str]
    legit_edge_keys: frozenset[tuple[str, str, str]]


@st.composite
def _fold_extra_scenario(draw: st.DrawFn) -> _FoldExtraScenario:
    pool = _draw_pool(draw, 40)
    take = _taker(pool)

    n_survivors = draw(st.integers(min_value=1, max_value=4))
    survivors = take(n_survivors)
    n_fold_extra_nodes = draw(st.integers(min_value=0, max_value=3))
    fold_extra_node_ids = take(n_fold_extra_nodes)

    fold_nodes: list[NodeSnapshot] = [
        NodeSnapshot(id=s, labels=frozenset({"Thing"}), props={}) for s in survivors
    ] + [NodeSnapshot(id=x, labels=frozenset({"Thing"}), props={}) for x in fold_extra_node_ids]

    alias_map: dict[str, str] = {}
    live_ids: list[str] = []
    for i, surv in enumerate(survivors):
        # survivor 0 is FORCED through an alias every single example — the legitimate E1
        # consolidation clause of INV-RECON-FOLD-EXTRA is exercised on every run, not just
        # probabilistically.
        use_alias = True if i == 0 else draw(st.booleans())
        if use_alias:
            alias_tok = take(1)[0]
            alias_map[alias_tok] = surv
            live_ids.append(alias_tok)
        else:
            live_ids.append(surv)

    live_nodes = [NodeSnapshot(id=lid, labels=frozenset({"Thing"}), props={}) for lid in live_ids]
    survivor_to_live_id = dict(zip(survivors, live_ids, strict=True))

    def survivor_of(token: str) -> str:
        return alias_map.get(token, token)

    n_legit_edges = draw(st.integers(min_value=0, max_value=2))
    n_extra_edges = draw(st.integers(min_value=0, max_value=2))

    fold_legit_edges: list[EdgeSnapshot] = []
    live_edges: list[EdgeSnapshot] = []
    for _ in range(n_legit_edges):
        src, dst = draw(st.tuples(st.sampled_from(survivors), st.sampled_from(survivors)))
        fold_legit_edges.append(EdgeSnapshot(type="LEGIT", src=src, dst=dst, props={}))
        live_edges.append(
            EdgeSnapshot(
                type="LEGIT",
                src=survivor_to_live_id[src],
                dst=survivor_to_live_id[dst],
                props={},
            )
        )

    extra_edges: list[EdgeSnapshot] = []
    for _ in range(n_extra_edges):
        src, dst = draw(st.tuples(st.sampled_from(survivors), st.sampled_from(survivors)))
        # type "EXTRA" never appears on the live side, so the (type, src, dst) key can never
        # match a live edge, regardless of endpoint values.
        extra_edges.append(EdgeSnapshot(type="EXTRA", src=src, dst=dst, props={}))

    fold = GraphSnapshot(
        nodes=tuple(fold_nodes), edges=tuple(fold_legit_edges) + tuple(extra_edges)
    )
    live = GraphSnapshot(nodes=tuple(live_nodes), edges=tuple(live_edges))

    return _FoldExtraScenario(
        fold=fold,
        live=live,
        survivor_of=survivor_of,
        extra_node_ids=frozenset(fold_extra_node_ids),
        extra_edges=tuple(extra_edges),
        survivor_ids=frozenset(survivors),
        legit_edge_keys=frozenset((e.type, e.src, e.dst) for e in fold_legit_edges),
    )


@given(scenario=_fold_extra_scenario())
@_SETTINGS
def test_inv_recon_fold_extra_flags_only_true_extras_and_spares_e1_consolidation(
    scenario: _FoldExtraScenario,
) -> None:
    """INV-RECON-FOLD-EXTRA: a fold node/edge with no ``survivor_of``-preimage in live is
    enumerated; a legitimate E1 consolidation (a live alias resolving to a fold survivor) is NOT."""
    result = enumerate_fold_side_extras(scenario.live, scenario.fold, scenario.survivor_of)
    assert isinstance(result, FoldSideExtras)

    got_node_ids = frozenset(n.id for n in result.nodes)
    assert got_node_ids == scenario.extra_node_ids, (
        "enumerate_fold_side_extras must report EXACTLY the fold nodes with no survivor_of "
        f"preimage in live; got {got_node_ids!r}, expected {scenario.extra_node_ids!r}"
    )
    assert got_node_ids.isdisjoint(scenario.survivor_ids), (
        "a legitimate E1 consolidation (a live alias L with survivor_of(L.id)==S, S a fold node) "
        "must NEVER be reported as a fold-side extra"
    )
    node_id_order = [n.id for n in result.nodes]
    assert node_id_order == sorted(node_id_order), (
        "fold-side extra nodes must be deterministically SORTED (by id)"
    )

    got_edge_keys = frozenset((e.type, e.src, e.dst) for e in result.edges)
    expected_edge_keys = frozenset((e.type, e.src, e.dst) for e in scenario.extra_edges)
    assert got_edge_keys == expected_edge_keys, (
        "enumerate_fold_side_extras must report EXACTLY the fold edges with no matching live edge "
        f"under (type, survivor_of(src), survivor_of(dst)); got {got_edge_keys!r}, expected "
        f"{expected_edge_keys!r}"
    )
    assert got_edge_keys.isdisjoint(scenario.legit_edge_keys), (
        "a legitimate (possibly endpoint-aliased) fold edge reproduced live must NEVER be reported "
        "as a fold-side extra edge"
    )
    edge_key_order = [(e.type, e.src, e.dst) for e in result.edges]
    assert edge_key_order == sorted(edge_key_order), (
        "fold-side extra edges must be deterministically SORTED (by (type, src, dst))"
    )


# ===========================================================================================
# INV-RECON-COPRESENT — find_copresent_value_divergence
# ===========================================================================================

_EXCLUDED_PROP_EXAMPLES: tuple[str, ...] = (
    "id",
    "caption",
    "datasets",
    "prov_source_id",
    "prov_witnesses",
    *CANONICAL_ID_FIELDS,
)


@dataclass(frozen=True)
class _CopresentScenario:
    fold: GraphSnapshot
    live: GraphSnapshot
    survivor_of: Callable[[str], str]
    node_id: str
    expected_values: frozenset[str]


@st.composite
def _copresent_divergence_scenario(draw: st.DrawFn) -> _CopresentScenario:
    pool = _draw_pool(draw, 40)
    take = _taker(pool)

    surv = take(1)[0]
    if draw(st.booleans()):
        alias_tok = take(1)[0]
        live_id = alias_tok
        alias_map = {alias_tok: surv}
    else:
        live_id = surv
        alias_map = {}

    def survivor_of(token: str) -> str:
        return alias_map.get(token, token)

    n_shared = draw(st.integers(min_value=0, max_value=2))
    shared_values = take(n_shared)
    k = draw(st.integers(min_value=1, max_value=3))
    # fold-only, never aliased -> guaranteed absent from the survivor_of-normalised live set.
    divergent_values = take(k)

    fold_props: dict[str, frozenset[str]] = {
        "traits": frozenset({*shared_values, *divergent_values}),
        "wikidata_id": frozenset({f"Q-{surv}-fold-only"}),
        "datasets": frozenset({"fold-batch"}),
        "prov_source_id": frozenset({"src:fold"}),
        "prov_witnesses": frozenset({"{}"}),
        "caption": frozenset({"Fold Pick"}),
    }
    live_props: dict[str, frozenset[str]] = {
        "caption": frozenset({"Live Pick"}),
        "datasets": frozenset({"live-batch"}),
        "prov_source_id": frozenset({"src:live"}),
    }
    if shared_values:
        live_props["traits"] = frozenset(shared_values)

    fold = GraphSnapshot(
        nodes=(NodeSnapshot(id=surv, labels=frozenset({"Thing"}), props=fold_props),), edges=()
    )
    live = GraphSnapshot(
        nodes=(NodeSnapshot(id=live_id, labels=frozenset({"Thing"}), props=live_props),), edges=()
    )
    return _CopresentScenario(
        fold=fold,
        live=live,
        survivor_of=survivor_of,
        node_id=surv,
        expected_values=frozenset(divergent_values),
    )


@given(scenario=_copresent_divergence_scenario())
@_SETTINGS
def test_inv_recon_copresent_flags_nonexcluded_fold_only_values(
    scenario: _CopresentScenario,
) -> None:
    """INV-RECON-COPRESENT: a non-excluded fold value with no survivor_of-normalised live
    counterpart is flagged; the deliberately-diverging EXCLUDED-axis values on the SAME node
    (anchor/datasets/prov_*/caption) never surface as findings."""
    result = find_copresent_value_divergence(scenario.live, scenario.fold, scenario.survivor_of)
    for entry in result:
        assert isinstance(entry, CoPresentValueDivergence)

    matches = [e for e in result if e.node_id == scenario.node_id and e.prop == "traits"]
    got_values = frozenset(e.value for e in matches)
    assert got_values == scenario.expected_values, (
        "find_copresent_value_divergence must flag EXACTLY the fold-only 'traits' values with no "
        f"live counterpart; got {got_values!r}, expected {scenario.expected_values!r}"
    )

    excluded_hits = [e for e in result if e.node_id == scenario.node_id and e.prop != "traits"]
    assert excluded_hits == [], (
        "the excluded-axis divergences deliberately injected on this node (caption/datasets/"
        f"prov_*/anchor) must NEVER be reported; got {excluded_hits!r}"
    )

    order = [(e.node_id, e.prop, e.value) for e in result]
    assert order == sorted(order), (
        "find_copresent_value_divergence must return deterministically SORTED "
        "(node_id, prop, value) output"
    )


@given(
    excluded_prop=st.sampled_from(_EXCLUDED_PROP_EXAMPLES),
    fold_i=st.integers(min_value=0, max_value=999),
    live_i=st.integers(min_value=0, max_value=999),
)
@_SETTINGS
def test_inv_recon_copresent_never_flags_excluded_axis(
    excluded_prop: str, fold_i: int, live_i: int
) -> None:
    """INV-RECON-COPRESENT: adding a diverging value on an EXCLUDED axis (id/caption/anchor/
    datasets/prov_*) — with NO other divergence anywhere on the node — must yield ZERO findings."""
    assume(fold_i != live_i)
    fold_value, live_value = f"v{fold_i}", f"v{live_i}"

    fold = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1", labels=frozenset(), props={excluded_prop: frozenset({fold_value})}
            ),
        ),
        edges=(),
    )
    live = GraphSnapshot(
        nodes=(
            NodeSnapshot(
                id="s1", labels=frozenset(), props={excluded_prop: frozenset({live_value})}
            ),
        ),
        edges=(),
    )
    result = find_copresent_value_divergence(live, fold, _identity)
    assert result == [], (
        f"INV-RECON-COPRESENT VIOLATED: a diverging value on the EXCLUDED prop {excluded_prop!r} "
        f"(fold={fold_value!r} vs live={live_value!r}, no other divergence present) must yield "
        f"ZERO findings; got {result!r}"
    )


# ===========================================================================================
# INV-RECON-MULTIPLICITY — reconcile_counts
# ===========================================================================================


@dataclass(frozen=True)
class _BalancedCountsScenario:
    fold: GraphSnapshot
    live: GraphSnapshot
    survivor_ids: tuple[str, ...]
    fold_edge_keys: tuple[tuple[str, str, str], ...]


@st.composite
def _balanced_counts_scenario(draw: st.DrawFn) -> _BalancedCountsScenario:
    """A perfectly-balanced (identity survivor_of, no duplicates, no fold-side extras) scenario —
    the shared zero-residual baseline both multiplicity properties mutate."""
    pool = _draw_pool(draw, 30)
    take = _taker(pool)
    n = draw(st.integers(min_value=1, max_value=5))
    survivors = take(n)
    fold_nodes = tuple(NodeSnapshot(id=s, labels=frozenset(), props={}) for s in survivors)
    live_nodes = tuple(NodeSnapshot(id=s, labels=frozenset(), props={}) for s in survivors)

    n_edges = draw(st.integers(min_value=0, max_value=2))
    seen_keys: set[tuple[str, str, str]] = set()
    fold_edges: list[EdgeSnapshot] = []
    for _ in range(n_edges):
        src, dst = draw(st.tuples(st.sampled_from(survivors), st.sampled_from(survivors)))
        key = ("REL", src, dst)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        fold_edges.append(EdgeSnapshot(type="REL", src=src, dst=dst, props={}))

    fold = GraphSnapshot(nodes=fold_nodes, edges=tuple(fold_edges))
    live = GraphSnapshot(nodes=live_nodes, edges=tuple(fold_edges))  # byte-identical edge set too
    return _BalancedCountsScenario(
        fold=fold,
        live=live,
        survivor_ids=tuple(survivors),
        fold_edge_keys=tuple((e.type, e.src, e.dst) for e in fold_edges),
    )


@given(scenario=_balanced_counts_scenario(), k_nodes=st.integers(min_value=1, max_value=3))
@_SETTINGS
def test_inv_recon_multiplicity_duplicate_live_node_id_not_absorbed(
    scenario: _BalancedCountsScenario, k_nodes: int
) -> None:
    """INV-RECON-MULTIPLICITY: a same-id live-node duplicate increments duplicate_live_node_ids and
    is NOT absorbed into the alias/survivor arithmetic — node_residual must NOT silently balance to
    0 when the duplicate exists."""
    n = len(scenario.survivor_ids)
    k = min(k_nodes, n)
    assume(k >= 1)

    baseline = reconcile_counts(scenario.live, scenario.fold, _identity)
    assert isinstance(baseline, CountReconciliation)
    assert baseline.live_nodes == n
    assert baseline.fold_nodes == n
    assert baseline.distinct_live_node_survivors == n
    assert baseline.duplicate_live_node_ids == 0
    assert baseline.fold_side_extra_nodes == 0
    assert baseline.node_residual == 0, (
        "the shared scenario generator must start from a zero node_residual baseline "
        f"(got {baseline.node_residual})"
    )

    dup_ids = scenario.survivor_ids[:k]
    dup_nodes = tuple(
        NodeSnapshot(
            id=sid,
            labels=frozenset(),
            # an un-logged anchor the ORIGINAL row never carried (82 §4 red-team scenario: a
            # same-id duplicate carrying data the original row lacks must not vanish count-clean).
            props={"wikidata_id": frozenset({f"Q-DUP-{sid}"})},
        )
        for sid in dup_ids
    )
    live_with_dups = GraphSnapshot(nodes=scenario.live.nodes + dup_nodes, edges=scenario.live.edges)
    result = reconcile_counts(live_with_dups, scenario.fold, _identity)

    assert result.live_nodes == n + k
    assert result.fold_nodes == n
    assert result.fold_side_extra_nodes == 0
    assert result.distinct_live_node_survivors == n, (
        "INV-RECON-MULTIPLICITY: a same-id duplicate's survivor_of value is IDENTICAL to the "
        "original row's — distinct_live_node_survivors must stay UNCHANGED. This is exactly the "
        "absorption hazard: if duplicate detection relied only on this field, the duplicate would "
        "vanish count-clean."
    )
    assert result.duplicate_live_node_ids == k, (
        f"a same-id duplicate MUST increment duplicate_live_node_ids by exactly the duplicate "
        f"count (RAW id multiplicity, never survivor_of-normalised); got "
        f"{result.duplicate_live_node_ids}, expected {k}"
    )
    assert result.node_residual == k, (
        "INV-RECON-MULTIPLICITY VIOLATED: node_residual must NOT silently balance to 0 when a "
        f"same-id duplicate exists; got node_residual={result.node_residual}, expected {k} (the "
        "residual formula is duplicate_live_node_ids + (fold_nodes - distinct_live_node_survivors "
        "- fold_side_extra_nodes))"
    )
    assert result.node_residual != 0


@given(scenario=_balanced_counts_scenario(), k_edges=st.integers(min_value=1, max_value=2))
@_SETTINGS
def test_inv_recon_multiplicity_duplicate_live_edge_key_not_absorbed(
    scenario: _BalancedCountsScenario, k_edges: int
) -> None:
    """INV-RECON-MULTIPLICITY, edge side: a same-(type, src, dst) live-edge duplicate increments
    duplicate_live_edge_ids and must not silently balance edge_residual to 0."""
    assume(len(scenario.fold_edge_keys) >= 1)
    k = min(k_edges, len(scenario.fold_edge_keys))
    assume(k >= 1)

    baseline = reconcile_counts(scenario.live, scenario.fold, _identity)
    assert baseline.live_edges == len(scenario.fold_edge_keys)
    assert baseline.distinct_live_edge_survivors == len(scenario.fold_edge_keys)
    assert baseline.duplicate_live_edge_ids == 0
    assert baseline.fold_side_extra_edges == 0
    assert baseline.edge_residual == 0, (
        "the shared scenario generator must start from a zero edge_residual baseline "
        f"(got {baseline.edge_residual})"
    )

    dup_keys = scenario.fold_edge_keys[:k]
    dup_edges = tuple(EdgeSnapshot(type=t, src=s, dst=d, props={}) for (t, s, d) in dup_keys)
    live_with_dups = GraphSnapshot(nodes=scenario.live.nodes, edges=scenario.live.edges + dup_edges)
    result = reconcile_counts(live_with_dups, scenario.fold, _identity)

    assert result.live_edges == len(scenario.fold_edge_keys) + k
    assert result.distinct_live_edge_survivors == len(scenario.fold_edge_keys), (
        "a same-key duplicate edge's survivor-normalised key is IDENTICAL to the original's — "
        "distinct_live_edge_survivors must stay UNCHANGED (the same absorption hazard as nodes)."
    )
    assert result.duplicate_live_edge_ids == k
    assert result.edge_residual == k, (
        f"edge_residual must NOT silently balance to 0 when a same-key duplicate edge exists; got "
        f"{result.edge_residual}, expected {k}"
    )
    assert result.edge_residual != 0


@given(scenario=_fold_extra_scenario())
@_SETTINGS
def test_reconcile_counts_fold_side_extra_matches_enumerate_fold_side_extras(
    scenario: _FoldExtraScenario,
) -> None:
    """Cross-check: reconcile_counts' fold_side_extra_{nodes,edges} counts must agree exactly with
    the independently-enumerated FoldSideExtras — the two instruments must never disagree on what
    counts as a fold-side extra."""
    extras = enumerate_fold_side_extras(scenario.live, scenario.fold, scenario.survivor_of)
    counts = reconcile_counts(scenario.live, scenario.fold, scenario.survivor_of)
    assert counts.fold_side_extra_nodes == len(extras.nodes)
    assert counts.fold_side_extra_edges == len(extras.edges)


# ===========================================================================================
# INV-RECON-LABEL-LOSS — compare_labels
# ===========================================================================================


@dataclass(frozen=True)
class _LabelScenario:
    fold: GraphSnapshot
    live: GraphSnapshot
    survivor_of: Callable[[str], str]
    node_id: str
    lost_labels: frozenset[str]
    invented_labels: frozenset[str]


@st.composite
def _label_scenario(draw: st.DrawFn) -> _LabelScenario:
    pool = _draw_pool(draw, 30)
    take = _taker(pool)
    surv = take(1)[0]
    if draw(st.booleans()):
        alias_tok = take(1)[0]
        live_id = alias_tok
        alias_map = {alias_tok: surv}
    else:
        live_id = surv
        alias_map = {}

    def survivor_of(token: str) -> str:
        return alias_map.get(token, token)

    n_shared = draw(st.integers(min_value=0, max_value=2))
    shared_labels = take(n_shared)
    n_lost = draw(st.integers(min_value=1, max_value=3))
    lost_labels = take(n_lost)  # present on LIVE, absent from fold -> missing_in_fold (the LOSS)
    n_invented = draw(st.integers(min_value=1, max_value=3))
    invented_labels = take(n_invented)  # present on FOLD only -> extra_in_fold

    live_labels = frozenset({*shared_labels, *lost_labels})
    fold_labels = frozenset({*shared_labels, *invented_labels})

    fold = GraphSnapshot(nodes=(NodeSnapshot(id=surv, labels=fold_labels, props={}),), edges=())
    live = GraphSnapshot(nodes=(NodeSnapshot(id=live_id, labels=live_labels, props={}),), edges=())
    return _LabelScenario(
        fold=fold,
        live=live,
        survivor_of=survivor_of,
        node_id=surv,
        lost_labels=frozenset(lost_labels),
        invented_labels=frozenset(invented_labels),
    )


@given(scenario=_label_scenario())
@_SETTINGS
def test_inv_recon_label_loss_direction(scenario: _LabelScenario) -> None:
    """INV-RECON-LABEL-LOSS: a label present live but absent from the co-present fold node appears
    in missing_in_fold (the LOSS direction); a fold-only label appears in extra_in_fold."""
    result = compare_labels(scenario.live, scenario.fold, scenario.survivor_of)
    assert isinstance(result, LabelParity)

    missing = {(e.node_id, e.label) for e in result.missing_in_fold}
    extra = {(e.node_id, e.label) for e in result.extra_in_fold}

    expected_missing = {(scenario.node_id, lbl) for lbl in scenario.lost_labels}
    expected_extra = {(scenario.node_id, lbl) for lbl in scenario.invented_labels}

    assert missing == expected_missing, (
        f"compare_labels missing_in_fold must be EXACTLY the live-only (lost) labels; got "
        f"{missing!r}, expected {expected_missing!r}"
    )
    assert extra == expected_extra, (
        f"compare_labels extra_in_fold must be EXACTLY the fold-only (invented) labels; got "
        f"{extra!r}, expected {expected_extra!r}"
    )

    missing_order = [(e.node_id, e.label) for e in result.missing_in_fold]
    assert missing_order == sorted(missing_order), (
        "missing_in_fold must be deterministically sorted"
    )
    extra_order = [(e.node_id, e.label) for e in result.extra_in_fold]
    assert extra_order == sorted(extra_order), "extra_in_fold must be deterministically sorted"


# ===========================================================================================
# INV-RECON-ERASED — find_erased_source_residue
# ===========================================================================================


@dataclass(frozen=True)
class _ErasedResidueScenario:
    fold: GraphSnapshot
    erased_source_ids: frozenset[str]
    expected: frozenset[tuple[str, str]]


@st.composite
def _erased_residue_scenario(draw: st.DrawFn) -> _ErasedResidueScenario:
    pool = _draw_pool(draw, 30)
    take = _taker(pool)

    erased_a, erased_b, clean_source = take(3)
    fold_extra_id, copresent_id, untouched_id = take(3)

    # (a) FOLD-EXTRA node: its `datasets` value-set directly references an erased source.
    node_a = NodeSnapshot(
        id=fold_extra_id,
        labels=frozenset(),
        props={"datasets": frozenset({erased_a, clean_source})},
    )
    # (b) CO-PRESENT node (a multi-source survivor): `datasets` is clean; the erased reference
    # lives ONLY inside the decoded `prov_witnesses` map -- exercises the OR leg independently.
    witnesses = json.dumps({"traits": sorted([clean_source, erased_b])})
    node_b = NodeSnapshot(
        id=copresent_id,
        labels=frozenset(),
        props={
            "datasets": frozenset({clean_source}),
            "prov_witnesses": frozenset({witnesses}),
        },
    )
    # (c) untouched node: references only the non-erased source -- must NEVER be flagged.
    node_c = NodeSnapshot(
        id=untouched_id, labels=frozenset(), props={"datasets": frozenset({clean_source})}
    )

    fold = GraphSnapshot(nodes=(node_a, node_b, node_c), edges=())
    return _ErasedResidueScenario(
        fold=fold,
        erased_source_ids=frozenset({erased_a, erased_b}),
        expected=frozenset({(fold_extra_id, erased_a), (copresent_id, erased_b)}),
    )


@given(scenario=_erased_residue_scenario())
@_SETTINGS
def test_inv_recon_erased_flags_both_fold_extra_and_copresent(
    scenario: _ErasedResidueScenario,
) -> None:
    """INV-RECON-ERASED: a fold node whose datasets/prov_witnesses reference an erased source_id
    is flagged, for BOTH a fold-extra node AND a co-present (multi-source) node — and NEVER for a
    node that only references a non-erased source."""
    result = find_erased_source_residue(scenario.fold, scenario.erased_source_ids)
    for entry in result:
        assert isinstance(entry, ErasedResidue)

    got = frozenset((r.node_id, r.source_id) for r in result)
    assert got == scenario.expected, (
        f"find_erased_source_residue must flag EXACTLY {scenario.expected!r}; got {got!r} (must "
        "fire for BOTH the fold-extra node's `datasets` reference AND the co-present node's "
        "`prov_witnesses` reference, and never for the untouched/clean-only node)"
    )
    order = [(r.node_id, r.source_id) for r in result]
    assert order == sorted(order), (
        "find_erased_source_residue must return deterministically SORTED (node_id, source_id) "
        "output"
    )


# ===========================================================================================
# INV-RECON-PURE — static import-surface check (no @given, no Docker)
# ===========================================================================================

_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "neo4j",
    "sqlalchemy",
    "psycopg",
    "asyncpg",
    "worldmonitor.db",
    "worldmonitor.graph.neo4j_client",
    "worldmonitor.graph.writer",
    "worldmonitor.graph.ops",
    "worldmonitor.runner",
)


def test_inv_recon_pure_no_db_imports() -> None:
    """INV-RECON-PURE: ``reconciliation.py``'s own source imports no Neo4j/SQLAlchemy/DB module —
    an AST-walk of the module's imports (not a runtime probe), so this runs Docker-free and does
    not depend on any symbol actually being importable beyond the module object itself."""
    import worldmonitor.resolution.reconciliation as reconciliation_module

    source = inspect.getsource(reconciliation_module)
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    hits = {
        name
        for name in imported
        for prefix in _FORBIDDEN_IMPORT_PREFIXES
        if name == prefix or name.startswith(prefix + ".")
    }
    assert not hits, (
        f"INV-RECON-PURE VIOLATED: reconciliation.py imports {hits!r} — forbidden. The module MUST "
        "stay pure/Neo4j-free/DB-free (mirroring divergence.py, ADR 0102 D9) so its @given suite "
        "runs entirely in memory, Docker-free."
    )
