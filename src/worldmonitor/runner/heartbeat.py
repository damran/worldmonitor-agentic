"""The driver last-tick heartbeat (Gate B-4c, ADR 0051).

A file-based, per-container liveness signal: the ingest driver writes the current time to a
file once per loop iteration (every tick, even when idle — an idle driver is still alive).
A separate ``--healthcheck`` reader (the container HEALTHCHECK) treats the file as STALE once
it is older than ``stale_after_seconds`` — or missing/unparseable — and reports the pipeline
DOWN. This is the other half of the audit's live-vs-dead distinction (spec §2): the driver
process can keep echoing ``/health``=ok while doing no ingest/resolve work at all; a stale
heartbeat makes that detectable.

Storage is a FILE, deliberately (ADR 0051 D3): no table (no migration), no Redis (not yet
wired), no shared volume — the driver checks its OWN file via its OWN ``--healthcheck``.
Writes are atomic (temp file + ``os.replace``) so a reader never sees a half-written timestamp.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


class Heartbeat:
    """A last-tick timestamp file with a staleness window."""

    def __init__(self, path: Path, stale_after_seconds: float) -> None:
        self._path = Path(path)
        self._stale_after_seconds = stale_after_seconds

    def touch(self, now: datetime) -> None:
        """Atomically record ``now`` as the last tick (creating the parent dir if missing).

        Writes to a temp file in the same directory then ``os.replace`` (atomic on POSIX),
        so a concurrent reader sees either the old or the new timestamp — never a partial one,
        and no leftover temp file remains.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        tmp.write_text(now.isoformat())
        os.replace(tmp, self._path)

    def read(self, now: datetime | None = None) -> datetime | None:
        """Return the last-tick timestamp, or ``None`` if the file is missing/unparseable."""
        try:
            raw = self._path.read_text().strip()
        except OSError:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def is_alive(self, now: datetime) -> bool:
        """True IFF the file exists AND ``now - last_tick <= stale_after_seconds``.

        A missing or unparseable file is NOT alive (fail-closed) — exactly the signal the
        container HEALTHCHECK acts on.
        """
        last_tick = self.read()
        if last_tick is None:
            return False
        return (now - last_tick).total_seconds() <= self._stale_after_seconds
