"""Gate F-1 slice 1 — the freshness state machine + shared derivation (ADR 0123 D1/D2).

`docs/reviews/GATE_F1_FRESHNESS_SURFACE_SPEC.md` §2/§6.1/§6.4. Three sections:

  A. `freshness_status` — the pure, total, deterministic 6-state truth table (§2), incl.
     precedence (disabled beats error beats no_data beats the age states), the exact boundary
     inequality direction (`age >= very_stale_after` / `stale_after <= age < very_stale_after`),
     `"running"`/hostile-status totality, and tz-aware UTC handling.
  B. `compute_instance_freshness` — the ONE shared query+derivation (§2.1), run against a REAL
     in-memory SQLite session (Docker-free, mirrors `tests/unit/test_metrics_collector.py`'s
     `_sqlite_sessions` idiom). `test_compute_instance_freshness_uses_last_success_not_last_run`
     is the LOAD-BEARING regression: a recent `last_run` with only a FAILED `task_run` history
     must NOT read `fresh` (ADR 0123 D2 / spec §9 item 1).
  C. REST <-> gauge lockstep parity (§6.4, ADR-0076 INV-5 idiom): for the SAME seeded session +
     budgets, the collector's emitted `{state}` label per instance must equal the REST body's
     `freshness_status` for that instance.

RED at collection: `worldmonitor.observability.freshness` does not exist yet
(`ModuleNotFoundError` on the module-level import below) — the entire file fails to collect,
which is the correct RED reason (no code to test yet). Section C additionally needs
`worldmonitor.api.freshness` (mounted by `create_app`) and the collector's new
`stale_after_seconds`/`very_stale_after_seconds` constructor keywords, both absent today.
"""

from __future__ import annotations

import time
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.db.models import Base, ConnectorInstance, TaskRun

# RED import: the observability package / freshness module do not exist yet.
from worldmonitor.observability.freshness import (
    FRESHNESS_STATES,
    InstanceFreshness,
    compute_instance_freshness,
    freshness_status,
)
from worldmonitor.settings import Settings

_STALE_AFTER = 14400  # 4h, the spec's default (also asserted against Settings() elsewhere)
_VERY_STALE_AFTER = 86400  # 24h
_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

AUTH = {"Authorization": "Bearer good"}


# --------------------------------------------------------------------------------------------- #
# SQLite JSONB shim (idempotent if another test module already registered it) + a real in-memory
# session factory — mirrors tests/unit/test_metrics_collector.py exactly.
# --------------------------------------------------------------------------------------------- #
@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _sqlite_sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _as_utc(dt: datetime) -> datetime:
    """Normalize a possibly-naive datetime (SQLite hands back naive) to aware UTC for comparison.

    Test-side normalization ONLY — this does not assert how the builder internally represents
    timestamps, just lets the test compare wall-clock instants regardless of that choice.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class _FakeVerifier:
    """Accepts the bearer 'good'; rejects everything else (mirrors the sibling REST test files)."""

    def verify(self, token: str) -> dict[str, str]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


class _FakeNeo4j:
    """Freshness is Postgres-only (spec §3.6) — ANY graph call here is a bug."""

    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("the freshness surface must never touch Neo4j (Postgres-only)")

    def execute_write(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("the freshness surface must never write (read-only, AC-6)")


class _StubNeo4jForCollector:
    """The collector's own Neo4j dependency — canned counts, execute_write is fatal."""

    def __init__(self) -> None:
        pass

    def execute_read(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        is_edges = "[r]" in query or "->()" in query or "count(r)" in query
        return [{"n": 0 if is_edges else 0}]

    def execute_write(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        raise AssertionError("collector must be read-only — execute_write must never be called")

    def close(self) -> None:
        pass


# ================================================================================================
# A. freshness_status — the pure 6-state truth table (spec §2).
# ================================================================================================


def test_freshness_states_is_the_closed_six_set() -> None:
    assert set(FRESHNESS_STATES) == {
        "fresh",
        "stale",
        "very_stale",
        "no_data",
        "error",
        "disabled",
    }
    assert len(FRESHNESS_STATES) == 6, "FRESHNESS_STATES must be exactly the closed 6-set"


@pytest.mark.parametrize(
    "last_success_offset",
    [None, timedelta(seconds=1), timedelta(days=100_000)],
    ids=["never", "recent", "ancient"],
)
def test_state_disabled_beats_everything_age_invariant(
    last_success_offset: timedelta | None,
) -> None:
    """Row 1: status=='disabled' wins regardless of last_success/age
    (precedence + age-invariant)."""
    last_success = None if last_success_offset is None else _NOW - last_success_offset
    result = freshness_status(
        status="disabled",
        last_success=last_success,
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert result == "disabled"


@pytest.mark.parametrize(
    "last_success_offset",
    [None, timedelta(seconds=1), timedelta(days=100_000)],
    ids=["never", "recent", "ancient"],
)
def test_state_error_beats_no_data_and_age(last_success_offset: timedelta | None) -> None:
    """Row 2: status=='error' wins over BOTH the no_data branch (last_success is None) AND every
    age branch (a RECENT success does not make an auto-hard-disabled instance read 'fresh')."""
    last_success = None if last_success_offset is None else _NOW - last_success_offset
    result = freshness_status(
        status="error",
        last_success=last_success,
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert result == "error"


@pytest.mark.parametrize("status", ["enabled", "running", "some-unexpected-status"])
def test_state_no_data_when_active_and_never_succeeded(status: str) -> None:
    """Row 3: any ACTIVE status (not disabled/error, incl. hostile/unexpected) with
    last_success is None -> no_data."""
    result = freshness_status(
        status=status,
        last_success=None,
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert result == "no_data"


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [
        (0, "fresh"),
        (_STALE_AFTER - 1, "fresh"),
        (_STALE_AFTER, "stale"),  # exact boundary: age == stale_after -> stale (row 5, >=)
        (_STALE_AFTER + 1, "stale"),
        (_VERY_STALE_AFTER - 1, "stale"),
        (_VERY_STALE_AFTER, "very_stale"),  # exact boundary: age == very_stale_after -> very_stale
        (_VERY_STALE_AFTER + 1, "very_stale"),
        (_VERY_STALE_AFTER * 100, "very_stale"),
    ],
)
def test_state_fresh_stale_very_stale_boundaries(age_seconds: int, expected: str) -> None:
    """Rows 4-6: the exact inequality directions the spec pins —
    `age < stale_after` -> fresh; `stale_after <= age < very_stale_after` -> stale;
    `age >= very_stale_after` -> very_stale. Both exact-boundary ages are asserted."""
    last_success = _NOW - timedelta(seconds=age_seconds)
    result = freshness_status(
        status="enabled",
        last_success=last_success,
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert result == expected, f"age={age_seconds}s: expected {expected!r}, got {result!r}"


def test_running_status_treated_as_active() -> None:
    """'running' (a real, transient ConnectorInstance.status) is NOT disabled/error -> the age
    (or no_data) branch applies, exactly like 'enabled'."""
    fresh = freshness_status(
        status="running",
        last_success=_NOW - timedelta(seconds=10),
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert fresh == "fresh"
    no_data = freshness_status(
        status="running",
        last_success=None,
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert no_data == "no_data"


@pytest.mark.parametrize(
    "status", ["", "DROP TABLE connector_instance;", "unicode-\U0001f525", "a" * 500]
)
def test_unknown_status_is_total(status: str) -> None:
    """Defense-in-depth (spec §9 item 3): ANY status string not in {disabled, error} is treated
    as active — the function never raises/returns an out-of-alphabet value for hostile input."""
    fresh = freshness_status(
        status=status,
        last_success=_NOW - timedelta(seconds=10),
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert fresh == "fresh"
    assert fresh in FRESHNESS_STATES
    no_data = freshness_status(
        status=status,
        last_success=None,
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert no_data == "no_data"


@pytest.mark.parametrize(
    "status", ["ENABLED", " disabled", "Disabled", "DISABLED", "error ", " error"]
)
def test_status_matching_is_exact_no_case_or_whitespace_folding(status: str) -> None:
    """The spec's condition is a literal `status == "disabled"` / `status == "error"` (§2) — a
    case-folded or whitespace-trimmed match would silently treat a typo'd/garbled status as
    terminal. None of these variants are the literal string, so all must land in the active
    branch (here: fresh, since last_success is recent)."""
    result = freshness_status(
        status=status,
        last_success=_NOW - timedelta(seconds=10),
        now=_NOW,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    assert result == "fresh", (
        f"status={status!r} must NOT match 'disabled'/'error' via case/whitespace folding; "
        f"got {result!r}"
    )


def test_instance_freshness_is_frozen_dataclass_with_expected_fields() -> None:
    """Pins the exact §2.1 dataclass shape (frozen, 7 named fields)."""
    row = InstanceFreshness(
        instance_id="i1",
        connector_id="c1",
        status="enabled",
        freshness_status="fresh",
        last_run=None,
        last_success_at=None,
        age_seconds=None,
    )
    assert row.instance_id == "i1"
    assert row.connector_id == "c1"
    assert row.status == "enabled"
    assert row.freshness_status == "fresh"
    assert row.last_run is None
    assert row.last_success_at is None
    assert row.age_seconds is None
    with pytest.raises(FrozenInstanceError):
        row.instance_id = "mutated"  # type: ignore[misc]


# ================================================================================================
# B. compute_instance_freshness — the ONE shared query+derivation (spec §2.1/§6.1), over a REAL
#    in-memory SQLite session (no Docker).
# ================================================================================================


def test_compute_instance_freshness_uses_last_success_not_last_run() -> None:
    """LOAD-BEARING (spec §9 item 1 / ADR 0123 D2, A5): a forever-failing feed with a RECENT
    ConnectorInstance.last_run but ONLY a FAILED task_run history must read no_data, NEVER fresh.
    Using last_run instead of the last successful ingest is the exact bug the shipped
    worldmonitor_connector_last_success_timestamp gauge's own comment already calls out."""
    sessions = _sqlite_sessions()
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-forever-failing",
                    connector_id="feeds",
                    config_encrypted="x",
                    status="enabled",
                    last_run=now,  # a RECENT attempt ...
                ),
                TaskRun(
                    id="t-failed",
                    kind="ingest",
                    status="error",
                    connector_instance_id="ci-forever-failing",
                    started_at=now,
                    finished_at=now,  # ... that FAILED. No successful ingest ever recorded.
                ),
            ]
        )
        session.commit()

    with sessions() as session:
        results = compute_instance_freshness(
            session,
            now=now,
            stale_after_seconds=_STALE_AFTER,
            very_stale_after_seconds=_VERY_STALE_AFTER,
        )
    row = next(r for r in results if r.instance_id == "ci-forever-failing")

    assert row.last_success_at is None, (
        "the only task_run row is a FAILED ingest -- last_success_at must be None"
    )
    assert row.age_seconds is None
    assert row.freshness_status != "fresh", (
        "a forever-failing feed with a RECENT last_run read 'fresh' -- this means the derivation "
        "used ConnectorInstance.last_run instead of the last SUCCESSFUL ingest (ADR 0123 D2 / "
        "A5), exactly the bug this gate must avoid"
    )
    assert row.freshness_status == "no_data", f"expected no_data, got {row.freshness_status!r}"
    # last_run is still surfaced (display-only) — the fix belongs in the STATE derivation, not in
    # hiding the raw attempt timestamp.
    assert row.last_run is not None
    assert _as_utc(row.last_run) == now


def test_compute_instance_freshness_shape() -> None:
    """§6.1: one InstanceFreshness per instance, age_seconds computed from last_success_at."""
    sessions = _sqlite_sessions()
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    success_at = now - timedelta(seconds=500)
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-x", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                TaskRun(
                    id="t-ok",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-x",
                    started_at=success_at,
                    finished_at=success_at,
                ),
            ]
        )
        session.commit()

    with sessions() as session:
        results = compute_instance_freshness(
            session,
            now=now,
            stale_after_seconds=_STALE_AFTER,
            very_stale_after_seconds=_VERY_STALE_AFTER,
        )
    row = next(r for r in results if r.instance_id == "ci-x")

    assert row.connector_id == "feeds"
    assert row.status == "enabled"
    assert row.freshness_status == "fresh"
    assert row.age_seconds is not None
    assert abs(row.age_seconds - 500.0) < 1e-6, (
        f"age_seconds must be exactly (now - last_success_at).total_seconds(); "
        f"got {row.age_seconds}"
    )
    assert row.last_success_at is not None
    assert _as_utc(row.last_success_at) == success_at


def test_compute_instance_freshness_covers_disabled_and_error() -> None:
    """The shared query must derive disabled/error instances too (not just the age branches),
    with no successful-ingest join noise affecting the terminal states."""
    sessions = _sqlite_sessions()
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-off", connector_id="feeds", config_encrypted="x", status="disabled"
                ),
                ConnectorInstance(
                    id="ci-bad", connector_id="feeds", config_encrypted="x", status="error"
                ),
            ]
        )
        session.commit()

    with sessions() as session:
        results = compute_instance_freshness(
            session,
            now=now,
            stale_after_seconds=_STALE_AFTER,
            very_stale_after_seconds=_VERY_STALE_AFTER,
        )
    by_id = {r.instance_id: r for r in results}
    assert by_id["ci-off"].freshness_status == "disabled"
    assert by_id["ci-bad"].freshness_status == "error"
    assert by_id["ci-off"].last_success_at is None
    assert by_id["ci-bad"].last_success_at is None


def test_compute_instance_freshness_naive_sqlite_timestamps_assumed_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Open item 2 (tz-aware UTC): SQLite hands back a NAIVE datetime for a
    ``DateTime(timezone=True)`` column. The derivation must treat that naive value as UTC — NOT
    as the process's local timezone — otherwise age_seconds silently shifts by the host's UTC
    offset. This forces a non-UTC local timezone for the duration of the test so a local-time
    misinterpretation bug would change the computed age (it must NOT, and the CI/dev host being
    UTC-configured would otherwise mask exactly this bug class)."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        sessions = _sqlite_sessions()
        now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
        success_at = now - timedelta(hours=5)  # 18000s -> 'stale' under the default budgets
        with sessions() as session:
            session.add_all(
                [
                    ConnectorInstance(
                        id="ci-tz", connector_id="feeds", config_encrypted="x", status="enabled"
                    ),
                    TaskRun(
                        id="t-tz",
                        kind="ingest",
                        status="ok",
                        connector_instance_id="ci-tz",
                        started_at=success_at,
                        finished_at=success_at,
                    ),
                ]
            )
            session.commit()

        with sessions() as session:
            results = compute_instance_freshness(
                session,
                now=now,
                stale_after_seconds=_STALE_AFTER,
                very_stale_after_seconds=_VERY_STALE_AFTER,
            )
        row = next(r for r in results if r.instance_id == "ci-tz")

        assert row.age_seconds is not None
        assert abs(row.age_seconds - 18000.0) < 1e-6, (
            "a naive SQLite timestamp must be treated as UTC regardless of the process TZ "
            f"(America/New_York here); got age_seconds={row.age_seconds} (expected 18000.0 = 5h)"
        )
        assert row.freshness_status == "stale"
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


# ================================================================================================
# C. REST <-> gauge lockstep parity (spec §3.2/§6.4, the ADR-0076 INV-5 idiom).
# ================================================================================================


def test_rest_and_gauge_agree_per_instance() -> None:
    """For the SAME seeded session + budgets, GET /sources/freshness's freshness_status per
    instance must equal the collector's emitted worldmonitor_connector_freshness {state} label
    for that SAME instance — both are thin consumers of compute_instance_freshness (ADR 0123 D2).

    Uses real wall-clock deltas (not a frozen `now`) because the REST route and the collector
    each compute `now` independently a few milliseconds apart; every seeded age is chosen far
    from a state boundary so that drift cannot flip a state between the two calls.
    """
    from worldmonitor.metrics.collector import DriverMetricsCollector

    sessions = _sqlite_sessions()
    now_ref = datetime.now(UTC)
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-disabled", connector_id="a", config_encrypted="x", status="disabled"
                ),
                ConnectorInstance(
                    id="ci-error", connector_id="b", config_encrypted="x", status="error"
                ),
                ConnectorInstance(
                    id="ci-no-data", connector_id="c", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-fresh", connector_id="d", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-stale", connector_id="e", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-very-stale", connector_id="f", config_encrypted="x", status="enabled"
                ),
                TaskRun(
                    id="t-fresh",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-fresh",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(minutes=2),
                ),
                TaskRun(
                    id="t-stale",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-stale",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(hours=6),
                ),
                TaskRun(
                    id="t-very-stale",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-very-stale",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(hours=30),
                ),
            ]
        )
        session.commit()

    settings = Settings(environment="test")
    app = create_app(
        settings=settings,
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
        db_sessions=sessions,
    )
    client = TestClient(app)
    resp = client.get("/sources/freshness", headers=AUTH)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    rest_states: dict[str, str] = {
        row["instance_id"]: row["freshness_status"] for row in body["sources"]
    }

    collector = DriverMetricsCollector(
        session_factory=sessions,
        neo4j=_StubNeo4jForCollector(),  # type: ignore[arg-type]
        skip_counter=lambda: 0,
        stale_after_seconds=settings.freshness_stale_after_seconds,
        very_stale_after_seconds=settings.freshness_very_stale_after_seconds,
    )
    gauge_states: dict[str, str] = {}
    for family in collector.collect():
        if family.name != "worldmonitor_connector_freshness":
            continue
        for sample in family.samples:
            gauge_states[sample.labels["instance"]] = sample.labels["state"]

    expected_ids = {
        "ci-disabled",
        "ci-error",
        "ci-no-data",
        "ci-fresh",
        "ci-stale",
        "ci-very-stale",
    }
    assert expected_ids <= rest_states.keys(), f"REST body missing seeded instances: {rest_states}"
    assert expected_ids <= gauge_states.keys(), f"gauge missing seeded instances: {gauge_states}"

    for instance_id in expected_ids:
        assert gauge_states[instance_id] == rest_states[instance_id], (
            f"REST/gauge DRIFT for {instance_id}: REST={rest_states[instance_id]!r} "
            f"gauge={gauge_states[instance_id]!r}"
        )

    # Not a vacuous all-fresh pass: the full closed state alphabet was actually exercised.
    assert {rest_states[i] for i in expected_ids} == set(FRESHNESS_STATES)
