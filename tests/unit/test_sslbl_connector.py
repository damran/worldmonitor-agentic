"""Primary invariant tests (RED) for the `sslbl` connector (Gate S-2 phase 2, slice D — the

FINAL slice of the gate).

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/sslbl/`` (an ``EXTERNAL_IMPORT`` / ``PASSIVE`` connector
mirroring the ``threatfox``/``urlhaus``/``feodo`` package shape — see
``docs/decisions/GATE_S2P2_ABUSECH_SIBLINGS_SPEC.md`` §7, the buildable spec companion to
ADR 0119):

* MANIFEST: ``connector_id="sslbl"``, ``kind=CONNECTOR``, ``mode=EXTERNAL_IMPORT``,
  ``capability=PASSIVE``, ``status=IMPLEMENTED``.
* CONFIG SCHEMA: an empty config validates (``url``/``limit``/``auth_key`` all optional, pinned
  default url); ``additionalProperties: false`` rejects a smuggled key; ``auth_key`` carries
  ``"secret": true`` (opencorporates/threatfox/urlhaus precedent).
* ``collect()``: the body is an unquoted CSV (``Listingdate,SHA1,Listingreason``) with ``#``-
  comment lines. Live-probed 2026-07-22 reality (spec §10): the column header itself is a
  ``#``-comment line (``# Listingdate,SHA1,Listingreason``) inside a leading comment block — so
  skipping ``#`` lines already disposes of it; a bare, non-comment ``Listingdate,...`` header row
  is a DEFENSIVE hostile-format-change guard that must also be skipped (spec §7/§10). Each data
  line splits on ``,`` with the first token = ``Listingdate``, second = ``SHA1``, and the
  REMAINder rejoined = ``Listingreason`` (maxsplit-2 semantics: a reason containing a comma stays
  intact — pinned directly). One ``RawRecord`` per data row, in order; ``limit`` hard-caps the
  yield count. The body is bounded to the 16 MiB cap — a fabricated oversized body raises
  (fail-closed) before any parsing. The optional ``auth_key`` config rides as the ``Auth-Key`` HTTP
  header when present, absent entirely when not configured. A 401/403 response raises loud with an
  ACTIONABLE message (naming ``Auth-Key``/``auth`` case-insensitively) rather than silently
  returning ``[]``.
* ``map()``: one CSV row -> ONE FtM ``Indicator``: ``value = SHA1`` (40-hex, non-blank) else
  ``[]`` — a blank OR a malformed (non-40-hex) SHA1 both fail-soft drop, never raise.
  ``indicatorType == ["sha1_cert"]`` UNCONDITIONALLY and is explicitly pinned DISTINCT from the
  threatfox file-hash ``["sha1"]`` (spec §3 — a certificate fingerprint is not a file hash).
  Deterministic ``id = indicator_id(value)`` (the shared ``ioc-<sha1>`` scheme, S-2b — computed
  INDEPENDENTLY here via stdlib ``hashlib``, never borrowed from the implementation); an uppercase
  SHA1 value converges on the SAME id as its lowercase form via casefold (live data is already
  lowercase, but the scheme's casefold normalization must hold regardless). ``malwareFamily`` is
  parsed from ``Listingreason`` per the verified live-probed nuances (spec §7):
    - ends with `` C&C`` (case-insensitive) -> family = the stripped prefix;
    - else ends with `` malware distribution`` (case-insensitive) -> family = the stripped prefix;
    - else -> no family;
    - the generic bare token ``Malware`` is EXCLUDED (case-insensitive compare) even though it
      satisfies one of the two suffix rules (``Malware C&C`` / ``Malware distribution`` -> no
      family);
    - a BARE reason with no recognized suffix (``FIN7``, ``Dridex``, ``KINS MITM``,
      ``QuasarRAT`` — live-probed, heterogeneous: an actor name, a family name, and two more with
      no clean suffix) -> NO family, never guessed; the Indicator is still emitted regardless.
  ``firstSeenAt`` comes from ``Listingdate`` via ``entity.add()`` (FtM date cleaning, ISO-
  normalized). **``lastSeenAt`` is NEVER emitted** (the CSV carries no last-seen column) — pinned
  as an absolute, not merely "often absent". ``datasets == {"sslbl"}``; no ``topics``, no
  ``country``, no ``indicates`` edge; provenance round-trips via ``get_provenance``.
* SEED: ``db.seed.SEED_CONNECTORS`` carries a ``sslbl`` ``SeedSpec``, seeded ``enabled=True``,
  whose config ``url`` is the pinned default anon CC0 CSV endpoint (spec §7) spelled out
  explicitly (ADR 0117 residual-c / feodo/threatfox/urlhaus precedent) — RED TODAY against
  EXISTING code (``worldmonitor.db.seed`` already exists; no ``sslbl`` row in it yet — verified via
  ``uv run python -c "from worldmonitor.db.seed import SEED_CONNECTORS; ..."``, which prints
  ``[]`` for the ``sslbl`` filter as of this writing).

FIXTURE DATA — the real live-probed 2026-07-22 ``sslblacklist.csv`` head (spec §10, reproduced
VERBATIM in the gate brief) plus five real-shaped data rows (``Listingdate``/``SHA1``/
``Listingreason`` fields preserved exactly; the SHA1 hex values themselves are the ones handed to
this agent in the gate brief, already 40-hex-valid — verified independently with a bounded
``hashlib``/regex probe before use here, never trusted blindly). Hostile variants (a ``#``-only
line, a bare non-comment header row, a reason containing a comma, a blank SHA1, a malformed
non-40-hex SHA1 in three shapes: too-short, too-long, and containing a non-hex character) are
included per spec §7/§10's own hostile-variant list.

AMBIGUITY NOTED #1 (test-author choice — the connector CLASS NAME is unpinned anywhere in the
spec/ADR, same ambiguity as urlhaus's "URLhaus" brand capitalization applied to "SSLBL"/"SSL
Blacklist"): registration is auto-discovery-based on ``manifest.connector_id`` alone (spec §2).
Rather than guess a class name, this file DISCOVERS the connector class the same way
``plugins.registry.Registry.discover_module`` does — introspecting
``worldmonitor.plugins.connectors.sslbl.connector`` for the ONE concrete ``Connector`` subclass
DEFINED there (identical helper to ``test_threatfox_connector.py``/``test_urlhaus_connector.py``).

AMBIGUITY NOTED #2 (a deliberate strengthening, directly sourced from the spec's own wording, not
a test-author invention): spec §7's mapping paragraph states plainly "``value = SHA1`` (40-hex,
non-blank) else ``[]``" — unlike ``threatfox``/``urlhaus`` (whose identity fields are free-form
strings with no format constraint), sslbl's identity field IS format-constrained by the spec text
itself. A malformed value (wrong length, or containing a non-hex character) is therefore NOT a
valid SHA1 and must fail-soft drop exactly like a blank one — the failure-mode table's "blank/
missing identity field" row is read here as "blank/missing/malformed", consistent with §7's
"(40-hex, non-blank)" qualifier. This is pinned directly (not weakened to a bare non-empty-string
check), because accepting a non-40-hex value would let a corrupted/truncated CSV row mint a
plausible-looking but WRONG entity id — silently breaking cross-connector convergence for that
value's true SHA1 (the load-bearing invariant of this whole gate, spec §1).

AMBIGUITY NOTED #3 (test-author choice on the bare-header defensive skip, spec §7/§10): the exact
matching rule for "a bare (non-comment) header line" is not specified beyond the literal example
(``Listingdate,SHA1,Listingreason``). This file asserts only the OBSERVABLE behavior spec §10
demands — the header row itself never appears as a yielded ``RawRecord`` — rather than pinning a
specific case-sensitivity or prefix-matching implementation choice, so any conforming
implementation (exact literal match, case-insensitive match, or a stricter structural check)
satisfies the test.

RED today (two distinct, independent failure modes, precedent per
``tests/unit/test_urlhaus_connector.py``):
1. Every test in this module fails at COLLECTION with ``ModuleNotFoundError`` — the
   ``worldmonitor.plugins.connectors.sslbl`` package does not exist yet. This is the expected RED
   for a wholly new component.
2. Independently of (1) — ``worldmonitor.db.seed.SEED_CONNECTORS`` (existing, already-importable
   code) carries NO ``sslbl`` entry today, so
   ``test_sslbl_is_seeded_enabled_with_pinned_default_url`` is RED on the precise seed-row
   invariant, not merely swept up in (1)'s collection error.

No live network: ``httpx.MockTransport`` is injected through the connector ``transport=`` ctor
kwarg (feodo/threatfox/urlhaus precedent), and ``socket.getaddrinfo`` is monkeypatched so the SSRF
host check runs with no real DNS. Any ``Auth-Key`` literal used here is a short dummy (``"k" * 8``),
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

import httpx
import jsonschema
import pytest

# Top-level import of the not-yet-built connector submodule — ModuleNotFoundError today (RED
# reason #1, see module docstring). Importing the SUBMODULE (not a specific class name) so the
# discovery helper below can introspect it without pinning an unstated class-name choice.
import worldmonitor.plugins.connectors.sslbl.connector as _sslbl_connector_module
from worldmonitor.db.seed import SEED_CONNECTORS
from worldmonitor.ontology.ftm import register_wm_schemata
from worldmonitor.plugins.base import Capability, Connector, Kind, Mode, RawRecord, Status
from worldmonitor.provenance.model import Provenance, get_provenance

register_wm_schemata()  # Indicator must exist before map() can construct one.

_TEST_URL = "https://sslbl.abuse.ch/blacklist/sslblacklist_test.csv"
_DEFAULT_URL = "https://sslbl.abuse.ch/blacklist/sslblacklist.csv"  # spec §7 pinned default
_AUTH_KEY = "k" * 8  # short dummy — never a real key (secret-scan hook)
_MAX_FEED_BYTES = 16 * 1024 * 1024  # spec §7's own stated cap, asserted independently of the
# builder's private constant name (a hardcoded number is a strictly more decoupled oracle).

_PROV = Provenance(
    source_id="sslbl:blacklist",
    retrieved_at="2026-07-22T00:00:00Z",
    reliability="B",
    source_record="s3://landing/sslbl/blacklist-20260722.csv",
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


_SslblConnector = _discover_connector_class(_sslbl_connector_module)

# --------------------------------------------------------------------------------------------------
# 40-hex SHA1 fixtures — verified with a bounded regex probe BEFORE use (see module docstring),
# never trusted blindly. Distinct hex patterns per fixture (no accidental id collisions).
# --------------------------------------------------------------------------------------------------

_HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")

_SHA1_RATONRAT = "b8b339de5ea80d17fb5ce2eb144d7ba28b33337a"
_SHA1_VIDAR = "9000e46cabc64219fb1447d59d5443afcb412e36"
_SHA1_MALWARE_CC = "632061b26a93455e9c4f0ac413deae710c920216"
_SHA1_NETSUPPORT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_SHA1_MALWARE_DIST = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_SHA1_LOWERCASE = "cfce3aaff2ad3eae49b37a60d606984bd1492e16"
_SHA1_UPPERCASE = _SHA1_LOWERCASE.upper()
_SHA1_COMMA_ROW = "1234567890abcdef1234567890abcdef12345678"
_SHA1_BARE_HEADER_GOOD = "abcdefabcdefabcdefabcdefabcdefabcdefabcd"

for _candidate in (
    _SHA1_RATONRAT,
    _SHA1_VIDAR,
    _SHA1_MALWARE_CC,
    _SHA1_NETSUPPORT,
    _SHA1_MALWARE_DIST,
    _SHA1_LOWERCASE,
    _SHA1_UPPERCASE,
    _SHA1_COMMA_ROW,
    _SHA1_BARE_HEADER_GOOD,
):
    assert _HEX40_RE.match(_candidate), f"fixture SHA1 is not valid 40-hex: {_candidate!r}"

# Malformed (non-40-hex) SHA1 fixtures — deliberately invalid, per AMBIGUITY NOTED #2.
_SHA1_TOO_SHORT = "c" * 39
_SHA1_TOO_LONG = "d" * 41
_SHA1_NON_HEX_CHAR = "g" + "a" * 39
assert len(_SHA1_TOO_SHORT) == 39 and not _HEX40_RE.match(_SHA1_TOO_SHORT)
assert len(_SHA1_TOO_LONG) == 41 and not _HEX40_RE.match(_SHA1_TOO_LONG)
assert len(_SHA1_NON_HEX_CHAR) == 40 and not _HEX40_RE.match(_SHA1_NON_HEX_CHAR)

# --------------------------------------------------------------------------------------------------
# CSV fixtures — real live-probed 2026-07-22 shape (spec §10, reproduced verbatim in the gate
# brief): the header is itself a `#`-comment line inside the leading comment block.
# --------------------------------------------------------------------------------------------------

_COMMENT_HEAD = (
    "################################################################\n"
    "# abuse.ch SSLBL SSL Certificate Blacklist (SHA1 Fingerprints) #\n"
    "# Last updated: 2026-07-22 07:30:49 UTC                        #\n"
    "################################################################\n"
    "#\n"
    "# Listingdate,SHA1,Listingreason\n"
)

_CSV_BODY_FIVE_ROWS = (
    _COMMENT_HEAD
    + f"2026-07-21 12:29:18,{_SHA1_RATONRAT},RatonRAT C&C\n"
    + f"2026-07-21 12:29:16,{_SHA1_VIDAR},Vidar C&C\n"
    + f"2026-07-21 06:09:26,{_SHA1_MALWARE_CC},Malware C&C\n"
    + f"2026-07-20 09:00:00,{_SHA1_NETSUPPORT},NetSupport RAT malware distribution\n"
    + f"2026-07-20 08:00:00,{_SHA1_MALWARE_DIST},Malware distribution\n"
)

_ROWS_FIVE_EXPECTED: list[dict[str, str]] = [
    {
        "Listingdate": "2026-07-21 12:29:18",
        "SHA1": _SHA1_RATONRAT,
        "Listingreason": "RatonRAT C&C",
    },
    {
        "Listingdate": "2026-07-21 12:29:16",
        "SHA1": _SHA1_VIDAR,
        "Listingreason": "Vidar C&C",
    },
    {
        "Listingdate": "2026-07-21 06:09:26",
        "SHA1": _SHA1_MALWARE_CC,
        "Listingreason": "Malware C&C",
    },
    {
        "Listingdate": "2026-07-20 09:00:00",
        "SHA1": _SHA1_NETSUPPORT,
        "Listingreason": "NetSupport RAT malware distribution",
    },
    {
        "Listingdate": "2026-07-20 08:00:00",
        "SHA1": _SHA1_MALWARE_DIST,
        "Listingreason": "Malware distribution",
    },
]

# Hostile: a bare, non-comment header row (spec §7/§10 — must be defensively skipped) followed by
# ONE well-formed data row.
_CSV_BODY_BARE_HEADER = (
    f"Listingdate,SHA1,Listingreason\n2026-07-18 00:00:00,{_SHA1_BARE_HEADER_GOOD},Dridex\n"
)

# Hostile: a Listingreason containing an internal comma — maxsplit-2 must keep it intact.
_CSV_BODY_COMMA_REASON = (
    _COMMENT_HEAD + f"2026-07-19 10:00:00,{_SHA1_COMMA_ROW},Cobalt, Strike C&C\n"
)

_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")


def _indicator_id_oracle(value: str) -> str:
    """The SHARED id rule (``ontology.ioc.indicator_id``), computed INDEPENDENTLY (stdlib
    ``hashlib``) — the oracle for `map()`'s output, never borrowed from the implementation."""
    return f"ioc-{hashlib.sha1(value.strip().casefold().encode('utf-8')).hexdigest()}"


# --------------------------------------------------------------------------------------------------
# Hermetic-HTTP helpers (mirror tests/unit/test_threatfox_connector.py / test_urlhaus_connector.py).
# --------------------------------------------------------------------------------------------------


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in resolving EVERY host to ``ip`` (one IPv4 5-tuple)."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def _transport_serving_csv(
    body: str, calls: list[httpx.Request], *, status: int = 200
) -> httpx.MockTransport:
    """Serve ``body`` (raw CSV text, utf-8 encoded) for any request; record every request."""
    encoded = body.encode("utf-8")

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, content=encoded, headers={"content-type": "text/csv"})

    return httpx.MockTransport(_handler)


def _record(entry: dict[str, str], *, key: str = "test") -> RawRecord:
    """Wrap a raw sslbl row dict as the JSON ``RawRecord`` that ``map()`` consumes.

    Spec §7: ``data = json.dumps({"Listingdate":..., "SHA1":..., "Listingreason":...})``.
    """
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
    manifest = _SslblConnector().manifest
    assert manifest.connector_id == "sslbl"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — closed schema; auth_key carries "secret": true.
# --------------------------------------------------------------------------------------------------


def test_config_schema_validates_default_and_rejects_additional_properties() -> None:
    connector = _SslblConnector()
    schema = connector.config_schema
    assert schema["additionalProperties"] is False

    connector.validate_config({})  # url/limit/auth_key all optional, pinned default url
    connector.validate_config({"url": _TEST_URL, "limit": 3, "auth_key": _AUTH_KEY})
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"url": _TEST_URL, "bogus": 1})


def test_config_schema_auth_key_is_marked_secret() -> None:
    schema = _SslblConnector().config_schema
    assert schema["properties"]["auth_key"]["secret"] is True


# --------------------------------------------------------------------------------------------------
# collect() — comment/header skip, maxsplit-2 comma-preserving parse, limit, 16 MiB cap.
# --------------------------------------------------------------------------------------------------


def test_collect_skips_comment_lines_and_commented_header_yields_data_rows_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _SslblConnector(transport=_transport_serving_csv(_CSV_BODY_FIVE_ROWS, calls))

    records = list(connector.collect({"url": _TEST_URL}))

    assert calls, "collect() never issued a request through the transport"
    assert len(records) == 5, (
        "collect() must yield ONE RawRecord per data row — the leading `#`-comment block "
        "(including the commented column header) must never yield a record"
    )
    parsed = [json.loads(r.data) for r in records]
    assert parsed == _ROWS_FIVE_EXPECTED, (
        "collect() must traverse data rows in order, parsing Listingdate/SHA1/Listingreason "
        f"exactly; got {parsed!r}"
    )


def test_collect_defensively_skips_bare_noncomment_header_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare (non-`#`) ``Listingdate,SHA1,Listingreason`` header row must never yield a record
    (spec §7/§10's own hostile-variant list — a defensive guard against a future format change)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _SslblConnector(transport=_transport_serving_csv(_CSV_BODY_BARE_HEADER, calls))

    records = list(connector.collect({"url": _TEST_URL}))  # must not raise

    parsed_any = [json.loads(r.data) for r in records]
    assert len(records) == 1, (
        "expected ONLY the well-formed data row to yield (the bare header row must be "
        f"defensively skipped), got {len(records)} records: {parsed_any!r}"
    )
    parsed = json.loads(records[0].data)
    assert parsed == {
        "Listingdate": "2026-07-18 00:00:00",
        "SHA1": _SHA1_BARE_HEADER_GOOD,
        "Listingreason": "Dridex",
    }


def test_collect_preserves_comma_within_listingreason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """maxsplit-2 semantics: everything after the SECOND comma is rejoined verbatim as
    Listingreason — a reason containing an internal comma must stay intact (spec §7)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _SslblConnector(transport=_transport_serving_csv(_CSV_BODY_COMMA_REASON, calls))

    records = list(connector.collect({"url": _TEST_URL}))

    assert len(records) == 1
    parsed = json.loads(records[0].data)
    assert parsed["Listingreason"] == "Cobalt, Strike C&C", (
        "a comma inside Listingreason must survive intact via maxsplit-2 rejoin, got "
        f"{parsed['Listingreason']!r}"
    )
    assert parsed["SHA1"] == _SHA1_COMMA_ROW
    assert parsed["Listingdate"] == "2026-07-19 10:00:00"


def test_collect_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _SslblConnector(transport=_transport_serving_csv(_CSV_BODY_FIVE_ROWS, calls))

    records = list(connector.collect({"url": _TEST_URL, "limit": 2}))

    assert len(records) == 2
    parsed = [json.loads(r.data) for r in records]
    assert parsed == _ROWS_FIVE_EXPECTED[:2], "limit must hard-cap in traversal order"


def test_collect_fails_closed_on_oversized_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body larger than the 16 MiB cap raises (fail-closed) — never an unbounded read."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    oversized = b"A" * (_MAX_FEED_BYTES + 4096)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"content-type": "text/csv"})

    connector = _SslblConnector(transport=httpx.MockTransport(_handler))

    with pytest.raises(ValueError):
        list(connector.collect({"url": _TEST_URL}))


# --------------------------------------------------------------------------------------------------
# collect() — Auth-Key header present-when-configured / absent-when-not.
# --------------------------------------------------------------------------------------------------


def test_collect_sends_auth_key_header_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _SslblConnector(transport=_transport_serving_csv(_CSV_BODY_FIVE_ROWS, calls))

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
    connector = _SslblConnector(transport=_transport_serving_csv(_CSV_BODY_FIVE_ROWS, calls))

    list(connector.collect({"url": _TEST_URL}))

    assert calls, "collect() never issued a request"
    headers = dict(calls[0].headers)
    assert "auth-key" not in headers, (
        f"Auth-Key header must be ABSENT when auth_key is not configured, got headers={headers}"
    )


# --------------------------------------------------------------------------------------------------
# collect() — 401/403 raises loud with an actionable auth message (never silently []).
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_collect_auth_error_raises_actionable_message(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _SslblConnector(transport=_transport_serving_csv("# gated\n", calls, status=status))

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
# map() — value=SHA1, indicatorType == ["sha1_cert"] (DISTINCT from file-hash "sha1"), det. id.
# --------------------------------------------------------------------------------------------------


def test_map_value_is_sha1_with_distinct_cert_type_and_deterministic_id() -> None:
    connector = _SslblConnector()
    row = {
        "Listingdate": "2026-07-21 12:29:18",
        "SHA1": _SHA1_RATONRAT,
        "Listingreason": "RatonRAT C&C",
    }
    entities = list(connector.map(_record(row), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]

    assert entity.schema.name == "Indicator"
    assert entity.id == _indicator_id_oracle(_SHA1_RATONRAT)
    assert entity.get("indicatorValue") == [_SHA1_RATONRAT]
    assert entity.get("indicatorType") == ["sha1_cert"], (
        "sslbl indicatorType must be exactly ['sha1_cert']"
    )
    assert entity.get("indicatorType") != ["sha1"], (
        "sslbl's certificate fingerprint MUST NOT be confused with threatfox's file-hash "
        "'sha1' — spec §3 explicitly requires these stay DISTINCT"
    )


def test_map_uppercase_sha1_converges_to_same_id_as_lowercase_via_casefold() -> None:
    connector = _SslblConnector()
    row_upper = {
        "Listingdate": "2026-07-22 07:30:49",
        "SHA1": _SHA1_UPPERCASE,
        "Listingreason": "PureLogsStealer C&C",
    }
    entities = list(connector.map(_record(row_upper), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    assert entity.id == _indicator_id_oracle(_SHA1_LOWERCASE), (
        "an uppercase SHA1 must converge on the SAME entity id as its lowercase form "
        "(the shared ontology.ioc.indicator_id casefold-normalization contract)"
    )
    assert entity.id == _indicator_id_oracle(_SHA1_UPPERCASE)


# --------------------------------------------------------------------------------------------------
# map() — malwareFamily parsing per the verified Listingreason nuances (spec §7).
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("listingreason", "expected_family"),
    [
        ("RatonRAT C&C", "RatonRAT"),
        ("Vidar C&C", "Vidar"),
        ("Malware C&C", None),
        ("MALWARE C&C", None),  # generic exclusion is case-insensitive
        ("NetSupport RAT malware distribution", "NetSupport RAT"),
        ("ACRStealer malware distribution", "ACRStealer"),
        ("Malware distribution", None),
        ("MALWARE DISTRIBUTION", None),  # generic exclusion is case-insensitive
        ("AsyncRAT c&c", "AsyncRAT"),  # case-insensitive SUFFIX matching, prefix case preserved
        ("FIN7", None),  # bare, no recognized suffix — an ACTOR name, never guessed
        ("Dridex", None),  # bare, no recognized suffix — a REAL family, never guessed
        ("KINS MITM", None),  # bare, no recognized suffix
        ("QuasarRAT", None),  # bare, no recognized suffix
    ],
)
def test_map_family_parsing_rules(listingreason: str, expected_family: str | None) -> None:
    connector = _SslblConnector()
    row = {
        "Listingdate": "2026-07-21 00:00:00",
        "SHA1": _SHA1_LOWERCASE,
        "Listingreason": listingreason,
    }
    entities = list(connector.map(_record(row), provenance=_PROV))

    assert len(entities) == 1, f"Listingreason={listingreason!r} must still emit the Indicator"
    family = entities[0].get("malwareFamily", quiet=True)
    if expected_family is None:
        assert family == [], (
            f"Listingreason={listingreason!r} must emit NO malwareFamily, got {family!r}"
        )
    else:
        assert family == [expected_family], (
            f"Listingreason={listingreason!r} expected malwareFamily=[{expected_family!r}], "
            f"got {family!r}"
        )


# --------------------------------------------------------------------------------------------------
# map() — firstSeenAt ISO-normalizes from Listingdate; lastSeenAt is NEVER emitted.
# --------------------------------------------------------------------------------------------------


def test_map_first_seen_at_iso_normalizes_from_listingdate() -> None:
    connector = _SslblConnector()
    row = {
        "Listingdate": "2026-07-21 12:29:18",
        "SHA1": _SHA1_RATONRAT,
        "Listingreason": "RatonRAT C&C",
    }
    entities = list(connector.map(_record(row), provenance=_PROV))

    assert len(entities) == 1
    first_seen = entities[0].get("firstSeenAt")
    assert len(first_seen) == 1
    assert _ISO_DATETIME_RE.match(first_seen[0])
    assert first_seen == ["2026-07-21T12:29:18"]


@pytest.mark.parametrize(
    "row",
    [
        {
            "Listingdate": "2026-07-21 12:29:18",
            "SHA1": _SHA1_RATONRAT,
            "Listingreason": "RatonRAT C&C",
        },
        {
            "Listingdate": "2026-07-20 08:00:00",
            "SHA1": _SHA1_MALWARE_DIST,
            "Listingreason": "Malware distribution",
        },
    ],
    ids=["with_family", "generic_no_family"],
)
def test_map_never_emits_last_seen_at(row: dict[str, str]) -> None:
    """The sslbl CSV carries no last-seen column at all — lastSeenAt must NEVER be emitted,
    not merely "usually absent" (spec §7)."""
    connector = _SslblConnector()
    entities = list(connector.map(_record(row), provenance=_PROV))

    assert len(entities) == 1
    assert entities[0].get("lastSeenAt", quiet=True) == [], (
        "sslbl must NEVER emit lastSeenAt (no last-seen column exists in the source CSV)"
    )


# --------------------------------------------------------------------------------------------------
# map() — blank/malformed (non-40-hex) SHA1 -> [] (fail-soft, never raise).
# --------------------------------------------------------------------------------------------------


def test_map_blank_sha1_returns_empty_without_raising() -> None:
    connector = _SslblConnector()
    row = {"Listingdate": "2026-07-17 00:00:00", "SHA1": "", "Listingreason": "Vidar C&C"}
    assert list(connector.map(_record(row), provenance=_PROV)) == []


def test_map_missing_sha1_key_returns_empty_without_raising() -> None:
    connector = _SslblConnector()
    row = {"Listingdate": "2026-07-16 00:00:00", "Listingreason": "Vidar C&C"}
    assert list(connector.map(_record(row), provenance=_PROV)) == []


@pytest.mark.parametrize(
    "malformed_sha1",
    [_SHA1_TOO_SHORT, _SHA1_TOO_LONG, _SHA1_NON_HEX_CHAR],
    ids=["too_short_39", "too_long_41", "non_hex_char"],
)
def test_map_malformed_non_40_hex_sha1_returns_empty_without_raising(
    malformed_sha1: str,
) -> None:
    """A SHA1 value that is not exactly 40 hex characters is NOT a valid identity value (spec §7:
    "value = SHA1 (40-hex, non-blank) else []") — minting an id from a truncated/corrupted value
    would silently break cross-connector convergence for the value's TRUE fingerprint."""
    connector = _SslblConnector()
    row = {
        "Listingdate": "2026-07-15 00:00:00",
        "SHA1": malformed_sha1,
        "Listingreason": "Vidar C&C",
    }
    assert list(connector.map(_record(row), provenance=_PROV)) == [], (
        f"a malformed (non-40-hex) SHA1 {malformed_sha1!r} must fail-soft to [], never raise "
        "and never mint an entity from an invalid identity value"
    )


# --------------------------------------------------------------------------------------------------
# map() — datasets, no topics/country/indicates, provenance round-trip, deterministic id.
# --------------------------------------------------------------------------------------------------


def test_map_full_field_shape_no_edges_and_provenance_roundtrips() -> None:
    connector = _SslblConnector()
    row = {
        "Listingdate": "2026-07-21 12:29:18",
        "SHA1": _SHA1_RATONRAT,
        "Listingreason": "RatonRAT C&C",
    }
    record = _record(row, key="raton")

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]

    assert entity.id == _indicator_id_oracle(_SHA1_RATONRAT)
    assert entity.datasets == {"sslbl"}

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


def test_sslbl_is_seeded_enabled_with_pinned_default_url() -> None:
    specs = [spec for spec in SEED_CONNECTORS if spec.connector_id == "sslbl"]
    assert specs, "expected a sslbl SeedSpec in SEED_CONNECTORS"
    spec = specs[0]
    assert spec.enabled is True, "sslbl must be seeded enabled (CTI substrate, ADR 0119)"
    assert spec.config.get("url") == _DEFAULT_URL, (
        f"seeded url must equal the spec §7 pinned default {_DEFAULT_URL!r}, got {spec.config!r}"
    )
