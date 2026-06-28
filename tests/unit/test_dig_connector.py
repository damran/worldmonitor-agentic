"""Gate 3h — dig connector: ACTIVE / subprocess-sandbox + argv-safe + fail-soft map (ADR 0072 §4).

dig is the second subprocess ACTIVE CLI tool (read-only DNS). This file pins its contract,
independent of the runner (NO real ``dig`` binary, no network):

* ``manifest.capability is Capability.ACTIVE`` AND ``connector.sandbox == "subprocess"`` (it runs
  via the subprocess seam — it is NOT the container-gated heavy tool);
* ``_build_argv({"target": t})`` is a LIST starting ``["dig", ...]`` with the ``--`` flag terminator
  present and the target a single element right after it (never spliced into a flag / shell string);
* ``collect()`` over a fake runner yields exactly one ``RawRecord`` of the captured stdout;
* ``map()`` is FAIL-SOFT: a sample ``dig +short`` body never raises (it yields a thin FtM entity
  with provenance, or ``[]``); garbage yields ``[]``;
* ``_validate_target`` (inherited from ``CliToolConnector``) refuses a hostile target.

LOCKED ASSUMPTIONS the builder MUST match:

  ``from worldmonitor.plugins.connectors.dig.connector import DigConnector``
  ``DigConnector(CliToolConnector)`` — manifest ``connector_id="dig"``, ``capability=ACTIVE``,
        class attr ``sandbox="subprocess"``; ``_build_argv`` -> ``["dig", "+short", ..., "--", t]``
        (list, ``--`` terminator, the target the single positional after ``--``); ``map`` fail-soft.

RED on the base tree: ``worldmonitor.plugins.connectors.dig`` is absent (ModuleNotFoundError).
"""

from __future__ import annotations

from typing import Any

import pytest

from worldmonitor.plugins.base import Capability, RawRecord
from worldmonitor.plugins.connectors.dig.connector import DigConnector
from worldmonitor.provenance.model import Provenance, get_provenance
from worldmonitor.runner.subprocess import RunResult

# A canonical ``dig +short example.com`` body: one A record per line.
_DIG_SHORT_STDOUT = b"93.184.216.34\n93.184.216.35\n"

# Non-DNS garbage (hostile tool stdout): no dotted addresses / parseable answer -> map() -> [].
_GARBAGE = b"\x00\x01\x02\x03 GARBAGE-NO-DOTS-HERE 42 \xfe\xff"


class _FakeRunner:
    """An injectable ``run_command``-compatible async callable recording argv; canned stdout."""

    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv: Any, *, timeout: float, **kwargs: Any) -> RunResult:
        self.calls.append({"argv": argv, "timeout": timeout})
        return RunResult(
            returncode=0, stdout=self._stdout, stderr=b"", timed_out=False, duration=0.0
        )


def _provenance() -> Provenance:
    return Provenance(
        source_id="dig:example.com",
        retrieved_at="2026-06-28T00:00:00Z",
        reliability="B",
        source_record="s3://landing/dig/example.com.json",
    )


def test_manifest_is_active_and_subprocess_sandbox() -> None:
    """dig is an ACTIVE connector that runs in the ``subprocess`` sandbox (NOT container-gated)."""
    connector = DigConnector()
    assert connector.manifest.connector_id == "dig"
    assert connector.manifest.capability is Capability.ACTIVE
    assert connector.sandbox == "subprocess", (
        f"dig must declare sandbox='subprocess', got {connector.sandbox!r}"
    )


def test_build_argv_is_list_with_flag_terminator() -> None:
    """``_build_argv`` is a LIST starting with ``dig``, carries the ``--`` terminator, and puts the
    target as exactly one element immediately after ``--`` (never a flag / shell string)."""
    connector = DigConnector()
    argv = connector._build_argv({"target": "example.com"})

    assert isinstance(argv, list), f"argv must be a list, got {type(argv)!r}"
    assert argv[0] == "dig", f"argv must start with 'dig', got {argv!r}"
    assert "--" in argv, f"the '--' flag terminator must be present, got {argv!r}"
    idx = argv.index("--")
    assert argv[idx + 1] == "example.com", "the target must be one element right after '--'"
    assert argv.count("example.com") == 1, "the target must appear exactly once"


def test_collect_yields_one_record() -> None:
    """collect() over a fake runner yields exactly one RawRecord of the captured stdout; the runner
    is invoked once with the argv LIST (the no-shell invariant)."""
    runner = _FakeRunner(_DIG_SHORT_STDOUT)
    connector = DigConnector(runner=runner)

    records = list(connector.collect({"_scope": {"target": "example.com"}}))

    assert len(records) == 1, f"collect must yield exactly one record, got {len(records)}"
    assert records[0].data == _DIG_SHORT_STDOUT, (
        "the record must carry the runner's stdout verbatim"
    )
    assert len(runner.calls) == 1, "the runner must be invoked exactly once"
    argv = runner.calls[0]["argv"]
    assert isinstance(argv, list), f"argv must be a list (no shell), got {type(argv)!r}"
    assert argv[0] == "dig" and "--" in argv, argv


def test_map_sample_output_is_fail_soft_with_provenance() -> None:
    """map() of a sample ``dig +short`` body never raises (>=0 entities); any entity it yields is a
    valid FtM entity carrying the stamped provenance (round-trip)."""
    connector = DigConnector()
    provenance = _provenance()
    record = RawRecord(
        key="example.com",
        data=_DIG_SHORT_STDOUT,
        retrieved_at="2026-06-28T00:00:00Z",
        content_type="text/plain",
    )

    entities = list(connector.map(record, provenance=provenance))  # must not raise

    assert len(entities) >= 0  # the minimal map yields a thin entity OR [] — both are fail-soft
    for entity in entities:
        recovered = get_provenance(entity)
        assert recovered is not None, "a mapped dig entity must carry provenance"
        assert recovered.source_id == provenance.source_id
        assert recovered.source_record == provenance.source_record


def test_map_of_garbage_is_fail_soft_empty() -> None:
    """map() of non-DNS garbage (hostile tool output) yields ``[]`` — fail-soft, never raises."""
    connector = DigConnector()
    record = RawRecord(
        key="garbage",
        data=_GARBAGE,
        retrieved_at="2026-06-28T00:00:00Z",
        content_type="text/plain",
    )

    assert list(connector.map(record, provenance=_provenance())) == []


@pytest.mark.parametrize("hostile", ["-x", "..", "; rm", "a b", "$(id)"])
def test_validate_target_rejects_hostile(hostile: str) -> None:
    """dig inherits the shared hardened validator: a hostile target is refused (ValueError)."""
    with pytest.raises(ValueError):
        DigConnector()._validate_target(hostile)
