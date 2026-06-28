"""Gate 3g — whois connector: the ARG-INJECTION-SAFETY oracle (ADR 0071 §5/§6, the headline).

whois is the first ACTIVE connector. It is also the active-execution security boundary, so this
file is deliberately rigorous on the one threat that matters: a hostile scope target must NEVER
reach the subprocess as a flag or a shell command. The contract (failing-test-first list, ADR 0071
§Invariant gate note (d)):

* ``manifest.capability is Capability.ACTIVE`` (the gate);
* ``_validate_target`` REJECTS hostile targets (``-oX …``, ``; rm``, ``$(…)``, whitespace, ``..``,
  backticks, newlines, a LEADING ``-``, empty) with ``ValueError`` and ACCEPTS a plain domain / IP;
* a hostile target is refused BEFORE the runner runs (it never becomes an argv element);
* ``_build_argv({"target": t})`` is EXACTLY ``["whois", "--", t]`` — a LIST, with the ``--`` flag
  terminator present so even a bypassed validation can't turn the target into a flag;
* ``collect`` over a fake runner yields one ``RawRecord`` of the captured stdout;
* ``map`` is fail-soft: a real whois block -> one FtM ``Organization`` (+ provenance round-trip),
  garbage -> ``[]``.

LOCKED ASSUMPTIONS the builder MUST match:

  ``from worldmonitor.plugins.connectors.whois.connector import WhoisConnector``
  ``WhoisConnector(CliToolConnector)`` — manifest ``connector_id="whois"``, ``capability=ACTIVE``;
  accepted-target shape ``^[A-Za-z0-9.:-]+$`` AND not startswith("-") AND no whitespace;
  ``_build_argv({"target": t}) == ["whois", "--", t]``; ``map`` parses the registrant Organization.

RED on the base tree: ``worldmonitor.plugins.connectors.whois`` is absent (ModuleNotFoundError).
"""

from __future__ import annotations

from typing import Any

import pytest

from worldmonitor.plugins.base import Capability, RawRecord
from worldmonitor.plugins.connectors.whois.connector import WhoisConnector
from worldmonitor.provenance.model import Provenance, get_provenance
from worldmonitor.runner.subprocess import RunResult

# A canonical whois block with a clear registrant Organization line for map() to parse.
_WHOIS_STDOUT = (
    b"Domain Name: EXAMPLE.COM\n"
    b"Registry Domain ID: 2336799_DOMAIN_COM-VRSN\n"
    b"Registrar: Example Registrar, Inc.\n"
    b"Registrant Organization: Example Holdings LLC\n"
    b"Registrant Country: US\n"
)

# The hostile set the boundary MUST refuse (flag injection, shell metachars, whitespace, traversal,
# a leading dash that still matches the charset, an empty target).
_HOSTILE_TARGETS = [
    "-oX /tmp/x",
    "; rm -rf /",
    "$(id)",
    "a b",
    "../etc/passwd",
    "`id`",
    "a\nb",
    "-h",
    "",
]

# Plain domains / IPs the boundary MUST accept.
_VALID_TARGETS = ["example.com", "192.0.2.1", "sub.example.co.uk"]


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


def test_manifest_capability_is_active() -> None:
    """whois is ACTIVE-capability (the cadence driver refuses it; only the operator path runs)."""
    connector = WhoisConnector()
    assert connector.manifest.connector_id == "whois"
    assert connector.manifest.capability is Capability.ACTIVE


@pytest.mark.parametrize("hostile", _HOSTILE_TARGETS)
def test_validate_target_rejects_hostile(hostile: str) -> None:
    """Every hostile target is refused by ``_validate_target`` (ValueError). This is the
    arg-injection boundary: a flag (``-oX``/``-h``), shell metachars (``;`` ``$()`` backticks),
    whitespace, traversal (``..``), a newline, and an empty string are all rejected."""
    connector = WhoisConnector()
    with pytest.raises(ValueError):
        connector._validate_target(hostile)


@pytest.mark.parametrize("valid", _VALID_TARGETS)
def test_validate_target_accepts_plain_domain_or_ip(valid: str) -> None:
    """A plain domain or IP is accepted (``_validate_target`` returns without raising)."""
    connector = WhoisConnector()
    connector._validate_target(valid)  # must not raise


def test_build_argv_is_exact_list_with_flag_terminator() -> None:
    """``_build_argv`` is EXACTLY ``["whois", "--", target]`` — a list, the ``--`` terminator
    present, the target a single element after it (never spliced into a flag or a shell string)."""
    connector = WhoisConnector()
    argv = connector._build_argv({"target": "example.com"})

    assert isinstance(argv, list), f"argv must be a list, got {type(argv)!r}"
    assert argv == ["whois", "--", "example.com"], argv
    assert argv[1] == "--", "the '--' flag terminator must precede the target"
    assert argv[2] == "example.com", "the target must be exactly one element after '--'"


@pytest.mark.parametrize("hostile", _HOSTILE_TARGETS)
def test_collect_refuses_hostile_target_before_running(hostile: str) -> None:
    """collect() over a hostile scope refuses (ValueError) and NEVER invokes the runner — a hostile
    target cannot reach the subprocess at all."""
    runner = _FakeRunner(_WHOIS_STDOUT)
    connector = WhoisConnector(runner=runner)

    with pytest.raises(ValueError):
        list(connector.collect({"_scope": {"target": hostile}}))

    assert runner.calls == [], f"a hostile target ({hostile!r}) must NOT reach the runner"


def test_collect_runs_whois_and_yields_one_record() -> None:
    """collect() over a valid scope calls the runner with the exact argv and yields one RawRecord of
    the captured stdout."""
    runner = _FakeRunner(_WHOIS_STDOUT)
    connector = WhoisConnector(runner=runner)

    records = list(connector.collect({"_scope": {"target": "example.com"}}))

    assert len(records) == 1, f"collect must yield exactly one record, got {len(records)}"
    assert records[0].data == _WHOIS_STDOUT, "the record must carry the runner's stdout verbatim"
    assert len(runner.calls) == 1
    assert runner.calls[0]["argv"] == ["whois", "--", "example.com"], runner.calls[0]["argv"]


def test_map_parses_registrant_organization_with_provenance() -> None:
    """map() of a real whois block -> exactly one FtM Organization (the registrant) carrying the
    provenance it was stamped with (round-trip)."""
    connector = WhoisConnector()
    provenance = Provenance(
        source_id="whois:example.com",
        retrieved_at="2026-06-28T00:00:00Z",
        reliability="B",
        source_record="s3://landing/whois/example.com.json",
    )
    record = RawRecord(
        key="example.com",
        data=_WHOIS_STDOUT,
        retrieved_at="2026-06-28T00:00:00Z",
        content_type="text/plain",
    )

    entities = list(connector.map(record, provenance=provenance))

    assert len(entities) == 1, f"map must yield exactly one Organization, got {len(entities)}"
    org = entities[0]
    assert org.schema.name == "Organization", (
        f"the registrant must be an Organization, got {org.schema.name}"
    )
    assert "Example Holdings LLC" in org.get("name"), org.get("name")

    recovered = get_provenance(org)
    assert recovered is not None, "the mapped Organization must carry provenance"
    assert recovered.source_id == provenance.source_id
    assert recovered.source_record == provenance.source_record


def test_map_of_garbage_is_fail_soft_empty() -> None:
    """map() of non-whois garbage (hostile tool output) yields ``[]`` — fail-soft, never raises."""
    connector = WhoisConnector()
    provenance = Provenance(
        source_id="whois:garbage",
        retrieved_at="2026-06-28T00:00:00Z",
        reliability="B",
        source_record="s3://landing/whois/garbage.json",
    )
    record = RawRecord(
        key="garbage",
        data=b"\x00\x01 this is not a whois response, just random bytes 12345",
        retrieved_at="2026-06-28T00:00:00Z",
        content_type="text/plain",
    )

    assert list(connector.map(record, provenance=provenance)) == []
