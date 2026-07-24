"""Gate F-6 slice 1 -- entry-point smoke tests for the ``wm`` CLI (AC-19, AC-20).

Two things that a pure in-process unit test (``tests/unit/test_cli.py``) cannot prove:

* AC-19: ``python -m worldmonitor.cli`` actually works as a **module entry point** -- no
  reliance on an editable/console-script install, no live server required.
* AC-20: the packaging metadata (``pyproject.toml``) actually declares the
  ``[project.scripts] wm = "worldmonitor.cli:main"`` console-script entry the spec pins.

RED reason (today):

* AC-19: ``worldmonitor.cli`` does not exist, so the subprocess exits non-zero with
  ``No module named worldmonitor.cli`` (or ``worldmonitor.cli`` -- Python's exact wording
  varies by version) on stderr -- this test asserts exit code 0, so it fails for the
  intended reason (module absent), not a superficial one.
* AC-20: ``pyproject.toml`` has no ``[project.scripts]`` table at all (verified fact V9 in
  the spec) -- parsing it and indexing ``["project"]["scripts"]`` raises ``KeyError``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_module_help_runs() -> None:
    """AC-19: ``python -m worldmonitor.cli --help`` exits 0 and lists the three subcommands.

    Uses ``PYTHONPATH=src`` (not an installed/editable package) so this proves the module is
    reachable purely via the source tree -- no live server, no network, no dependency on
    ``uv pip install -e .`` having been run.
    """
    env = {**os.environ, "PYTHONPATH": "src"}
    result = subprocess.run(
        [sys.executable, "-m", "worldmonitor.cli", "--help"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"expected exit 0, got {result.returncode}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    usage = result.stdout
    assert "health" in usage
    assert "ready" in usage
    assert "entity" in usage


def test_module_runs_with_no_live_server() -> None:
    """AC-19 (network isolation): --help must not attempt any network I/O.

    Regression guard against a builder accidentally probing WM_BASE_URL (e.g. a default
    connectivity check) before argparse even dispatches --help. Runs with a WM_BASE_URL that
    points at a port nothing listens on; if the module tried to connect it would hang or
    error, not exit 0 promptly.
    """
    env = {
        **os.environ,
        "PYTHONPATH": "src",
        "WM_BASE_URL": "http://127.0.0.1:1",  # nothing listens here
    }
    result = subprocess.run(
        [sys.executable, "-m", "worldmonitor.cli", "--help"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0


def test_project_script_entry_declared() -> None:
    """AC-20: pyproject.toml declares [project.scripts] wm = "worldmonitor.cli:main".

    Parses the real ``pyproject.toml`` with stdlib ``tomllib`` -- no install needed.
    """
    pyproject_path = _REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)

    scripts = data["project"]["scripts"]
    assert scripts["wm"] == "worldmonitor.cli:main"
