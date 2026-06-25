"""Unit tests pinning B³ / CEAFe / over_merge_rate on hand-computed fixtures (ADR 0043 / A3).

These pin the cluster metrics' arithmetic on small, hand-worked partitions so a future refactor
cannot silently change the math. They complement the PRIMARY oracle (``tests/test_eval_harness``)
and the gate's APPROVE criteria A3 (B³ + CEAFe unit-verified) and A4 (over_merge_rate). Pure
Python, no Splink / Docker.
"""

from __future__ import annotations

import pytest

from worldmonitor.resolution import eval as harness

_TOL = 1e-9


# --------------------------------------------------------------------------------------------
# B³ (Bagga & Baldwin 1998)
# --------------------------------------------------------------------------------------------
def test_bcubed_perfect_clustering_is_unit() -> None:
    gold = {"r1": "A", "r2": "A", "r3": "B"}
    predicted = {"r1": "x", "r2": "x", "r3": "y"}  # same grouping, relabelled
    precision, recall, f1 = harness.bcubed(gold, predicted)
    assert precision == pytest.approx(1.0, abs=_TOL)
    assert recall == pytest.approx(1.0, abs=_TOL)
    assert f1 == pytest.approx(1.0, abs=_TOL)


def test_bcubed_full_over_merge_halves_precision() -> None:
    """gold {A:{r1,r2}, B:{r3,r4}} fused into one cluster: Precision(r)=2/4 for all -> 0.5;
    Recall(r)=2/2=1.0 for all; F1 = 2*0.5*1/(1.5) = 2/3."""
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "X", "r2": "X", "r3": "X", "r4": "X"}
    precision, recall, f1 = harness.bcubed(gold, predicted)
    assert precision == pytest.approx(0.5, abs=_TOL)
    assert recall == pytest.approx(1.0, abs=_TOL)
    assert f1 == pytest.approx(2 / 3, abs=_TOL)


def test_bcubed_full_fragmentation_halves_recall() -> None:
    """gold {A:{r1,r2}, B:{r3,r4}} split into singletons: Precision(r)=1/1=1.0 for all;
    Recall(r)=1/2=0.5 for all; F1 = 2/3."""
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "a", "r2": "b", "r3": "c", "r4": "d"}
    precision, recall, f1 = harness.bcubed(gold, predicted)
    assert precision == pytest.approx(1.0, abs=_TOL)
    assert recall == pytest.approx(0.5, abs=_TOL)
    assert f1 == pytest.approx(2 / 3, abs=_TOL)


def test_bcubed_partial_over_merge_exact() -> None:
    """gold {A:{r1,r2}, B:{r3,r4}}; predicted X={r1,r2,r3}, Y={r4}.
    Precision: r1,r2 -> 2/3; r3 -> 1/3; r4 -> 1. Mean = (2/3+2/3+1/3+1)/4 = 2/3.
    Recall: r1,r2 -> 2/2; r3 -> 1/2; r4 -> 1/2. Mean = (1+1+0.5+0.5)/4 = 0.75."""
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "X", "r2": "X", "r3": "X", "r4": "Y"}
    precision, recall, _f1 = harness.bcubed(gold, predicted)
    assert precision == pytest.approx(2 / 3, abs=_TOL)
    assert recall == pytest.approx(0.75, abs=_TOL)


# --------------------------------------------------------------------------------------------
# CEAFe (Luo 2005, φ4)
# --------------------------------------------------------------------------------------------
def test_ceafe_perfect_clustering_is_unit() -> None:
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "p", "r2": "p", "r3": "q", "r4": "q"}
    precision, recall, f1 = harness.ceafe(gold, predicted)
    assert precision == pytest.approx(1.0, abs=_TOL)
    assert recall == pytest.approx(1.0, abs=_TOL)
    assert f1 == pytest.approx(1.0, abs=_TOL)


def test_ceafe_full_over_merge_exact() -> None:
    """gold {A:{r1,r2}, B:{r3,r4}} (2 clusters); predicted one cluster {r1,r2,r3,r4} (1 cluster).
    Best alignment maps ONE gold cluster to the fused predicted cluster:
      φ4(A, fused) = 2*2/(2+4) = 2/3 (B->unaligned, φ4 0).
    precision = (2/3)/|predicted=1| = 2/3; recall = (2/3)/|gold=2| = 1/3;
    F1 = 2*(2/3)*(1/3)/(2/3+1/3) = (4/9)/1 = 4/9."""
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "X", "r2": "X", "r3": "X", "r4": "X"}
    precision, recall, f1 = harness.ceafe(gold, predicted)
    assert precision == pytest.approx(2 / 3, abs=_TOL)
    assert recall == pytest.approx(1 / 3, abs=_TOL)
    assert f1 == pytest.approx(4 / 9, abs=_TOL)


def test_ceafe_partial_over_merge_exact() -> None:
    """gold {A:{r1,r2}, B:{r3,r4}}; predicted X={r1,r2,r3}, Y={r4}.
    φ4(A,X)=2*2/(2+3)=4/5=0.8; φ4(B,X)=2*1/(2+3)=2/5; φ4(B,Y)=2*1/(2+1)=2/3.
    Optimal alignment: A->X (0.8) and B->Y (2/3); total = 0.8 + 2/3 = 22/15.
    precision = (22/15)/2 = 11/15; recall = (22/15)/2 = 11/15."""
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "X", "r2": "X", "r3": "X", "r4": "Y"}
    precision, recall, f1 = harness.ceafe(gold, predicted)
    assert precision == pytest.approx(11 / 15, abs=_TOL)
    assert recall == pytest.approx(11 / 15, abs=_TOL)
    assert f1 == pytest.approx(11 / 15, abs=_TOL)


# --------------------------------------------------------------------------------------------
# over_merge_rate
# --------------------------------------------------------------------------------------------
def test_over_merge_rate_zero_on_correct_clustering() -> None:
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "x", "r2": "x", "r3": "y", "r4": "y"}
    assert harness.over_merge_rate(gold, predicted) == pytest.approx(0.0, abs=_TOL)


def test_over_merge_rate_full_fuse() -> None:
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "X", "r2": "X", "r3": "X", "r4": "X"}
    assert harness.over_merge_rate(gold, predicted) == pytest.approx(1.0, abs=_TOL)


def test_over_merge_rate_partial() -> None:
    """X fuses gold A+B (bad); Y is clean -> 1/2."""
    gold = {"r1": "A", "r2": "A", "r3": "B", "r4": "B"}
    predicted = {"r1": "X", "r2": "X", "r3": "X", "r4": "Y"}
    assert harness.over_merge_rate(gold, predicted) == pytest.approx(0.5, abs=_TOL)


def test_over_merge_rate_ignores_fragmentation() -> None:
    """Splitting one gold entity across clean clusters is NOT an over-merge (it is
    fragmentation): no predicted cluster mixes gold entities -> rate 0."""
    gold = {"r1": "A", "r2": "A", "r3": "A", "r4": "B"}
    predicted = {"r1": "x", "r2": "x", "r3": "z", "r4": "y"}  # A split into x,z; both pure A
    assert harness.over_merge_rate(gold, predicted) == pytest.approx(0.0, abs=_TOL)
