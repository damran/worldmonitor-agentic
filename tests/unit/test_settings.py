"""Unit tests for application settings (merge-guard mode + ER batch size)."""

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


def test_resolve_batch_size_defaults_to_1000() -> None:
    """ADR 0026: resolve_pending windows the queue in batches of this size."""
    assert Settings().resolve_batch_size == 1000


def test_resolve_batch_size_accepts_override() -> None:
    assert Settings(resolve_batch_size=250).resolve_batch_size == 250


def test_resolve_batch_size_rejects_non_positive() -> None:
    with pytest.raises(ValidationError):
        Settings(resolve_batch_size=0)
