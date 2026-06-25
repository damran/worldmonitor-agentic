"""Unit tests for the gold-pair set + EM candidate + recommended-threshold report (ADR 0043).

Covers gate APPROVE criteria:
* gold-set sampling DETERMINISM and the required over-merge / blocking-conditional traps (§7);
* A2 — the EM candidate model LOADS + is evaluable against a gold-derived clustering;
* A5 — the cost-sensitive recommended threshold is COMPUTED from the measured PR curve (not
  stubbed) and a non-``0.92`` value is derivable.

All Docker-free: in-process DuckDB (Splink is embedded), pure-Python metrics, no testcontainers
and NO ``@pytest.mark.integration``. The Postgres-persist path (``persist_gold_pairs``) is
exercised by the integration suite, not here.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution import eval as harness
from worldmonitor.resolution import gold
from worldmonitor.resolution.splink_model import _flatten, train_candidate_model

# Splink is chatty; keep the test output quiet (mirrors splink_model.py).
logging.getLogger("splink").setLevel(logging.ERROR)


def _company(entity_id: str, name: str, country: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [name], "jurisdiction": [country]},
            "datasets": ["t"],
        }
    )


def _mid_band_frame() -> list[FtmEntity]:
    """Entities whose live-model pair scores land in the 0.5-0.95 uncertainty band (so the
    stratified-uncertainty path is genuinely exercised, not just the OS-Pairs set)."""
    return [
        _company("c1", "Acme Trading Group", "us"),
        _company("c2", "Acme Trading Holdings", "us"),
        _company("c3", "Acme Trade Group", "us"),
        _company("c4", "Beta Logistics", "us"),
        _company("c5", "Beta Logistic Co", "us"),
        _company("c6", "Acme Trding Grp", "us"),
    ]


# --------------------------------------------------------------------------------------------
# Gold-set construction (§7)
# --------------------------------------------------------------------------------------------
def test_gold_build_is_deterministic() -> None:
    entities = _mid_band_frame()
    first = gold.build_gold_pairs(entities, seed=gold.DEFAULT_SEED)
    second = gold.build_gold_pairs(entities, seed=gold.DEFAULT_SEED)
    assert first == second  # fully seeded — reproducible run-to-run


def test_gold_includes_uncertainty_and_os_pairs() -> None:
    entities = _mid_band_frame()
    pairs = gold.build_gold_pairs(entities, seed=gold.DEFAULT_SEED)
    sources = {p.source for p in pairs}
    assert "os_pairs" in sources
    assert "uncertainty" in sources  # mid-band candidates were sampled


def test_gold_uncertainty_pairs_are_in_band() -> None:
    entities = _mid_band_frame()
    pairs = gold.build_gold_pairs(entities, seed=gold.DEFAULT_SEED)
    sampled = [p for p in pairs if p.source == "uncertainty"]
    assert sampled, "expected at least one uncertainty-sampled pair from the mid-band frame"
    for pair in sampled:
        assert pair.clerical_score is not None
        assert gold.UNCERTAINTY_LOW <= pair.clerical_score <= gold.UNCERTAINTY_HIGH


def test_gold_pairs_are_canonically_ordered() -> None:
    pairs = gold.build_gold_pairs(_mid_band_frame(), seed=gold.DEFAULT_SEED)
    for pair in pairs:
        assert pair.left_id <= pair.right_id  # matches uq_er_gold_pair ordering


def test_gold_has_over_merge_trap_by_construction() -> None:
    """The set MUST contain at least one over-merge trap (a same-name/different-entity
    non_match) — present even when no scored candidate lands in the band (§7)."""
    pairs = gold.build_gold_pairs([], seed=gold.DEFAULT_SEED)  # no entities -> only OS-Pairs
    left, right = gold._canonical(*gold.OVER_MERGE_TRAP)
    trap = next(p for p in pairs if (p.left_id, p.right_id) == (left, right))
    assert trap.label == "non_match"


def test_gold_has_blocking_conditional_pair_by_construction() -> None:
    """The set MUST contain at least one blocking-conditional pair (outside every blocking
    rule) so the §5.3 pairwise blind spot is exercisable — present by construction."""
    pairs = gold.build_gold_pairs([], seed=gold.DEFAULT_SEED)
    left, right = gold._canonical(*gold.BLOCKING_CONDITIONAL_PAIR)
    pair = next(p for p in pairs if (p.left_id, p.right_id) == (left, right))
    assert pair.label == "non_match"


def test_gold_labels_and_sources_are_valid() -> None:
    pairs = gold.build_gold_pairs(_mid_band_frame(), seed=gold.DEFAULT_SEED)
    assert pairs
    for pair in pairs:
        assert pair.label in {"match", "non_match"}
        assert pair.source in {"uncertainty", "os_pairs"}


# --------------------------------------------------------------------------------------------
# A2 — EM candidate model LOADS + is evaluable
# --------------------------------------------------------------------------------------------
def _training_frame() -> list[FtmEntity]:
    """Entities with duplicate pairs the EM can learn m-weights over."""
    return [
        _company("c1", "Acme Corporation Ltd", "us"),
        _company("c2", "Acme Corporation Ltd", "us"),
        _company("c3", "Globex Incorporated", "gb"),
        _company("c4", "Globex Incorporated", "gb"),
        _company("c5", "Initech LLC", "us"),
        _company("c6", "Initech LLC", "us"),
    ]


def test_em_candidate_model_loads_and_is_evaluable() -> None:
    """A2: train_candidate_model returns a loadable settings artefact that reconstitutes into a
    scorable Linker — the candidate is EVALUABLE against the gold set. It is a measured candidate
    only (NOT promoted into score_pairs)."""
    from splink import DuckDBAPI, Linker

    entities = _training_frame()
    model = train_candidate_model(entities, seed=7)

    # The artefact is a JSON-serialisable settings dict carrying the trained model structure.
    assert isinstance(model, dict)
    assert "comparisons" in model

    # LOADABLE: a fresh Linker reconstitutes from the trained settings dict ...
    frame = pd.DataFrame([_flatten(e) for e in entities])
    loaded = Linker(frame, model, db_api=DuckDBAPI())  # pyright: ignore[reportArgumentType]
    # ... and is EVALUABLE: it scores candidate pairs without error.
    scored = loaded.inference.predict(threshold_match_probability=0.5).as_pandas_dataframe()
    assert "match_probability" in scored.columns


def test_em_candidate_model_is_reproducible() -> None:
    """Seeded u-estimation makes the candidate reproducible run-to-run (gold harness needs it)."""
    entities = _training_frame()
    first = train_candidate_model(entities, seed=7)
    second = train_candidate_model(entities, seed=7)
    # Compare the trained comparison structure (the m/u weights), not volatile linker metadata.
    assert first["comparisons"] == second["comparisons"]


# --------------------------------------------------------------------------------------------
# A5 — cost-sensitive recommended threshold is COMPUTED (not stubbed); non-0.92 derivable
# --------------------------------------------------------------------------------------------
def _pr_table_with_clear_optimum() -> pd.DataFrame:
    """A synthetic but realistic PR-curve table (the shape Splink's
    accuracy_analysis_from_labels_table(output_type="table") emits): one row per candidate
    match-probability threshold with fp/fn confusion counts. Constructed so the cost-minimising
    threshold under c_fp:c_fn = 10:1 is NOT 0.92 — proving the harness derives the value from
    the measured curve, not a hard-coded constant (A5 / DENY D1)."""
    return pd.DataFrame(
        [
            # threshold, fp, fn  -> cost = 10*fp + 1*fn
            {"match_probability": 0.30, "fp": 5, "fn": 0},  # cost 50
            {"match_probability": 0.55, "fp": 2, "fn": 1},  # cost 21
            {"match_probability": 0.70, "fp": 0, "fn": 2},  # cost  2  <- minimum
            {"match_probability": 0.92, "fp": 0, "fn": 6},  # cost  6  (the old expert default)
            {"match_probability": 0.99, "fp": 0, "fn": 9},  # cost  9
        ]
    )


def test_recommended_threshold_is_computed_from_curve() -> None:
    report = harness.recommended_threshold(_pr_table_with_clear_optimum())
    # The cost-minimising threshold (10*fp + fn) is 0.70 — derived from the measured curve.
    assert report.threshold == pytest.approx(0.70)
    assert report.cost == pytest.approx(2.0)
    assert report.false_positives == 0
    assert report.false_negatives == 2
    # The cost ratio echoes ADR 0043 (10:1) — false merge weighted >> false split.
    assert report.c_false_merge == 10.0
    assert report.c_false_split == 1.0


def test_recommended_threshold_can_differ_from_0_92() -> None:
    """A5 / Workflow-B end-state: the harness MUST be able to derive a non-0.92 threshold from
    the measured curve (slice-1 derives+proposes; slice-2 promotes under sign-off)."""
    report = harness.recommended_threshold(_pr_table_with_clear_optimum())
    assert report.threshold != pytest.approx(0.92)


def test_recommended_threshold_is_cost_sensitive_to_over_merge() -> None:
    """Raising the false-MERGE cost (c_fp) pushes the recommendation toward a HIGHER (more
    conservative) threshold — proving the asymmetry is genuinely cost-driven, not stubbed."""
    table = pd.DataFrame(
        [
            {"match_probability": 0.50, "fp": 1, "fn": 0},  # one false merge
            {"match_probability": 0.90, "fp": 0, "fn": 3},  # three false splits
        ]
    )
    cheap = harness.recommended_threshold(table, c_false_merge=1.0, c_false_split=1.0)
    expensive = harness.recommended_threshold(table, c_false_merge=10.0, c_false_split=1.0)
    # With symmetric cost, the low threshold (1 error) wins; with a 10x false-merge penalty the
    # over-merge is too costly, so the higher threshold (avoid the false merge) wins.
    assert cheap.threshold == pytest.approx(0.50)
    assert expensive.threshold == pytest.approx(0.90)


def test_recommended_threshold_rejects_empty_curve() -> None:
    with pytest.raises(ValueError, match="empty"):
        harness.recommended_threshold(pd.DataFrame())
