"""Unit tests for application settings (the catastrophic-merge-guard mode flag)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from worldmonitor.settings import Settings


def test_merge_guard_mode_defaults_to_alert() -> None:
    """Build phase (ADR 0024): the guard alerts rather than blocks by default."""
    assert Settings().merge_guard_mode == "alert"


def test_merge_guard_mode_accepts_block() -> None:
    assert Settings(merge_guard_mode="block").merge_guard_mode == "block"


def test_merge_guard_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Settings(merge_guard_mode="lenient")  # type: ignore[arg-type]
