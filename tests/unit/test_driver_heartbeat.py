"""Gate B-4c R2 — the driver last-tick heartbeat detects a stalled pipeline.

The other half of the audit's live-vs-dead distinction (spec §2): the driver process can
keep echoing ``/health``=ok while it does no ingest/resolve work at all. A file-based
last-tick heartbeat makes that detectable — once the heartbeat goes stale (or is missing),
``is_alive`` / ``python -m worldmonitor.runner.driver --healthcheck`` reports the pipeline
DOWN (the container HEALTHCHECK hook).

Failing-test-first oracle for slice 2. RED today: ``worldmonitor.runner.heartbeat`` does
not exist (ImportError) and the driver has no ``--healthcheck`` flag. GREEN once the
builder adds ``heartbeat.Heartbeat`` + the ``--healthcheck`` mode.

Contract the builder must satisfy:

* ``Heartbeat(path, stale_after_seconds)`` (positional) with:
  - ``touch(now: datetime) -> None`` — atomic write (temp + os.replace) of the ISO
    timestamp; creates the parent dir if missing.
  - ``is_alive(now: datetime) -> bool`` — file exists AND ``now - last_tick <=
    stale_after_seconds``. Missing/unparseable file ⇒ not alive.
* ``python -m worldmonitor.runner.driver --healthcheck`` builds the ``Heartbeat`` from
  settings (``DRIVER_HEARTBEAT_PATH`` / ``DRIVER_HEARTBEAT_STALE_SECONDS``), checks
  ``is_alive(now)`` and exits 0 (alive) / 1 (missing-or-stale). It must NOT construct the
  full driver (no store connections) — it only reads the file.

The clock is injected explicitly (no wall-clock ambiguity); the file lives under tmp_path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from worldmonitor.runner.heartbeat import Heartbeat

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXED_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)


# --- is_alive staleness logic (injected clock, no subprocess) -------------- #


def test_fresh_touch_is_alive(tmp_path: Path) -> None:
    hb = Heartbeat(tmp_path / "driver.heartbeat", 90.0)
    hb.touch(_FIXED_NOW)
    assert hb.is_alive(_FIXED_NOW) is True
    # Still alive just within the staleness window.
    assert hb.is_alive(_FIXED_NOW + timedelta(seconds=89)) is True


def test_stale_heartbeat_is_not_alive(tmp_path: Path) -> None:
    hb = Heartbeat(tmp_path / "driver.heartbeat", 90.0)
    hb.touch(_FIXED_NOW)
    # One second past the staleness window ⇒ the pipeline is reported down.
    assert hb.is_alive(_FIXED_NOW + timedelta(seconds=91)) is False


def test_missing_heartbeat_file_is_not_alive(tmp_path: Path) -> None:
    hb = Heartbeat(tmp_path / "never-written.heartbeat", 90.0)
    assert hb.is_alive(_FIXED_NOW) is False


def test_touch_writes_atomically_and_round_trips(tmp_path: Path) -> None:
    # Parent dir is missing — touch must create it.
    path = tmp_path / "run" / "worldmonitor" / "driver.heartbeat"
    Heartbeat(path, 90.0).touch(_FIXED_NOW)

    assert path.exists()
    # Atomic temp+replace leaves no leftover temp file in the directory.
    assert [p.name for p in path.parent.iterdir()] == [path.name]

    # The ISO timestamp round-trips exactly.
    persisted = datetime.fromisoformat(path.read_text().strip())
    assert persisted == _FIXED_NOW

    # A fresh instance reading the same file agrees on liveness.
    fresh = Heartbeat(path, 90.0)
    assert fresh.is_alive(_FIXED_NOW) is True
    assert fresh.is_alive(_FIXED_NOW + timedelta(seconds=1000)) is False


# --- `driver --healthcheck` exit codes (subprocess, no live stack) --------- #


def _run_healthcheck(path: Path, stale_seconds: float) -> subprocess.CompletedProcess[bytes]:
    """Invoke the container HEALTHCHECK command with the heartbeat wired via env."""
    env = {
        **os.environ,
        "DRIVER_HEARTBEAT_PATH": str(path),
        "DRIVER_HEARTBEAT_STALE_SECONDS": str(stale_seconds),
    }
    return subprocess.run(
        [sys.executable, "-m", "worldmonitor.runner.driver", "--healthcheck"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=60,
    )


def test_healthcheck_exits_0_when_heartbeat_fresh(tmp_path: Path) -> None:
    path = tmp_path / "driver.heartbeat"
    # Write a genuinely-now timestamp so the subprocess's real clock sees it as fresh.
    Heartbeat(path, 3600.0).touch(datetime.now(UTC))
    result = _run_healthcheck(path, 3600.0)
    assert result.returncode == 0, result.stderr.decode()


def test_healthcheck_exits_1_when_heartbeat_stale(tmp_path: Path) -> None:
    path = tmp_path / "driver.heartbeat"
    # A day-old tick is far past any sane staleness window ⇒ down.
    Heartbeat(path, 90.0).touch(datetime.now(UTC) - timedelta(days=1))
    result = _run_healthcheck(path, 90.0)
    assert result.returncode == 1, result.stderr.decode()


def test_healthcheck_exits_1_when_heartbeat_missing(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.heartbeat"
    result = _run_healthcheck(path, 90.0)
    assert result.returncode == 1, result.stderr.decode()
