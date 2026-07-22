"""Primary invariant tests (RED) for the `threatfox` connector (Gate S-2 phase 2, slice B).

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/threatfox/`` (an ``EXTERNAL_IMPORT`` / ``PASSIVE`` connector
mirroring the ``feodo`` package shape — see ``docs/decisions/GATE_S2P2_ABUSECH_SIBLINGS_SPEC.md``
§5, the buildable spec companion to ADR 0119):

* MANIFEST: ``connector_id="threatfox"``, ``kind=CONNECTOR``, ``mode=EXTERNAL_IMPORT``,
  ``capability=PASSIVE``, ``status=IMPLEMENTED``.
* CONFIG SCHEMA: an empty config validates (``url``/``limit``/``auth_key`` all optional, pinned
  default url); ``additionalProperties: false`` rejects a smuggled key; ``auth_key`` carries
  ``"secret": true`` (opencorporates ``api_token`` precedent).
* ``collect()``: the body is a JSON **object** keyed by numeric IOC id, each value a **list** of
  record dicts (usually one element) — traverse ``{id: [record, ...]}``, yielding ONE ``RawRecord``
  per inner record (``data = json.dumps(record)``). A top-level value that is not a list, or a list
  element that is not a dict, is skipped (no yield, no raise). ``limit`` hard-caps the yield count
  in traversal order. The optional ``auth_key`` config is sent as the ``Auth-Key`` HTTP header when
  present, absent entirely when not configured. A 401/403 response raises loud with an ACTIONABLE
  message (naming ``Auth-Key``/``auth.abuse.ch``) rather than silently returning ``[]``.
* ``map()``: one record -> ONE FtM ``Indicator``: deterministic ``id=indicator_id(value)`` (the
  shared ``ioc-<sha1>`` scheme, S-2b — computed INDEPENDENTLY here via stdlib ``hashlib``, the
  connector's own oracle, never borrowed from its implementation) where ``value=ioc_value``;
  ``name``/``indicatorValue`` = the same ``value``; ``indicatorType`` per the shared vocabulary
  (``ip:port``->``ipv4`` — MUST equal feodo exactly; ``domain``->``domain``; ``url``->``url``;
  ``md5_hash``->``md5``; ``sha1_hash``->``sha1``; ``sha256_hash``->``sha256``; an unknown member,
  e.g. ``envelope_from``, passes through as ``lower(raw ioc_type)`` — the IOC is still emitted,
  never dropped; a MISSING ``ioc_type`` key emits no ``indicatorType`` rather than raising).
  ``malwareFamily`` = ``malware_printable`` only when it is a non-empty string AND the literal
  ``malware`` key is not ``"unknown"`` (and ``malware_printable`` is not literally ``"Unknown"``) —
  the REAL family ``unknown_stealer``/``"Unknown Stealer"`` is DISTINCT from the literal placeholder
  and KEEPS its family. ``firstSeenAt``/``lastSeenAt`` are added via ``entity.add()`` (FtM date
  cleaning) from ``first_seen_utc``/``last_seen_utc``; a ``null`` ``last_seen_utc`` is harmless (a
  no-op add, never raises). ``datasets={"threatfox"}``, provenance round-trips via
  ``get_provenance``, and NO ``topics``, NO ``country``, NO ``indicates`` edge (attribution is
  S-2 phase 3, out of scope for this gate). A record with a blank/missing ``ioc_value`` (the
  identity field) maps to ``[]`` (fail-soft), never raising.
* CROSS-CONNECTOR CONVERGENCE: a threatfox ``ip:port`` record and a feodo-shaped record carrying
  the identical ``ip:port`` value converge on the SAME entity id (the shared ``ontology.ioc``
  scheme) and both report ``indicatorType == ["ipv4"]`` — the load-bearing cross-connector vocab
  consistency pin (spec §3).
* SEED: ``db.seed.SEED_CONNECTORS`` carries a ``threatfox`` ``SeedSpec``, seeded ``enabled=True``,
  whose config ``url`` is the pinned default legacy anon export endpoint (spec §5) spelled out
  explicitly (ADR 0117 residual-c / feodo precedent) — this pins the builder's seed row and is
  RED TODAY against EXISTING code (``worldmonitor.db.seed`` already exists; no threatfox row in it
  yet — verified via ``uv run python -c "from worldmonitor.db.seed import SEED_CONNECTORS; ..."``).

FIXTURE RECORDS — anonymized shapes based on the REAL live-probed 2026-07-22 ThreatFox export
(field names/formats/null-patterns preserved EXACTLY; domain/URL values anonymized to
``*.example`` forms; the IP:port record's IP is kept from the launching agent's verified probe
since an IP literal carries no PII the way a domain/URL does). Hostile variants (non-list
top-level value, non-dict list element, blank/missing ``ioc_value``, missing ``ioc_type``) are
included per spec §10's fixture sketch.

AMBIGUITY NOTED (test-author choice — spec §5 is silent on the exact ``RawRecord.key`` a
``collect()`` yield should carry, unlike ``feodo`` which pins ``key=f"{ip_address}:{port}"``):
this file asserts on ``RawRecord.data`` (the parsed record content, in traversal order) rather
than on ``.key``, so the builder is free to key records however it likes (e.g. the numeric IOC id,
or the ``ioc_value``) without failing an unstated invariant.

RED today (two distinct failure modes):
1. Every test in this module that imports ``worldmonitor.plugins.connectors.threatfox`` fails at
   COLLECTION with ``ModuleNotFoundError`` — the package does not exist yet. This is the expected
   RED for a wholly new component (mirrors the ``feodo`` gate's original RED).
2. Independently of (1) — verified by the test-author out-of-band, since (1) prevents this module
   from running the seed-pin assertion in isolation — ``worldmonitor.db.seed.SEED_CONNECTORS``
   (existing, already-importable code) carries NO ``threatfox`` entry today, so
   ``test_threatfox_is_seeded_enabled_with_pinned_default_url`` is RED on the precise seed-row
   invariant, not merely swept up in (1)'s collection error.

No live network: ``httpx.MockTransport`` is injected through the connector ``transport=`` ctor
kwarg (feodo/mitre_attack precedent), and ``socket.getaddrinfo`` is monkeypatched so the SSRF host
check runs with no real DNS. Any ``Auth-Key`` literal used here is a short dummy (``"k" * 8``),
never a real key (secret-scan hook).
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

from worldmonitor.db.seed import SEED_CONNECTORS
from worldmonitor.ontology.ftm import register_wm_schemata
from worldmonitor.plugins.base import Capability, Kind, Mode, RawRecord, Status

# Top-level import of the not-yet-built connector — ModuleNotFoundError today (RED reason #1,
# see module docstring).
from worldmonitor.plugins.connectors.feodo import FeodoConnector
from worldmonitor.plugins.connectors.threatfox import ThreatFoxConnector
from worldmonitor.provenance.model import Provenance, get_provenance

register_wm_schemata()  # Indicator must exist before map() can construct one.

_TEST_URL = "https://threatfox.abuse.ch/export/json/test-recent/"
_DEFAULT_URL = "https://threatfox.abuse.ch/export/json/recent/"  # spec §5 pinned default
_AUTH_KEY = "k" * 8  # short dummy — never a real key (secret-scan hook)

_PROV = Provenance(
    source_id="threatfox:recent",
    retrieved_at="2026-07-22T00:00:00Z",
    reliability="B",
    source_record="s3://landing/threatfox/recent-20260722.json",
)

# --------------------------------------------------------------------------------------------------
# Fixture records — anonymized real 2026-07-22 shapes (see module docstring). Field names, formats,
# and null patterns are preserved EXACTLY; only domain/URL/hash values are fabricated/anonymized.
# --------------------------------------------------------------------------------------------------

_REC_IP_PORT: dict[str, Any] = {
    "ioc_value": "149.202.227.107:8443",
    "ioc_type": "ip:port",
    "threat_type": "botnet_cc",
    "malware": "win.adaptix_c2",
    "malware_alias": None,
    "malware_printable": "AdaptixC2",
    "first_seen_utc": "2026-07-22 11:05:11",
    "last_seen_utc": None,
    "confidence_level": 100,
    "is_compromised": True,
    "reference": None,
    "tags": "adaptix",
    "anonymous": 1,
    "reporter": "anonymous",
}
_REC_DOMAIN: dict[str, Any] = {
    "ioc_value": "bad-domain.example",
    "ioc_type": "domain",
    "threat_type": "payload_delivery",
    "malware": "js.clearfake",
    "malware_alias": None,
    "malware_printable": "ClearFake",
    "first_seen_utc": "2026-07-21 09:15:00",
    "last_seen_utc": "2026-07-22 03:00:00",
    "confidence_level": 80,
    "is_compromised": False,
    "reference": None,
    "tags": "clearfake,fakeupdate",
    "anonymous": 0,
    "reporter": "abuse_ch",
}
_REC_URL: dict[str, Any] = {
    "ioc_value": "http://malicious-payload.example/drop.exe",
    "ioc_type": "url",
    "threat_type": "payload_delivery",
    "malware": "win.vidar",
    "malware_alias": None,
    "malware_printable": "Vidar",
    "first_seen_utc": "2026-07-20 18:42:09",
    "last_seen_utc": None,
    "confidence_level": 90,
    "is_compromised": False,
    "reference": None,
    "tags": "vidar,stealer",
    "anonymous": 0,
    "reporter": "abuse_ch",
}
_REC_SHA256: dict[str, Any] = {
    "ioc_value": "a3f5c9e21d7b6480fa1c9e4d7b3a5f60c1e2d3f4a5b6c7d8e9f0a1b2c3d4e5f6",
    "ioc_type": "sha256_hash",
    "threat_type": "payload",
    "malware": "unknown_stealer",
    "malware_alias": None,
    "malware_printable": "Unknown Stealer",
    "first_seen_utc": "2026-07-19 07:00:00",
    "last_seen_utc": "2026-07-19 07:00:00",
    "confidence_level": 60,
    "is_compromised": False,
    "reference": "",
    "tags": "stealer",
    "anonymous": 1,
    "reporter": "anonymous",
}
_REC_MD5: dict[str, Any] = {
    "ioc_value": "44d88612fea8a8f36de82e1278abb02f",
    "ioc_type": "md5_hash",
    "threat_type": "payload",
    "malware": "win.emotet",
    "malware_alias": None,
    "malware_printable": "Emotet",
    "first_seen_utc": "2026-07-18 12:00:00",
    "last_seen_utc": None,
    "confidence_level": 70,
    "is_compromised": False,
    "reference": None,
    "tags": "emotet",
    "anonymous": 0,
    "reporter": "abuse_ch",
}
_REC_SHA1: dict[str, Any] = {
    "ioc_value": "da39a3ee5e6b4b0d3255bfef95601890afd80709",
    "ioc_type": "sha1_hash",
    "threat_type": "payload",
    "malware": "win.qakbot",
    "malware_alias": None,
    "malware_printable": "QakBot",
    "first_seen_utc": "2026-07-17 05:30:00",
    "last_seen_utc": None,
    "confidence_level": 65,
    "is_compromised": False,
    "reference": None,
    "tags": "qakbot",
    "anonymous": 0,
    "reporter": "abuse_ch",
}
# Unknown ioc_type member (spec's own example, uppercase here to pin the .lower() transform);
# malware/malware_printable both null — a phishing IOC with no family attribution at all.
_REC_UNKNOWN_TYPE: dict[str, Any] = {
    "ioc_value": "spoof@malicious-sender.example",
    "ioc_type": "ENVELOPE_FROM",
    "threat_type": "phishing",
    "malware": None,
    "malware_alias": None,
    "malware_printable": None,
    "first_seen_utc": "2026-07-16 08:00:00",
    "last_seen_utc": None,
    "confidence_level": 50,
    "is_compromised": False,
    "reference": None,
    "tags": None,
    "anonymous": 1,
    "reporter": "anonymous",
}
# Literal-"unknown" malware — must SUPPRESS malwareFamily (distinct from _REC_SHA256 above, whose
# "unknown_stealer" is a real, if vague, family and must KEEP it).
_REC_LITERAL_UNKNOWN_MALWARE: dict[str, Any] = {
    "ioc_value": "192.0.2.55:4444",
    "ioc_type": "ip:port",
    "threat_type": "botnet_cc",
    "malware": "unknown",
    "malware_alias": None,
    "malware_printable": "Unknown",
    "first_seen_utc": "2026-07-15 00:00:00",
    "last_seen_utc": None,
    "confidence_level": 40,
    "is_compromised": False,
    "reference": None,
    "tags": None,
    "anonymous": 1,
    "reporter": "anonymous",
}
_REC_BLANK_IOC_VALUE: dict[str, Any] = {
    "ioc_value": "",
    "ioc_type": "domain",
    "malware": "win.something",
    "malware_printable": "Something",
    "first_seen_utc": "2026-07-14 00:00:00",
    "last_seen_utc": None,
}
_REC_MISSING_IOC_VALUE_KEY: dict[str, Any] = {
    "ioc_type": "domain",
    "malware": "win.something",
    "malware_printable": "Something",
    "first_seen_utc": "2026-07-14 00:00:00",
}
_REC_MISSING_IOC_TYPE_KEY: dict[str, Any] = {
    "ioc_value": "203.0.113.9:9001",
    "malware": "win.something",
    "malware_printable": "Something",
    "first_seen_utc": "2026-07-13 00:00:00",
    "last_seen_utc": None,
}

# Traversal fixture: the real {numeric_id: [record, ...]} bulk shape, 3 well-formed entries.
_TRAVERSAL_FEED: dict[str, Any] = {
    "1855269": [_REC_IP_PORT],
    "1855270": [_REC_DOMAIN],
    "1855271": [_REC_SHA256],
}

# Hostile traversal fixture (spec §10): a top-level value that is not a list, a list element that
# is not a dict, and one well-formed entry — only the well-formed entry yields.
_HOSTILE_TRAVERSAL_FEED: dict[str, Any] = {
    "bad_not_a_list": "not-a-list-value",
    "bad_element_not_dict": [123, "also-not-a-dict"],
    "good": [_REC_IP_PORT],
}


def _indicator_id_oracle(value: str) -> str:
    """The SHARED id rule (``ontology.ioc.indicator_id``), computed INDEPENDENTLY (stdlib
    ``hashlib``) — the oracle for `map()`'s output, never borrowed from the implementation."""
    return f"ioc-{hashlib.sha1(value.strip().casefold().encode('utf-8')).hexdigest()}"


# --------------------------------------------------------------------------------------------------
# Hermetic-HTTP helpers (mirror tests/unit/test_feodo_connector.py).
# --------------------------------------------------------------------------------------------------


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in resolving EVERY host to ``ip`` (one IPv4 5-tuple)."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def _transport_serving(
    payload: Any, calls: list[httpx.Request], *, status: int = 200
) -> httpx.MockTransport:
    """Serve ``payload`` (JSON-serialized verbatim) for any request; record every request."""
    body = json.dumps(payload).encode("utf-8")

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})

    return httpx.MockTransport(_handler)


def _record(entry: dict[str, Any], *, key: str = "test") -> RawRecord:
    """Wrap a raw ThreatFox entry dict as the JSON ``RawRecord`` that ``map()`` consumes."""
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
    manifest = ThreatFoxConnector().manifest
    assert manifest.connector_id == "threatfox"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — closed schema; auth_key carries "secret": true.
# --------------------------------------------------------------------------------------------------


def test_config_schema_validates_default_and_rejects_additional_properties() -> None:
    connector = ThreatFoxConnector()
    schema = connector.config_schema
    assert schema["additionalProperties"] is False

    connector.validate_config({})  # url/limit/auth_key all optional, pinned default url
    connector.validate_config({"url": _TEST_URL, "limit": 3, "auth_key": _AUTH_KEY})
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"url": _TEST_URL, "bogus": 1})


def test_config_schema_auth_key_is_marked_secret() -> None:
    schema = ThreatFoxConnector().config_schema
    assert schema["properties"]["auth_key"]["secret"] is True


# --------------------------------------------------------------------------------------------------
# collect() — {id:[record,...]} traversal, unwrap, skip non-list/non-dict, limit.
# --------------------------------------------------------------------------------------------------


def test_collect_traverses_id_keyed_object_and_unwraps_inner_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = ThreatFoxConnector(transport=_transport_serving(_TRAVERSAL_FEED, calls))

    records = list(connector.collect({"url": _TEST_URL}))

    assert calls, "collect() never issued a request through the transport"
    assert len(records) == 3, "collect() must yield ONE RawRecord per inner list element"
    parsed = [json.loads(r.data) for r in records]
    assert parsed == [_REC_IP_PORT, _REC_DOMAIN, _REC_SHA256], (
        "collect() must traverse {id:[record,...]} in order and unwrap each one-element list"
    )


def test_collect_skips_non_list_value_and_non_dict_list_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = ThreatFoxConnector(transport=_transport_serving(_HOSTILE_TRAVERSAL_FEED, calls))

    records = list(connector.collect({"url": _TEST_URL}))  # must not raise

    assert len(records) == 1, (
        f"expected ONLY the well-formed entry to yield, got {len(records)} records"
    )
    assert json.loads(records[0].data) == _REC_IP_PORT


def test_collect_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = ThreatFoxConnector(transport=_transport_serving(_TRAVERSAL_FEED, calls))

    records = list(connector.collect({"url": _TEST_URL, "limit": 2}))

    assert len(records) == 2
    parsed = [json.loads(r.data) for r in records]
    assert parsed == [_REC_IP_PORT, _REC_DOMAIN], "limit must hard-cap in traversal order"


# --------------------------------------------------------------------------------------------------
# collect() — Auth-Key header present-when-configured / absent-when-not.
# --------------------------------------------------------------------------------------------------


def test_collect_sends_auth_key_header_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = ThreatFoxConnector(transport=_transport_serving(_TRAVERSAL_FEED, calls))

    list(connector.collect({"url": _TEST_URL, "auth_key": _AUTH_KEY}))

    assert calls, "collect() never issued a request"
    headers = dict(calls[0].headers)
    assert headers.get("auth-key") == _AUTH_KEY, (
        f"expected the Auth-Key header to carry the configured value, got headers={headers}"
    )


def test_collect_omits_auth_key_header_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = ThreatFoxConnector(transport=_transport_serving(_TRAVERSAL_FEED, calls))

    list(connector.collect({"url": _TEST_URL}))

    assert calls, "collect() never issued a request"
    headers = dict(calls[0].headers)
    assert "auth-key" not in headers, (
        f"Auth-Key header must be ABSENT when auth_key is not configured, got headers={headers}"
    )


# --------------------------------------------------------------------------------------------------
# collect() — 401/403 raises loud with an actionable auth message (never silently [] ).
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_collect_auth_error_raises_actionable_message(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = ThreatFoxConnector(
        transport=_transport_serving({"query_status": "no_result"}, calls, status=status)
    )

    with pytest.raises(Exception) as exc_info:  # noqa: PT011 - message content is what we pin
        list(connector.collect({"url": _TEST_URL}))

    message = str(exc_info.value)
    assert "auth" in message.lower(), (
        f"a {status} response must raise an ACTIONABLE message naming the auth requirement, "
        f"got: {message!r}"
    )
    assert "Auth-Key" in message, (
        f"the actionable message must name the Auth-Key config field, got: {message!r}"
    )


# --------------------------------------------------------------------------------------------------
# map() — the ioc_type -> indicatorType vocabulary (spec §3, load-bearing cross-connector).
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "expected_type"),
    [
        (_REC_IP_PORT, "ipv4"),
        (_REC_DOMAIN, "domain"),
        (_REC_URL, "url"),
        (_REC_MD5, "md5"),
        (_REC_SHA1, "sha1"),
        (_REC_SHA256, "sha256"),
    ],
    ids=["ip_port", "domain", "url", "md5_hash", "sha1_hash", "sha256_hash"],
)
def test_map_ioc_type_vocabulary(entry: dict[str, Any], expected_type: str) -> None:
    connector = ThreatFoxConnector()
    entities = list(connector.map(_record(entry), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    value = entry["ioc_value"]

    assert entity.schema.name == "Indicator"
    assert entity.id == _indicator_id_oracle(value)
    assert entity.get("name") == [value]
    assert entity.get("indicatorValue") == [value]
    assert entity.get("indicatorType") == [expected_type]


def test_map_unknown_ioc_type_passes_through_lowercased_and_still_emits() -> None:
    connector = ThreatFoxConnector()
    entities = list(connector.map(_record(_REC_UNKNOWN_TYPE), provenance=_PROV))

    assert len(entities) == 1, "an unknown ioc_type must NEVER drop the IOC"
    entity = entities[0]
    assert entity.get("indicatorType") == ["envelope_from"], (
        "unknown-member ioc_type must pass through as lower(raw), never dropped/raised"
    )
    assert entity.get("indicatorValue") == [_REC_UNKNOWN_TYPE["ioc_value"]]
    # malware and malware_printable are both null on this record -> no family at all.
    assert entity.get("malwareFamily", quiet=True) == []


def test_map_missing_ioc_type_key_emits_no_indicator_type() -> None:
    connector = ThreatFoxConnector()
    entities = list(connector.map(_record(_REC_MISSING_IOC_TYPE_KEY), provenance=_PROV))

    assert len(entities) == 1, "a missing ioc_type must not drop the IOC"
    entity = entities[0]
    assert entity.get("indicatorType", quiet=True) == [], (
        "a missing ioc_type key must emit NO indicatorType, not raise"
    )
    # the rest of the record still maps normally.
    assert entity.get("indicatorValue") == [_REC_MISSING_IOC_TYPE_KEY["ioc_value"]]
    assert entity.get("malwareFamily") == ["Something"]


# --------------------------------------------------------------------------------------------------
# map() — malwareFamily: literal "unknown" suppressed vs the REAL family "unknown_stealer" kept.
# --------------------------------------------------------------------------------------------------


def test_map_literal_unknown_malware_suppresses_family() -> None:
    connector = ThreatFoxConnector()
    entities = list(connector.map(_record(_REC_LITERAL_UNKNOWN_MALWARE), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    assert entity.get("malwareFamily", quiet=True) == [], (
        'a literal malware=="unknown" (malware_printable=="Unknown") must suppress malwareFamily'
    )
    # the IOC itself is still emitted (leads-not-verdicts — an unattributed value is still real).
    assert entity.get("indicatorValue") == [_REC_LITERAL_UNKNOWN_MALWARE["ioc_value"]]


def test_map_unknown_stealer_is_a_distinct_real_family_and_is_kept() -> None:
    """`unknown_stealer`/"Unknown Stealer" is a REAL (if vague) Malpedia family, distinct from
    the literal placeholder `"unknown"`/"Unknown" — it must KEEP its malwareFamily (pinned per
    the launching agent's explicit distinction)."""
    connector = ThreatFoxConnector()
    entities = list(connector.map(_record(_REC_SHA256), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    assert entity.get("malwareFamily") == ["Unknown Stealer"], (
        'malware=="unknown_stealer" is NOT the literal "unknown" — its malwareFamily must survive'
    )


# --------------------------------------------------------------------------------------------------
# map() — blank/missing ioc_value -> [] (fail-soft, the identity field).
# --------------------------------------------------------------------------------------------------


def test_map_blank_ioc_value_returns_empty_without_raising() -> None:
    connector = ThreatFoxConnector()
    assert list(connector.map(_record(_REC_BLANK_IOC_VALUE), provenance=_PROV)) == []


def test_map_missing_ioc_value_key_returns_empty_without_raising() -> None:
    connector = ThreatFoxConnector()
    assert list(connector.map(_record(_REC_MISSING_IOC_VALUE_KEY), provenance=_PROV)) == []


# --------------------------------------------------------------------------------------------------
# map() — timestamps ISO-normalize; a nullable last_seen_utc is harmless.
# --------------------------------------------------------------------------------------------------


def test_map_timestamps_iso_normalize_and_null_last_seen_is_harmless() -> None:
    connector = ThreatFoxConnector()

    # _REC_IP_PORT: last_seen_utc is null -> lastSeenAt must simply be absent, never raise.
    entities = list(connector.map(_record(_REC_IP_PORT), provenance=_PROV))
    assert len(entities) == 1
    entity = entities[0]
    assert entity.get("firstSeenAt") == ["2026-07-22T11:05:11"]
    assert entity.get("lastSeenAt", quiet=True) == []

    # _REC_DOMAIN: last_seen_utc is a real timestamp -> ISO-normalized too.
    entities2 = list(connector.map(_record(_REC_DOMAIN), provenance=_PROV))
    assert len(entities2) == 1
    entity2 = entities2[0]
    assert entity2.get("firstSeenAt") == ["2026-07-21T09:15:00"]
    assert entity2.get("lastSeenAt") == ["2026-07-22T03:00:00"]


# --------------------------------------------------------------------------------------------------
# map() — datasets, no topics/country/indicates, provenance round-trip, deterministic id.
# --------------------------------------------------------------------------------------------------


def test_map_full_field_shape_no_edges_and_provenance_roundtrips() -> None:
    connector = ThreatFoxConnector()
    record = _record(_REC_IP_PORT, key="ip-port")

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    value = _REC_IP_PORT["ioc_value"]

    assert entity.id == _indicator_id_oracle(value)
    assert entity.get("malwareFamily") == ["AdaptixC2"]
    assert entity.datasets == {"threatfox"}

    # No topics, no country, no indicates edge — attribution is S-2 phase 3, out of this gate.
    assert entity.get("topics", quiet=True) == []
    assert entity.get("country", quiet=True) == []
    assert entity.get("indicates", quiet=True) == []

    # Provenance round-trips intact (the non-negotiable invariant).
    prov = get_provenance(entity)
    assert prov is not None
    assert prov == _PROV
    assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])

    # Deterministic: re-mapping the same record yields the same id (idempotent re-ingest).
    again = list(connector.map(record, provenance=_PROV))
    assert again[0].id == entity.id


# --------------------------------------------------------------------------------------------------
# Cross-connector convergence pin (spec §3, load-bearing): a threatfox ip:port record and a
# feodo-shaped record carrying the SAME ip:port value converge on ONE entity id, both ipv4.
# --------------------------------------------------------------------------------------------------


def test_threatfox_and_feodo_converge_on_the_same_ip_port_indicator_id() -> None:
    threatfox_entities = list(ThreatFoxConnector().map(_record(_REC_IP_PORT), provenance=_PROV))
    assert len(threatfox_entities) == 1
    threatfox_entity = threatfox_entities[0]

    feodo_entry: dict[str, Any] = {
        "ip_address": "149.202.227.107",
        "port": 8443,
        "status": "online",
        "hostname": None,
        "as_number": 1,
        "as_name": "TEST-ASN",
        "country": "FR",
        "first_seen": "2026-07-22 11:05:11",
        "last_online": "2026-07-22",
        "malware": "AdaptixC2",
    }
    feodo_record = RawRecord(
        key="feodo-test",
        data=json.dumps(feodo_entry).encode("utf-8"),
        retrieved_at=_PROV.retrieved_at,
        content_type="application/json",
    )
    feodo_entities = list(FeodoConnector().map(feodo_record, provenance=_PROV))
    assert len(feodo_entities) == 1
    feodo_entity = feodo_entities[0]

    assert threatfox_entity.id == feodo_entity.id, (
        "the SAME real-world ip:port value from threatfox and feodo must converge on ONE node "
        f"by deterministic id — threatfox={threatfox_entity.id!r} feodo={feodo_entity.id!r}"
    )
    assert threatfox_entity.get("indicatorType") == ["ipv4"]
    assert feodo_entity.get("indicatorType") == ["ipv4"]


# --------------------------------------------------------------------------------------------------
# Seed pin — RED against EXISTING code today (no ModuleNotFoundError involved in the invariant
# itself, though this test module as a whole still fails to COLLECT until the connector package
# exists; see module docstring "RED today" #2 for the out-of-band verification).
# --------------------------------------------------------------------------------------------------


def test_threatfox_is_seeded_enabled_with_pinned_default_url() -> None:
    specs = [spec for spec in SEED_CONNECTORS if spec.connector_id == "threatfox"]
    assert specs, "expected a threatfox SeedSpec in SEED_CONNECTORS"
    spec = specs[0]
    assert spec.enabled is True, "threatfox must be seeded enabled (CTI substrate, ADR 0119)"
    assert spec.config.get("url") == _DEFAULT_URL, (
        f"seeded url must equal the spec §5 pinned default {_DEFAULT_URL!r}, got {spec.config!r}"
    )
