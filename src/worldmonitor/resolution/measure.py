"""ER measurement executable end-to-end (Gate WP-1, spec
``docs/reviews/GATE_WP1_MEASUREMENT_SPEC.md`` D1).

Closes re-review finding #1 (``docs/fable-review/90_REREVIEW_2026-07-11.md`` §1a: "the eval
harness is not executable end-to-end" — ``eval.py``'s ``bcubed``/``ceafe``/``over_merge_rate``/
``precision_recall_table``/``recommended_threshold`` had zero non-test call sites, nothing read
``er_gold_pair`` back into a :data:`~worldmonitor.resolution.eval.Partition`, there was no report
entrypoint) and the roadmap's G7 "Label-sufficiency report" line (``docs/40_ROADMAP.md``:
"``eval.py``: labels by source + boundary coverage + metric CIs").

This module is the **ruler an operator can actually pick up**: it turns the labelled
``er_gold_pair`` table plus the ``canonical_id_ledger`` into a scored ``(gold, predicted)``
:data:`~worldmonitor.resolution.eval.Partition` pair and a human-and-machine-readable
:class:`SufficiencyReport` — labels by source, 0.5-0.95 boundary-band coverage, and bootstrap
confidence intervals over B-cubed precision/recall/F1, CEAFe F1, and the over-merge rate.

**Measurement-only, strictly READ-ONLY (DENY D3 posture unchanged):** this module contains no
``session.commit()``/``.add()``/``.delete()`` call anywhere in its source (proven structurally by
``tests/unit/test_measure.py``'s AST walk) and never imports :mod:`worldmonitor.resolution.merge`.
:func:`~worldmonitor.resolution.eval.recommended_threshold` remains a **report value only** — this
module does not even call it in v1 (no Splink linker is wired here; PR-curve reporting over a live
linker stays a follow-up, not faked). Promotion of any calibrated value into the live ER path (G7)
stays human-sign-off-gated, unchanged by this gate.

CLI::

    python -m worldmonitor.resolution.measure [--json] [--boot N] [--seed N]

Builds the report from the process settings (``engine_from_settings`` / ``session_factory``) and
prints :meth:`SufficiencyReport.as_text` (or, with ``--json``, ``json.dumps(report.as_dict())``).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.db.models import CanonicalIdLedger, ErGoldPair
from worldmonitor.resolution.eval import Partition, bcubed, ceafe, over_merge_rate
from worldmonitor.resolution.gold import UNCERTAINTY_HIGH, UNCERTAINTY_LOW, GoldPair
from worldmonitor.settings import get_settings

logger = logging.getLogger(__name__)

# The five metrics every report + CI dict carries (spec D1 item 4: "B3 P/R/F1, CEAFe F1,
# over_merge_rate").
_METRIC_KEYS: tuple[str, ...] = (
    "bcubed_precision",
    "bcubed_recall",
    "bcubed_f1",
    "ceafe_f1",
    "over_merge_rate",
)


# ---------------------------------------------------------------------------
# D1 item 1 — load_labelled_pairs
# ---------------------------------------------------------------------------


def load_labelled_pairs(
    session: Session, *, sources: Sequence[str] | None = None
) -> list[GoldPair]:
    """Read ``er_gold_pair`` rows (optionally filtered by ``source``) as :class:`GoldPair`\\ s.

    Ordered by ``(left_id, right_id)`` for a deterministic result — this is a pure READ (no
    mutation of the table it queries).
    """
    stmt = select(ErGoldPair).order_by(ErGoldPair.left_id, ErGoldPair.right_id)
    if sources is not None:
        stmt = stmt.where(ErGoldPair.source.in_(sources))
    rows = session.execute(stmt).scalars()
    return [
        GoldPair(
            left_id=row.left_id,
            right_id=row.right_id,
            label=row.label,
            source=row.source,
            clerical_score=row.clerical_score,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# D1 item 2 — gold_partition (union-find over match pairs)
# ---------------------------------------------------------------------------


def gold_partition(pairs: Sequence[GoldPair]) -> dict[str, str]:
    """Union-find over ``match`` pairs — every id mentioned by any pair appears, singleton unless
    united (spec D1 item 2).

    Both ids of EVERY pair (``match`` and ``non_match`` alike) are registered as members;
    ``non_match`` edges add members but never union them. The label of a component is the
    lexicographically smallest member id — deterministic and independent of pair order.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            # The winning root doesn't matter — components are relabelled by min-member below —
            # but a deterministic tie-break keeps this function reproducible in isolation too.
            if root_a < root_b:
                parent[root_b] = root_a
            else:
                parent[root_a] = root_b

    for pair in pairs:
        if pair.left_id not in parent:
            parent[pair.left_id] = pair.left_id
        if pair.right_id not in parent:
            parent[pair.right_id] = pair.right_id
        if pair.label == "match":
            union(pair.left_id, pair.right_id)

    members_by_root: dict[str, list[str]] = {}
    for member in parent:
        root = find(member)
        members_by_root.setdefault(root, []).append(member)

    result: dict[str, str] = {}
    for members in members_by_root.values():
        label = min(members)
        for member in members:
            result[member] = label
    return result


# ---------------------------------------------------------------------------
# D1 item 3 — predicted_partition (ledger alias -> canonical; missing -> itself)
# ---------------------------------------------------------------------------


def predicted_partition(session: Session, record_ids: Iterable[str]) -> dict[str, str]:
    """Map each id through ``canonical_id_ledger`` (alias -> surviving canonical id); an id with
    no ledger row maps to itself (spec D1 item 3).

    Reads the table directly (rather than one row-at-a-time via
    :func:`~worldmonitor.resolution.canonical.resolve_durable`) so a single query covers every
    requested id and duplicate ``(alias)`` rows resolve to the LATEST (``created_at``-ordered)
    canonical id, per the spec's "latest row per alias" tie-break.
    """
    ids = list(record_ids)
    if not ids:
        return {}

    stmt = (
        select(CanonicalIdLedger.canonical_alias, CanonicalIdLedger.canonical_id)
        .where(CanonicalIdLedger.canonical_alias.in_(ids))
        .order_by(CanonicalIdLedger.canonical_alias, CanonicalIdLedger.created_at.asc())
    )
    latest: dict[str, str] = {}
    for alias, canonical_id in session.execute(stmt):
        latest[alias] = canonical_id  # ascending order -> the last write per alias wins

    return {record_id: latest.get(record_id, record_id) for record_id in ids}


# ---------------------------------------------------------------------------
# D1 item 4 — metric_confidence_intervals (bootstrap over gold clusters)
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence (numpy's default method)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = (pct / 100.0) * (len(sorted_values) - 1)
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return sorted_values[int(index)]
    fraction = index - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def _point_metrics(gold: Partition, predicted: Partition) -> dict[str, float]:
    """The (non-bootstrapped) metrics computed over the FULL gold/predicted partitions."""
    precision, recall, f1 = bcubed(gold, predicted)
    _, _, ceafe_f1 = ceafe(gold, predicted)
    return {
        "bcubed_precision": precision,
        "bcubed_recall": recall,
        "bcubed_f1": f1,
        "ceafe_f1": ceafe_f1,
        "over_merge_rate": over_merge_rate(gold, predicted),
    }


def metric_confidence_intervals(
    gold: Partition,
    predicted: Partition,
    *,
    n_boot: int = 200,
    seed: int = 0,
) -> dict[str, tuple[float, float, float]]:
    """Bootstrap confidence intervals over GOLD clusters (spec D1 item 4).

    Resamples gold-cluster labels with replacement (``len(clusters)`` draws per resample,
    deduplicated into a SET so a repeated draw simply keeps that cluster IN the resample rather
    than double-weighting it — "restrict both partitions to the sampled records"); recomputes
    B-cubed P/R/F1, CEAFe F1, and the over-merge rate on each resample's records; and reports the
    2.5th/97.5th percentiles alongside the point estimate (the metric on the FULL, unrestricted
    partitions). Seeded with stdlib ``random.Random(seed)`` — deterministic for a fixed
    ``(gold, predicted, seed)``, never wall-clock/OS-entropy nondeterminism.
    """
    point = _point_metrics(gold, predicted)

    records_by_label: dict[str, list[str]] = {}
    for record, label in gold.items():
        records_by_label.setdefault(label, []).append(record)
    cluster_labels = sorted(records_by_label)

    samples: dict[str, list[float]] = {key: [] for key in _METRIC_KEYS}
    if cluster_labels:
        rng = random.Random(seed)
        for _ in range(n_boot):
            drawn = {rng.choice(cluster_labels) for _ in range(len(cluster_labels))}
            sample_records = [record for label in drawn for record in records_by_label[label]]
            gold_sample = {record: gold[record] for record in sample_records}
            predicted_sample = {record: predicted.get(record, record) for record in sample_records}
            resample_metrics = _point_metrics(gold_sample, predicted_sample)
            for key in _METRIC_KEYS:
                samples[key].append(resample_metrics[key])

    result: dict[str, tuple[float, float, float]] = {}
    for key in _METRIC_KEYS:
        values = sorted(samples[key])
        if values:
            lo = _percentile(values, 2.5)
            hi = _percentile(values, 97.5)
        else:
            lo = hi = point[key]
        result[key] = (point[key], lo, hi)
    return result


# ---------------------------------------------------------------------------
# D1 item 5 — SufficiencyReport + build_sufficiency_report
# ---------------------------------------------------------------------------

_NO_LEDGER_MAPPING_REASON = "no resolved corpus in the ledger yet"
_NO_PAIRS_REASON = "no labelled pairs loaded — nothing to measure"


def _labels_by_source(pairs: Sequence[GoldPair]) -> dict[str, dict[str, int]]:
    """``{source: {label: count}}`` over the loaded pairs — zero-count labels are OMITTED."""
    counts: dict[str, dict[str, int]] = {}
    for pair in pairs:
        by_label = counts.setdefault(pair.source, {})
        by_label[pair.label] = by_label.get(pair.label, 0) + 1
    return counts


@dataclass(frozen=True, slots=True)
class SufficiencyReport:
    """The G7 label-sufficiency report (spec D1 item 5): what's labelled, how much of it sits in
    the 0.5-0.95 decision band, and — when the ledger has resolved anything — how the current
    resolution scores against the loaded gold set.

    ``metrics`` is ``None`` (with a non-empty ``reason``) whenever the ``canonical_id_ledger``
    has not yet united or renamed any of the record ids the loaded pairs reference — an empty or
    unrelated ledger never crashes the report, it just says so (spec: "never crash on an empty
    deploy").
    """

    labels_by_source: dict[str, dict[str, int]]
    distinct_records: int
    total_pairs: int
    scored_pairs: int
    scoreless_pairs: int
    boundary_band_pairs: int
    boundary_band_fraction: float
    metrics: dict[str, tuple[float, float, float]] | None
    reason: str | None
    n_boot: int = 200
    seed: int = 0
    boundary_low: float = field(default=UNCERTAINTY_LOW)
    boundary_high: float = field(default=UNCERTAINTY_HIGH)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-round-trippable rendering of the report."""
        return {
            "labels_by_source": self.labels_by_source,
            "distinct_records": self.distinct_records,
            "total_pairs": self.total_pairs,
            "scored_pairs": self.scored_pairs,
            "scoreless_pairs": self.scoreless_pairs,
            "boundary_band_pairs": self.boundary_band_pairs,
            "boundary_band_fraction": self.boundary_band_fraction,
            "boundary_band": [self.boundary_low, self.boundary_high],
            "metrics": (
                {key: list(value) for key, value in self.metrics.items()}
                if self.metrics is not None
                else None
            ),
            "reason": self.reason,
            "n_boot": self.n_boot,
            "seed": self.seed,
        }

    def as_text(self) -> str:
        """A human-readable report — the operator-facing rendering the CLI prints by default."""
        lines: list[str] = ["ER label-sufficiency report", "=" * 28]
        lines.append(f"distinct records: {self.distinct_records}")
        lines.append(f"total labelled pairs: {self.total_pairs}")
        lines.append("labels by source:")
        if self.labels_by_source:
            for source in sorted(self.labels_by_source):
                by_label = self.labels_by_source[source]
                rendered = ", ".join(
                    f"{label}={count}" for label, count in sorted(by_label.items())
                )
                lines.append(f"  {source}: {rendered}")
        else:
            lines.append("  (none)")
        lines.append(
            f"boundary band [{self.boundary_low}, {self.boundary_high}): "
            f"{self.boundary_band_pairs}/{self.scored_pairs} scored pairs "
            f"({self.boundary_band_fraction:.1%}); {self.scoreless_pairs} scoreless pair(s)"
        )
        if self.metrics is None:
            lines.append(f"metrics: unavailable — {self.reason}")
        else:
            lines.append(f"metrics (point, 95% CI) [n_boot={self.n_boot} seed={self.seed}]:")
            for name in sorted(self.metrics):
                point, lo, hi = self.metrics[name]
                lines.append(f"  {name}: {point:.4f}  [{lo:.4f}, {hi:.4f}]")
        return "\n".join(lines)


def build_sufficiency_report(
    session: Session, *, n_boot: int = 200, seed: int = 0
) -> SufficiencyReport:
    """Build the G7 label-sufficiency report over every ``er_gold_pair`` row (spec D1 item 5).

    Strictly READ-ONLY: loads the labelled pairs, resolves the record ids they reference through
    the ledger, and — ONLY when that resolution is non-trivial (at least one referenced id maps
    to something other than itself) — scores the current resolution against the gold partition
    via :func:`metric_confidence_intervals`. An empty or ledger-unrelated-to-this-corpus deploy
    reports ``metrics=None`` with an explanatory ``reason`` instead of a degenerate/misleading
    all-singletons score.
    """
    pairs = load_labelled_pairs(session)
    labels_by_source = _labels_by_source(pairs)

    distinct_ids = sorted({pair.left_id for pair in pairs} | {pair.right_id for pair in pairs})
    total_pairs = len(pairs)
    scored_pairs = sum(1 for pair in pairs if pair.clerical_score is not None)
    scoreless_pairs = total_pairs - scored_pairs
    boundary_band_pairs = sum(
        1
        for pair in pairs
        if pair.clerical_score is not None
        and UNCERTAINTY_LOW <= pair.clerical_score < UNCERTAINTY_HIGH
    )
    boundary_band_fraction = boundary_band_pairs / scored_pairs if scored_pairs else 0.0

    metrics: dict[str, tuple[float, float, float]] | None = None
    reason: str | None = None
    if not distinct_ids:
        reason = _NO_PAIRS_REASON
    else:
        predicted = predicted_partition(session, distinct_ids)
        non_trivial = any(predicted[record_id] != record_id for record_id in distinct_ids)
        if non_trivial:
            gold = gold_partition(pairs)
            metrics = metric_confidence_intervals(gold, predicted, n_boot=n_boot, seed=seed)
        else:
            reason = _NO_LEDGER_MAPPING_REASON

    return SufficiencyReport(
        labels_by_source=labels_by_source,
        distinct_records=len(distinct_ids),
        total_pairs=total_pairs,
        scored_pairs=scored_pairs,
        scoreless_pairs=scoreless_pairs,
        boundary_band_pairs=boundary_band_pairs,
        boundary_band_fraction=boundary_band_fraction,
        metrics=metrics,
        reason=reason,
        n_boot=n_boot,
        seed=seed,
    )


# ---------------------------------------------------------------------------
# D1 item 6 — CLI
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worldmonitor.resolution.measure",
        description=(
            "Print the G7 label-sufficiency report (labels by source, 0.5-0.95 boundary "
            "coverage, B3/CEAFe/over-merge bootstrap CIs). Read-only — never writes."
        ),
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--boot",
        type=int,
        default=200,
        dest="n_boot",
        help="bootstrap resample count (default 200)",
    )
    parser.add_argument("--seed", type=int, default=0, help="bootstrap RNG seed (default 0)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = engine_from_settings(settings)
    sessions = session_factory(engine)
    try:
        with sessions() as session:
            report = build_sufficiency_report(session, n_boot=args.n_boot, seed=args.seed)
        if args.json:
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            print(report.as_text())
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
