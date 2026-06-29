"""Unit tests for ``resolution.benchmark`` — external-benchmark FLOOR (ADR 0080, G7 slice 3).

ALL tests are Docker-free, network-free, and hermetic.
OS-Pairs fixture: tiny hand-authored synthetic lines (no real OpenSanctions data).
Febrl mapper tests: synthetic record dict (no recordlinkage import).
Febrl loader tests: gated on pytest.importorskip("recordlinkage").
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
from typing import Any
from unittest.mock import patch

import pytest

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.benchmark import (
    BENCHMARK_SOURCE,
    BenchmarkPair,
    FloorMetrics,
    _febrl_record_to_entity,
    drop_contaminated,
    evaluate_floor,
    identity_keys,
    load_os_pairs,
)

# ---------------------------------------------------------------------------
# OS-Pairs fixture helpers
# ---------------------------------------------------------------------------


def _os_line(judgement: str, left_id: str, right_id: str) -> str:
    """Build one OS-Pairs JSON line with a minimal FtM entity dict on each side."""
    left = {"id": left_id, "schema": "Company", "properties": {"name": ["Left Corp"]}}
    right = {"id": right_id, "schema": "Company", "properties": {"name": ["Right Corp"]}}
    return json.dumps({"judgement": judgement, "left": left, "right": right})


_OS_FIXTURE: list[str] = [
    _os_line("positive", "os-l-1", "os-r-1"),  # -> "match"
    _os_line("negative", "os-l-2", "os-r-2"),  # -> "non_match"
    _os_line("unsure", "os-l-3", "os-r-3"),  # -> SKIPPED
]


# ---------------------------------------------------------------------------
# OS-Pairs importer tests (INV-JUDGEMENT-MAP)
# ---------------------------------------------------------------------------


def test_os_pairs_positive_maps_to_match() -> None:
    """judgement='positive' must emit BenchmarkPair with label='match'."""
    pairs = list(load_os_pairs([_os_line("positive", "l1", "r1")]))
    assert len(pairs) == 1
    assert pairs[0].label == "match"


def test_os_pairs_negative_maps_to_non_match() -> None:
    """judgement='negative' must emit BenchmarkPair with label='non_match'."""
    pairs = list(load_os_pairs([_os_line("negative", "l2", "r2")]))
    assert len(pairs) == 1
    assert pairs[0].label == "non_match"


def test_os_pairs_unsure_is_skipped() -> None:
    """judgement='unsure' must NOT emit a pair — it is skipped (INV-JUDGEMENT-MAP)."""
    pairs = list(load_os_pairs([_os_line("unsure", "l3", "r3")]))
    assert pairs == [], f"'unsure' must be skipped; got {pairs}"


def test_os_pairs_no_judgement_key_is_skipped() -> None:
    """A line missing the 'judgement' key must be skipped (INV-JUDGEMENT-MAP)."""
    line = json.dumps(
        {
            "left": {"id": "l4", "schema": "Company", "properties": {"name": ["A"]}},
            "right": {"id": "r4", "schema": "Company", "properties": {"name": ["B"]}},
        }
    )
    pairs = list(load_os_pairs([line]))
    assert pairs == [], f"missing judgement key must be skipped; got {pairs}"


def test_os_pairs_garbage_judgement_is_skipped() -> None:
    """An unknown judgement value must be skipped, never coerced (INV-JUDGEMENT-MAP)."""
    pairs = list(load_os_pairs([_os_line("maybe", "l5", "r5")]))
    assert pairs == [], f"unknown judgement must be skipped; got {pairs}"


def test_os_pairs_fixture_yields_exactly_two_pairs() -> None:
    """The 3-line fixture (positive+negative+unsure) must yield exactly 2 BenchmarkPairs."""
    pairs = list(load_os_pairs(_OS_FIXTURE))
    assert len(pairs) == 2
    labels = {p.label for p in pairs}
    assert labels == {"match", "non_match"}


def test_os_pairs_source_tagging() -> None:
    """Every emitted BenchmarkPair must carry source=BENCHMARK_SOURCE and sub_source='os_pairs'."""
    pairs = list(load_os_pairs(_OS_FIXTURE))
    assert len(pairs) == 2
    for pair in pairs:
        assert pair.source == BENCHMARK_SOURCE, (
            f"expected source={BENCHMARK_SOURCE!r}, got {pair.source!r}"
        )
        assert pair.source == "external_benchmark", (
            f"BENCHMARK_SOURCE must equal 'external_benchmark'; got {pair.source!r}"
        )
        assert pair.sub_source == "os_pairs", (
            f"expected sub_source='os_pairs', got {pair.sub_source!r}"
        )


def test_os_pairs_left_right_are_ftm_entities() -> None:
    """left and right of each BenchmarkPair must be FtmEntity instances (INV-FTM-NATIVE)."""
    pairs = list(load_os_pairs([_os_line("positive", "l6", "r6")]))
    assert len(pairs) == 1
    pair = pairs[0]
    assert isinstance(pair.left, FtmEntity), f"left is {type(pair.left)}, expected FtmEntity"
    assert isinstance(pair.right, FtmEntity), f"right is {type(pair.right)}, expected FtmEntity"


def test_os_pairs_entity_ids_preserved() -> None:
    """The FtM entity ids from the JSON must be preserved in left.id / right.id."""
    pairs = list(load_os_pairs([_os_line("positive", "my-left-id", "my-right-id")]))
    assert len(pairs) == 1
    assert pairs[0].left.id == "my-left-id", f"left.id={pairs[0].left.id!r}"
    assert pairs[0].right.id == "my-right-id", f"right.id={pairs[0].right.id!r}"


# ---------------------------------------------------------------------------
# Febrl record mapper (_febrl_record_to_entity) — ALWAYS runs, no recordlinkage import
# ---------------------------------------------------------------------------


def test_febrl_record_to_entity_person_schema() -> None:
    """_febrl_record_to_entity must return a FtmEntity with schema='Person' (INV-FTM-NATIVE)."""
    row: dict[str, Any] = {
        "given_name": "Jane",
        "surname": "Smith",
        "date_of_birth": "1985-06-15",
        "age": "38",
        "suburb": "Sydney",
        "state": "NSW",
    }
    entity = _febrl_record_to_entity("rec-001", row)
    assert isinstance(entity, FtmEntity), f"expected FtmEntity, got {type(entity)}"
    assert entity.id == "rec-001", f"expected id='rec-001', got {entity.id!r}"
    assert entity.schema.name == "Person", f"expected schema=Person, got {entity.schema.name!r}"


def test_febrl_record_to_entity_name_set() -> None:
    """_febrl_record_to_entity must surface given_name/surname in name-bearing properties."""
    row: dict[str, Any] = {"given_name": "Alice", "surname": "Jones", "date_of_birth": ""}
    entity = _febrl_record_to_entity("rec-002", row)
    # Collect all name-related property values.
    all_name_values = (
        list(entity.get("name", quiet=True))
        + list(entity.get("firstName", quiet=True))
        + list(entity.get("lastName", quiet=True))
    )
    all_str = " ".join(str(v) for v in all_name_values).lower()
    assert "alice" in all_str or "jones" in all_str, (
        f"expected Alice/Jones in name properties; got values: {all_name_values}"
    )


def test_febrl_record_to_entity_birth_date_mapped() -> None:
    """_febrl_record_to_entity must map non-empty date_of_birth -> birthDate property."""
    row: dict[str, Any] = {"given_name": "Bob", "surname": "Lee", "date_of_birth": "1970-03-22"}
    entity = _febrl_record_to_entity("rec-003", row)
    birth_dates = list(entity.get("birthDate", quiet=True))
    assert birth_dates, (
        f"expected birthDate to be set for date_of_birth='1970-03-22'; got empty. "
        f"Entity properties: {dict(entity.properties)}"
    )


def test_febrl_record_to_entity_empty_birth_date_ok() -> None:
    """_febrl_record_to_entity must not crash when date_of_birth is empty or missing."""
    row: dict[str, Any] = {"given_name": "Carol", "surname": "Kim"}
    entity = _febrl_record_to_entity("rec-004", row)
    assert isinstance(entity, FtmEntity)


def test_febrl_record_to_entity_id_is_record_id() -> None:
    """The entity id must match the record_id argument passed to _febrl_record_to_entity."""
    row: dict[str, Any] = {"given_name": "Dave", "surname": "Park", "date_of_birth": "2000-12-01"}
    entity = _febrl_record_to_entity("febrl-42", row)
    assert entity.id == "febrl-42", f"expected id='febrl-42', got {entity.id!r}"


# ---------------------------------------------------------------------------
# Febrl loader — gated on recordlinkage availability
# ---------------------------------------------------------------------------


def test_febrl_load_raises_importerror_when_absent() -> None:
    """load_febrl must raise a clear ImportError mentioning 'recordlinkage' when absent."""
    from worldmonitor.resolution.benchmark import load_febrl

    with patch.dict(sys.modules, {"recordlinkage": None}):
        with pytest.raises((ImportError, ModuleNotFoundError), match="recordlinkage"):
            # Force the lazy import to re-run by consuming the iterator.
            list(load_febrl("febrl1"))


def test_febrl_load_febrl1_yields_match_pairs() -> None:
    """load_febrl('febrl1') must yield BenchmarkPairs with label='match' (true-link set)."""
    pytest.importorskip(
        "recordlinkage",
        reason=(
            "recordlinkage not installed; builder must add it to the 'benchmark' optional dep group"
        ),
    )
    from worldmonitor.resolution.benchmark import load_febrl

    pairs = list(load_febrl("febrl1"))
    match_pairs = [p for p in pairs if p.label == "match"]
    assert match_pairs, "load_febrl('febrl1') yielded no 'match' pairs"


def test_febrl_load_febrl1_sub_source_tagging() -> None:
    """load_febrl must tag each pair with source=BENCHMARK_SOURCE and sub_source='febrl1'."""
    pytest.importorskip(
        "recordlinkage",
        reason=(
            "recordlinkage not installed; builder must add it to the 'benchmark' optional dep group"
        ),
    )
    from worldmonitor.resolution.benchmark import load_febrl

    pairs = list(load_febrl("febrl1"))
    assert pairs, "load_febrl('febrl1') returned no pairs"
    for pair in pairs[:10]:  # spot-check the first few
        assert pair.source == BENCHMARK_SOURCE, f"source={pair.source!r}"
        assert pair.sub_source == "febrl1", f"sub_source={pair.sub_source!r}"


def test_febrl_load_febrl1_entities_are_persons() -> None:
    """Febrl pairs must carry FtM Person entities on left and right (INV-FTM-NATIVE)."""
    pytest.importorskip(
        "recordlinkage",
        reason=(
            "recordlinkage not installed; builder must add it to the 'benchmark' optional dep group"
        ),
    )
    from worldmonitor.resolution.benchmark import load_febrl

    pairs = list(load_febrl("febrl1"))
    assert pairs
    for pair in pairs[:5]:
        assert pair.left.schema.name == "Person", (
            f"expected Person on left, got {pair.left.schema.name!r}"
        )
        assert pair.right.schema.name == "Person", (
            f"expected Person on right, got {pair.right.schema.name!r}"
        )


# ---------------------------------------------------------------------------
# identity_keys (INV-CONTAM: single source of truth shared with silver)
# ---------------------------------------------------------------------------


def test_identity_keys_includes_entity_id() -> None:
    """identity_keys must always include entity.id."""
    entity = make_entity(
        {
            "id": "unique-id-xyz",
            "schema": "Company",
            "properties": {"name": ["X"]},
        }
    )
    keys = identity_keys(entity)
    assert isinstance(keys, frozenset), f"expected frozenset, got {type(keys)}"
    assert "unique-id-xyz" in keys, f"entity.id missing from identity_keys: {keys}"


def test_identity_keys_includes_anchor_property_value() -> None:
    """identity_keys must include anchor property values (leiCode example, INV-CONTAM)."""
    entity = make_entity(
        {
            "id": "e-anchor-test",
            "schema": "Company",
            "properties": {"name": ["Corp"], "leiCode": ["LEI-ABC123"]},
        }
    )
    keys = identity_keys(entity)
    assert "LEI-ABC123" in keys, (
        f"leiCode value 'LEI-ABC123' must appear in identity_keys; got {keys}"
    )
    assert "e-anchor-test" in keys


def test_identity_keys_excludes_non_anchor_values() -> None:
    """identity_keys must NOT include arbitrary non-anchor property values (only id + anchors)."""
    entity = make_entity(
        {
            "id": "e-noanchor",
            "schema": "Company",
            "properties": {"name": ["SomeName"], "country": ["DE"]},
        }
    )
    keys = identity_keys(entity)
    assert "SomeName" not in keys, f"non-anchor 'name' value leaked into identity_keys: {keys}"
    assert "DE" not in keys, f"non-anchor 'country' value leaked into identity_keys: {keys}"


# ---------------------------------------------------------------------------
# drop_contaminated — the load-bearing invariant (INV-CONTAM)
# ---------------------------------------------------------------------------


def _pair(
    left_id: str,
    right_id: str,
    label: str = "match",
    *,
    left_lei: str | None = None,
) -> BenchmarkPair:
    """Minimal BenchmarkPair with optional leiCode anchor on left entity."""
    props_l: dict[str, list[str]] = {"name": ["L"]}
    if left_lei:
        props_l["leiCode"] = [left_lei]
    left = make_entity({"id": left_id, "schema": "Company", "properties": props_l})
    right = make_entity({"id": right_id, "schema": "Company", "properties": {"name": ["R"]}})
    return BenchmarkPair(left=left, right=right, label=label, sub_source="os_pairs")


def test_drop_contaminated_drops_pair_by_entity_id() -> None:
    """A pair whose left.id is in our_keys must be dropped (INV-CONTAM)."""
    clean = _pair("clean-left", "clean-right")
    dirty = _pair("dirty-left", "dirty-right")
    kept, n_dropped = drop_contaminated([clean, dirty], {"dirty-left"})

    assert n_dropped == 1, f"expected n_dropped=1, got {n_dropped}"
    assert len(kept) == 1, f"expected 1 kept pair, got {len(kept)}"
    assert kept[0].left.id == "clean-left", f"wrong pair kept: {kept[0].left.id!r}"


def test_drop_contaminated_drops_pair_by_right_entity_id() -> None:
    """A pair whose right.id is in our_keys must also be dropped (INV-CONTAM)."""
    clean = _pair("l1", "r1")
    dirty = _pair("l2", "r2-contaminated")
    kept, n_dropped = drop_contaminated([clean, dirty], {"r2-contaminated"})

    assert n_dropped == 1
    assert len(kept) == 1
    assert kept[0].left.id == "l1"


def test_drop_contaminated_drops_pair_by_anchor_value() -> None:
    """A pair whose entity's anchor value is in our_keys must be dropped (INV-CONTAM)."""
    clean = _pair("l1", "r1")
    dirty = _pair("l2", "r2", left_lei="LEI-CONTAMINATED")
    kept, n_dropped = drop_contaminated([clean, dirty], {"LEI-CONTAMINATED"})

    assert n_dropped == 1, f"expected n_dropped=1, got {n_dropped}"
    assert len(kept) == 1
    assert kept[0].left.id == "l1"


def test_drop_contaminated_no_silent_truncation_count_invariant() -> None:
    """len(kept) + n_dropped == len(input) — no silent truncation (INV-CONTAM)."""
    pairs = [_pair(f"l{i}", f"r{i}") for i in range(10)]
    our_keys = {"l3", "l7"}  # contaminate exactly 2 pairs (by left id)

    kept, n_dropped = drop_contaminated(pairs, our_keys)

    assert len(kept) + n_dropped == len(pairs), (
        f"silent truncation: {len(kept)} kept + {n_dropped} dropped != {len(pairs)} input"
    )
    assert n_dropped == 2, f"expected 2 dropped, got {n_dropped}"
    assert len(kept) == 8


def test_drop_contaminated_empty_our_keys_keeps_all() -> None:
    """With our_keys=frozenset(), nothing is dropped (P-GUARD-EMPTY)."""
    pairs = [_pair(f"l{i}", f"r{i}") for i in range(5)]
    kept, n_dropped = drop_contaminated(pairs, frozenset())

    assert n_dropped == 0, f"expected 0 dropped with empty our_keys, got {n_dropped}"
    assert len(kept) == 5


def test_drop_contaminated_all_contaminated_empty_kept() -> None:
    """When every pair is contaminated, kept is empty and n_dropped == len(input)."""
    pairs = [_pair(f"l{i}", f"r{i}") for i in range(3)]
    our_keys = {f"l{i}" for i in range(3)}  # all left ids contaminated

    kept, n_dropped = drop_contaminated(pairs, our_keys)

    assert len(kept) == 0, f"expected 0 kept, got {len(kept)}"
    assert n_dropped == 3
    assert len(kept) + n_dropped == 3


def test_drop_contaminated_returns_tuple_of_list_and_int() -> None:
    """drop_contaminated must return (list[BenchmarkPair], int)."""
    result = drop_contaminated([_pair("l", "r")], frozenset())
    assert isinstance(result, tuple) and len(result) == 2
    kept, n_dropped = result
    assert isinstance(kept, list)
    assert isinstance(n_dropped, int)


# ---------------------------------------------------------------------------
# evaluate_floor — math correctness (INV-FLOOR-MATH)
# ---------------------------------------------------------------------------


def _score_map(scores: dict[tuple[str, str], float]):
    """Return a score_fn stub keyed on (left.id, right.id)."""

    def _fn(left: FtmEntity, right: FtmEntity) -> float:
        return scores.get((left.id, right.id), 0.0)

    return _fn


def test_evaluate_floor_math_known_set() -> None:
    """evaluate_floor on a 4-pair known set with hand-computed values.

    threshold=0.5:
      pair A: label=match,     score=0.8 -> TP
      pair B: label=match,     score=0.3 -> FN
      pair C: label=non_match, score=0.9 -> FP
      pair D: label=non_match, score=0.1 -> TN

    TP=1, FP=1, FN=1, TN=1
    precision        = 1/(1+1) = 0.5
    recall           = 1/(1+1) = 0.5
    f1               = harmonic_mean(0.5, 0.5) = 0.5
    over_merge_rate  = FP/(TP+FP) = 1/2 = 0.5
    n=4, n_dropped_contaminated=0
    """
    pa = _pair("lA", "rA", label="match")
    pb = _pair("lB", "rB", label="match")
    pc = _pair("lC", "rC", label="non_match")
    pd = _pair("lD", "rD", label="non_match")

    scores = {("lA", "rA"): 0.8, ("lB", "rB"): 0.3, ("lC", "rC"): 0.9, ("lD", "rD"): 0.1}
    result = evaluate_floor([pa, pb, pc, pd], _score_map(scores), threshold=0.5)

    assert isinstance(result, FloorMetrics), f"expected FloorMetrics, got {type(result)}"
    assert result.n == 4, f"n={result.n}"
    assert result.n_dropped_contaminated == 0
    assert abs(result.precision - 0.5) < 1e-9, f"precision={result.precision}"
    assert abs(result.recall - 0.5) < 1e-9, f"recall={result.recall}"
    assert abs(result.f1 - 0.5) < 1e-9, f"f1={result.f1}"
    assert abs(result.over_merge_rate - 0.5) < 1e-9, f"over_merge_rate={result.over_merge_rate}"


def test_evaluate_floor_oracle_perfect_scores() -> None:
    """An oracle score_fn (1.0 for match, 0.0 for non_match) => precision=recall=f1=1.0, OMR=0.0."""
    match_pairs = [_pair(f"lm{i}", f"rm{i}", label="match") for i in range(3)]
    non_match_pairs = [_pair(f"ln{i}", f"rn{i}", label="non_match") for i in range(2)]
    all_pairs = match_pairs + non_match_pairs

    match_ids = {(p.left.id, p.right.id) for p in match_pairs}

    def oracle(left: FtmEntity, right: FtmEntity) -> float:
        return 1.0 if (left.id, right.id) in match_ids else 0.0

    result = evaluate_floor(all_pairs, oracle, threshold=0.5)

    assert result.n == 5
    assert abs(result.precision - 1.0) < 1e-9, f"precision={result.precision}"
    assert abs(result.recall - 1.0) < 1e-9, f"recall={result.recall}"
    assert abs(result.f1 - 1.0) < 1e-9, f"f1={result.f1}"
    assert abs(result.over_merge_rate - 0.0) < 1e-9, f"over_merge_rate={result.over_merge_rate}"


def test_evaluate_floor_no_predicted_matches_over_merge_rate_zero() -> None:
    """When no pairs are predicted as matches (score always < threshold), over_merge_rate=0.0."""
    pairs = [_pair("l1", "r1", label="match"), _pair("l2", "r2", label="non_match")]
    result = evaluate_floor(pairs, lambda l, r: 0.0, threshold=0.5)
    assert result.over_merge_rate == 0.0, (
        f"over_merge_rate must be 0.0 when no predicted matches; got {result.over_merge_rate}"
    )


def test_evaluate_floor_over_merge_rate_formula() -> None:
    """over_merge_rate = FP/(TP+FP) — verify on an explicit 2-TP / 3-FP set.

    TP=2 (match, score=0.9), FP=3 (non_match, score=0.9), FN=0, TN=0.
    precision = 2/5 = 0.4
    over_merge_rate = 3/5 = 0.6
    """
    tp_pairs = [_pair(f"tp-l{i}", f"tp-r{i}", label="match") for i in range(2)]
    fp_pairs = [_pair(f"fp-l{i}", f"fp-r{i}", label="non_match") for i in range(3)]
    all_pairs = tp_pairs + fp_pairs

    result = evaluate_floor(all_pairs, lambda l, r: 0.9, threshold=0.5)

    assert abs(result.precision - 0.4) < 1e-9, f"precision={result.precision}"
    assert abs(result.over_merge_rate - 0.6) < 1e-9, f"over_merge_rate={result.over_merge_rate}"


def test_evaluate_floor_surfaces_n_dropped_contaminated() -> None:
    """evaluate_floor surfaces n_dropped_contaminated from drop_contaminated (INV-CONTAM)."""
    clean = _pair("l-clean", "r-clean", label="match")
    dirty = _pair("l-dirty", "r-dirty", label="non_match")

    result = evaluate_floor(
        [clean, dirty],
        lambda l, r: 1.0,
        threshold=0.5,
        contamination_keys={"l-dirty"},
    )

    assert result.n_dropped_contaminated == 1, (
        f"expected n_dropped_contaminated=1, got {result.n_dropped_contaminated}"
    )
    assert result.n == 1, f"expected n=1 after decontamination, got {result.n}"


def test_evaluate_floor_n_is_post_decontamination_count() -> None:
    """evaluate_floor.n must equal number of pairs scored AFTER decontamination."""
    pairs = [_pair(f"l{i}", f"r{i}") for i in range(6)]
    # contaminate 2 of them by their left-entity id
    result = evaluate_floor(pairs, lambda l, r: 0.0, threshold=0.5, contamination_keys={"l2", "l4"})
    assert result.n == 4, f"expected n=4 (6 - 2 contaminated); got {result.n}"
    assert result.n_dropped_contaminated == 2


# ---------------------------------------------------------------------------
# INV-IMPORT-PURITY: benchmark.py must not reference scoring symbols
# ---------------------------------------------------------------------------


def test_import_purity_no_scoring_symbols_in_code() -> None:
    """benchmark.py must not reference score_pairs / match_probability / ScoredPair in code
    (INV-IMPORT-PURITY: the matcher is injected via score_fn, not imported).
    """
    from worldmonitor.resolution import benchmark

    source = inspect.getsource(benchmark)
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

    forbidden = ("score_pairs", "match_probability", "ScoredPair")
    for symbol in forbidden:
        assert symbol not in code_names, (
            f"INV-IMPORT-PURITY: benchmark.py must not reference {symbol!r}; "
            f"found in code symbols: {sorted(code_names)}"
        )


def test_import_purity_evaluate_floor_has_score_fn_param() -> None:
    """evaluate_floor signature must carry score_fn as a parameter (injected, not baked-in)."""
    from worldmonitor.resolution.benchmark import evaluate_floor as _ef

    sig = inspect.signature(_ef)
    assert "score_fn" in sig.parameters, (
        f"evaluate_floor must accept a score_fn parameter; got params: {list(sig.parameters)}"
    )
