"""Unit tests for Gate WP-1 — ``resolution/measure.py`` (spec
``docs/reviews/GATE_WP1_MEASUREMENT_SPEC.md`` D1). Docker-free: SQLite in-memory (the
``@compiles(JSONB, "sqlite")`` shim + ``make_engine("sqlite:///:memory:")`` idiom, mirrors
``tests/unit/test_backfill.py``).

D1 pins ``resolution/measure.py`` as a strictly READ-ONLY module that turns ``er_gold_pair`` +
``canonical_id_ledger`` into a scored ``Partition`` pair (gold vs predicted) and a
``SufficiencyReport``. This file is the CONTRACT test the builder's ``measure.py`` must satisfy —
where the spec leaves a shape underspecified (metric key names, ``SufficiencyReport`` field
names), this file pins the concrete choice; see the test-author's report for the rationale.

Contract pinned here (D1 items 1-6):

* ``load_labelled_pairs(session, *, sources=None) -> list[GoldPair]`` — optional source filter.
* ``gold_partition(pairs) -> dict[str, str]`` — union-find over ``match`` edges only; EVERY id
  mentioned by any pair (including ``non_match``-only ids) appears, singleton unless united;
  deterministic label = the lexicographically smallest member id of the component;
  order-independent.
* ``predicted_partition(session, record_ids) -> dict[str, str]`` — ledger alias -> canonical;
  missing id -> itself.
* ``metric_confidence_intervals(gold, predicted, *, n_boot=200, seed=0) ->
  dict[str, tuple[float, float, float]]`` keyed by ``{"bcubed_precision", "bcubed_recall",
  "bcubed_f1", "ceafe_f1", "over_merge_rate"}`` (spec: "B3 P/R/F1, CEAFe F1, over_merge_rate"),
  each value ``(point, lo, hi)`` at the 2.5/97.5 percentiles; seeded + deterministic.
* ``SufficiencyReport`` (frozen dataclass) + ``build_sufficiency_report(session, *, n_boot=200,
  seed=0)``: ``labels_by_source: dict[str, dict[str, int]]``, ``distinct_records: int``,
  ``total_pairs: int``, ``scored_pairs: int`` (``clerical_score is not None``),
  ``scoreless_pairs: int``, ``boundary_band_pairs: int`` (score in ``[0.5, 0.95)``),
  ``boundary_band_fraction: float``, ``metrics: dict[str, tuple[float,float,float]] | None``
  (``None`` unless the ledger yields at least one NON-TRIVIAL mapping restricted to the record
  ids the loaded pairs actually reference), ``reason: str | None`` (non-empty iff
  ``metrics is None``), ``as_dict()`` (JSON-round-trippable), ``as_text()`` (non-empty str).
* The module is READ-ONLY: no ``session.commit``/``.add``/``.delete`` call anywhere in its
  source, and it never imports ``resolution.merge``.

RED today: ``ModuleNotFoundError`` — ``worldmonitor.resolution.measure`` does not exist yet
(WP-1 D1).
"""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import Base, ErGoldPair
from worldmonitor.resolution.canonical import record_alias, record_canonical
from worldmonitor.resolution.gold import GoldPair

# ---- GATE IMPORT — does not exist yet (RED for the right reason, WP-1 D1) ----
from worldmonitor.resolution.measure import (
    build_sufficiency_report,
    gold_partition,
    load_labelled_pairs,
    metric_confidence_intervals,
    predicted_partition,
)

_METRIC_KEYS = {
    "bcubed_precision",
    "bcubed_recall",
    "bcubed_f1",
    "ceafe_f1",
    "over_merge_rate",
}


# ---------------------------------------------------------------------------
# SQLite JSONB shim (idempotent if already registered by another test module) — this file must
# be self-contained (mirrors test_backfill.py's unit-test file exactly).
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _sqlite_sessions() -> tuple[Any, sessionmaker[Session]]:
    engine = make_engine("sqlite:///:memory:")
    create_all(engine)
    return engine, session_factory(engine)


def _add_pair(
    session: Session,
    *,
    left: str,
    right: str,
    label: str,
    source: str,
    score: float | None = None,
) -> None:
    session.add(
        ErGoldPair(
            id=str(uuid.uuid4()),
            left_id=left,
            right_id=right,
            label=label,
            source=source,
            clerical_score=score,
        )
    )


# A seeded, deliberately mixed corpus spanning all three sources, a full boundary-band spread,
# and both labels — used by the report-shape + row-count-invariance tests below.
_CORPUS_PAIRS: tuple[tuple[str, str, str, str, float | None], ...] = (
    ("a1", "a2", "match", "os_pairs", None),
    ("a3", "a4", "non_match", "os_pairs", None),
    ("b1", "b2", "match", "uncertainty", 0.30),  # below the [0.5, 0.95) band
    ("b3", "b4", "match", "uncertainty", 0.60),  # inside the band
    ("b5", "b6", "non_match", "uncertainty", 0.97),  # at/above the band ceiling
    ("c1", "c2", "match", "canonical_silver", None),
    ("c3", "c4", "match", "canonical_silver", None),
)


def _seed_corpus(session: Session) -> None:
    for left, right, label, source, score in _CORPUS_PAIRS:
        _add_pair(session, left=left, right=right, label=label, source=source, score=score)
    session.commit()


# ===========================================================================
# load_labelled_pairs — reads er_gold_pair, optional source filter
# ===========================================================================


def test_load_labelled_pairs_reads_all_rows_as_gold_pairs() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        pairs = load_labelled_pairs(session)

    assert len(pairs) == 7, f"expected all 7 seeded rows, got {len(pairs)}"
    assert {p.source for p in pairs} == {"os_pairs", "uncertainty", "canonical_silver"}
    assert all(isinstance(p, GoldPair) for p in pairs)
    engine.dispose()


def test_load_labelled_pairs_filters_by_source() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        os_only = load_labelled_pairs(session, sources=["os_pairs"])
        silver_and_uncertainty = load_labelled_pairs(
            session, sources=["canonical_silver", "uncertainty"]
        )

    assert {(p.left_id, p.right_id, p.label) for p in os_only} == {
        ("a1", "a2", "match"),
        ("a3", "a4", "non_match"),
    }, f"unexpected os_pairs-filtered rows: {os_only}"
    assert len(silver_and_uncertainty) == 5, (
        f"expected 3 uncertainty + 2 canonical_silver rows, got {len(silver_and_uncertainty)}"
    )
    engine.dispose()


# ===========================================================================
# gold_partition — union-find over match pairs (spec D1 item 2)
# ===========================================================================


def test_gold_partition_transitive_chain_unites_all_members() -> None:
    """A transitive chain of match pairs (b~c, a~b) must unite ALL THREE ids under one
    deterministic label — the lexicographically smallest member id ("a") — even though a and c
    never co-occur in a single pair."""
    pairs = [
        GoldPair(left_id="b", right_id="c", label="match", source="test"),
        GoldPair(left_id="a", right_id="b", label="match", source="test"),
    ]
    partition = gold_partition(pairs)
    assert partition == {"a": "a", "b": "a", "c": "a"}, (
        f"expected the chain to unite under the smallest id 'a', got {partition}"
    )


def test_gold_partition_non_match_ids_present_as_singletons() -> None:
    """A non_match pair adds BOTH ids to the partition as their OWN singleton — never united."""
    pairs = [GoldPair(left_id="x", right_id="y", label="non_match", source="test")]
    partition = gold_partition(pairs)
    assert partition == {"x": "x", "y": "y"}, (
        f"non_match ids must appear as distinct singletons, got {partition}"
    )


def test_gold_partition_non_match_never_unites_even_when_sharing_a_matched_id() -> None:
    """(a,b,match) unites a+b under 'a'; (b,c,non_match) must NOT pull c into that component —
    c stays its own singleton even though it shares id b with the matched pair."""
    pairs = [
        GoldPair(left_id="a", right_id="b", label="match", source="test"),
        GoldPair(left_id="b", right_id="c", label="non_match", source="test"),
    ]
    partition = gold_partition(pairs)
    assert partition == {"a": "a", "b": "a", "c": "c"}, (
        f"non_match must never unite, even sharing an id with a matched pair; got {partition}"
    )


def test_gold_partition_is_order_independent() -> None:
    """The partition must not depend on the order the pairs are supplied in."""
    pairs = [
        GoldPair(left_id="a", right_id="b", label="match", source="test"),
        GoldPair(left_id="c", right_id="d", label="match", source="test"),
        GoldPair(left_id="b", right_id="c", label="match", source="test"),  # unites both pairs
        GoldPair(left_id="e", right_id="f", label="non_match", source="test"),
    ]
    forward = gold_partition(pairs)
    backward = gold_partition(list(reversed(pairs)))
    assert (
        forward
        == backward
        == {
            "a": "a",
            "b": "a",
            "c": "a",
            "d": "a",
            "e": "e",
            "f": "f",
        }
    ), f"expected an order-independent, deterministic partition; got fwd={forward} bwd={backward}"


# ===========================================================================
# predicted_partition — ledger alias -> canonical; missing id -> itself (spec D1 item 3)
# ===========================================================================


def test_predicted_partition_maps_alias_to_canonical_and_self_row_and_missing_to_itself() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        record_canonical(session, "canon-1")
        record_alias(session, "canon-1", "alias-1")
        session.commit()

        result = predicted_partition(session, ["alias-1", "canon-1", "unknown-id"])

    assert result == {
        "alias-1": "canon-1",
        "canon-1": "canon-1",
        "unknown-id": "unknown-id",
    }, f"unexpected predicted partition: {result}"
    engine.dispose()


def test_predicted_partition_with_no_ledger_maps_every_id_to_itself() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        result = predicted_partition(session, ["r1", "r2", "r3"])
    assert result == {"r1": "r1", "r2": "r2", "r3": "r3"}, f"unexpected: {result}"
    engine.dispose()


# ===========================================================================
# build_sufficiency_report — shape on a seeded SQLite corpus (spec D1 item 5)
# ===========================================================================


def test_build_sufficiency_report_counts_by_source_and_label() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        record_canonical(session, "a1")
        record_alias(session, "a1", "a2")  # a non-trivial mapping so metrics compute
        session.commit()

        report = build_sufficiency_report(session, n_boot=20, seed=1)

    assert report.labels_by_source == {
        "os_pairs": {"match": 1, "non_match": 1},
        "uncertainty": {"match": 2, "non_match": 1},
        "canonical_silver": {"match": 2},
    }, f"unexpected labels_by_source: {report.labels_by_source}"
    assert report.distinct_records == 14, (
        f"expected 14 distinct record ids (a1..a4,b1..b6,c1..c4), got {report.distinct_records}"
    )
    engine.dispose()


def test_build_sufficiency_report_boundary_band_and_scoreless_counts() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        record_canonical(session, "a1")
        record_alias(session, "a1", "a2")
        session.commit()

        report = build_sufficiency_report(session, n_boot=20, seed=1)

    assert report.total_pairs == 7
    assert report.scored_pairs == 3, "3 pairs carry a clerical_score (all from 'uncertainty')"
    assert report.scoreless_pairs == 4, "os_pairs (2) + canonical_silver (2) rows are scoreless"
    assert report.boundary_band_pairs == 1, "only the 0.60-scored pair lies in [0.5, 0.95)"
    assert abs(report.boundary_band_fraction - (1 / 3)) < 1e-9, (
        f"expected 1/3, got {report.boundary_band_fraction}"
    )
    engine.dispose()


def test_build_sufficiency_report_metrics_present_with_non_trivial_ledger_mapping() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        record_canonical(session, "a1")
        record_alias(session, "a1", "a2")  # predicted[a2] == 'a1' != 'a2' -> non-trivial
        session.commit()

        report = build_sufficiency_report(session, n_boot=20, seed=1)

    assert report.metrics is not None, f"expected computed metrics, got reason={report.reason!r}"
    assert set(report.metrics) == _METRIC_KEYS, f"unexpected metric keys: {sorted(report.metrics)}"
    for name, (point, lo, hi) in report.metrics.items():
        assert lo <= point <= hi, f"{name}: bounds violated point={point} lo={lo} hi={hi}"
    # No predicted cluster in this corpus ever mixes two distinct gold clusters (the only ledger
    # merge, a1+a2, is itself a correct gold match) -> over_merge_rate is exactly 0 for EVERY
    # bootstrap resample, not merely on average.
    assert report.metrics["over_merge_rate"] == (0.0, 0.0, 0.0), report.metrics["over_merge_rate"]
    assert report.reason is None
    engine.dispose()


def test_build_sufficiency_report_as_dict_is_json_ready_and_as_text_is_a_report() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        record_canonical(session, "a1")
        record_alias(session, "a1", "a2")
        session.commit()
        report = build_sufficiency_report(session, n_boot=10, seed=2)

    payload = report.as_dict()
    json.dumps(payload)  # must not raise — JSON-ready (spec D1 item 5)
    assert payload["distinct_records"] == report.distinct_records

    text = report.as_text()
    assert isinstance(text, str) and text.strip(), "as_text() must return a non-empty report"
    engine.dispose()


# ===========================================================================
# empty-ledger -> metrics is None + a non-empty reason (never crash) (spec D1 item 5)
# ===========================================================================


def test_build_sufficiency_report_empty_ledger_yields_none_metrics_with_reason() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        session.commit()  # NO canonical_id_ledger rows at all

        report = build_sufficiency_report(session, n_boot=20, seed=1)

    assert report.metrics is None
    assert isinstance(report.reason, str) and report.reason.strip(), (
        f"expected a non-empty reason, got {report.reason!r}"
    )
    engine.dispose()


def test_build_sufficiency_report_ledger_unrelated_to_pairs_still_yields_none_metrics() -> None:
    """A populated ledger that maps ids NEVER referenced by any gold pair must still be treated
    as trivial FOR THIS CORPUS — metrics must stay ``None``, not silently compute over an
    empty/irrelevant restriction (proves the check is scoped to the loaded record ids, not "is
    the ledger table non-empty anywhere in the database")."""
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        record_canonical(session, "z-canon")
        record_alias(session, "z-canon", "z-alias")  # unrelated to a1..c4
        session.commit()

        report = build_sufficiency_report(session, n_boot=20, seed=1)

    assert report.metrics is None
    assert isinstance(report.reason, str) and report.reason.strip()
    engine.dispose()


# ===========================================================================
# metric_confidence_intervals — determinism + bound sanity (spec D1 item 4)
# ===========================================================================

_PERFECT_GOLD = {"a": "g1", "b": "g1", "c": "g2", "d": "g2", "e": "g3"}
_PERFECT_PREDICTED = dict(_PERFECT_GOLD)  # a perfect resolution


def test_metric_confidence_intervals_is_deterministic_for_a_fixed_seed() -> None:
    first = metric_confidence_intervals(_PERFECT_GOLD, _PERFECT_PREDICTED, n_boot=50, seed=7)
    second = metric_confidence_intervals(_PERFECT_GOLD, _PERFECT_PREDICTED, n_boot=50, seed=7)
    assert first == second, "same seed + same inputs must yield byte-identical bootstrap output"


def test_metric_confidence_intervals_bounds_hold_on_a_perfect_match_corpus() -> None:
    result = metric_confidence_intervals(_PERFECT_GOLD, _PERFECT_PREDICTED, n_boot=50, seed=7)
    assert set(result) == _METRIC_KEYS, f"unexpected metric keys: {sorted(result)}"
    for name, (point, lo, hi) in result.items():
        assert lo <= point <= hi, f"{name}: bounds violated point={point} lo={lo} hi={hi}"
    # A perfect resolution: every bootstrap resample stays perfect — over_merge never fires and
    # B3 F1 is exactly 1.0 for any subsample of gold clusters (no ambiguity to resample into).
    assert result["over_merge_rate"] == (0.0, 0.0, 0.0), result["over_merge_rate"]
    assert result["bcubed_f1"] == (1.0, 1.0, 1.0), result["bcubed_f1"]


# ===========================================================================
# read-only smoke — calling build_sufficiency_report must NOT write (spec D1 items 1 + 6)
# ===========================================================================


def _row_counts(session: Session) -> dict[str, int]:
    return {
        table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
        for table in Base.metadata.sorted_tables
    }


def test_build_sufficiency_report_is_read_only_row_counts_unchanged() -> None:
    """``build_sufficiency_report`` is the module's report entrypoint the CLI wraps (spec D1 item
    6: "the module contains NO session.commit/add/delete"). Calling it against a fully-seeded
    corpus must leave EVERY table's row count byte-identical and must not even leave a pending
    (unflushed) ORM write on the session."""
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        _seed_corpus(session)
        record_canonical(session, "a1")
        record_alias(session, "a1", "a2")
        session.commit()

        before = _row_counts(session)
        build_sufficiency_report(session, n_boot=20, seed=1)
        after = _row_counts(session)

        assert before == after, f"row counts changed: before={before} after={after}"
        assert not session.new, f"pending inserts after a read-only call: {session.new}"
        assert not session.dirty, f"pending updates after a read-only call: {session.dirty}"
        assert not session.deleted, f"pending deletes after a read-only call: {session.deleted}"
    engine.dispose()


# ===========================================================================
# structural read-only proof — the module never writes / never imports merge.py (spec D1 item 6)
# ===========================================================================


def test_measure_module_never_calls_session_commit_add_or_delete() -> None:
    """Structural proof of spec D1 item 6: the module's source contains no
    ``session.commit``/``.add``/``.delete`` call and never imports ``resolution.merge``."""
    from worldmonitor.resolution import measure

    tree = ast.parse(inspect.getsource(measure))
    forbidden_calls = {"commit", "add", "delete"}
    found_calls: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in forbidden_calls
        ):
            found_calls.add(node.func.attr)
    assert not found_calls, f"measure.py must never write; found calls to: {found_calls}"

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any("resolution.merge" in name or name == "merge" for name in imported_modules), (
        f"measure.py must never import resolution.merge; imports={imported_modules}"
    )
