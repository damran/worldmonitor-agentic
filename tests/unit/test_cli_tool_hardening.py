"""Gate 3h — CliToolConnector hardening: the shared validator + enforced allowlist (ADR 0072).

6b promotes the strict target validator to ``CliToolConnector._validate_target`` (the default,
inherited by whois/dig) and ADDS an ENFORCED ``allowed_targets`` allowlist to ``collect()``. This
file pins both BASE invariants over a tiny in-test subclass (NO real whois/dig/nmap, no network):

* SHARED VALIDATOR (ADR 0072 §3) — the inherited ``_validate_target`` rejects the hardened set
  (bare ``..``, a ``>253``-char string, a leading ``-``, whitespace, shell metachars, a newline)
  and accepts a plain domain / IP. The subclass below deliberately does NOT override
  ``_validate_target`` — it MUST inherit the shared base validator.
* ENFORCED ALLOWLIST (ADR 0072 §2) — ``collect()`` checks the (already-validated) scope target
  against ``config.get("allowed_targets")``: a non-empty list that does NOT contain the target
  (exact match) -> ``ValueError`` BEFORE the runner is ever invoked (0 calls). An empty / absent
  allowlist admits any valid target (the per-run scope token stays the primary authorization).

LOCKED ASSUMPTIONS the builder MUST match so this oracle stays meaningful:

  ``CliToolConnector._validate_target`` is CONCRETE (no longer abstract) — the strict 6a rules
        PLUS rejecting any ``..`` substring and ``len(target) > 253``; subclasses inherit it.
  ``CliToolConnector.collect`` enforces a top-level ``allowed_targets`` config key (a list of str);
        a non-empty list refuses an out-of-list target (exact match) with ``ValueError`` before the
        runner. The allowlist check runs AFTER ``_validate_target`` and BEFORE building the argv.

RED on the base tree: ``CliToolConnector._validate_target`` is still ``@abstractmethod`` (so the
subclass below cannot be instantiated) and ``collect()`` enforces no allowlist.
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
    """An injectable ``run_command``-compatible async callable recording argv; canned stdout."""

    def __init__(self, stdout: bytes = b"out") -> None:
        self._stdout = stdout
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv: Any, *, timeout: float, **kwargs: Any) -> RunResult:
        self.calls.append({"argv": argv, "timeout": timeout})
        return RunResult(
            returncode=0, stdout=self._stdout, stderr=b"", timed_out=False, duration=0.0
        )


class _HardenedTool(CliToolConnector):
    """A minimal in-test ACTIVE CLI tool that does NOT override ``_validate_target`` — it inherits
    the shared, hardened base validator. A permissive object schema so any config validates."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="hardened-tool",
            name="Hardened Tool",
            version="0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.ACTIVE,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def _build_argv(self, scope: Mapping[str, Any]) -> list[str]:
        return ["tool", "--", str(scope["target"])]

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        return []


# The hardened set the SHARED validator MUST refuse: a bare ``..``, an embedded ``..`` (with a path
# separator), a >253-char string (two flavours — slash-joined and pure-charset so the length rule is
# pinned unambiguously), a leading-dash flag, whitespace, shell metachars, a newline, and the empty
# string (the ``+`` quantifier rejects it).
_REJECTED = [
    "..",
    "a/../b",
    "/".join(["a"] * 200),
    "a" * 254,
    "-x",
    "a b",
    "; rm",
    "$(id)",
    "`id`",
    "a\nb",
    "",
]

# Plain domains / IPs the validator MUST accept.
_ACCEPTED = ["example.com", "192.0.2.1"]


@pytest.mark.parametrize("hostile", _REJECTED)
def test_shared_validator_rejects_hardened_set(hostile: str) -> None:
    """The inherited ``_validate_target`` refuses every hardened-set target with ``ValueError`` —
    closing the bare-``..`` and over-length gaps the 6a checker flagged (ADR 0072 §3)."""
    tool = _HardenedTool(runner=_FakeRunner())
    with pytest.raises(ValueError):
        tool._validate_target(hostile)


@pytest.mark.parametrize("good", _ACCEPTED)
def test_shared_validator_accepts_plain_domain_or_ip(good: str) -> None:
    """A plain domain or IP is accepted by the inherited validator (returns without raising)."""
    tool = _HardenedTool(runner=_FakeRunner())
    tool._validate_target(good)  # must not raise


def test_allowlist_refuses_out_of_list_target_before_runner() -> None:
    """With a non-empty ``allowed_targets``, a (validly-shaped) target NOT in the list is refused
    with ``ValueError`` BEFORE the runner — the heavy-tool is never invoked (ADR 0072 §2)."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    with pytest.raises(ValueError):
        list(tool.collect({"allowed_targets": ["good.com"], "_scope": {"target": "evil.com"}}))

    assert runner.calls == [], "an out-of-allowlist target must NOT reach the runner"


def test_allowlist_admits_in_list_target() -> None:
    """A target that IS in a non-empty ``allowed_targets`` runs (one record; the runner is invoked
    once with the argv list)."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    records = list(
        tool.collect({"allowed_targets": ["good.com"], "_scope": {"target": "good.com"}})
    )

    assert len(records) == 1, f"an in-list target must run, got {len(records)} record(s)"
    assert len(runner.calls) == 1, "the in-list target must invoke the runner exactly once"
    assert runner.calls[0]["argv"] == ["tool", "--", "good.com"], runner.calls[0]["argv"]


def test_absent_allowlist_admits_any_valid_target() -> None:
    """An ABSENT ``allowed_targets`` admits any valid target (the scope token stays the auth)."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    records = list(tool.collect({"_scope": {"target": "anything.com"}}))

    assert len(records) == 1, "an absent allowlist must admit any valid target"
    assert len(runner.calls) == 1, "an absent allowlist must let the runner run"


def test_empty_allowlist_admits_any_valid_target() -> None:
    """An EMPTY ``allowed_targets`` list is treated as 'no restriction' — any valid target runs."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    records = list(tool.collect({"allowed_targets": [], "_scope": {"target": "anything.com"}}))

    assert len(records) == 1, "an empty allowlist must admit any valid target"
    assert len(runner.calls) == 1, "an empty allowlist must let the runner run"
