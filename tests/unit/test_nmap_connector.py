"""Gate 3h — nmap connector: ACTIVE / container-sandbox + argv-safe + fail-soft map (ADR 0072 §5).

nmap is the heavy, container-gated ACTIVE tool: it is BUILT (manifest, schema, argv, map all tested)
but its EXECUTION is refused in v1 (the sandbox gate — see ``tests/integration/
test_active_run_sandbox_ui.py``). This file pins the connector's static contract, independent of any
runner (NO real ``nmap`` binary, no network, no execution):

* ``manifest.capability is Capability.ACTIVE`` AND ``connector.sandbox == "container"`` (the heavy
  tool the runner refuses until ``container_sandbox_enabled`` flips);
* ``_build_argv({"target": t})`` is a LIST starting ``["nmap", ...]`` with the ``--`` terminator
  and the target a single element right after it (an argv-safe, no-shell command);
* ``map()`` is FAIL-SOFT: a sample nmap-XML body never raises (a thin entity, or ``[]``); garbage
  yields ``[]``;
* ``_validate_target`` (inherited from ``CliToolConnector``) refuses a hostile target.

LOCKED ASSUMPTIONS the builder MUST match:

  ``from worldmonitor.plugins.connectors.nmap.connector import NmapConnector``
  ``NmapConnector(CliToolConnector)`` — manifest ``connector_id="nmap"``, ``capability=ACTIVE``,
        class attr ``sandbox="container"``; ``_build_argv`` -> ``["nmap", "-oX", "-", "--", t]``
        style (list, ``--`` terminator, the target the single positional after ``--``); fail-soft.

RED on the base tree: ``worldmonitor.plugins.connectors.nmap`` is absent (ModuleNotFoundError).
"""

from __future__ import annotations

import pytest

from worldmonitor.plugins.base import Capability, RawRecord
from worldmonitor.plugins.connectors.nmap.connector import NmapConnector
from worldmonitor.provenance.model import Provenance, get_provenance

# A minimal ``nmap -oX -`` body (one open tcp/80 on one host).
_NMAP_XML = (
    b'<?xml version="1.0"?>\n'
    b"<nmaprun>"
    b'<host><address addr="93.184.216.34" addrtype="ipv4"/>'
    b'<ports><port protocol="tcp" portid="80"><state state="open"/></port></ports>'
    b"</host></nmaprun>"
)

# Non-XML garbage (hostile tool stdout): nothing parseable -> map() yields [].
_GARBAGE = b"\x00\x01\x02 not-xml-at-all 99999 \xfe\xff"


def _provenance() -> Provenance:
    return Provenance(
        source_id="nmap:example.com",
        retrieved_at="2026-06-28T00:00:00Z",
        reliability="B",
        source_record="s3://landing/nmap/example.com.json",
    )


def test_manifest_is_active_and_container_sandbox() -> None:
    """nmap is an ACTIVE connector that declares the ``container`` sandbox — the heavy-tool gate."""
    connector = NmapConnector()
    assert connector.manifest.connector_id == "nmap"
    assert connector.manifest.capability is Capability.ACTIVE
    assert connector.sandbox == "container", (
        f"nmap must declare sandbox='container', got {connector.sandbox!r}"
    )


def test_build_argv_is_list_with_flag_terminator() -> None:
    """``_build_argv`` is a LIST starting with ``nmap``, carries the ``--`` terminator, and puts the
    target as one element immediately after ``--`` (an argv-safe, no-shell command)."""
    connector = NmapConnector()
    argv = connector._build_argv({"target": "example.com"})

    assert isinstance(argv, list), f"argv must be a list, got {type(argv)!r}"
    assert argv[0] == "nmap", f"argv must start with 'nmap', got {argv!r}"
    assert "--" in argv, f"the '--' flag terminator must be present, got {argv!r}"
    idx = argv.index("--")
    assert argv[idx + 1] == "example.com", "the target must be one element right after '--'"


def test_map_sample_xml_is_fail_soft_with_provenance() -> None:
    """map() of a sample nmap-XML body never raises (>=0 entities); any entity it yields is a valid
    FtM entity carrying the stamped provenance (round-trip)."""
    connector = NmapConnector()
    provenance = _provenance()
    record = RawRecord(
        key="example.com",
        data=_NMAP_XML,
        retrieved_at="2026-06-28T00:00:00Z",
        content_type="text/xml",
    )

    entities = list(connector.map(record, provenance=provenance))  # must not raise

    assert len(entities) >= 0  # the minimal map yields a thin entity OR [] — both are fail-soft
    for entity in entities:
        recovered = get_provenance(entity)
        assert recovered is not None, "a mapped nmap entity must carry provenance"
        assert recovered.source_id == provenance.source_id


def test_map_of_garbage_is_fail_soft_empty() -> None:
    """map() of non-XML garbage (hostile tool output) yields ``[]`` — fail-soft, never raises."""
    connector = NmapConnector()
    record = RawRecord(
        key="garbage",
        data=_GARBAGE,
        retrieved_at="2026-06-28T00:00:00Z",
        content_type="text/xml",
    )

    assert list(connector.map(record, provenance=_provenance())) == []


@pytest.mark.parametrize("hostile", ["-x", "..", "; rm", "a b", "`id`"])
def test_validate_target_rejects_hostile(hostile: str) -> None:
    """nmap inherits the shared hardened validator: a hostile target is refused (ValueError)."""
    with pytest.raises(ValueError):
        NmapConnector()._validate_target(hostile)
