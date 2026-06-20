"""The async subprocess runner: success, failure, timeout, and stderr capture."""

import sys
import time

import pytest

from worldmonitor.runner import RunResult, run_command


async def test_successful_command() -> None:
    result = await run_command([sys.executable, "-c", "print('hi')"], timeout=10)
    assert isinstance(result, RunResult)
    assert result.ok
    assert result.returncode == 0
    assert result.stdout.strip() == b"hi"
    assert not result.timed_out


async def test_nonzero_exit_is_reported_not_raised() -> None:
    result = await run_command([sys.executable, "-c", "import sys; sys.exit(3)"], timeout=10)
    assert not result.ok
    assert result.returncode == 3
    assert not result.timed_out


async def test_stderr_is_captured() -> None:
    result = await run_command(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom')"], timeout=10
    )
    assert result.stderr.strip() == b"boom"


async def test_timeout_kills_the_process() -> None:
    start = time.monotonic()
    result = await run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    elapsed = time.monotonic() - start
    assert result.timed_out
    assert not result.ok
    assert elapsed < 5  # killed promptly, not after the full 30s


async def test_stdin_is_forwarded() -> None:
    result = await run_command(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
        timeout=10,
        input_data=b"abc",
    )
    assert result.stdout == b"ABC"


async def test_empty_command_rejected() -> None:
    with pytest.raises(ValueError):
        await run_command([], timeout=1)
