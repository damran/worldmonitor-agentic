"""Async subprocess runner with timeout + error handling.

The base primitive for ``CliToolConnector`` and any code that shells out to a
tool. It uses ``asyncio.create_subprocess_exec`` (an argv list — **never** a
shell string, so there is no shell-interpolation surface) and enforces a hard
timeout, killing the whole process group if the command overruns.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of a finished (or timed-out) command."""

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    duration: float

    @property
    def ok(self) -> bool:
        """True when the command exited 0 and did not time out."""
        return self.returncode == 0 and not self.timed_out


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of the process (and its group, where supported)."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - platform-specific
            proc.kill()
    except (ProcessLookupError, PermissionError):  # pragma: no cover - race on exit
        pass


async def run_command(
    cmd: Sequence[str],
    *,
    timeout: float,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    input_data: bytes | None = None,
) -> RunResult:
    """Run ``cmd`` (argv list), capturing output, bounded by ``timeout`` seconds.

    Never raises on a non-zero exit or a timeout — inspect ``RunResult.ok`` /
    ``RunResult.timed_out`` / ``RunResult.returncode``. Raises ``ValueError`` only
    for a misuse (empty command).
    """
    if not cmd:
        raise ValueError("cmd must be a non-empty argv sequence")

    start = time.monotonic()
    # start_new_session=True puts the child in its own process group so a
    # timeout can reap any grandchildren it spawned.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        start_new_session=os.name == "posix",
    )

    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=input_data), timeout)
    except TimeoutError:
        timed_out = True
        _kill_tree(proc)
        # Drain whatever the (now dead) process produced.
        stdout, stderr = await proc.communicate()

    duration = time.monotonic() - start
    return RunResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or b"",
        stderr=stderr or b"",
        timed_out=timed_out,
        duration=duration,
    )
