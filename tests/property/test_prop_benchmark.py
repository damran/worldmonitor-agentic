"""Property / metamorphic tests for ``resolution.benchmark`` — external-benchmark FLOOR (ADR 0080).

ALL inputs are SYNTHETIC FtM entities — NO real OpenSanctions or Febrl bulk data.

Property plan (gate.scope §(B)):
* P-GUARD-SOUND (load-bearing): drop_contaminated is sound — no under-drop, no over-drop, exact count.
* P-GUARD-EMPTY: our_keys == ∅ ⇒ nothing dropped; kept == input.
* P-JUDGEMENT-TOTAL: load_os_pairs emits a pair iff judgement in {'positive','negative'}.
* P-FLOOR-MATH: oracle ⇒ P=R=F1=1.0, OMR=0.0; inverted oracle ⇒ P=R=0.0, OMR=1.0;
  general OMR == FP/(TP+FP) (or 0.0).
* P-CONTAM-IN-FLOOR: injecting a pair's key drops exactly that pair from the floor.
* P-IMPORT-PURITY (proves INV-IMPORT-PURITY): benchmark module references no scoring symbol.
"""

from __future__ import annotations

import ast
import inspect
import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution import benchmark as _benchmark_module
from worldmonitor.resolution.benchmark import (
    BENCHMARK_SOURCE,
    BenchmarkPair,
    drop_contaminated,
    evaluate_floor,
    identity_keys,
    load_os_pairs,
)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS = settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow], deadline=None)
_FAST = settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow], deadline=None)

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

# ID pool: shared between entity ids and the our_keys pool so hypothesis can generate
# contaminated pairs (entity id matches a drawn key) as well as clean ones.
_ID_POOL = [f"id-{i}" for i in range(30)]
_KEY_POOL = _ID_POOL + ["key-X", "key-Y", "key-Z", "anchor-A", "anchor-B"]
_LABEL = st.sampled_from(["match", "non_match"])


def _make_entity(entity_id: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": ["Corp"]},
        }
    )


@st.composite
def _pair_with_unique_ids(
    draw: st.DrawFn,
    idx: int,
) -> BenchmarkPair:
    """A BenchmarkPair with deterministic unique entity ids (no id collision across pairs)."""
    label = draw(_LABEL)
    lid = f"pair{idx}-left"
    rid = f"pair{idx}-right"
    return BenchmarkPair(
        left=_make_entity(lid),
        right=_make_entity(rid),
        label=label,
        sub_source="os_pairs",
    )


@st.composite
def _pair_list_unique(
    draw: st.DrawFn,
    *,
    min_size: int = 0,
    max_size: int = 8,
) -> list[BenchmarkPair]:
    """A list of BenchmarkPairs with pair-unique entity ids."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    pairs = []
    for i in range(n):
        label = draw(_LABEL)
        pairs.append(
            BenchmarkPair(
                left=_make_entity(f"p{i}-left"),
                right=_make_entity(f"p{i}-right"),
                label=label,
                sub_source="os_pairs",
            )
        )
    return pairs


@st.composite
def _pair_list_pooled(
    draw: st.DrawFn,
    *,
    min_size: int = 0,
    max_size: int = 8,
) -> list[BenchmarkPair]:
    """A list of BenchmarkPairs where entity ids are drawn from _ID_POOL (can overlap with our_keys)."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    pairs = []
    for i in range(n):
        label = draw(_LABEL)
        lid = draw(st.sampled_from(_ID_POOL))
        rid = draw(st.sampled_from(_ID_POOL))
        pairs.append(
            BenchmarkPair(
                left=_make_entity(lid),
                right=_make_entity(rid),
                label=label,
                sub_source="os_pairs",
            )
        )
    return pairs


# ---------------------------------------------------------------------------
# P-GUARD-SOUND (load-bearing): drop_contaminated soundness — three sub-properties
# ---------------------------------------------------------------------------


@given(
    pairs=_pair_list_pooled(min_size=0, max_size=8),
    our_keys=st.frozensets(st.sampled_from(_KEY_POOL), max_size=6),
)
@_SETTINGS
def test_p_guard_sound_no_truncation(pairs: list[BenchmarkPair], our_keys: frozenset[str]) -> None:
    """P-GUARD-SOUND: len(kept) + n_dropped == len(input) for any pair list + our_keys.

    This is the load-bearing non-truncation invariant: every pair is accounted for.
    """
    kept, n_dropped = drop_contaminated(pairs, our_keys)
    assert len(kept) + n_dropped == len(pairs), (
        f"silent truncation detected: "
        f"{len(kept)} kept + {n_dropped} dropped != {len(pairs)} input\n"
        f"our_keys={our_keys}"
    )


@given(
    pairs=_pair_list_pooled(min_size=0, max_size=8),
    our_keys=st.frozensets(st.sampled_from(_KEY_POOL), max_size=6),
)
@_SETTINGS
def test_p_guard_sound_kept_disjoint_from_our_keys(
    pairs: list[BenchmarkPair], our_keys: frozenset[str]
) -> None:
    """P-GUARD-SOUND: every KEPT pair's identity_keys must be disjoint from our_keys (no under-drop).

    If any kept pair has a key in common with our_keys, the guard silently allowed a contaminated
    pair through — a floor that grades against its own training data.
    """
    kept, _ = drop_contaminated(pairs, our_keys)
    for pair in kept:
        combined = identity_keys(pair.left) | identity_keys(pair.right)
        assert combined.isdisjoint(our_keys), (
            f"P-GUARD-SOUND under-drop: kept pair has identity_keys overlapping our_keys.\n"
            f"  left.id={pair.left.id!r}, right.id={pair.right.id!r}\n"
            f"  combined identity_keys={combined}\n"
            f"  our_keys={our_keys}\n"
            f"  overlap={combined & our_keys}"
        )


@given(
    pairs=_pair_list_pooled(min_size=0, max_size=8),
    our_keys=st.frozensets(st.sampled_from(_KEY_POOL), max_size=6),
)
@_SETTINGS
def test_p_guard_sound_every_dropped_pair_intersected(
    pairs: list[BenchmarkPair], our_keys: frozenset[str]
) -> None:
    """P-GUARD-SOUND: every DROPPED pair DID have identity_keys intersecting our_keys (no over-drop).

    If a pair was dropped without intersection, the guard removed a clean benchmark pair — shrinking
    the floor without justification.
    """
    kept, _ = drop_contaminated(pairs, our_keys)
    kept_ids = {(p.left.id, p.right.id) for p in kept}

    for pair in pairs:
        if (pair.left.id, pair.right.id) in kept_ids:
            continue  # was kept — skip
        # Was dropped; must have intersected our_keys.
        combined = identity_keys(pair.left) | identity_keys(pair.right)
        assert not combined.isdisjoint(our_keys), (
            f"P-GUARD-SOUND over-drop: pair dropped but its identity_keys do NOT overlap our_keys.\n"
            f"  left.id={pair.left.id!r}, right.id={pair.right.id!r}\n"
            f"  combined identity_keys={combined}\n"
            f"  our_keys={our_keys}"
        )


# ---------------------------------------------------------------------------
# P-GUARD-EMPTY: our_keys == ∅ ⇒ nothing dropped
# ---------------------------------------------------------------------------


@given(pairs=_pair_list_unique(min_size=0, max_size=8))
@_FAST
def test_p_guard_empty_nothing_dropped(pairs: list[BenchmarkPair]) -> None:
    """P-GUARD-EMPTY: with our_keys == frozenset(), n_dropped == 0 and kept == input (by ids)."""
    kept, n_dropped = drop_contaminated(pairs, frozenset())
    assert n_dropped == 0, f"expected 0 dropped with empty our_keys, got {n_dropped}"
    assert len(kept) == len(pairs), f"expected all {len(pairs)} pairs kept, got {len(kept)}"
    kept_ids = {(p.left.id, p.right.id) for p in kept}
    input_ids = {(p.left.id, p.right.id) for p in pairs}
    assert kept_ids == input_ids, f"kept id-set differs from input: {kept_ids} vs {input_ids}"


# ---------------------------------------------------------------------------
# P-JUDGEMENT-TOTAL: load_os_pairs only emits on positive/negative
# ---------------------------------------------------------------------------

_SKIP_JUDGEMENTS = ["unsure", "no_judgement", "maybe", "garbage123", "POSITIVE", ""]


def _os_line(judgement: str, lid: str = "l1", rid: str = "r1") -> str:
    left = {"id": lid, "schema": "Company", "properties": {"name": ["A"]}}
    right = {"id": rid, "schema": "Company", "properties": {"name": ["B"]}}
    return json.dumps({"judgement": judgement, "left": left, "right": right})


@given(judgement=st.sampled_from(["positive", "negative"] + _SKIP_JUDGEMENTS))
@_FAST
def test_p_judgement_total_emit_iff_pos_or_neg(judgement: str) -> None:
    """P-JUDGEMENT-TOTAL: load_os_pairs emits a pair iff judgement in {'positive','negative'};
    correct label mapping; all other judgement values are skipped (never coerced).
    """
    pairs = list(load_os_pairs([_os_line(judgement)]))
    if judgement == "positive":
        assert len(pairs) == 1, f"'positive' must emit 1 pair; got {len(pairs)}"
        assert pairs[0].label == "match", f"'positive' -> 'match'; got {pairs[0].label!r}"
        assert pairs[0].source == BENCHMARK_SOURCE
        assert pairs[0].sub_source == "os_pairs"
    elif judgement == "negative":
        assert len(pairs) == 1, f"'negative' must emit 1 pair; got {len(pairs)}"
        assert pairs[0].label == "non_match", f"'negative' -> 'non_match'; got {pairs[0].label!r}"
    else:
        assert len(pairs) == 0, (
            f"judgement {judgement!r} must be skipped (INV-JUDGEMENT-MAP); got {len(pairs)} pairs"
        )


# ---------------------------------------------------------------------------
# P-FLOOR-MATH: evaluate_floor metric formulas
# ---------------------------------------------------------------------------


@given(
    n_match=st.integers(min_value=1, max_value=5),
    n_non_match=st.integers(min_value=1, max_value=5),
)
@_FAST
def test_p_floor_math_oracle_precision_recall_f1_one(n_match: int, n_non_match: int) -> None:
    """P-FLOOR-MATH: oracle score_fn ⇒ precision=recall=f1=1.0, over_merge_rate=0.0."""
    match_pairs = [
        BenchmarkPair(
            left=_make_entity(f"lm{i}"),
            right=_make_entity(f"rm{i}"),
            label="match",
            sub_source="os_pairs",
        )
        for i in range(n_match)
    ]
    non_match_pairs = [
        BenchmarkPair(
            left=_make_entity(f"ln{i}"),
            right=_make_entity(f"rn{i}"),
            label="non_match",
            sub_source="os_pairs",
        )
        for i in range(n_non_match)
    ]
    all_pairs = match_pairs + non_match_pairs
    match_ids = {(p.left.id, p.right.id) for p in match_pairs}

    def oracle(left: FtmEntity, right: FtmEntity) -> float:
        return 1.0 if (left.id, right.id) in match_ids else 0.0

    result = evaluate_floor(all_pairs, oracle, threshold=0.5)

    assert result.n == n_match + n_non_match
    assert abs(result.precision - 1.0) < 1e-9, f"precision={result.precision}"
    assert abs(result.recall - 1.0) < 1e-9, f"recall={result.recall}"
    assert abs(result.f1 - 1.0) < 1e-9, f"f1={result.f1}"
    assert abs(result.over_merge_rate - 0.0) < 1e-9, f"over_merge_rate={result.over_merge_rate}"


@given(
    n_match=st.integers(min_value=1, max_value=5),
    n_non_match=st.integers(min_value=1, max_value=5),
)
@_FAST
def test_p_floor_math_inverted_oracle(n_match: int, n_non_match: int) -> None:
    """P-FLOOR-MATH: inverted oracle ⇒ precision=recall=f1=0.0, over_merge_rate=1.0.

    Inverted oracle: predicts match for non_match pairs, non_match for match pairs.
    TP=0, FP=n_non_match, FN=n_match.
    over_merge_rate = FP/(TP+FP) = n_non_match/n_non_match = 1.0.
    """
    match_pairs = [
        BenchmarkPair(
            left=_make_entity(f"lm{i}"),
            right=_make_entity(f"rm{i}"),
            label="match",
            sub_source="os_pairs",
        )
        for i in range(n_match)
    ]
    non_match_pairs = [
        BenchmarkPair(
            left=_make_entity(f"ln{i}"),
            right=_make_entity(f"rn{i}"),
            label="non_match",
            sub_source="os_pairs",
        )
        for i in range(n_non_match)
    ]
    all_pairs = match_pairs + non_match_pairs
    non_match_ids = {(p.left.id, p.right.id) for p in non_match_pairs}

    def inverted(left: FtmEntity, right: FtmEntity) -> float:
        # Score 1.0 for what are actually non_match pairs → all FP; 0.0 for match → all FN
        return 1.0 if (left.id, right.id) in non_match_ids else 0.0

    result = evaluate_floor(all_pairs, inverted, threshold=0.5)

    assert abs(result.precision - 0.0) < 1e-9, f"inverted oracle: precision={result.precision}"
    assert abs(result.recall - 0.0) < 1e-9, f"inverted oracle: recall={result.recall}"
    assert abs(result.f1 - 0.0) < 1e-9, f"inverted oracle: f1={result.f1}"
    # FP=n_non_match, TP=0 → OMR = n_non_match/n_non_match = 1.0
    assert abs(result.over_merge_rate - 1.0) < 1e-9, (
        f"inverted oracle: over_merge_rate={result.over_merge_rate} (expected 1.0)"
    )


@given(
    n_tp=st.integers(min_value=0, max_value=4),
    n_fp=st.integers(min_value=0, max_value=4),
    n_fn=st.integers(min_value=0, max_value=4),
    n_tn=st.integers(min_value=0, max_value=4),
)
@_FAST
def test_p_floor_math_over_merge_rate_general_formula(
    n_tp: int, n_fp: int, n_fn: int, n_tn: int
) -> None:
    """P-FLOOR-MATH: over_merge_rate == FP/(TP+FP), or 0.0 when TP+FP==0 (pairwise floor formula).

    Builds a score_fn stub that assigns score=0.9 for TP/FP pairs (predicted match) and 0.1 for
    FN/TN pairs (predicted non_match). Verifies the formula exactly.
    """
    score_map: dict[tuple[str, str], float] = {}
    all_pairs: list[BenchmarkPair] = []

    for i in range(n_tp):
        lid, rid = f"tp-l{i}", f"tp-r{i}"
        all_pairs.append(
            BenchmarkPair(
                left=_make_entity(lid),
                right=_make_entity(rid),
                label="match",
                sub_source="os_pairs",
            )
        )
        score_map[(lid, rid)] = 0.9  # TP: label=match, predicted match

    for i in range(n_fp):
        lid, rid = f"fp-l{i}", f"fp-r{i}"
        all_pairs.append(
            BenchmarkPair(
                left=_make_entity(lid),
                right=_make_entity(rid),
                label="non_match",
                sub_source="os_pairs",
            )
        )
        score_map[(lid, rid)] = 0.9  # FP: label=non_match, predicted match

    for i in range(n_fn):
        lid, rid = f"fn-l{i}", f"fn-r{i}"
        all_pairs.append(
            BenchmarkPair(
                left=_make_entity(lid),
                right=_make_entity(rid),
                label="match",
                sub_source="os_pairs",
            )
        )
        score_map[(lid, rid)] = 0.1  # FN: label=match, predicted non_match

    for i in range(n_tn):
        lid, rid = f"tn-l{i}", f"tn-r{i}"
        all_pairs.append(
            BenchmarkPair(
                left=_make_entity(lid),
                right=_make_entity(rid),
                label="non_match",
                sub_source="os_pairs",
            )
        )
        score_map[(lid, rid)] = 0.1  # TN: label=non_match, predicted non_match

    def score_fn(left: FtmEntity, right: FtmEntity) -> float:
        return score_map.get((left.id, right.id), 0.0)

    result = evaluate_floor(all_pairs, score_fn, threshold=0.5)

    expected_omr = n_fp / (n_tp + n_fp) if (n_tp + n_fp) > 0 else 0.0
    assert abs(result.over_merge_rate - expected_omr) < 1e-9, (
        f"over_merge_rate mismatch: expected {expected_omr}, got {result.over_merge_rate} "
        f"(n_tp={n_tp}, n_fp={n_fp}, n_fn={n_fn}, n_tn={n_tn})"
    )
    assert result.n == len(all_pairs), f"n mismatch: {result.n} vs {len(all_pairs)}"
    assert result.n_dropped_contaminated == 0


# ---------------------------------------------------------------------------
# P-CONTAM-IN-FLOOR: injecting a key drops exactly that pair from the floor
# ---------------------------------------------------------------------------


@given(
    n_clean=st.integers(min_value=1, max_value=6),
    dirty_label=_LABEL,
)
@_FAST
def test_p_contam_in_floor_drops_exactly_one_increments_count(
    n_clean: int, dirty_label: str
) -> None:
    """P-CONTAM-IN-FLOOR: injecting a key from a pair's identity into contamination_keys drops
    exactly that pair: n_dropped_contaminated == 1, n == clean_n - 1 vs the clean run.
    """
    clean_pairs = [
        BenchmarkPair(
            left=_make_entity(f"cl{i}"),
            right=_make_entity(f"cr{i}"),
            label="match",
            sub_source="os_pairs",
        )
        for i in range(n_clean)
    ]
    dirty_pair = BenchmarkPair(
        left=_make_entity("dirty-left-sentinel"),
        right=_make_entity("dirty-right-sentinel"),
        label=dirty_label,
        sub_source="os_pairs",
    )
    all_pairs = clean_pairs + [dirty_pair]

    # Clean run: no contamination keys.
    clean_result = evaluate_floor(all_pairs, lambda l, r: 0.0, threshold=0.5)
    assert clean_result.n_dropped_contaminated == 0

    # Contaminated run: inject the dirty pair's left entity id.
    contaminated_result = evaluate_floor(
        all_pairs,
        lambda l, r: 0.0,
        threshold=0.5,
        contamination_keys={"dirty-left-sentinel"},
    )

    assert contaminated_result.n_dropped_contaminated == 1, (
        f"expected n_dropped_contaminated=1 after injecting one key; "
        f"got {contaminated_result.n_dropped_contaminated}"
    )
    assert contaminated_result.n == clean_result.n - 1, (
        f"n must decrement by 1 when one pair is dropped: "
        f"clean.n={clean_result.n}, contaminated.n={contaminated_result.n}"
    )


@given(
    n_clean=st.integers(min_value=0, max_value=5),
    n_dirty=st.integers(min_value=1, max_value=4),
)
@_FAST
def test_p_contam_in_floor_multiple_contaminated(n_clean: int, n_dirty: int) -> None:
    """P-CONTAM-IN-FLOOR: injecting keys for n_dirty pairs drops exactly n_dirty, n decrements."""
    clean_pairs = [
        BenchmarkPair(
            left=_make_entity(f"cl{i}"),
            right=_make_entity(f"cr{i}"),
            label="match",
            sub_source="os_pairs",
        )
        for i in range(n_clean)
    ]
    dirty_pairs = [
        BenchmarkPair(
            left=_make_entity(f"dl{i}"),
            right=_make_entity(f"dr{i}"),
            label="non_match",
            sub_source="os_pairs",
        )
        for i in range(n_dirty)
    ]
    all_pairs = clean_pairs + dirty_pairs
    dirty_keys = {f"dl{i}" for i in range(n_dirty)}  # all dirty left-entity ids

    result = evaluate_floor(
        all_pairs, lambda l, r: 0.0, threshold=0.5, contamination_keys=dirty_keys
    )

    assert result.n_dropped_contaminated == n_dirty, (
        f"expected n_dropped_contaminated={n_dirty}, got {result.n_dropped_contaminated}"
    )
    assert result.n == n_clean, f"expected n={n_clean}, got {result.n}"
    assert result.n + result.n_dropped_contaminated == len(all_pairs), (
        "n + n_dropped_contaminated must equal total input pairs (no silent truncation)"
    )


# ---------------------------------------------------------------------------
# P-IMPORT-PURITY: benchmark.py references no scoring symbol (INV-IMPORT-PURITY)
# ---------------------------------------------------------------------------

_FORBIDDEN_SYMBOLS = ("score_pairs", "match_probability", "ScoredPair")


def test_p_import_purity_no_scoring_symbol_as_code_node() -> None:
    """P-IMPORT-PURITY: inspect.getsource(benchmark) has no forbidden scoring symbol in AST nodes.

    Uses the AST to check actual code-level names (imports, Name nodes, Attribute nodes) so that
    docstring text mentioning a symbol doesn't trigger a false positive.
    The key invariant: the matcher is INJECTED via score_fn; benchmark.py must not import or
    reference any scoring function from the model layer.
    """
    source = inspect.getsource(_benchmark_module)
    tree = ast.parse(source)

    code_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                code_names.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, ast.Name):
            code_names.add(node.id)
        elif isinstance(node, ast.Attribute):
            code_names.add(node.attr)

    for symbol in _FORBIDDEN_SYMBOLS:
        assert symbol not in code_names, (
            f"P-IMPORT-PURITY / INV-IMPORT-PURITY: benchmark.py must not reference "
            f"{symbol!r} in any code node; found among collected names.\n"
            f"Forbidden symbol {symbol!r} appears in AST names: {sorted(code_names)}"
        )


def test_p_import_purity_evaluate_floor_accepts_score_fn_param() -> None:
    """P-IMPORT-PURITY: evaluate_floor must have a score_fn parameter (injected, not baked-in)."""
    from worldmonitor.resolution.benchmark import evaluate_floor as _ef

    sig = inspect.signature(_ef)
    assert "score_fn" in sig.parameters, (
        f"P-IMPORT-PURITY: evaluate_floor must accept score_fn; got params: {list(sig.parameters)}"
    )
