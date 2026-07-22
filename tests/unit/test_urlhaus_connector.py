"""Primary invariant tests (RED) for the `urlhaus` connector (Gate S-2 phase 2, slice C).

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/urlhaus/`` (an ``EXTERNAL_IMPORT`` / ``PASSIVE`` connector
mirroring the ``threatfox``/``feodo`` package shape — see
``docs/decisions/GATE_S2P2_ABUSECH_SIBLINGS_SPEC.md`` §6, the buildable spec companion to
ADR 0119):

* MANIFEST: ``connector_id="urlhaus"``, ``kind=CONNECTOR``, ``mode=EXTERNAL_IMPORT``,
  ``capability=PASSIVE``, ``status=IMPLEMENTED``.
* CONFIG SCHEMA: an empty config validates (``url``/``limit``/``auth_key`` all optional, pinned
  default url); ``additionalProperties: false`` rejects a smuggled key; ``auth_key`` carries
  ``"secret": true`` (opencorporates ``api_token`` / threatfox precedent).
* ``collect()``: the body is a JSON **object** keyed by numeric urlhaus id, each value a **list**
  of record dicts — traverse ``{id: [record, ...]}`` identically to ``threatfox``, yielding ONE
  ``RawRecord`` per inner record. A top-level value that is not a list, or a list element that is
  not a dict, is skipped (no yield, no raise). ``limit`` hard-caps the yield count in traversal
  order. The body is bounded to a 16 MiB cap (spec §6: "~10.8 MB — under the 16 MiB cap but assert
  the cap explicitly") — a fabricated oversized body raises (fail-closed) before any parsing. The
  optional ``auth_key`` config is sent as the ``Auth-Key`` HTTP header when present, absent
  entirely when not configured. A 401/403 response raises loud with an ACTIONABLE message (naming
  ``Auth-Key``/``auth`` case-insensitively) rather than silently returning ``[]``.
* ``map()``: one record -> ONE FtM ``Indicator``: ``value = url`` (str, non-blank) else ``[]``;
  ``indicatorType == ["url"]`` unconditionally; deterministic ``id = indicator_id(value)`` (the
  shared ``ioc-<sha1>`` scheme, S-2b — computed INDEPENDENTLY here via stdlib ``hashlib``, never
  borrowed from the implementation). ``firstSeenAt``/``lastSeenAt`` come from ``dateadded``/
  ``last_online`` via ``entity.add()`` (FtM date cleaning) — THE slice-specific trap: the real
  ``json_recent`` export's ``dateadded``/``last_online`` values carry a trailing `` UTC`` suffix
  (spec §6), and the properties must still come out non-empty and ISO-formatted (both WITH and
  WITHOUT the suffix present on the raw value). A ``null`` ``last_online`` is harmless (no
  ``lastSeenAt``, no crash). Both ``null`` and JSON-array ``tags`` (including an empty array) are
  harmless — ``tags`` has no corresponding Indicator property and must never crash ``map()``.
  **No ``malwareFamily`` is EVER emitted** (locked decision, spec §6: ``tags``/``threat`` are not
  a reliable family signal) — pinned even against a record whose ``tags`` include a
  family-looking token (``"Mozi"``) and whose ``threat`` is set. ``datasets == {"urlhaus"}``,
  provenance round-trips via ``get_provenance``, and NO ``topics``, NO ``country``, NO
  ``indicates`` edge (attribution is S-2 phase 3, out of scope for this gate). A record with a
  blank/missing ``url`` (the identity field) maps to ``[]`` (fail-soft), never raising.
* SEED: ``db.seed.SEED_CONNECTORS`` carries a ``urlhaus`` ``SeedSpec``, seeded ``enabled=True``,
  whose config ``url`` is the pinned default anon export endpoint (spec §6) spelled out explicitly
  (ADR 0117 residual-c / feodo/threatfox precedent) — this pins the builder's seed row and is RED
  TODAY against EXISTING code (``worldmonitor.db.seed`` already exists; no ``urlhaus`` row in it
  yet — verified via ``uv run python -c "from worldmonitor.db.seed import SEED_CONNECTORS; ..."``).

FIXTURE RECORDS — anonymized shapes based on the REAL live-probed 2026-07-22 URLhaus
``json_recent`` export (field names/formats/null patterns preserved EXACTLY; IPs anonymized to
RFC 5737 TEST-NET ranges, domain-style URLs anonymized to ``*.example`` forms; numeric urlhaus ids
kept from the launching agent's verified probe since a bare integer carries no PII). Hostile
variants (non-list top-level value, non-dict list element, blank/missing ``url``, ``dateadded``
without the `` UTC`` suffix, empty ``tags`` array, ``null`` ``tags``) are included per spec §10's
own hostile-variant list.

AMBIGUITY NOTED #1 (test-author choice — the connector CLASS NAME is unpinned anywhere in the
spec/ADR): registration is auto-discovery-based on ``manifest.connector_id`` alone (spec §2 —
``pkgutil.walk_packages`` + "register every concrete Connector subclass found"; the internal class
NAME is not part of that contract). Rather than guess a class name (the sibling connectors are
inconsistent in style — ``GeoNamesConnector``/``OpenCorporatesConnector``/``ThreatFoxConnector``
all use the brand's own capitalization, which for "URLhaus" is genuinely ambiguous between
``URLhausConnector`` and ``UrlhausConnector``), this file DISCOVERS the connector class the same
way ``plugins.registry.Registry.discover_module`` does: introspect
``worldmonitor.plugins.connectors.urlhaus.connector`` for the one concrete ``Connector`` subclass
DEFINED there. This is strictly more faithful to the real invariant (spec §2's auto-discovery
contract) than hardcoding a guessed identifier, and it cannot be satisfied vacuously (there must be
EXACTLY one such class, or the assertion fails with a clear message).

AMBIGUITY NOTED #2 (empirically verified, not assumed): spec §6 describes stripping a trailing
`` UTC`` suffix "before ``entity.add()``" because "FtM's date type would otherwise silently drop
'... UTC' unstripped." Verified directly against the pinned dependency versions in THIS repo
(FollowTheMoney 4.9.2 / prefixdate 0.5.0): ``registry.date.clean("2026-07-22 11:46:20 UTC")`` and
``entity.add("firstSeenAt", "2026-07-22 11:46:20 UTC")`` BOTH already normalize correctly to
``"2026-07-22T11:46:20"`` at this pin — the described drop does not currently reproduce. The tests
below therefore pin the OBSERVABLE, black-box requirement (spec's own wording: "assert the
properties are non-empty and ISO-formatted") rather than requiring the builder to literally call
``.replace(" UTC", "")`` — a correct implementation is free to strip defensively (recommended, per
spec, in case a future FollowTheMoney/prefixdate bump reintroduces the drop) or to rely on FtM's
current leniency; either satisfies this test. Since the exact normalized value is ALSO
deterministic and empirically stable either way, the tests additionally pin the precise expected
ISO string as a strictly stronger check (belt-and-suspenders), not a weaker one.

RED today (two distinct failure modes, precedent per ``tests/unit/test_threatfox_connector.py``):
1. Every test in this module fails at COLLECTION with ``ModuleNotFoundError`` (or an
   ``AttributeError``/``AssertionError`` from ``_discover_connector_class`` finding zero
   candidates) — the ``worldmonitor.plugins.connectors.urlhaus`` package does not exist yet. This
   is the expected RED for a wholly new component.
2. Independently of (1) — ``worldmonitor.db.seed.SEED_CONNECTORS`` (existing, already-importable
   code) carries NO ``urlhaus`` entry today, so
   ``test_urlhaus_is_seeded_enabled_with_pinned_default_url`` is RED on the precise seed-row
   invariant, not merely swept up in (1)'s collection error.

No live network: ``httpx.MockTransport`` is injected through the connector ``transport=`` ctor
kwarg (feodo/threatfox precedent), and ``socket.getaddrinfo`` is monkeypatched so the SSRF host
check runs with no real DNS. Any ``Auth-Key`` literal used here is a short dummy (``"k" * 8``),
never a real key (secret-scan hook).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import socket
from collections.abc import Callable
from types import ModuleType
from typing import Any

import httpx
import jsonschema
import pytest

# Top-level import of the not-yet-built connector submodule — ModuleNotFoundError today (RED
# reason #1, see module docstring). Importing the SUBMODULE (not a specific class name) so the
# discovery helper below can introspect it without pinning an unstated class-name choice.
import worldmonitor.plugins.connectors.urlhaus.connector as _urlhaus_connector_module
from worldmonitor.db.seed import SEED_CONNECTORS
from worldmonitor.ontology.ftm import register_wm_schemata
from worldmonitor.plugins.base import Capability, Connector, Kind, Mode, RawRecord, Status
from worldmonitor.provenance.model import Provenance, get_provenance

register_wm_schemata()  # Indicator must exist before map() can construct one.

_TEST_URL = "https://urlhaus.abuse.ch/downloads/json_test-recent/"
_DEFAULT_URL = "https://urlhaus.abuse.ch/downloads/json_recent/"  # spec §6 pinned default
_AUTH_KEY = "k" * 8  # short dummy — never a real key (secret-scan hook)
_MAX_FEED_BYTES = 16 * 1024 * 1024  # spec §6's own stated cap, asserted independently of the
# builder's private constant name (a hardcoded number is a strictly more decoupled oracle).

_PROV = Provenance(
    source_id="urlhaus:recent",
    retrieved_at="2026-07-22T00:00:00Z",
    reliability="B",
    source_record="s3://landing/urlhaus/recent-20260722.json",
)


def _discover_connector_class(module: ModuleType) -> type[Connector]:
    """Find the ONE concrete ``Connector`` subclass defined in ``module``.

    Mirrors ``worldmonitor.plugins.registry.Registry.discover_module``'s own introspection
    (``inspect.getmembers`` + ``issubclass(obj, Connector)`` + "defined here, not imported")
    exactly — see AMBIGUITY NOTED #1 in the module docstring for why this file does not hardcode
    a guessed class name.
    """
    candidates = [
        obj
        for _name, obj in inspect.getmembers(module, inspect.isclass)
        if obj.__module__ == module.__name__
        and issubclass(obj, Connector)
        and obj is not Connector
        and not inspect.isabstract(obj)
    ]
    assert len(candidates) == 1, (
        "expected exactly one concrete Connector subclass defined in "
        f"{module.__name__!r} (the plugins.registry auto-discovery contract, spec §2); "
        f"found {candidates!r}"
    )
    return candidates[0]


_UrlhausConnector = _discover_connector_class(_urlhaus_connector_module)

# --------------------------------------------------------------------------------------------------
# Fixture records — anonymized real 2026-07-22 shapes (see module docstring). Field names, formats,
# and null patterns are preserved EXACTLY; only IP/URL/domain values are fabricated/anonymized.
# --------------------------------------------------------------------------------------------------

_REC_ONLINE: dict[str, Any] = {
    "dateadded": "2026-07-22 11:46:20 UTC",
    "url": "http://203.0.113.14:37432/i",
    "url_status": "online",
    "last_online": "2026-07-22 11:46:20 UTC",
    "threat": "malware_download",
    "tags": ["32-bit", "elf", "mips", "Mozi"],
    "urlhaus_link": "https://urlhaus.abuse.ch/url/3890096/",
    "reporter": "geenensp",
}
_REC_OFFLINE_NULL_LAST_ONLINE: dict[str, Any] = {
    "dateadded": "2026-07-22 10:06:21 UTC",
    "url": "http://198.51.100.13/bins/phantom.arm4",
    "url_status": "offline",
    "last_online": None,
    "threat": "malware_download",
    "tags": ["elf", "ua-wget"],
    "urlhaus_link": "https://urlhaus.abuse.ch/url/3890068/",
    "reporter": "abuse_ch",
}
# NULL tags — real production data, beyond the spec's own "empty tags" hostile-variant sketch.
_REC_NULL_TAGS: dict[str, Any] = {
    "dateadded": "2026-07-22 09:30:00 UTC",
    "url": "http://drop-payload.example/x.bin",
    "url_status": "online",
    "last_online": "2026-07-22 09:31:00 UTC",
    "threat": "malware_download",
    "tags": None,
    "urlhaus_link": "https://urlhaus.abuse.ch/url/3890085/",
    "reporter": "abuse_ch",
}
_REC_EMPTY_TAGS: dict[str, Any] = {
    "dateadded": "2026-07-18 12:00:00 UTC",
    "url": "http://empty-tags.example/x",
    "url_status": "online",
    "last_online": "2026-07-18 12:05:00 UTC",
    "threat": "malware_download",
    "tags": [],
    "urlhaus_link": "https://urlhaus.abuse.ch/url/3889000/",
    "reporter": "abuse_ch",
}
# Hostile variant (spec §10): dateadded/last_online WITHOUT the trailing " UTC" suffix.
_REC_NO_UTC_SUFFIX: dict[str, Any] = {
    "dateadded": "2026-07-20 08:00:00",
    "url": "http://no-suffix.example/x",
    "url_status": "online",
    "last_online": "2026-07-20 08:05:00",
    "threat": "malware_download",
    "tags": ["elf"],
    "urlhaus_link": "https://urlhaus.abuse.ch/url/3889999/",
    "reporter": "abuse_ch",
}
_REC_BLANK_URL: dict[str, Any] = {
    "dateadded": "2026-07-17 00:00:00 UTC",
    "url": "",
    "url_status": "offline",
    "last_online": None,
    "threat": "malware_download",
    "tags": [],
    "urlhaus_link": "https://urlhaus.abuse.ch/url/0000000/",
    "reporter": "abuse_ch",
}
_REC_MISSING_URL_KEY: dict[str, Any] = {
    "dateadded": "2026-07-16 00:00:00 UTC",
    "url_status": "offline",
    "last_online": None,
    "threat": "malware_download",
    "tags": [],
}

# Traversal fixture: the real {numeric_id: [record, ...]} bulk shape, 3 well-formed entries.
_TRAVERSAL_FEED: dict[str, Any] = {
    "3890096": [_REC_ONLINE],
    "3890068": [_REC_OFFLINE_NULL_LAST_ONLINE],
    "3890085": [_REC_NULL_TAGS],
}

# Hostile traversal fixture (spec §10): a top-level value that is not a list, a list element that
# is not a dict, and one well-formed entry — only the well-formed entry yields.
_HOSTILE_TRAVERSAL_FEED: dict[str, Any] = {
    "bad_not_a_list": "not-a-list-value",
    "bad_element_not_dict": [123, "also-not-a-dict"],
    "good": [_REC_ONLINE],
}

_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


def _indicator_id_oracle(value: str) -> str:
    """The SHARED id rule (``ontology.ioc.indicator_id``), computed INDEPENDENTLY (stdlib
    ``hashlib``) — the oracle for `map()`'s output, never borrowed from the implementation."""
    return f"ioc-{hashlib.sha1(value.strip().casefold().encode('utf-8')).hexdigest()}"


def _assert_non_empty_iso(values: list[str]) -> None:
    """The spec's own weakest black-box pin for the ` UTC`-suffix trap (see AMBIGUITY NOTED #2):
    exactly one value, and it parses as an ISO-formatted date/time string."""
    assert len(values) == 1, f"expected exactly one normalized date value, got {values!r}"
    assert _ISO_DATETIME_RE.match(values[0]), f"expected an ISO-formatted date, got {values[0]!r}"


# --------------------------------------------------------------------------------------------------
# Hermetic-HTTP helpers (mirror tests/unit/test_threatfox_connector.py / test_feodo_connector.py).
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
    """Wrap a raw URLhaus entry dict as the JSON ``RawRecord`` that ``map()`` consumes."""
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
    manifest = _UrlhausConnector().manifest
    assert manifest.connector_id == "urlhaus"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — closed schema; auth_key carries "secret": true.
# --------------------------------------------------------------------------------------------------


def test_config_schema_validates_default_and_rejects_additional_properties() -> None:
    connector = _UrlhausConnector()
    schema = connector.config_schema
    assert schema["additionalProperties"] is False

    connector.validate_config({})  # url/limit/auth_key all optional, pinned default url
    connector.validate_config({"url": _TEST_URL, "limit": 3, "auth_key": _AUTH_KEY})
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"url": _TEST_URL, "bogus": 1})


def test_config_schema_auth_key_is_marked_secret() -> None:
    schema = _UrlhausConnector().config_schema
    assert schema["properties"]["auth_key"]["secret"] is True


# --------------------------------------------------------------------------------------------------
# collect() — {id:[record,...]} traversal, unwrap, skip non-list/non-dict, limit, 16 MiB cap.
# --------------------------------------------------------------------------------------------------


def test_collect_traverses_id_keyed_object_and_unwraps_inner_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _UrlhausConnector(transport=_transport_serving(_TRAVERSAL_FEED, calls))

    records = list(connector.collect({"url": _TEST_URL}))

    assert calls, "collect() never issued a request through the transport"
    assert len(records) == 3, "collect() must yield ONE RawRecord per inner list element"
    parsed = [json.loads(r.data) for r in records]
    assert parsed == [_REC_ONLINE, _REC_OFFLINE_NULL_LAST_ONLINE, _REC_NULL_TAGS], (
        "collect() must traverse {id:[record,...]} in order and unwrap each one-element list"
    )


def test_collect_skips_non_list_value_and_non_dict_list_element(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _UrlhausConnector(transport=_transport_serving(_HOSTILE_TRAVERSAL_FEED, calls))

    records = list(connector.collect({"url": _TEST_URL}))  # must not raise

    assert len(records) == 1, (
        f"expected ONLY the well-formed entry to yield, got {len(records)} records"
    )
    assert json.loads(records[0].data) == _REC_ONLINE


def test_collect_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _UrlhausConnector(transport=_transport_serving(_TRAVERSAL_FEED, calls))

    records = list(connector.collect({"url": _TEST_URL, "limit": 2}))

    assert len(records) == 2
    parsed = [json.loads(r.data) for r in records]
    assert parsed == [_REC_ONLINE, _REC_OFFLINE_NULL_LAST_ONLINE], (
        "limit must hard-cap in traversal order"
    )


def test_collect_fails_closed_on_oversized_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body larger than the 16 MiB cap raises (fail-closed) — never an unbounded read.

    Spec §6 explicitly calls out asserting this cap for urlhaus (the real feed is ~10.8 MB,
    comfortably under the cap in practice, but a hostile/corrupted response must still be
    refused rather than read unbounded into memory)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    oversized = b"A" * (_MAX_FEED_BYTES + 4096)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"content-type": "application/json"})

    connector = _UrlhausConnector(transport=httpx.MockTransport(_handler))

    with pytest.raises(ValueError):
        list(connector.collect({"url": _TEST_URL}))


# --------------------------------------------------------------------------------------------------
# collect() — Auth-Key header present-when-configured / absent-when-not.
# --------------------------------------------------------------------------------------------------


def test_collect_sends_auth_key_header_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _UrlhausConnector(transport=_transport_serving(_TRAVERSAL_FEED, calls))

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
    connector = _UrlhausConnector(transport=_transport_serving(_TRAVERSAL_FEED, calls))

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
    connector = _UrlhausConnector(
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
# map() — value=url, indicatorType == ["url"] unconditionally, deterministic id.
# --------------------------------------------------------------------------------------------------


def test_map_value_is_url_with_correct_type_and_deterministic_id() -> None:
    connector = _UrlhausConnector()
    entities = list(connector.map(_record(_REC_ONLINE), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    value = _REC_ONLINE["url"]

    assert entity.schema.name == "Indicator"
    assert entity.id == _indicator_id_oracle(value)
    assert entity.get("indicatorValue") == [value]
    assert entity.get("indicatorType") == ["url"], (
        "urlhaus records are ALWAYS a url IOC — indicatorType must be exactly ['url']"
    )


# --------------------------------------------------------------------------------------------------
# map() — THE ` UTC`-suffix trap (spec §6 / AMBIGUITY NOTED #2): firstSeenAt/lastSeenAt must
# ISO-normalize whether or not the raw dateadded/last_online value carries the suffix.
# --------------------------------------------------------------------------------------------------


def test_map_dateadded_and_last_online_utc_suffix_normalizes() -> None:
    connector = _UrlhausConnector()
    entities = list(connector.map(_record(_REC_ONLINE), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    first_seen = entity.get("firstSeenAt")
    last_seen = entity.get("lastSeenAt")
    _assert_non_empty_iso(first_seen)
    _assert_non_empty_iso(last_seen)
    # Empirically verified exact FtM normalization (see AMBIGUITY NOTED #2) — a strictly stronger
    # pin than the bare ISO-format check above, satisfied either way the builder handles the
    # suffix.
    assert first_seen == ["2026-07-22T11:46:20"]
    assert last_seen == ["2026-07-22T11:46:20"]


def test_map_dateadded_without_utc_suffix_also_normalizes() -> None:
    connector = _UrlhausConnector()
    entities = list(connector.map(_record(_REC_NO_UTC_SUFFIX), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    first_seen = entity.get("firstSeenAt")
    last_seen = entity.get("lastSeenAt")
    _assert_non_empty_iso(first_seen)
    _assert_non_empty_iso(last_seen)
    assert first_seen == ["2026-07-20T08:00:00"]
    assert last_seen == ["2026-07-20T08:05:00"]


def test_map_null_last_online_is_harmless_no_lastseen_no_crash() -> None:
    connector = _UrlhausConnector()
    entities = list(connector.map(_record(_REC_OFFLINE_NULL_LAST_ONLINE), provenance=_PROV))

    assert len(entities) == 1, "a null last_online must not drop or crash the record"
    entity = entities[0]
    assert entity.get("lastSeenAt", quiet=True) == []
    first_seen = entity.get("firstSeenAt")
    _assert_non_empty_iso(first_seen)
    assert first_seen == ["2026-07-22T10:06:21"]


# --------------------------------------------------------------------------------------------------
# map() — null tags AND array tags (including empty) are both harmless.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [_REC_NULL_TAGS, _REC_ONLINE, _REC_EMPTY_TAGS],
    ids=["null_tags", "array_tags_with_content", "empty_array_tags"],
)
def test_map_tags_shape_is_harmless(entry: dict[str, Any]) -> None:
    connector = _UrlhausConnector()
    entities = list(connector.map(_record(entry), provenance=_PROV))

    assert len(entities) == 1, f"tags={entry.get('tags')!r} must never drop or crash the record"
    assert entities[0].get("indicatorValue") == [entry["url"]]


# --------------------------------------------------------------------------------------------------
# map() — the locked no-malwareFamily decision: NEVER, even with a family-looking tag + threat set.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [_REC_ONLINE, _REC_OFFLINE_NULL_LAST_ONLINE, _REC_NULL_TAGS],
    ids=["tags_incl_mozi", "tags_no_family_token", "null_tags"],
)
def test_map_never_emits_malware_family(entry: dict[str, Any]) -> None:
    """Locked decision (spec §6): tags/threat are not a reliable family signal — even a
    Mozi-style botnet tag (present on _REC_ONLINE, alongside threat="malware_download") must
    NEVER populate malwareFamily."""
    connector = _UrlhausConnector()
    entities = list(connector.map(_record(entry), provenance=_PROV))

    assert len(entities) == 1
    assert entities[0].get("malwareFamily", quiet=True) == [], (
        "urlhaus must NEVER emit malwareFamily (locked decision, spec §6) — "
        f"tags={entry.get('tags')!r} threat={entry.get('threat')!r}"
    )


# --------------------------------------------------------------------------------------------------
# map() — blank/missing url -> [] (fail-soft, the identity field).
# --------------------------------------------------------------------------------------------------


def test_map_blank_url_returns_empty_without_raising() -> None:
    connector = _UrlhausConnector()
    assert list(connector.map(_record(_REC_BLANK_URL), provenance=_PROV)) == []


def test_map_missing_url_key_returns_empty_without_raising() -> None:
    connector = _UrlhausConnector()
    assert list(connector.map(_record(_REC_MISSING_URL_KEY), provenance=_PROV)) == []


# --------------------------------------------------------------------------------------------------
# map() — datasets, no topics/country/indicates, provenance round-trip, deterministic id.
# --------------------------------------------------------------------------------------------------


def test_map_full_field_shape_no_edges_and_provenance_roundtrips() -> None:
    connector = _UrlhausConnector()
    record = _record(_REC_ONLINE, key="online")

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    value = _REC_ONLINE["url"]

    assert entity.id == _indicator_id_oracle(value)
    assert entity.datasets == {"urlhaus"}

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
# Seed pin — RED against EXISTING code today (independent of the ModuleNotFoundError above; see
# module docstring "RED today" #2).
# --------------------------------------------------------------------------------------------------


def test_urlhaus_is_seeded_enabled_with_pinned_default_url() -> None:
    specs = [spec for spec in SEED_CONNECTORS if spec.connector_id == "urlhaus"]
    assert specs, "expected a urlhaus SeedSpec in SEED_CONNECTORS"
    spec = specs[0]
    assert spec.enabled is True, "urlhaus must be seeded enabled (CTI substrate, ADR 0119)"
    assert spec.config.get("url") == _DEFAULT_URL, (
        f"seeded url must equal the spec §6 pinned default {_DEFAULT_URL!r}, got {spec.config!r}"
    )
