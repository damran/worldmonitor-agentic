"""Gate 3g — CliToolConnector base: the argv-list / injectable-runner oracle (ADR 0071 §5).

``CliToolConnector`` is the base every ACTIVE CLI-tool connector subclasses. It runs a real binary
via ``run_command`` (``asyncio.create_subprocess_exec`` — an argv LIST, never a shell string, so
there is no shell-interpolation surface) and yields the captured stdout as a ``RawRecord``. This
file pins the BASE contract, independent of whois:

* ``collect(config)`` reads ``config["_scope"]``, validates the target via the subclass hook
  ``_validate_target``, builds the argv via the subclass hook ``_build_argv(scope) -> list[str]``,
  runs it through the INJECTABLE runner seam, and yields ONE ``RawRecord`` of the captured stdout;
* the runner is always called with an argv **LIST** (never a ``str``) — the no-shell invariant;
* the capability is ``Capability.ACTIVE`` (the gate the cadence driver refuses).

LOCKED ASSUMPTIONS the builder MUST match so this oracle stays meaningful:

  ``from worldmonitor.plugins.cli_tool import CliToolConnector``
  ``CliToolConnector(Connector)`` with ``__init__(self, *, runner=None)`` — ``runner`` is an
        injectable ``run_command``-compatible callable (``async def runner(argv, *, timeout) ->
        RunResult``); the default is the real :func:`worldmonitor.runner.subprocess.run_command`.
        Subclasses implement the abstract hooks ``_validate_target(target)`` (raises ``ValueError``
        on a bad target) and ``_build_argv(scope) -> list[str]`` (plus ``manifest`` /
        ``config_schema`` / ``map``). ``collect`` drives the async runner from sync code
        (``asyncio.run``) and yields the captured stdout.

RED on the base tree: ``worldmonitor.plugins.cli_tool`` does not exist (ModuleNotFoundError).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord
from worldmonitor.plugins.cli_tool import CliToolConnector
from worldmonitor.provenance.model import Provenance
from worldmonitor.runner.subprocess import RunResult


class _FakeRunner:
    """An injectable ``run_command``-compatible async callable that records its argv + timeout and
    returns canned stdout (no real subprocess, no binary, no network)."""

    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv: Any, *, timeout: float, **kwargs: Any) -> RunResult:
        self.calls.append({"argv": argv, "timeout": timeout})
        return RunResult(
            returncode=0, stdout=self._stdout, stderr=b"", timed_out=False, duration=0.0
        )


class _EchoTool(CliToolConnector):
    """A minimal in-test ACTIVE CLI tool: argv = ``["echo", <target>]``, permissive validation."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="echo-tool",
            name="Echo Tool",
            version="0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.ACTIVE,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def _validate_target(self, target: Any) -> None:
        if not isinstance(target, str) or not target:
            raise ValueError(f"bad target: {target!r}")

    def _build_argv(self, scope: Mapping[str, Any]) -> list[str]:
        return ["echo", str(scope["target"])]

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        return []


def test_collect_reads_scope_runs_argv_list_and_yields_stdout() -> None:
    """collect() reads ``config["_scope"]``, calls the injected runner with an argv LIST, and yields
    one RawRecord of the canned stdout."""
    runner = _FakeRunner(b"echoed-output-bytes")
    tool = _EchoTool(runner=runner)

    records = list(tool.collect({"_scope": {"target": "hello-world"}}))

    assert len(records) == 1, f"collect must yield exactly one record, got {len(records)}"
    record = records[0]
    assert isinstance(record, RawRecord), f"collect must yield a RawRecord, got {type(record)!r}"
    assert record.data == b"echoed-output-bytes", "the RawRecord must carry the runner's stdout"

    assert len(runner.calls) == 1, "the runner must be invoked exactly once"
    argv = runner.calls[0]["argv"]
    # The headline no-shell invariant: argv is a LIST (exec), never a shell string.
    assert isinstance(argv, list), f"argv must be a list (no shell), got {type(argv)!r}: {argv!r}"
    assert not isinstance(argv, str), "argv must NOT be a shell string"
    assert argv == ["echo", "hello-world"], argv
    # A timeout bound is always passed (run_command is hard-bounded).
    assert isinstance(runner.calls[0]["timeout"], (int, float)), runner.calls[0]["timeout"]


def test_capability_is_active() -> None:
    """A CliToolConnector is ACTIVE-capability — the gate the cadence driver refuses."""
    tool = _EchoTool(runner=_FakeRunner(b""))
    assert tool.manifest.capability is Capability.ACTIVE


def test_hostile_target_is_refused_before_the_runner_runs() -> None:
    """A target the subclass hook rejects never reaches the runner — collect refuses and the
    (would-be) subprocess is never invoked."""
    runner = _FakeRunner(b"should-never-run")
    tool = _EchoTool(runner=runner)

    with pytest.raises(ValueError):
        # collect is a generator: validation fires on iteration.
        list(tool.collect({"_scope": {"target": ""}}))

    assert runner.calls == [], "a rejected target must NOT reach the runner"
