"""Primary invariant tests (RED) for the `feodo` connector (Gate S-2, ADR 0118).

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/feodo/`` (an ``EXTERNAL_IMPORT`` / ``PASSIVE`` connector
mirroring the ``mitre_attack`` package shape):

* MANIFEST: ``connector_id="feodo"``, ``kind=CONNECTOR``, ``mode=EXTERNAL_IMPORT``,
  ``capability=PASSIVE``, ``status=IMPLEMENTED``.
* CONFIG SCHEMA: an empty config validates (``url``/``limit`` both optional, pinned default url);
  ``additionalProperties: false`` rejects a smuggled extra key.
* ``collect()``: over an in-test 4-entry Feodo ipblocklist JSON array (REAL field names, verified
  against the live feed 2026-07-18 — see below) served via ``httpx.MockTransport`` (NO live HTTP),
  yields ONE ``RawRecord`` PER ENTRY — Feodo's feed has no revoked/deprecated eligibility concept
  (unlike ``mitre_attack``), so ``collect()`` does NOT filter; malformed-entry rejection is entirely
  ``map()``'s job (pinned separately below). ``limit`` is honored (hard cap, feed order).
* ``map()``: one entry -> ONE FtM ``Indicator``: deterministic ``id=indicator_id(value)`` (the
  shared ``ioc-<sha1>`` scheme, S-2b) where
  ``value=f"{ip_address}:{port}"`` (the sha1 rule is computed INDEPENDENTLY here via stdlib
  ``hashlib`` — the connector's own oracle, not borrowed from its implementation),
  ``name``/``indicatorValue`` = the same ``value``, ``indicatorType=["ipv4"]``,
  ``malwareFamily=[malware]`` when the feed's ``malware`` field is present, ``firstSeenAt`` /
  ``lastSeenAt`` ISO-normalized from the feed's ``first_seen`` / ``last_online`` timestamps,
  ``datasets={"feodo"}``, provenance that round-trips via ``get_provenance``, and NO ``topics``,
  NO ``country`` (ASN geo is not an event location — keep Indicators off the dashboard globe). An
  entry with a blank/missing ``ip_address`` (malformed/garbage) maps to ``[]`` (fail-soft), never
  raising.

REAL FEODO FIELD NAMES (live-fetched 2026-07-18 from
https://feodotracker.abuse.ch/downloads/ipblocklist.json, bounded read, 200 OK): each entry is
a flat JSON object with keys ``ip_address`` (str), ``port`` (int), ``status`` (str,
online/offline), ``hostname`` (str or null), ``as_number`` (int), ``as_name`` (str), ``country``
(str, ISO-3166 alpha-2), ``first_seen`` (str, ``"YYYY-MM-DD HH:MM:SS"`` — SPACE-separated, not
ISO-T), ``last_online`` (str, ``"YYYY-MM-DD"`` — date-only, already ISO), ``malware`` (str,
absent on some entries — there is NO separate ``last_seen`` field in the real feed).

AMBIGUITY RESOLVED (test-author choice, spec is silent on the exact source-field mapping for
``lastSeenAt``): the real feed has no ``last_seen`` field, only ``last_online`` — the sole plausible
timestamp candidate — so ``lastSeenAt`` is pinned to source from ``last_online`` and ``firstSeenAt``
from ``first_seen``. ``registry.date.clean("2022-06-04 21:24:53") == "2022-06-04T21:24:53"`` and
``datetime.strptime(..., "%Y-%m-%d %H:%M:%S").isoformat()`` both independently normalize to the
exact SAME string for this space-separated shape (no fractional seconds/timezone in the source), so
pinning that exact ISO string is a fair, implementation-agnostic invariant, not an over-fit to one
normalization technique.

RED today: ``worldmonitor.ontology.ftm`` does not yet export ``register_wm_schemata`` (needed
because an Indicator must exist before ``map()`` can construct one) — the top-level
``from worldmonitor.ontology.ftm import ... register_wm_schemata`` raises ``ImportError`` and the
whole module errors at collection — the correct RED. GREEN once the builder lands
``ontology/ftm.py::register_wm_schemata`` (ADR 0118 D1) AND
``src/worldmonitor/plugins/connectors/feodo/`` (ADR 0118 D2).

No live network: ``httpx.MockTransport`` is injected through the connector ``transport=`` ctor
kwarg (mirrors ``MitreAttackConnector``/``tests/unit/test_mitre_attack_connector.py``), and
``socket.getaddrinfo`` is monkeypatched so the SSRF host check runs with no real DNS.
"""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Callable
from typing import Any

import httpx
import jsonschema
import pytest

from worldmonitor.ontology.ftm import register_wm_schemata
from worldmonitor.plugins.base import Capability, Kind, Mode, RawRecord, Status

# Top-level import of the not-yet-built connector — ModuleNotFoundError today (would be the
# correct RED too, but `register_wm_schemata` above already fails first — same RED family).
from worldmonitor.plugins.connectors.feodo import FeodoConnector
from worldmonitor.provenance.model import Provenance, get_provenance

register_wm_schemata()  # Indicator must exist before map() can construct one.

_TEST_URL = "https://feodotracker.abuse.ch/downloads/test-ipblocklist.json"

_PROV = Provenance(
    source_id="feodo:ipblocklist",
    retrieved_at="2026-07-18T00:00:00Z",
    reliability="B",
    source_record="s3://landing/feodo/ipblocklist-20260718.json",
)

# --------------------------------------------------------------------------------------------------
# A 4-entry hermetic sample using the REAL field names (see module docstring): 2 valid entries
# with a malware family + timestamps, 1 valid entry with NO malware field, 1 malformed/garbage
# entry (blank ip_address — the identity field a real Indicator cannot be built without).
# --------------------------------------------------------------------------------------------------

_ENTRY_EMOTET: dict[str, Any] = {
    "ip_address": "162.243.103.246",
    "port": 8080,
    "status": "offline",
    "hostname": None,
    "as_number": 14061,
    "as_name": "DIGITALOCEAN-ASN",
    "country": "US",
    "first_seen": "2022-06-04 21:24:53",
    "last_online": "2026-03-07",
    "malware": "Emotet",
}
_ENTRY_QAKBOT: dict[str, Any] = {
    "ip_address": "50.16.16.211",
    "port": 443,
    "status": "online",
    "hostname": "ec2-50-16-16-211.compute-1.amazonaws.com",
    "as_number": 14618,
    "as_name": "AMAZON-AES",
    "country": "US",
    "first_seen": "2025-12-30 13:56:31",
    "last_online": "2026-03-12",
    "malware": "QakBot",
}
_ENTRY_NO_MALWARE: dict[str, Any] = {
    "ip_address": "27.133.154.218",
    "port": 443,
    "status": "offline",
    "hostname": None,
    "as_number": 9370,
    "as_name": "SAKURA-B SAKURA Internet Inc.",
    "country": "JP",
    "first_seen": "2026-03-04 14:28:39",
    "last_online": "2026-03-05",
    # no "malware" key at all — a legitimate entry Feodo just hasn't attributed yet.
}
_ENTRY_MALFORMED: dict[str, Any] = {
    "ip_address": "",
    "port": None,
    "status": "offline",
    "hostname": None,
    "as_number": 0,
    "as_name": "",
    "country": "",
    "first_seen": "not-a-real-timestamp",
    "last_online": "also-not-a-date",
    "malware": 12345,  # wrong type too — never a usable value even if ip_address were present
}

_FEED: list[dict[str, Any]] = [
    _ENTRY_EMOTET,
    _ENTRY_QAKBOT,
    _ENTRY_NO_MALWARE,
    _ENTRY_MALFORMED,
]


def _sha1_id(value: str) -> str:
    """The SHARED id rule (ADR 0118's executed precondition — connector-independent
    ``ioc-<sha1(strip+casefold(value))>``), computed INDEPENDENTLY (stdlib hashlib) — the
    oracle for `map()`'s output, never borrowed from the implementation. PIN MOVED
    (S-2b, pre-first-deployment): the original gate pinned ``feodo-<sha1>``; the checker
    finding carried in ADR 0118 requires all Indicator connectors to share one scheme, and
    executing it before any deployment mints ids made the move a rename, not a migration."""
    return f"ioc-{hashlib.sha1(value.strip().casefold().encode('utf-8')).hexdigest()}"


# --------------------------------------------------------------------------------------------------
# Hermetic-HTTP helpers (mirror tests/unit/test_mitre_attack_connector.py / test_feed_connector.py):
# a fake getaddrinfo so the SSRF guard resolves the feed host with NO real DNS, plus a MockTransport
# serving the feed bytes.
# --------------------------------------------------------------------------------------------------


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in resolving EVERY host to ``ip`` (one IPv4 5-tuple)."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def _feed_transport(
    entries: list[dict[str, Any]], calls: list[httpx.Request]
) -> httpx.MockTransport:
    """Serve ``entries`` (JSON array, verbatim) for any request; record every request."""
    body = json.dumps(entries).encode("utf-8")

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    return httpx.MockTransport(_handler)


def _record(entry: dict[str, Any], *, key: str = "test") -> RawRecord:
    """Wrap a raw Feodo entry dict as the JSON ``RawRecord`` that ``map()`` consumes."""
    return RawRecord(
        key=key,
        data=json.dumps(entry).encode("utf-8"),
        retrieved_at=_PROV.retrieved_at,
        content_type="application/json",
    )


# --------------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------------


def test_manifest_is_external_import_and_passive() -> None:
    manifest = FeodoConnector().manifest
    assert manifest.connector_id == "feodo"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — an empty config validates (built-in pinned defaults); the schema is closed.
# --------------------------------------------------------------------------------------------------


def test_config_schema_validates_default_and_rejects_additional_properties() -> None:
    connector = FeodoConnector()
    schema = connector.config_schema
    assert schema["additionalProperties"] is False

    connector.validate_config({})  # must not raise: url/limit both fall back to pinned defaults
    connector.validate_config({"url": _TEST_URL, "limit": 3})  # explicit override must not raise
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"url": _TEST_URL, "bogus": 1})


# --------------------------------------------------------------------------------------------------
# collect() — one RawRecord per FEED ENTRY (no eligibility filtering — unlike mitre_attack, the
# Feodo feed has no revoked/deprecated concept); limit honored.
# --------------------------------------------------------------------------------------------------


def test_collect_yields_one_record_per_entry_unfiltered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = FeodoConnector(transport=_feed_transport(_FEED, calls))

    records = list(connector.collect({"url": _TEST_URL}))

    assert calls, "collect() never issued a request through the transport"
    assert len(records) == 4, "collect() must yield ONE RawRecord per feed entry, unfiltered"
    keys = {r.key for r in records}
    assert {"162.243.103.246:8080", "50.16.16.211:443", "27.133.154.218:443"} <= keys, (
        f"expected the three well-formed ip:port keys among {keys!r}"
    )


def test_collect_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = FeodoConnector(transport=_feed_transport(_FEED, calls))

    records = list(connector.collect({"url": _TEST_URL, "limit": 2}))

    assert len(records) == 2
    assert records[0].key == "162.243.103.246:8080"  # feed order — the FIRST entry
    assert records[1].key == "50.16.16.211:443"  # feed order — the SECOND entry


# --------------------------------------------------------------------------------------------------
# map() — FtM Indicator: deterministic sha1 id, malware/timestamps, no topics/country.
# --------------------------------------------------------------------------------------------------


def test_map_entry_with_malware_emits_indicator_with_all_fields() -> None:
    connector = FeodoConnector()
    record = _record(_ENTRY_EMOTET, key="162.243.103.246:8080")

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]

    value = "162.243.103.246:8080"
    assert entity.schema.name == "Indicator"
    assert entity.id == _sha1_id(value)  # deterministic, sha1(ip:port)-derived

    assert entity.get("name") == [value]
    assert entity.get("indicatorValue") == [value]
    assert entity.get("indicatorType") == ["ipv4"]
    assert entity.get("malwareFamily") == ["Emotet"]

    # ISO-normalized timestamps (see module docstring's ambiguity resolution).
    assert entity.get("firstSeenAt") == ["2022-06-04T21:24:53"]
    assert entity.get("lastSeenAt") == ["2026-03-07"]

    assert entity.datasets == {"feodo"}

    # No topics, no country — ASN geo is not an event location (off the dashboard globe).
    assert entity.get("topics", quiet=True) == []
    assert entity.get("country", quiet=True) == []

    # Provenance round-trips intact (the non-negotiable invariant).
    prov = get_provenance(entity)
    assert prov is not None
    assert prov == _PROV
    assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])

    # Deterministic: re-mapping the same record yields the same id (idempotent re-ingest).
    again = list(connector.map(record, provenance=_PROV))
    assert again[0].id == entity.id


def test_map_second_malware_entry_has_distinct_deterministic_id() -> None:
    connector = FeodoConnector()
    record = _record(_ENTRY_QAKBOT, key="50.16.16.211:443")

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    value = "50.16.16.211:443"
    assert entity.id == _sha1_id(value)
    assert entity.id != _sha1_id("162.243.103.246:8080")  # distinct IOC values, distinct ids
    assert entity.get("malwareFamily") == ["QakBot"]
    assert entity.get("firstSeenAt") == ["2025-12-30T13:56:31"]
    assert entity.get("lastSeenAt") == ["2026-03-12"]


def test_map_entry_without_malware_field_omits_malware_family() -> None:
    """The `malware` key is entirely absent — a legitimate entry, just no attribution yet."""
    connector = FeodoConnector()
    record = _record(_ENTRY_NO_MALWARE, key="27.133.154.218:443")

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    value = "27.133.154.218:443"
    assert entity.id == _sha1_id(value)
    assert entity.get("indicatorValue") == [value]
    assert entity.get("malwareFamily", quiet=True) == []
    assert entity.get("firstSeenAt") == ["2026-03-04T14:28:39"]
    assert entity.get("lastSeenAt") == ["2026-03-05"]


def test_map_malformed_entry_returns_empty_without_raising() -> None:
    """A blank ip_address (no usable identity) -> [] (fail-soft), never raises."""
    connector = FeodoConnector()
    record = _record(_ENTRY_MALFORMED, key="malformed")

    assert list(connector.map(record, provenance=_PROV)) == []


def test_map_missing_ip_address_key_entirely_returns_empty() -> None:
    """A record missing the `ip_address` key altogether (not just blank) -> [] (fail-soft)."""
    garbage: dict[str, Any] = {"port": 443, "status": "online", "malware": "Test"}
    connector = FeodoConnector()
    assert list(connector.map(_record(garbage), provenance=_PROV)) == []
