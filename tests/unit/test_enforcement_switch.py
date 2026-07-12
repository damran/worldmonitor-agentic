"""Unit tests for the runtime enforcement switch (ADR 0109).

The switch is operator config: `enforcement_profile` ("strict" DEFAULT / "off") with per-guard
`enforce_*` overrides, resolved by `is_enforced()` at each guard's choke point. Tests construct
`Settings(...)` with explicit values (init kwargs win over any local `.env`) so they are
deterministic regardless of the dev instance's own `ENFORCEMENT_PROFILE`.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

from worldmonitor import erasure
from worldmonitor.settings import Settings


def test_code_default_is_strict() -> None:
    """The declared DEFAULT must be strict (production-safe) — read off the field, so the
    assertion is independent of any local `.env` a dev instance may set."""
    assert Settings.model_fields["enforcement_profile"].default == "strict"


def test_strict_profile_enforces_every_guard() -> None:
    s = Settings(enforcement_profile="strict")
    assert s.is_enforced("merge_guard") is True
    assert s.is_enforced("erasure_authorization") is True
    assert s.disabled_enforcements() == []


def test_off_profile_bypasses_every_guard() -> None:
    s = Settings(enforcement_profile="off")
    assert s.is_enforced("merge_guard") is False
    assert s.is_enforced("erasure_authorization") is False
    assert set(s.disabled_enforcements()) == {"merge_guard", "erasure_authorization"}


def test_per_guard_override_wins_over_profile() -> None:
    # Off profile, but one guard forced back on.
    s = Settings(enforcement_profile="off", enforce_merge_guard=True)
    assert s.is_enforced("merge_guard") is True
    assert s.is_enforced("erasure_authorization") is False
    assert s.disabled_enforcements() == ["erasure_authorization"]

    # Strict profile, but one guard forced off.
    s2 = Settings(enforcement_profile="strict", enforce_erasure_authorization=False)
    assert s2.is_enforced("erasure_authorization") is False
    assert s2.is_enforced("merge_guard") is True
    assert s2.disabled_enforcements() == ["erasure_authorization"]


def test_unknown_guard_follows_the_profile() -> None:
    assert Settings(enforcement_profile="strict").is_enforced("nonexistent") is True
    assert Settings(enforcement_profile="off").is_enforced("nonexistent") is False


def test_log_enforcement_status_warns_only_when_a_guard_is_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="worldmonitor.settings"):
        Settings(enforcement_profile="strict").log_enforcement_status()
    assert caplog.records == []  # strict = silent

    with caplog.at_level(logging.WARNING, logger="worldmonitor.settings"):
        Settings(enforcement_profile="off").log_enforcement_status()
    assert any("DISABLED" in r.message for r in caplog.records)
    assert any("merge_guard" in r.getMessage() for r in caplog.records)


def test_erase_source_still_rejects_blank_authorization_when_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `authorized_by=""` default must NOT weaken the strict gate — a blank auth still
    fails closed before any store is touched (the check precedes `session.add`)."""
    monkeypatch.setattr(erasure, "get_settings", lambda: Settings(enforcement_profile="strict"))
    with pytest.raises(ValueError, match="authorized_by"):
        erasure.erase_source(
            neo4j=Mock(), session=Mock(), landing=Mock(), source_id="conn:ds", authorized_by=""
        )


def test_erase_source_skips_authorization_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the guard off, a blank auth must get PAST the authorization gate (it may fail later
    on the Mock stores — that failure is not the auth ValueError)."""
    monkeypatch.setattr(erasure, "get_settings", lambda: Settings(enforcement_profile="off"))
    try:
        erasure.erase_source(
            neo4j=Mock(), session=Mock(), landing=Mock(), source_id="conn:ds", authorized_by=""
        )
    except ValueError as exc:  # pragma: no cover - only if the gate wrongly fired
        assert "authorized_by" not in str(exc), "off mode must not raise the authorization gate"
    except Exception:  # noqa: BLE001 - any later Mock-driven failure means we passed the gate
        pass
