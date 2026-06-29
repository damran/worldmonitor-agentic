"""Gate 3i — CliToolConnector: wildcard-subdomain allowlist extension (ADR 0082).

Extends ``collect()``'s ``allowed_targets`` enforcement to support ``*.<domain>`` entries so an
operator can allow a domain subtree without enumerating every host.

The dot-boundary anchoring is the LOAD-BEARING security invariant — every adversarial case below
MUST stay failing if the anchor is weakened:

* ``*.example.com`` MATCHES: strict subdomains, multi-level, case-insensitive.
* ``*.example.com`` does NOT match: apex, siblings (no-dot), suffix-spoofs.
* Exact entries keep EXACT-MATCH only (no implicit sub-domain expansion).
* Mixed lists behave per-entry.
* Empty / absent allowlist ⇒ any valid target (unchanged).
* Malformed ``*.`` / ``*.*.x`` ⇒ matches nothing — never a catch-all bypass.

Security invariant tests are split by category for maximum readability:
each ``@pytest.mark.parametrize`` batch covers exactly one adversarial axis.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord
from worldmonitor.plugins.cli_tool import CliToolConnector, _target_allowed
from worldmonitor.provenance.model import Provenance
from worldmonitor.runner.subprocess import RunResult

# ---------------------------------------------------------------------------
# Shared helpers (mirror the pattern in test_cli_tool_hardening.py)
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Canned-stdout async runner; records each call so tests can assert runner-never-invoked."""

    def __init__(self, stdout: bytes = b"ok") -> None:
        self._stdout = stdout
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv: Any, *, timeout: float, **_: Any) -> RunResult:
        self.calls.append({"argv": argv, "timeout": timeout})
        return RunResult(
            returncode=0, stdout=self._stdout, stderr=b"", timed_out=False, duration=0.0
        )


class _HardenedTool(CliToolConnector):
    """Minimal in-test ACTIVE tool; inherits the shared base validator (does NOT override it)."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="hardened-tool-wildcard",
            name="Hardened Tool Wildcard",
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


# ---------------------------------------------------------------------------
# § 1 — Pure helper: _target_allowed
# ---------------------------------------------------------------------------


# 1a. Wildcard MATCHES — strict subdomains (including multi-level and case-insensitive)
@pytest.mark.parametrize(
    "target",
    ["a.example.com", "a.b.example.com", "A.EXAMPLE.COM"],
)
def test_target_allowed_wildcard_matches_strict_subdomains(target: str) -> None:
    """``*.example.com`` matches strict subdomains: single-level, multi-level, case-insensitive."""
    assert _target_allowed(target, ["*.example.com"]), (
        f"*.example.com should match strict subdomain {target!r}"
    )


# 1b. Wildcard NON-MATCHES — the security invariants
@pytest.mark.parametrize(
    "target,reason",
    [
        ("example.com", "apex must not match"),
        ("evil-example.com", "sibling without dot boundary must not match"),
        ("notexample.com", "no dot boundary — suffix without leading dot"),
        ("xexample.com", "prefix extension, no dot boundary"),
        ("example.com.attacker.com", "suffix-spoof: <domain>.attacker.com"),
        ("example.comX", "trailing junk appended directly (no dot)"),
    ],
)
def test_target_allowed_wildcard_refuses_non_strict_subdomains(target: str, reason: str) -> None:
    """``*.example.com`` must NOT match non-strict-subdomain targets (dot-boundary invariant).

    This is the security load-bearing test — do NOT remove or weaken.
    """
    assert not _target_allowed(target, ["*.example.com"]), (
        f"*.example.com must NOT match {target!r}: {reason}"
    )


# 1c. Exact entry — EXACT-MATCH ONLY, no implicit sub-domain expansion
def test_target_allowed_exact_matches_only_exact() -> None:
    """An exact ``example.com`` entry matches ``example.com`` but not ``a.example.com``."""
    assert _target_allowed("example.com", ["example.com"])
    assert not _target_allowed("a.example.com", ["example.com"]), (
        "exact entry must NOT expand to subdomains"
    )


# 1d. Mixed list — per-entry semantics
def test_target_allowed_mixed_list_per_entry() -> None:
    """A mixed list ``["example.com", "*.test.org"]`` applies each rule independently."""
    allowed = ["example.com", "*.test.org"]

    assert _target_allowed("example.com", allowed), "exact apex must match the exact entry"
    assert _target_allowed("a.test.org", allowed), "subdomain must match the wildcard entry"
    assert _target_allowed("sub.sub.test.org", allowed), "multi-level subdomain must match wildcard"

    assert not _target_allowed("test.org", allowed), (
        "test.org apex must NOT match *.test.org (apex exclusion)"
    )
    assert not _target_allowed("evil.com", allowed), "unrelated domain must not match"
    assert not _target_allowed("a.example.com", allowed), (
        "subdomain of exact entry must not match (no implicit expansion)"
    )


# 1e. Empty allowlist
def test_target_allowed_empty_list_permits_any() -> None:
    """An empty allowed list means 'any valid target' — always True."""
    assert _target_allowed("anything.example.com", [])
    assert _target_allowed("192.0.2.1", [])


# 1f. Malformed wildcards — must match NOTHING (no catch-all bypass)
def test_target_allowed_malformed_empty_domain_matches_nothing() -> None:
    """``*.`` (empty domain after prefix) must match nothing — not a catch-all."""
    assert not _target_allowed("example.com", ["*."])
    assert not _target_allowed("anything.example.com", ["*."])


def test_target_allowed_malformed_nested_star_matches_nothing() -> None:
    """``*.*.x`` (nested wildcard in the domain part) must match nothing."""
    assert not _target_allowed("foo.x", ["*.*.x"])
    assert not _target_allowed("foo.bar.x", ["*.*.x"])


# 1g. Case-insensitive exact match
def test_target_allowed_exact_is_case_insensitive() -> None:
    """Exact entries are compared case-insensitively (DNS is case-insensitive)."""
    assert _target_allowed("EXAMPLE.COM", ["example.com"])
    assert _target_allowed("example.com", ["EXAMPLE.COM"])


# ---------------------------------------------------------------------------
# § 2 — Integration: wildcard enforcement through collect()
# ---------------------------------------------------------------------------


def test_collect_wildcard_allows_strict_subdomain() -> None:
    """collect() with ``*.example.com`` in allowed_targets accepts a strict subdomain."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    records = list(
        tool.collect(
            {"allowed_targets": ["*.example.com"], "_scope": {"target": "sub.example.com"}}
        )
    )

    assert len(records) == 1, "a strict subdomain matching a wildcard entry must yield a record"
    assert len(runner.calls) == 1, "the runner must be invoked exactly once"


def test_collect_wildcard_allows_multilevel_subdomain() -> None:
    """collect() with ``*.example.com`` accepts a multi-level strict subdomain."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    records = list(
        tool.collect(
            {"allowed_targets": ["*.example.com"], "_scope": {"target": "a.b.example.com"}}
        )
    )

    assert len(records) == 1
    assert len(runner.calls) == 1


def test_collect_wildcard_refuses_apex_before_runner() -> None:
    """collect() with ``*.example.com`` refuses the apex ``example.com`` BEFORE the runner.

    The runner MUST NOT be invoked when the apex is refused — this is the key invariant.
    """
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    with pytest.raises(ValueError, match="not in the configured allowed_targets"):
        list(
            tool.collect(
                {
                    "allowed_targets": ["*.example.com"],
                    "_scope": {"target": "example.com"},
                }
            )
        )

    assert runner.calls == [], "the runner MUST NOT be invoked when the apex is refused"


def test_collect_wildcard_refuses_sibling_before_runner() -> None:
    """collect() with ``*.example.com`` refuses a sibling (``evil-example.com``) before runner."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    with pytest.raises(ValueError):
        list(
            tool.collect(
                {
                    "allowed_targets": ["*.example.com"],
                    "_scope": {"target": "evil-example.com"},
                }
            )
        )

    assert runner.calls == [], "sibling target must not reach the runner"


def test_collect_wildcard_refuses_suffix_spoof_before_runner() -> None:
    """collect() with ``*.example.com`` refuses a suffix-spoof ``example.com.attacker.com``."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    with pytest.raises(ValueError):
        list(
            tool.collect(
                {
                    "allowed_targets": ["*.example.com"],
                    "_scope": {"target": "example.com.attacker.com"},
                }
            )
        )

    assert runner.calls == []


def test_collect_absent_allowlist_permits_any_valid_target() -> None:
    """Absent allowed_targets (None) permits any valid target — unchanged behavior."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    records = list(tool.collect({"_scope": {"target": "anything.com"}}))

    assert len(records) == 1
    assert len(runner.calls) == 1


def test_collect_exact_and_wildcard_coexist_in_list() -> None:
    """collect() with a mixed list ``["example.com", "*.test.org"]`` handles both correctly."""
    runner = _FakeRunner()
    tool = _HardenedTool(runner=runner)

    # Exact apex succeeds
    records = list(
        tool.collect(
            {
                "allowed_targets": ["example.com", "*.test.org"],
                "_scope": {"target": "example.com"},
            }
        )
    )
    assert len(records) == 1

    # Wildcard subdomain succeeds
    records = list(
        tool.collect(
            {
                "allowed_targets": ["example.com", "*.test.org"],
                "_scope": {"target": "a.test.org"},
            }
        )
    )
    assert len(records) == 1

    # test.org apex fails (neither exact entry, nor wildcard match)
    with pytest.raises(ValueError):
        list(
            tool.collect(
                {
                    "allowed_targets": ["example.com", "*.test.org"],
                    "_scope": {"target": "test.org"},
                }
            )
        )
