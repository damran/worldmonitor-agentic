"""ER measurement harness — the ruler, not the cut (ADR 0043 / Gate A, slice-1).

This module is the regression instrument every later ER gate measures against. It is
**person-neutral**: it READS clusterings and a labelled gold set and computes quality
metrics; it never merges, splits, deletes, or writes any live value. In particular it
**never** writes ``DEFAULT_MERGE_THRESHOLD`` / ``merge.py`` / any config — the
cost-sensitive recommended threshold (:func:`recommended_threshold`) is a **report value
only** (gate spec §6.4 / DENY D3). Promotion of any derived value is the separate,
human-gated slice-2.

The three cluster metrics are **pure Python over the full gold partition** — NOT limited to
Splink's blocked candidate set. That is what lets them catch a *blocking-conditional*
over-merge: a fused pair that blocking never generated is invisible to the pairwise PR
analysis (:func:`pairwise_visible_gold_pairs` documents that blind spot), but the cluster
metrics iterate the gold partition directly, so they still fire (gate spec §5.3 / §6).

A clustering is a partition mapping ``record_id -> cluster_label``::

    Partition = Mapping[str, str]

Two records are in the same entity iff they carry the same cluster label.

Splink bindings are recorded verbatim in ``VERIFIED_API.md`` (the verify-before-code gate):

* ``linker.evaluation.accuracy_analysis_from_labels_table(labels, *, output_type=...)``
* ``linker.table_management.register_labels_table(df)``
* ``linker.inference.predict(...)`` (blocking-generated candidate pairs)
* ``scipy.optimize.linear_sum_assignment`` (CEAFe optimal alignment).

Namespacing is load-bearing — these hang off ``linker.training.*`` / ``linker.evaluation.*``
/ ``linker.table_management.*`` / ``linker.inference.*``, never the bare ``Linker``.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd
from scipy.optimize import linear_sum_assignment

if TYPE_CHECKING:  # pragma: no cover - typing only
    from splink import Linker

# A clustering: record_id -> cluster_label. Two records co-refer iff same label.
Partition = Mapping[str, str]


# --------------------------------------------------------------------------------------------
# Partition helpers
# --------------------------------------------------------------------------------------------
def _clusters(partition: Partition) -> dict[str, set[str]]:
    """Invert a ``record -> label`` partition into ``label -> {records}``."""
    out: dict[str, set[str]] = defaultdict(set)
    for record, label in partition.items():
        out[label].add(record)
    return dict(out)


def _harmonic_mean(precision: float, recall: float) -> float:
    """F1 = harmonic mean of precision and recall (0.0 when either is 0)."""
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


# --------------------------------------------------------------------------------------------
# Cluster metrics (pure Python over the FULL gold partition — gate spec §6)
# --------------------------------------------------------------------------------------------
def bcubed(gold: Partition, predicted: Partition) -> tuple[float, float, float]:
    """B-cubed (Bagga & Baldwin 1998). Returns ``(precision, recall, f1)``.

    For record ``r`` with predicted cluster ``P(r)`` and gold cluster ``G(r)``:

    * ``Precision(r) = |P(r) ∩ G(r)| / |P(r)|``
    * ``Recall(r)    = |P(r) ∩ G(r)| / |G(r)|``

    B³ precision/recall are the means over all records; F1 is their harmonic mean. The
    metric is computed over records the gold partition labels (``predicted`` must cover the
    same records). ``B³ precision < 1.0`` iff at least one predicted cluster mixes records
    from ≥2 gold clusters — the headline over-merge signal.
    """
    pred_clusters = _clusters(predicted)
    gold_clusters = _clusters(gold)
    records = list(gold)
    if not records:
        return 0.0, 0.0, 0.0

    precision_sum = 0.0
    recall_sum = 0.0
    for record in records:
        p_cluster = pred_clusters[predicted[record]]
        g_cluster = gold_clusters[gold[record]]
        overlap = len(p_cluster & g_cluster)
        precision_sum += overlap / len(p_cluster)
        recall_sum += overlap / len(g_cluster)

    precision = precision_sum / len(records)
    recall = recall_sum / len(records)
    return precision, recall, _harmonic_mean(precision, recall)


def _phi4(g: set[str], p: set[str]) -> float:
    """φ4 cluster similarity (Luo 2005): ``2·|g ∩ p| / (|g| + |p|)``."""
    denominator = len(g) + len(p)
    if denominator == 0:
        return 0.0
    return 2.0 * len(g & p) / denominator


def ceafe(gold: Partition, predicted: Partition) -> tuple[float, float, float]:
    """CEAFe (Luo 2005, entity-based CEAF, φ4 similarity). Returns ``(precision, recall, f1)``.

    Finds the optimal one-to-one alignment between gold and predicted clusters that maximises
    total φ4 similarity, solved with the Hungarian algorithm
    (:func:`scipy.optimize.linear_sum_assignment` on the negated similarity matrix, per
    ``VERIFIED_API.md``). Then:

    * ``precision = Σ φ4(aligned) / |predicted clusters|``  (Σ_p φ4(p, p) = |predicted|)
    * ``recall    = Σ φ4(aligned) / |gold clusters|``       (Σ_g φ4(g, g) = |gold|)
    * ``F1 = harmonic mean(precision, recall)``.

    φ4(c, c) = 1 for any non-empty cluster, so the normalisers are the cluster *counts*.
    CEAFe penalises both fragmentation and conflation at the entity level and is not
    dominated by large clusters the way pairwise metrics are.
    """
    gold_clusters = list(_clusters(gold).values())
    pred_clusters = list(_clusters(predicted).values())
    if not gold_clusters or not pred_clusters:
        return 0.0, 0.0, 0.0

    # Similarity matrix φ4(gold_i, pred_j); maximise total similarity over a 1:1 alignment.
    similarity = [[_phi4(g, p) for p in pred_clusters] for g in gold_clusters]
    cost = [[-value for value in row] for row in similarity]
    row_ind, col_ind = linear_sum_assignment(cost)
    aligned_similarity = sum(similarity[i][j] for i, j in zip(row_ind, col_ind, strict=True))

    # Σ_p φ4(p, p) = |predicted clusters|; Σ_g φ4(g, g) = |gold clusters| (non-empty clusters).
    precision = aligned_similarity / len(pred_clusters)
    recall = aligned_similarity / len(gold_clusters)
    return precision, recall, _harmonic_mean(precision, recall)


def over_merge_rate(gold: Partition, predicted: Partition) -> float:
    """Fraction of PREDICTED clusters that conflate ≥2 distinct gold entities.

    ``over_merge_rate = |{p ∈ predicted : p contains records from ≥2 gold clusters}| /
    |predicted|``. A direct, interpretable catastrophic-merge counter. ``0.0`` on a correct
    clustering; ``> 0`` whenever any predicted cluster fuses records that gold places in
    distinct entities. Computed over the gold partition, so it fires even when the offending
    pair was never blocked (gate spec §5.3).
    """
    pred_clusters = _clusters(predicted)
    if not pred_clusters:
        return 0.0
    over_merged = 0
    for members in pred_clusters.values():
        gold_labels = {gold[record] for record in members if record in gold}
        if len(gold_labels) >= 2:
            over_merged += 1
    return over_merged / len(pred_clusters)


# --------------------------------------------------------------------------------------------
# Pairwise / blocking-conditional helper (Splink — gate spec §5.3 / VERIFIED_API.md)
# --------------------------------------------------------------------------------------------
def pairwise_visible_gold_pairs(
    linker: Linker,
    gold_pairs: Iterable[tuple[str, str]],
) -> set[frozenset[str]]:
    """Return the subset of ``gold_pairs`` the pairwise PR analysis can actually SEE.

    Splink's labelled accuracy analysis
    (:meth:`linker.evaluation.accuracy_analysis_from_labels_table`) can only score pairs
    that Splink's **blocking** generated — a gold pair outside every blocking rule is
    structurally invisible to it (``VERIFIED_API.md``: the PAIRWISE + blocking-conditional
    caveat). This helper materialises exactly that blocked candidate set via
    ``linker.inference.predict(threshold_match_probability=0.0)`` (the set of pairs blocking
    produced, before any score threshold) and returns the gold pairs that appear in it.

    A gold pair NOT generated by any blocking rule is therefore absent from the result —
    documenting the caveat so a test can assert the pairwise blind spot while the gold-set
    cluster metrics still catch the unblocked over-merge. Each pair is an unordered
    ``frozenset({left_id, right_id})``; input ``(left, right)`` order is not significant.
    """
    wanted = {frozenset(pair) for pair in gold_pairs}
    if not wanted:
        return set()

    # The blocked candidate set: every pair Splink's blocking generated (threshold 0.0 keeps
    # them all, before any match-probability cut). This is precisely the universe the pairwise
    # PR analysis can score — a pair outside every blocking rule never appears here.
    candidates = linker.inference.predict(threshold_match_probability=0.0)
    frame = candidates.as_pandas_dataframe()
    visible: set[frozenset[str]] = set()
    for left, right in zip(frame["unique_id_l"], frame["unique_id_r"], strict=True):
        pair = frozenset({str(left), str(right)})
        if pair in wanted:
            visible.add(pair)
    return visible


# --------------------------------------------------------------------------------------------
# Cost-sensitive recommended threshold (REPORT VALUE ONLY — gate spec §6.4 / DENY D3)
# --------------------------------------------------------------------------------------------
# An over-merge of two real persons is treated as an order of magnitude worse than a missed
# duplicate (the §1 asymmetry). The ratio is recorded in ADR 0043: c_fp : c_fn = 10 : 1.
COST_FALSE_MERGE: float = 10.0  # c_fp — penalty for a false MERGE (an over-merge / false positive)
COST_FALSE_SPLIT: float = 1.0  # c_fn — penalty for a false SPLIT (a missed merge / false negative)


@dataclass(frozen=True, slots=True)
class ThresholdReport:
    """The cost-sensitive recommended-threshold REPORT (gate spec §6.4).

    A measured, returned-only artefact: ``threshold`` is derived from the PR curve and
    written NOWHERE live. ``cost`` is the minimised ``c_fp·FP + c_fn·FN`` at that threshold;
    ``false_positives`` / ``false_negatives`` are the confusion counts there;
    ``c_false_merge`` / ``c_false_split`` echo the cost ratio (ADR 0043, 10:1).
    """

    threshold: float
    cost: float
    false_positives: int
    false_negatives: int
    c_false_merge: float = COST_FALSE_MERGE
    c_false_split: float = COST_FALSE_SPLIT


def precision_recall_table(linker: Linker, labels: pd.DataFrame) -> pd.DataFrame:
    """Return the measured PR curve as a pandas table over candidate thresholds.

    Registers ``labels`` (Splink labels-table schema:
    ``unique_id_l | unique_id_r | clerical_match_score``; ``source_dataset_*`` omitted for the
    ``dedupe_only`` model — ``VERIFIED_API.md``) and reads
    ``accuracy_analysis_from_labels_table(..., output_type="table")``, whose
    ``SplinkDataFrame`` carries one row per match-probability threshold with ``tp/tn/fp/fn``
    and ``precision``/``recall``. The ``"table"`` output is the dataframe-bearing form of the
    ``"precision_recall"`` PR analysis the gate spec names.
    """
    labels_table = linker.table_management.register_labels_table(labels)
    # output_type="table" returns a SplinkDataFrame (the dataframe-bearing PR analysis), not a
    # chart — but the declared return type is the Union[ChartReturnType, SplinkDataFrame], so
    # narrow to the dataframe branch (Splink ships no usable stubs for the chart/df split).
    analysis = linker.evaluation.accuracy_analysis_from_labels_table(
        labels_table, output_type="table"
    )
    return analysis.as_pandas_dataframe()  # pyright: ignore[reportAttributeAccessIssue]


def recommended_threshold(
    pr_table: pd.DataFrame,
    *,
    c_false_merge: float = COST_FALSE_MERGE,
    c_false_split: float = COST_FALSE_SPLIT,
) -> ThresholdReport:
    """Derive the cost-sensitive recommended threshold from the measured PR curve.

    Chooses the ``match_probability`` threshold minimising ``cost = c_fp·FP + c_fn·FN`` with
    ``c_fp ≫ c_fn`` (ADR 0043: 10:1) — i.e. it weights a false MERGE (over-merge) an order of
    magnitude above a false SPLIT (missed merge), reflecting the §1 asymmetry. The value is
    **COMPUTED** from ``pr_table``'s confusion counts, never stubbed, and is **returned only**
    (DENY D3): callers report it; nothing here writes ``merge.py`` or any live config. The
    harness can therefore derive a non-``0.92`` threshold straight from the measured curve.
    """
    if pr_table.empty:
        raise ValueError("PR table is empty — no measured curve to derive a threshold from")

    best: ThresholdReport | None = None
    for row in pr_table.to_dict("records"):
        fp = int(row["fp"])
        fn = int(row["fn"])
        threshold = float(row["match_probability"])
        cost = c_false_merge * fp + c_false_split * fn
        if best is None or cost < best.cost or (cost == best.cost and threshold > best.threshold):
            # Tie-break toward the HIGHER (more conservative) threshold: at equal cost, demand
            # more evidence before merging — the over-merge-averse choice.
            best = ThresholdReport(
                threshold=threshold,
                cost=cost,
                false_positives=fp,
                false_negatives=fn,
                c_false_merge=c_false_merge,
                c_false_split=c_false_split,
            )
    assert best is not None  # non-empty table guaranteed above
    return best
