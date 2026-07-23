"""Property/metamorphic tests for the freshness state machine (Gate F-1 slice 1, ADR 0123 D1).

Freshness touches no CLAUDE.md invariant (spec §3.1 — read-only, no graph write, no resolution,
no provenance stamp), so a `@given` is not gate-mandatory; it is added anyway as a DECISION (spec
§3.5) because `freshness_status` is a pure, total, deterministic function with three cheap,
high-value algebraic properties to pin:

  P-FRESH-1  TOTALITY + DETERMINISM  — for ANY (status, last_success age-or-None, budgets with
             very_stale > stale), the result is always one of the closed FRESHNESS_STATES, and
             calling twice with identical inputs returns the identical value.
  P-FRESH-2  MONOTONICITY IN AGE     — for a fixed ACTIVE status + a PRESENT last_success + fixed
             budgets, increasing age never moves the state fresher: rank(fresh) < rank(stale) <
             rank(very_stale) is preserved for a1 <= a2 => rank(state(a1)) <= rank(state(a2)).
  P-FRESH-3  TERMINAL AGE-INVARIANCE — `disabled`/`error` (status-driven) and `no_data`
             (last_success is None, active status) are independent of `now`/age: varying `now`
             (equivalently, the notional age) never changes the derived state.

Pure in-process (`freshness_status` takes plain datetimes/ints/strings — no DB, no container, no
SQLAlchemy engine to leak — the `given_red_tests_leak_connections` lesson does not apply here).
NOT marked ``integration``.

RED at collection: `worldmonitor.observability.freshness` does not exist yet
(`ModuleNotFoundError` on the module-level import below).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.observability.freshness import FRESHNESS_STATES, freshness_status

_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_REAL_STATUSES = ("disabled", "enabled", "running", "error")
_RANK = {"fresh": 0, "stale": 1, "very_stale": 2}

# Any status, incl. the 4 real values + arbitrary/hostile text (totality must hold for BOTH).
_STATUS = st.one_of(st.sampled_from(_REAL_STATUSES), st.text(max_size=16))

# An ACTIVE status: never the literal "disabled"/"error" strings (any other text, incl. "", is
# treated as active per the spec's defense-in-depth branch).
_ACTIVE_STATUS = st.one_of(
    st.sampled_from(["enabled", "running"]),
    st.text(max_size=16).filter(lambda s: s not in ("disabled", "error")),
)

_AGE_OR_NONE = st.one_of(
    st.none(),
    st.floats(min_value=0, max_value=10_000_000, allow_nan=False, allow_infinity=False),
)
_AGE = st.floats(min_value=0, max_value=5_000_000, allow_nan=False, allow_infinity=False)


@st.composite
def _budgets(draw: st.DrawFn) -> tuple[int, int]:
    """(stale_after_seconds, very_stale_after_seconds) with very_stale STRICTLY > stale."""
    stale = draw(st.integers(min_value=1, max_value=1_000_000))
    very_stale = draw(st.integers(min_value=stale + 1, max_value=stale + 1_000_000))
    return stale, very_stale


def _last_success(age: float | None) -> datetime | None:
    return None if age is None else _NOW - timedelta(seconds=age)


# ================================================================================================
# P-FRESH-1 — totality + determinism.
# ================================================================================================
@given(status=_STATUS, age=_AGE_OR_NONE, budgets=_budgets())
@_SETTINGS
def test_prop_state_is_total_and_deterministic(
    status: str, age: float | None, budgets: tuple[int, int]
) -> None:
    stale_after, very_stale_after = budgets
    last_success = _last_success(age)

    result1 = freshness_status(
        status=status,
        last_success=last_success,
        now=_NOW,
        stale_after_seconds=stale_after,
        very_stale_after_seconds=very_stale_after,
    )
    result2 = freshness_status(
        status=status,
        last_success=last_success,
        now=_NOW,
        stale_after_seconds=stale_after,
        very_stale_after_seconds=very_stale_after,
    )

    assert result1 in FRESHNESS_STATES, f"non-total: {result1!r} not in {FRESHNESS_STATES}"
    assert result1 == result2, f"non-deterministic: {result1!r} != {result2!r} for identical input"


# ================================================================================================
# P-FRESH-2 — monotonicity in age (age never moves the state fresher).
# ================================================================================================
@given(status=_ACTIVE_STATUS, a1=_AGE, a2=_AGE, budgets=_budgets())
@_SETTINGS
def test_prop_state_monotone_in_age(
    status: str, a1: float, a2: float, budgets: tuple[int, int]
) -> None:
    stale_after, very_stale_after = budgets
    age_lo, age_hi = (a1, a2) if a1 <= a2 else (a2, a1)

    state_lo = freshness_status(
        status=status,
        last_success=_last_success(age_lo),
        now=_NOW,
        stale_after_seconds=stale_after,
        very_stale_after_seconds=very_stale_after,
    )
    state_hi = freshness_status(
        status=status,
        last_success=_last_success(age_hi),
        now=_NOW,
        stale_after_seconds=stale_after,
        very_stale_after_seconds=very_stale_after,
    )

    assert state_lo in _RANK and state_hi in _RANK, (
        f"an ACTIVE status with a present last_success must land in {{fresh,stale,very_stale}}, "
        f"got {state_lo!r}/{state_hi!r} for status={status!r}"
    )
    assert _RANK[state_lo] <= _RANK[state_hi], (
        f"age increased ({age_lo} -> {age_hi}) but the state got FRESHER "
        f"({state_lo!r} -> {state_hi!r}) for status={status!r}, budgets={budgets!r}"
    )


# ================================================================================================
# P-FRESH-3 — terminal-state age-invariance (disabled/error/no_data ignore now/age).
# ================================================================================================
@given(
    status=st.sampled_from(["disabled", "error"]),
    age1=_AGE_OR_NONE,
    age2=_AGE_OR_NONE,
    budgets=_budgets(),
)
@_SETTINGS
def test_prop_terminal_states_age_invariant(
    status: str, age1: float | None, age2: float | None, budgets: tuple[int, int]
) -> None:
    stale_after, very_stale_after = budgets
    state1 = freshness_status(
        status=status,
        last_success=_last_success(age1),
        now=_NOW,
        stale_after_seconds=stale_after,
        very_stale_after_seconds=very_stale_after,
    )
    state2 = freshness_status(
        status=status,
        last_success=_last_success(age2),
        now=_NOW,
        stale_after_seconds=stale_after,
        very_stale_after_seconds=very_stale_after,
    )
    assert state1 == state2 == status, (
        f"status={status!r} must be age-invariant and equal to the status name itself; "
        f"got {state1!r}/{state2!r} for ages {age1!r}/{age2!r}"
    )


@given(status=_ACTIVE_STATUS, budgets=_budgets())
@_SETTINGS
def test_prop_no_data_is_age_invariant_when_never_succeeded(
    status: str, budgets: tuple[int, int]
) -> None:
    """no_data (active status, last_success is None) never depends on `now` — there is no age to
    move; varying `now` across a huge range must never surface a different state."""
    stale_after, very_stale_after = budgets
    for offset_days in (0, 1, 10_000):
        now = _NOW + timedelta(days=offset_days)
        state = freshness_status(
            status=status,
            last_success=None,
            now=now,
            stale_after_seconds=stale_after,
            very_stale_after_seconds=very_stale_after,
        )
        assert state == "no_data", (
            f"active status={status!r} with last_success=None must always be 'no_data' "
            f"regardless of now={now!r}; got {state!r}"
        )
