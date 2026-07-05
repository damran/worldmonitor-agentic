"""Property/metamorphic tests for Gate 3a-ii-B — the projection divergence measure (ADR 0102).

Two MANDATORY ``@given`` invariants (CLAUDE.md build-discipline) on the PURE measure
(``worldmonitor.resolution.divergence.measure_divergence``) — no DB, no Neo4j, no SQLAlchemy
engine, so neither test needs a ``try/finally: engine.dispose()`` (the 3a-ii-A connection-leak
lesson applies only to ``@given`` tests that construct a per-example engine; these two operate
purely on in-memory ``GraphSnapshot`` objects).

P-DIV-1  NO FALSE ALARM / E-TOLERANCE — for ANY fold ``GraphSnapshot`` + ``survivor_of`` (an
         alias map; survivor -> self) and ANY live snapshot derived from the fold by E-legit
         transformations (ADR 0102 D6): node id set to the fold id OR an alias ``survivor_of``
         maps back to it; a subset of values dropped from the multi-valued ``traits`` prop; a
         compared value replaced by a fresh alias whose ``survivor_of`` is a value present in the
         fold node's set; arbitrary ``wm_anchor_*``/``datasets``/``prov_*`` additions; an extra
         label; and, on edges, endpoint aliases mapping back + a dropped value-subset + arbitrary
         ``datasets``/``prov_*`` — ``measure_divergence(live, fold, survivor_of,
         computed_at=...).total == 0``.

P-DIV-2  ROT IS DETECTED — starting from a zero-divergence ``(live, fold, survivor_of)`` triple
         (the P-DIV-1 generator), injecting (a) ``k`` fresh wholly-unexplained nodes increases
         ``total`` by EXACTLY ``k``; (b) ``k`` fresh wholly-unexplained edges increases ``total``
         by EXACTLY ``k``; (c) one unexplained compared-prop value added to an already-explained
         node increases ``total`` by EXACTLY 1. Every single injection also satisfies
         ``total >= 1``.

RED at collection time: ``worldmonitor.resolution.divergence`` does not exist yet — importing
``NodeSnapshot``/``EdgeSnapshot``/``GraphSnapshot``/``measure_divergence`` fails with
``ImportError``. That is the correct, intended TDD failure mode (the Gate 3a-i precedent).

NOT marked ``@pytest.mark.integration`` — this file is pure and Docker-free.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.resolution.divergence import (  # gate import: RED until builder lands
    EdgeSnapshot,
    GraphSnapshot,
    NodeSnapshot,
    measure_divergence,
)

_COMPUTED_AT = datetime(2026, 7, 5, 0, 0, 0, tzinfo=UTC)

_SETTINGS = settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# A fixed, generous pool of pairwise-DISTINCT short tokens — structurally guaranteed unique via a
# permutation of a fixed integer range (not a uniqueness filter), so survivor ids, alias tokens,
# and value tokens sliced from disjoint ranges of this pool can never collide.
_POOL_SIZE = 60


@dataclass(frozen=True)
class _ZeroDivergenceScenario:
    """A ``(fold, live, survivor_of)`` triple where ``live`` is derived from ``fold`` by ONLY
    E-legit transformations (ADR 0102 D6) — the shared base for both P-DIV-1 and P-DIV-2."""

    fold: GraphSnapshot
    live: GraphSnapshot
    survivor_of: Callable[[str], str]
    # Ten tokens NEVER used anywhere in ``fold``/``live``/``survivor_of``'s alias map — P-DIV-2's
    # raw material for injecting rot that is, by construction, unexplained.
    fresh_tokens: tuple[str, ...]


@st.composite
def _zero_divergence_scenario(draw: st.DrawFn) -> _ZeroDivergenceScenario:
    """Draw a zero-divergence ``_ZeroDivergenceScenario`` (see the class docstring)."""
    perm = draw(st.permutations(list(range(_POOL_SIZE))))
    pool = [f"tok{i}" for i in perm]
    cursor = 0

    def take(n: int) -> list[str]:
        nonlocal cursor
        assert cursor + n <= len(pool), "token pool exhausted — increase _POOL_SIZE"
        out = pool[cursor : cursor + n]
        cursor += n
        return out

    n_survivors = draw(st.integers(min_value=1, max_value=3))
    survivors = take(n_survivors)
    id_aliases = take(n_survivors)
    id_alias_of: dict[str, str] = dict(zip(survivors, id_aliases, strict=True))
    # Every survivor's reserved id-alias resolves back to it. Extra (unused) entries are
    # harmless: survivor_of is only ever called on tokens that actually appear in a snapshot.
    alias_map: dict[str, str] = dict(zip(id_aliases, survivors, strict=True))

    fold_nodes: list[NodeSnapshot] = []
    live_nodes: list[NodeSnapshot] = []

    for surv in survivors:
        n_vals = draw(st.integers(min_value=1, max_value=3))
        vals = take(n_vals)
        kind_val = take(1)[0]

        fold_nodes.append(
            NodeSnapshot(
                id=surv,
                labels=frozenset({"Thing"}),
                props={"traits": frozenset(vals), "kind": frozenset({kind_val})},
            )
        )

        # --- E-legit: drop a (possibly empty, possibly full) subset of 'traits' values ---
        keep_mask = draw(st.lists(st.booleans(), min_size=len(vals), max_size=len(vals)))
        kept = [v for v, keep in zip(vals, keep_mask, strict=True) if keep]

        # --- E-legit: replace ONE kept value with a fresh alias resolving back to it ---
        live_trait_values = list(kept)
        if kept and draw(st.booleans()):
            idx = draw(st.integers(min_value=0, max_value=len(kept) - 1))
            target_value = kept[idx]
            alias_tok = take(1)[0]
            alias_map[alias_tok] = target_value
            live_trait_values[idx] = alias_tok

        live_props: dict[str, frozenset[str]] = {"kind": frozenset({kind_val})}
        if live_trait_values:
            live_props["traits"] = frozenset(live_trait_values)

        # --- E-legit: mandatory EXCLUDED-class extras (always present; must NEVER count) ---
        live_props["wm_anchor_qid"] = frozenset({f"Q-{surv}"})
        live_props["datasets"] = frozenset({"live-batch-x"})
        live_props["prov_source_id"] = frozenset({"src:live"})
        live_props["prov_witnesses"] = frozenset({"{}"})
        # D6-iii: 'caption' is a picked scalar (not union-monotone) — a live caption matching
        # NOTHING in the fold must never count (the cross-batch name-update case).
        live_props["caption"] = frozenset({take(1)[0]})

        # --- E-legit: node id -> fold id, OR the reserved alias that resolves back to it ---
        live_id = id_alias_of[surv] if draw(st.booleans()) else surv

        live_nodes.append(
            NodeSnapshot(
                id=live_id,
                # E-legit: an extra label — labels are NEVER compared (D6-i).
                labels=frozenset({"Thing", "ExtraLiveLabel"}),
                props=live_props,
            )
        )

    n_edges = draw(st.integers(min_value=0, max_value=2))
    fold_edges: list[EdgeSnapshot] = []
    live_edges: list[EdgeSnapshot] = []
    for _ in range(n_edges):
        etype = draw(st.sampled_from(("OWNS", "LINKED")))
        src = draw(st.sampled_from(survivors))
        dst = draw(st.sampled_from(survivors))
        n_edge_vals = draw(st.integers(min_value=0, max_value=2))
        edge_vals = take(n_edge_vals)

        fold_edges.append(
            EdgeSnapshot(type=etype, src=src, dst=dst, props={"since": frozenset(edge_vals)})
        )

        keep_mask = draw(st.lists(st.booleans(), min_size=len(edge_vals), max_size=len(edge_vals)))
        kept_edge_vals = [v for v, keep in zip(edge_vals, keep_mask, strict=True) if keep]

        live_src = id_alias_of[src] if draw(st.booleans()) else src
        live_dst = id_alias_of[dst] if draw(st.booleans()) else dst

        live_edges.append(
            EdgeSnapshot(
                type=etype,
                src=live_src,
                dst=live_dst,
                props={
                    "since": frozenset(kept_edge_vals),
                    "datasets": frozenset({"live-batch-x"}),
                    "prov_source_id": frozenset({"src:live"}),
                },
            )
        )

    # Ten tokens reserved LAST, never assigned to any node/edge/alias above — P-DIV-2's rot
    # material.
    fresh_tokens = take(10)

    def survivor_of(token: str) -> str:
        return alias_map.get(token, token)

    return _ZeroDivergenceScenario(
        fold=GraphSnapshot(nodes=tuple(fold_nodes), edges=tuple(fold_edges)),
        live=GraphSnapshot(nodes=tuple(live_nodes), edges=tuple(live_edges)),
        survivor_of=survivor_of,
        fresh_tokens=tuple(fresh_tokens),
    )


# ===========================================================================
# P-DIV-1: no false alarm on E-legit transformations
# ===========================================================================


@given(scenario=_zero_divergence_scenario())
@_SETTINGS
def test_p_div_1_no_false_alarm_on_e_legit_transformations(
    scenario: _ZeroDivergenceScenario,
) -> None:
    """P-DIV-1: any E-legit-transformed live snapshot yields ``total == 0`` (ADR 0102 D6)."""
    result = measure_divergence(
        scenario.live, scenario.fold, scenario.survivor_of, computed_at=_COMPUTED_AT
    )
    assert result.total == 0, (
        "P-DIV-1 FALSE ALARM: measure_divergence flagged an E-legit-transformed live snapshot as "
        f"divergent (total={result.total}, unexplained_nodes={result.unexplained_nodes}, "
        f"unexplained_edges={result.unexplained_edges}).\n"
        f"fold={scenario.fold!r}\nlive={scenario.live!r}\n"
        "Every transformation applied here (id -> alias resolving back, dropped value-subsets, "
        "alias-resolving compared values, wm_anchor_*/datasets/prov_*/extra-label additions, and "
        "the mirrored edge-side transformations) is E-legit (ADR 0102 D6) and must yield total==0."
    )


# ===========================================================================
# P-DIV-2: rot is detected (exact-k sensitivity)
# ===========================================================================


@given(scenario=_zero_divergence_scenario(), k=st.integers(min_value=1, max_value=3))
@_SETTINGS
def test_p_div_2_rot_is_detected(scenario: _ZeroDivergenceScenario, k: int) -> None:
    """P-DIV-2: injecting rot into a zero-divergence scenario increases ``total`` by EXACTLY the
    injected amount (ADR 0102 D6)."""
    baseline = measure_divergence(
        scenario.live, scenario.fold, scenario.survivor_of, computed_at=_COMPUTED_AT
    )
    assert baseline.total == 0, (
        f"P-DIV-2 precondition failed: the shared scenario generator produced a non-zero baseline "
        f"divergence (total={baseline.total}) — see P-DIV-1; this generator must always start from "
        "a zero-divergence (live, fold, survivor_of) triple before rot is injected."
    )

    fresh = scenario.fresh_tokens  # 10 tokens, never used in fold/live/alias_map above

    # (a) k fresh, wholly UNEXPLAINED nodes: ids absent from the fold AND never aliased to
    # anything, so survivor_of is the identity on them and no fold node can ever match.
    fresh_node_ids = fresh[:k]
    extra_nodes = tuple(
        NodeSnapshot(id=tok, labels=frozenset({"Rot"}), props={"traits": frozenset({tok})})
        for tok in fresh_node_ids
    )
    live_node_rot = GraphSnapshot(
        nodes=scenario.live.nodes + extra_nodes, edges=scenario.live.edges
    )
    result_a = measure_divergence(
        live_node_rot, scenario.fold, scenario.survivor_of, computed_at=_COMPUTED_AT
    )
    assert result_a.total == k, (
        f"P-DIV-2(a) VIOLATED: injecting {k} fresh unexplained node(s) must increase total by "
        f"EXACTLY {k}; got total={result_a.total} (unexplained_nodes={result_a.unexplained_nodes}, "
        f"unexplained_edges={result_a.unexplained_edges})."
    )
    assert result_a.total >= 1

    # (b) k fresh, wholly UNEXPLAINED edges: both endpoints fresh, so no fold
    # (type, survivor_of(src), survivor_of(dst)) key can ever match.
    edge_tokens = fresh[k : k + 2 * k]
    extra_edges = tuple(
        EdgeSnapshot(type="ROT", src=edge_tokens[2 * i], dst=edge_tokens[2 * i + 1], props={})
        for i in range(k)
    )
    live_edge_rot = GraphSnapshot(
        nodes=scenario.live.nodes, edges=scenario.live.edges + extra_edges
    )
    result_b = measure_divergence(
        live_edge_rot, scenario.fold, scenario.survivor_of, computed_at=_COMPUTED_AT
    )
    assert result_b.total == k, (
        f"P-DIV-2(b) VIOLATED: injecting {k} fresh unexplained edge(s) must increase total by "
        f"EXACTLY {k}; got total={result_b.total}."
    )
    assert result_b.total >= 1

    # (c) ONE extra compared-prop value on an already-EXPLAINED node's 'kind' prop, disjoint (under
    # survivor_of) from every value in that node's fold 'kind' set -> flips exactly that node.
    extra_value_token = fresh[3 * k]
    target = scenario.live.nodes[0]
    mutated_props = dict(target.props)
    mutated_props["kind"] = frozenset(mutated_props.get("kind", frozenset()) | {extra_value_token})
    mutated_node = NodeSnapshot(id=target.id, labels=target.labels, props=mutated_props)
    live_value_rot = GraphSnapshot(
        nodes=(mutated_node,) + scenario.live.nodes[1:], edges=scenario.live.edges
    )
    result_c = measure_divergence(
        live_value_rot, scenario.fold, scenario.survivor_of, computed_at=_COMPUTED_AT
    )
    assert result_c.total == 1, (
        "P-DIV-2(c) VIOLATED: adding one unexplained compared-prop value to an already-explained "
        f"node must increase total by EXACTLY 1; got total={result_c.total} (extra value "
        f"{extra_value_token!r} added to node {target.id!r}'s 'kind' prop)."
    )
    assert result_c.total >= 1
