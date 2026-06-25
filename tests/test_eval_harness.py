"""Gate A — PRIMARY invariant test for the ER measurement harness (`resolution/eval.py`).

This is the oracle the builder must satisfy. It is written failing-test-first: on the current
tree `worldmonitor.resolution.eval` does not exist, so collection fails at import time (the
correct red state). It passes once the builder lands `eval.py` implementing the API defined
below. The test author writes ONLY `tests/`; the builder conforms to this contract verbatim.

This suite is intentionally placed at the repo-root `tests/` dir (NOT `tests/integration/`) and
carries NO `@pytest.mark.integration`, so it runs in the `quality` job under
`pytest -m "not integration"`. It must NOT require Docker/testcontainers. The cluster metrics
(B³ / CEAFe / over_merge_rate) are PURE PYTHON over partitions and are tested directly. The
blocking-conditional case (case 3) uses an in-process DuckDB Splink linker (Splink is embedded —
no Docker) to demonstrate the pairwise blind spot, and the cluster metrics to prove the harness
still catches the unblocked over-merge.

================================================================================================
THE `resolution/eval.py` API THE BUILDER MUST IMPLEMENT (verbatim — names + signatures locked)
================================================================================================

A clustering is represented as a partition mapping ``record_id -> cluster_label``:

    Partition = Mapping[str, str]

Both the gold partition and the predicted partition use this representation consistently. Two
records are in the same entity iff they carry the same cluster label.

Pure-Python cluster metrics (computed over the FULL gold partition, never limited to Splink's
blocked candidate set — that is what lets them catch blocking-conditional errors, gate spec §6):

    def bcubed(gold: Partition, predicted: Partition) -> tuple[float, float, float]:
        '''B³ (Bagga & Baldwin 1998). Returns (precision, recall, f1).
        Precision(r) = |P(r) ∩ G(r)| / |P(r)|; Recall(r) = |P(r) ∩ G(r)| / |G(r)|;
        B³ precision/recall = mean over all records; F1 = harmonic mean.'''

    def ceafe(gold: Partition, predicted: Partition) -> tuple[float, float, float]:
        '''CEAFe (Luo 2005, entity-based CEAF, φ4 similarity). Returns (precision, recall, f1).
        φ4(g, p) = 2·|g ∩ p| / (|g| + |p|). Optimal one-to-one alignment maximising total φ4
        is solved with scipy.optimize.linear_sum_assignment (Hungarian, on the negated matrix).
        precision = Σ φ4(aligned) / |predicted clusters|; recall = Σ φ4(aligned) / |gold
        clusters|; F1 = harmonic mean.'''

    def over_merge_rate(gold: Partition, predicted: Partition) -> float:
        '''Fraction of PREDICTED clusters that conflate ≥2 distinct gold entities:
        |{p ∈ predicted : p contains records from ≥2 gold clusters}| / |predicted|.
        0.0 on a correct clustering; > 0 whenever any cluster fuses distinct gold entities.'''

Pairwise / blocking-conditional helper (Splink pairwise PR analysis is structurally limited to
the pairs blocking generated — gate spec §5.3 / VERIFIED_API.md). The harness exposes the set of
gold pairs the pairwise PR analysis can actually SEE (i.e. that blocking generated), so a test
can assert the pairwise blind spot while the cluster metrics still catch the error:

    def pairwise_visible_gold_pairs(
        linker: "splink.Linker",
        gold_pairs: Iterable[tuple[str, str]],
    ) -> set[frozenset[str]]:
        '''Return the subset of `gold_pairs` that the pairwise PR analysis
        (`linker.evaluation.accuracy_analysis_from_labels_table`, which can only score pairs
        Splink's blocking generated) is able to see. A gold pair outside every blocking rule is
        NOT in the returned set — documenting the caveat. Each pair is returned as an unordered
        frozenset({left_id, right_id}). Order of (left, right) in the input is not significant.'''

The builder MAY add the cost-sensitive recommended-threshold report path (gate spec §6.4) and the
EM candidate model — they are exercised by other tests, not this PRIMARY invariant suite.
"""

from __future__ import annotations

import random
from collections.abc import Iterable

import pandas as pd
import pytest
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

# NOTE: this import is the failing-first trigger. On the current tree
# `worldmonitor.resolution.eval` does not exist, so collection of this module fails here with
# ModuleNotFoundError — the correct red state for a normal BUILD gate. Once the builder lands
# `src/worldmonitor/resolution/eval.py` implementing the API in the module docstring, the import
# resolves and the assertions below pin the behaviour.
from worldmonitor.resolution import eval as harness

# Tolerance for the float metric comparisons (the fixtures are hand-computed exact fractions).
_TOL = 1e-9


# --------------------------------------------------------------------------------------------
# Case 1 — PLANTED OVER-MERGE ⇒ the metric fires (THE PRIMARY INVARIANT).
# --------------------------------------------------------------------------------------------
def test_planted_over_merge_fires_bcubed_precision_and_over_merge_rate() -> None:
    """A predicted clustering that fuses two gold-DISTINCT entities into one cluster MUST be
    caught: B³ precision < 1.0 AND over_merge_rate > 0.

    Fixture (hand-computed): gold has two distinct entities, two records each —
        gold      = {r1:A, r2:A, r3:B, r4:B}            (entity A = {r1,r2}, entity B = {r3,r4})
    The resolver catastrophically fuses ALL FOUR records into one canonical node —
        predicted = {r1:X, r2:X, r3:X, r4:X}            (one cluster mixing gold A and gold B)

    B³ precision: every record's predicted cluster has 4 members but only 2 share its gold
    cluster, so Precision(r) = 2/4 = 0.5 for all four records ⇒ B³ precision = 0.5.
    B³ recall: each record's gold cluster (size 2) is fully contained in the fused cluster ⇒ 1.0.
    over_merge_rate: the single predicted cluster fuses 2 gold entities ⇒ 1/1 = 1.0.
    """
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "X", "r2": "X", "r3": "X", "r4": "X"}

    precision, recall, f1 = harness.bcubed(gold, predicted)
    # PRIMARY assertion: over-merge depresses B³ precision below 1.0.
    assert precision < 1.0
    assert precision == pytest.approx(0.5, abs=_TOL)
    assert recall == pytest.approx(1.0, abs=_TOL)
    assert f1 == pytest.approx(2 / 3, abs=_TOL)

    # PRIMARY assertion: the direct catastrophic-merge counter fires.
    omr = harness.over_merge_rate(gold, predicted)
    assert omr > 0.0
    assert omr == pytest.approx(1.0, abs=_TOL)


def test_planted_partial_over_merge_exact_values() -> None:
    """A PARTIAL over-merge (only one predicted cluster is bad) still fires, with exact values.

    Fixture (hand-computed):
        gold      = {r1:A, r2:A, r3:B, r4:B}
        predicted = {r1:X, r2:X, r3:X, r4:Y}   (X fuses gold A∪{r3}; Y = {r4} is clean)

    B³ precision: r1,r2 → 2/3 each (cluster X size 3, 2 share gold A); r3 → 1/3 (only itself
    shares gold B in X); r4 → 1/1. Mean = (2/3 + 2/3 + 1/3 + 1)/4 = 2/3.
    over_merge_rate: X conflates gold A and B (bad); Y is clean ⇒ 1/2 = 0.5.
    """
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "X", "r2": "X", "r3": "X", "r4": "Y"}

    precision, recall, _f1 = harness.bcubed(gold, predicted)
    assert precision < 1.0
    assert precision == pytest.approx(2 / 3, abs=_TOL)
    assert recall == pytest.approx(0.75, abs=_TOL)

    assert harness.over_merge_rate(gold, predicted) == pytest.approx(0.5, abs=_TOL)


# --------------------------------------------------------------------------------------------
# Case 2 — PERFECT-CLUSTERING PROPERTY: B³ = CEAFe = 1.0, over_merge_rate = 0.
# --------------------------------------------------------------------------------------------
def _relabel(partition: dict[str, str], rng: random.Random) -> dict[str, str]:
    """Return a clustering IDENTICAL to `partition` as an equivalence relation, but with the
    cluster LABELS randomly renamed. Same grouping, different label strings — so equality of
    metrics to 1.0 is a property of the PARTITION, not of label coincidence."""
    labels = sorted(set(partition.values()))
    shuffled = [f"k{rng.randrange(10_000_000)}" for _ in labels]
    # Guarantee distinct relabels (a collision would silently merge two gold clusters).
    while len(set(shuffled)) != len(shuffled):
        shuffled = [f"k{rng.randrange(10_000_000)}" for _ in labels]
    mapping = dict(zip(labels, shuffled, strict=True))
    return {record: mapping[label] for record, label in partition.items()}


def _random_partition(rng: random.Random, n_records: int, n_clusters: int) -> dict[str, str]:
    """A random-but-VALID gold partition: every cluster is non-empty (so it is a genuine
    partition of `n_records` records into exactly `n_clusters` entities)."""
    records = [f"rec{i}" for i in range(n_records)]
    # Seed one record per cluster so no cluster is empty, then scatter the remainder.
    assignment = list(range(n_clusters))
    assignment += [rng.randrange(n_clusters) for _ in range(n_records - n_clusters)]
    rng.shuffle(assignment)
    return {records[i]: f"g{assignment[i]}" for i in range(n_records)}


@pytest.mark.parametrize("seed", [1, 7, 13, 42, 99, 2024])
def test_perfect_clustering_is_unit_for_bcubed_and_ceafe(seed: int) -> None:
    """PROPERTY: for ANY clustering identical (as an equivalence relation) to the gold
    partition, B³ = CEAFe = (1.0, 1.0, 1.0) and over_merge_rate = 0.

    Parametrised over several seeded random-but-correct partitions so this is a genuine
    property, not a single fixture. The predicted partition relabels the clusters (different
    label strings, same grouping) to prove the metrics key off the PARTITION, not the labels.
    """
    rng = random.Random(seed)
    n_clusters = rng.randint(2, 6)
    n_records = rng.randint(n_clusters, n_clusters + 10)
    gold = _random_partition(rng, n_records, n_clusters)
    predicted = _relabel(gold, rng)

    bp, br, bf = harness.bcubed(gold, predicted)
    assert bp == pytest.approx(1.0, abs=_TOL)
    assert br == pytest.approx(1.0, abs=_TOL)
    assert bf == pytest.approx(1.0, abs=_TOL)

    cp, cr, cf = harness.ceafe(gold, predicted)
    assert cp == pytest.approx(1.0, abs=_TOL)
    assert cr == pytest.approx(1.0, abs=_TOL)
    assert cf == pytest.approx(1.0, abs=_TOL)

    assert harness.over_merge_rate(gold, predicted) == pytest.approx(0.0, abs=_TOL)


# --------------------------------------------------------------------------------------------
# Case 3 — BLOCKING-CONDITIONAL (THE ADVERSARIAL TARGET).
# --------------------------------------------------------------------------------------------
# A gold pair that falls OUTSIDE every blocking rule is invisible to the pairwise PR analysis
# (`accuracy_analysis_from_labels_table` can only score pairs Splink's blocking generated). The
# harness must NOT be blind to such an unblocked over-merge: the gold-set-driven cluster metrics
# + over_merge_rate iterate the FULL gold partition, so they still catch it.
#
# The linker below mirrors the live `splink_model.score_pairs` blocking surface (block on
# `country`, the 4-char name prefix, and `wikidata_id` — splink_model.py:426-430) so the
# unblocked construction is faithful to the real resolver. It is an in-process DuckDB linker
# (Splink embedded) — NO Docker.


def _faithful_linker(frame: pd.DataFrame) -> Linker:
    """A dedupe_only Splink linker whose blocking rules mirror the live resolver
    (`splink_model.score_pairs`): country, the 4-char name-fingerprint prefix, and wikidata_id.
    A pair sharing NONE of those is generated by NO blocking rule ⇒ invisible to the pairwise
    PR analysis. Comparisons are minimal — only blocking visibility matters for this case."""
    settings = SettingsCreator(
        link_type="dedupe_only",
        comparisons=[
            {
                "output_column_name": "name_fp",
                "comparison_levels": [
                    {
                        "sql_condition": '"name_fp_l" IS NULL OR "name_fp_r" IS NULL',
                        "is_null_level": True,
                    },
                    {
                        "sql_condition": '"name_fp_l" = "name_fp_r"',
                        "m_probability": 0.9,
                        "u_probability": 0.1,
                    },
                    {"sql_condition": "ELSE", "m_probability": 0.1, "u_probability": 0.9},
                ],
            },
        ],
        # Verbatim shape of the live blocking surface (splink_model.py:426-430).
        blocking_rules_to_generate_predictions=[
            block_on("country"),
            block_on("substr(name_fp, 1, 4)"),
            block_on("wikidata_id"),
        ],
        probability_two_random_records_match=0.001,
    )
    # Splink accepts a DataFrame at runtime; its type hint only admits table names.
    return Linker(frame, settings, db_api=DuckDBAPI())  # pyright: ignore[reportArgumentType]


def _blocking_conditional_fixture() -> tuple[pd.DataFrame, dict[str, str], dict[str, str]]:
    """Build the case-3 records, gold partition, and predicted partition.

    Records r1/r2 share country+name (blocked, visible). Records r3/r4 share NOTHING —
    different country, different name-prefix, no wikidata — so they are generated by NO blocking
    rule. In GOLD they are DISTINCT entities (B and C). The resolver nonetheless FUSES r3+r4
    into one predicted cluster (a blocking-conditional over-merge — e.g. via a transitive path
    the pairwise scorer never even evaluated). The cluster metrics must catch it regardless.
    """
    frame = pd.DataFrame(
        [
            {"unique_id": "r1", "name_fp": "acme corp", "country": "us", "wikidata_id": None},
            {"unique_id": "r2", "name_fp": "acme corp", "country": "us", "wikidata_id": None},
            {"unique_id": "r3", "name_fp": "zenith holdings", "country": "gb", "wikidata_id": None},
            {
                "unique_id": "r4",
                "name_fp": "quasar industries",
                "country": "jp",
                "wikidata_id": None,
            },
        ]
    )
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "C"}  # r3, r4 are DISTINCT gold entities
    predicted = {"r1": "X", "r2": "X", "r3": "Y", "r4": "Y"}  # r3+r4 over-merged into Y
    return frame, gold, predicted


def test_pairwise_metric_is_blind_to_unblocked_gold_pair() -> None:
    """The pairwise PR analysis CANNOT see a gold pair outside every blocking rule.

    The blocked pair (r1,r2) IS visible; the unblocked distinct pair (r3,r4) is NOT — this
    documents the caveat that the pairwise metric silently omits unblocked pairs.
    """
    frame, _gold, _predicted = _blocking_conditional_fixture()
    linker = _faithful_linker(frame)
    gold_pairs: Iterable[tuple[str, str]] = [("r1", "r2"), ("r3", "r4")]

    visible = harness.pairwise_visible_gold_pairs(linker, gold_pairs)

    # The blocked pair is visible to Splink's pairwise scorer ...
    assert frozenset({"r1", "r2"}) in visible
    # ... but the UNBLOCKED pair is structurally invisible (the documented blind spot).
    assert frozenset({"r3", "r4"}) not in visible
    assert visible == {frozenset({"r1", "r2"})}


def test_cluster_metrics_catch_blocking_conditional_over_merge() -> None:
    """Although the pairwise metric is blind to the unblocked pair (asserted above), the
    gold-set-driven cluster metrics + over_merge_rate STILL catch the blocking-conditional
    over-merge — the harness is NOT blind to it.

    Fixture (hand-computed):
        gold      = {r1:A, r2:A, r3:B, r4:C}   (r3 and r4 are DISTINCT gold entities)
        predicted = {r1:X, r2:X, r3:Y, r4:Y}   (r3+r4 fused into Y — the over-merge)

    over_merge_rate: predicted clusters X={r1,r2} (clean, gold A) and Y={r3,r4} (fuses gold B
    and C — bad) ⇒ 1/2 = 0.5.
    B³ precision: r1,r2 → 1.0 (X is pure A); r3 → 1/2, r4 → 1/2 (Y mixes B and C) ⇒
    mean = (1 + 1 + 0.5 + 0.5)/4 = 0.75 < 1.0.
    """
    _frame, gold, predicted = _blocking_conditional_fixture()

    omr = harness.over_merge_rate(gold, predicted)
    assert omr > 0.0
    assert omr == pytest.approx(0.5, abs=_TOL)

    precision, _recall, _f1 = harness.bcubed(gold, predicted)
    assert precision < 1.0
    assert precision == pytest.approx(0.75, abs=_TOL)
