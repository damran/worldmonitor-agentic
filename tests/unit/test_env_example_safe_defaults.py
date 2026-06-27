"""Guard: `.env.example` must not ship fail-OPEN safety defaults (audit M-3).

The documented bootstrap is "copy `.env.example` to `.env`". So any value the example ships
becomes a standard deployment's effective config. `MERGE_GUARD_MODE=alert` is **fail-open** — it
writes flagged PEP/sanctioned merges instead of parking them for review (ADR 0024). The example
MUST therefore ship the fail-CLOSED `block` (or omit the key so the code default `block`,
`settings.py`, applies). This test fails on the pre-fix example (which ships `alert`).
"""

from __future__ import annotations

import re
from pathlib import Path

_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _value_for(key: str) -> str | None:
    """Return the RHS of an uncommented `KEY=value` line in `.env.example`, or None if absent."""
    if not _ENV_EXAMPLE.is_file():
        return None
    for raw in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip()
    return None


def test_env_example_merge_guard_is_not_fail_open() -> None:
    """`.env.example` must not ship `MERGE_GUARD_MODE=alert` (fail-open). Block or absent only."""
    value = _value_for("MERGE_GUARD_MODE")
    assert value != "alert", (
        ".env.example ships MERGE_GUARD_MODE=alert (fail-open): a copy-to-.env deploy would "
        "auto-write flagged PEP/sanctioned merges. Ship 'block' or omit the key (audit M-3)."
    )
    assert value in (None, "block"), (
        f"MERGE_GUARD_MODE in .env.example must be 'block' or absent (fail-closed), got {value!r}"
    )


def test_env_example_exists_and_is_parseable() -> None:
    """Sanity: the example file exists and has at least one KEY=value line (test wired right)."""
    assert _ENV_EXAMPLE.is_file(), f"{_ENV_EXAMPLE} missing"
    assert re.search(r"^\s*[A-Z0-9_]+=", _ENV_EXAMPLE.read_text(encoding="utf-8"), re.MULTILINE)
