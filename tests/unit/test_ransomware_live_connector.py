"""Primary invariant tests (RED) for the `ransomware_live` connector (Gate S-4, slice 2).

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/ransomware_live/`` (an ``EXTERNAL_IMPORT`` / ``PASSIVE``
connector serving BOTH Ransomware.live v2 datasets via a single ``dataset`` config value —
opensanctions precedent — see ``docs/reviews/GATE_S4_RANSOMWARE_LIVE_SPEC.md`` §2-§9, the buildable
spec companion to ADR 0120):

* MANIFEST: ``connector_id="ransomware_live"``, ``kind=CONNECTOR``, ``mode=EXTERNAL_IMPORT``,
  ``capability=PASSIVE``, ``status=IMPLEMENTED``.
* CONFIG SCHEMA (spec §8): ``additionalProperties: false``, ``required: ["dataset"]``, ``dataset``
  is an ``enum: ["recentvictims", "groups"]``; a smuggled key is rejected; ``api_key`` carries
  ``"secret": true`` (threatfox/opencorporates precedent).
* ``collect()`` (spec §4): the body is a flat JSON **array** (NOT the abuse.ch ``{id:[record]}``
  object shape) for BOTH datasets. One ``RawRecord`` per dict element; ``key`` = the permalink
  ``url`` (recentvictims) / the url-slug, the last path segment of ``groups[].url`` (groups). A
  non-dict element is skipped (no yield, no raise); a non-list top-level body yields zero records
  (no raise). ``limit`` hard-caps the yield count in order. The body is bounded to a 16 MiB cap
  (feodo/threatfox/urlhaus ``_read_bounded`` idiom) — a fabricated oversized body raises
  ``ValueError`` fail-closed, before any parsing. An optional ``api_key`` config value rides as an
  HTTP header (PRO-ready; free v2 ignores it) when configured, absent entirely when not — see
  AMBIGUITY NOTED #1 below for why this file does not pin the exact header NAME. ``url`` falls back
  to a dataset-specific pinned default (spec §7) when omitted.
* ``map()`` (spec §3.1-§3.3, §4) — shape-dispatch since ``map()`` receives no config: a record
  with a non-blank ``victim`` string is the VICTIM path (Company + thin group Organization +
  UnknownLink edge, THREE entities, all provenance-stamped — the codebase's first edge-emitting
  ``map()``, G1 on the edge too); else a record with a non-blank ``name`` string is the GROUP path
  (one rich Organization); else fail-soft ``[]``. Ids are deterministic connector-minted natural-key
  hashes/slugs (spec §3.1) — computed here via INDEPENDENT oracles (stdlib ``re``/``hashlib``),
  never borrowed from the connector. Victim Companies NEVER carry a ``topics`` value (the
  allegation lives on the disclaimed edge, never as a risk tag on the victim node); every group
  Organization (thin AND rich) ALWAYS carries ``topics=["crime.cyber"]`` (sensitivity park,
  catastrophic-merge guard). The group id derived from the victim side (``_slug(group)``) and from
  the groups side (``_slug(last path segment of url)``) is byte-identical for the same real-world
  group (the convergence pin). ``added_date``/``tools``/``ttps`` (groups) are raw-only — landing
  zone only, never surfacing on ANY FtM property of the mapped Organization.

FIXTURE RECORDS — hermetic hand-crafted samples shaped per spec §3 (field names/null-patterns
modeled on the spec's own probed 2026-07-23 shapes; victim/company/onion/URL values are entirely
fabricated, never the raw probe files per the gate instruction). Both documented ``infostealer``
shapes (the empty string AND the nested-dict shape) appear across the recentvictims fixtures so
``collect()``'s type-instability tolerance is exercised without ever mapping the field. Every
omission rule in spec §3.2/§3.3 (blank ``domain``/``country``, the ``"Not Found"`` ``activity``
sentinel, the ``"N/A"``/``"[AI generated] N/A"`` ``description`` placeholders, null ``altname``/
``description`` on the groups side) is pinned by a dedicated fixture + assertion.

AMBIGUITY NOTED #1 (test-author choice — the PRO API-key HTTP header NAME is explicitly UNPINNED
by the spec itself, §4 step 2 / §12 open item 1: "the exact PRO header name is third-party-sourced
... unconfirmed ... do not block"). Rather than guess a header key (e.g. ``X-API-KEY``) and risk
failing a builder who reasonably chose a different name for this genuinely undecided detail, the
header tests here assert on the request's header VALUES (is the configured ``api_key`` string
present as *some* header's value, never present when unconfigured) rather than pinning a specific
header key. This is the correctly-scoped version of the stated invariant ("api_key rides as an HTTP
header when configured, absent when not") without manufacturing an unstated requirement.

AMBIGUITY NOTED #2 (test-author choice — the connector CLASS NAME is unpinned anywhere in the spec
or ADR, same situation as the ``urlhaus``/``sslbl`` gates). Rather than guess an identifier, this
file discovers the connector class the same way ``plugins.registry.Registry.discover_module`` does:
introspect ``worldmonitor.plugins.connectors.ransomware_live.connector`` for the ONE concrete
``Connector`` subclass DEFINED there (spec §2's auto-discovery contract, ``manifest.connector_id``
alone is load-bearing, not the class name).

RED today (two distinct failure modes, precedent per ``tests/unit/test_urlhaus_connector.py`` /
``tests/unit/test_threatfox_connector.py``):
1. The top-level ``import worldmonitor.plugins.connectors.ransomware_live.connector`` fails at
   COLLECTION with ``ModuleNotFoundError`` — the ``ransomware_live`` package does not exist yet on
   this branch (slice 2 is the connector build itself). This means EVERY test in this module errors
   at collection, not merely fails — the expected RED shape for a wholly new component.
2. Independently of (1), this gate's slice 2 scope (per spec §9) is ONLY the connector package —
   no seed row is added until slice 3 — so this file intentionally carries NO seed-pin test (unlike
   the threatfox/urlhaus gates, which bundled their seed pin into the same PR). Nothing else in this
   module depends on pre-existing, already-red state; RED reason (1) is the only one in play here.

No live network: ``httpx.MockTransport`` is injected through the connector ``transport=`` ctor
kwarg (feodo/threatfox/urlhaus precedent), and ``socket.getaddrinfo`` is monkeypatched so the SSRF
host check runs with no real DNS. Any ``api_key`` literal used here is a short dummy (``"k" * 8``),
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

# Top-level import of the not-yet-built connector SUBMODULE — ModuleNotFoundError today (RED
# reason #1, see module docstring). Importing the submodule (not a specific class name) so the
# discovery helper below can introspect it without pinning an unstated class-name choice (AMBIGUITY
# NOTED #2).
import worldmonitor.plugins.connectors.ransomware_live.connector as _rl_connector_module
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import Capability, Connector, Kind, Mode, RawRecord, Status
from worldmonitor.provenance.model import Provenance, get_provenance

_API_KEY = "k" * 8  # short dummy — never a real key (secret-scan hook)

_TEST_RECENTVICTIMS_URL = "https://api.ransomware.live/v2/test-recentvictims/"
_TEST_GROUPS_URL = "https://api.ransomware.live/v2/test-groups/"
# spec §7's own pinned defaults (SeedSpec literal values) — the connector's fallback when `url`
# is omitted from config, per dataset.
_DEFAULT_RECENTVICTIMS_URL = "https://api.ransomware.live/v2/recentvictims"
_DEFAULT_GROUPS_URL = "https://api.ransomware.live/v2/groups"
_MAX_FEED_BYTES = 16 * 1024 * 1024  # spec §4/§6's own stated cap, asserted independently of the
# builder's private constant name (a hardcoded number is a strictly more decoupled oracle).

_PROV_VICTIMS = Provenance(
    source_id="ransomware_live:recentvictims",
    retrieved_at="2026-07-23T09:00:00Z",
    reliability="E",  # Admiralty "E" — unreliable, criminal self-declaration (spec §5/§1).
    source_record="s3://landing/ransomware_live/recentvictims-20260723.json",
)
_PROV_GROUPS = Provenance(
    source_id="ransomware_live:groups",
    retrieved_at="2026-07-23T09:00:00Z",
    reliability="E",
    source_record="s3://landing/ransomware_live/groups-20260723.json",
)


def _discover_connector_class(module: ModuleType) -> type[Connector]:
    """Find the ONE concrete ``Connector`` subclass defined in ``module``.

    Mirrors ``worldmonitor.plugins.registry.Registry.discover_module``'s own introspection
    (``inspect.getmembers`` + ``issubclass(obj, Connector)`` + "defined here, not imported")
    exactly — see AMBIGUITY NOTED #2 in the module docstring.
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


_RansomwareLiveConnector = _discover_connector_class(_rl_connector_module)

# --------------------------------------------------------------------------------------------------
# Fixture records — hermetic hand-crafted shapes (see module docstring). Field names / null
# patterns follow spec §3 exactly; every value is fabricated (never the raw probe files).
# --------------------------------------------------------------------------------------------------

# Full-shape victim record: domain/country/activity all present, infostealer as a NESTED DICT
# (the type-unstable shape), description carries the "[AI generated] N/A" placeholder (omitted).
_VICTIM_FULL: dict[str, Any] = {
    "victim": "Indigo Energy",
    "activity": "Energy & Utilities",
    "attackdate": "2026-07-23T07:58:08.356507+00:00",
    "claim_url": "http://exampleonionaddr1111111111111111111111111111111111111.onion/news.php?id=1",
    "country": "US",
    "data_size": None,
    "description": "[AI generated] N/A",
    "discovered": "2026-07-23T07:58:27.555881+00:00",
    "domain": "indigoenergy.example",
    "group": "moneymessage",
    "infostealer": {
        "employees": 0,
        "employees_url": 0,
        "infostealer_stats": {},
        "last_employee_compromised": None,
        "last_user_compromised": None,
        "thirdparties": 1,
        "update": "2026-07-23T08:35:17.907862",
        "users": 0,
        "users_url": 0,
    },
    "press": None,
    "ransom": None,
    "screenshot": "https://images.ransomware.live/victims/fake0001.png",
    "url": "https://www.ransomware.live/id/ZmFrZS1pbmRpZ28tZW5lcmd5",
}
# Same real-world shape but infostealer is a STRING ("") — the other documented shape; description
# carries the PLAIN "N/A" placeholder (also omitted).
_VICTIM_STR_INFOSTEALER: dict[str, Any] = {
    "victim": "Corporate 360 Business Solutions",
    "activity": "Professional Services",
    "attackdate": "2026-07-23T08:31:31.869359+00:00",
    "claim_url": "http://exampleonionaddr2222222222222222222222222222222222222.onion/site/blog?uuid=abc",
    "country": "CA",
    "data_size": None,
    "description": "N/A",
    "discovered": "2026-07-23T08:31:50.379676+00:00",
    "domain": "corporate360.example",
    "group": "qilin",
    "infostealer": "",
    "press": None,
    "ransom": None,
    "screenshot": "https://images.ransomware.live/victims/fake0002.png",
    "url": "https://www.ransomware.live/id/ZmFrZS1jb3Jwb3JhdGUzNjA=",
}
# domain == "" (empty, omitted); a REAL (non-placeholder) description -> the edge summary IS
# mapped.
_VICTIM_EMPTY_DOMAIN: dict[str, Any] = {
    "victim": "Canal 9 Litoral",
    "activity": "Technology",
    "attackdate": "2026-07-22T00:13:23.197716+00:00",
    "claim_url": "http://exampleonionaddr3333333333333333333333333333333333333.onion/canal-9-litoral",
    "country": "AR",
    "data_size": None,
    "description": (
        "Canal 9 Litoral is a regional news outlet; alleged internal documents and subscriber "
        "records were exfiltrated prior to publication on the leak site."
    ),
    "discovered": "2026-07-22T00:14:37.054785+00:00",
    "domain": "",
    "group": "nova",
    "infostealer": "",
    "press": None,
    "ransom": None,
    "screenshot": "https://images.ransomware.live/victims/fake0003.png",
    "url": "https://www.ransomware.live/id/ZmFrZS1jYW5hbDlsaXRvcmFs",
}
# country == "" (empty, omitted).
_VICTIM_EMPTY_COUNTRY: dict[str, Any] = {
    "victim": "Infina Health",
    "activity": "Healthcare",
    "attackdate": "2026-07-22T16:04:51.606583+00:00",
    "claim_url": "http://exampleonionaddr4444444444444444444444444444444444444.onion/site/blog?uuid=def",
    "country": "",
    "data_size": None,
    "description": "N/A",
    "discovered": "2026-07-22T16:05:55.661998+00:00",
    "domain": "infinahealth.example",
    "group": "qilin",
    "infostealer": "",
    "press": None,
    "ransom": None,
    "screenshot": "",
    "url": "https://www.ransomware.live/id/ZmFrZS1pbmZpbmFoZWFsdGg=",
}
# activity == "Not Found" (the sentinel, omitted).
_VICTIM_NOTFOUND_ACTIVITY: dict[str, Any] = {
    "victim": "AppleOne Properties",
    "activity": "Not Found",
    "attackdate": "2026-07-23T08:29:05.190382+00:00",
    "claim_url": "http://exampleonionaddr5555555555555555555555555555555555555.onion/site/blog?uuid=ghi",
    "country": "PH",
    "data_size": None,
    "description": "N/A",
    "discovered": "2026-07-23T08:29:29.281906+00:00",
    "domain": "appleoneproperties.example",
    "group": "qilin",
    "infostealer": "",
    "press": None,
    "ransom": None,
    "screenshot": "https://images.ransomware.live/victims/fake0005.png",
    "url": "https://www.ransomware.live/id/ZmFrZS1hcHBsZW9uZQ==",
}
# group == "BrainCipher" (mixed case) — the convergence-pin fixture's victim-side half.
_VICTIM_CLAIMED_BY_BRAINCIPHER: dict[str, Any] = {
    "victim": "Convergence Target Co",
    "activity": "Manufacturing",
    "attackdate": "2026-07-20T10:00:00.000000+00:00",
    "claim_url": "http://braincipheronionaddr666666666666666666666666666666666.onion/post/1",
    "country": "DE",
    "data_size": None,
    "description": "N/A",
    "discovered": "2026-07-20T10:05:00.000000+00:00",
    "domain": "convergencetarget.example",
    "group": "BrainCipher",
    "infostealer": "",
    "press": None,
    "ransom": None,
    "screenshot": "https://images.ransomware.live/victims/fake0006.png",
    "url": "https://www.ransomware.live/id/ZmFrZS1jb252ZXJnZW5jZQ==",
}
# blank victim (the identity field) — must fail-soft to [] rather than raise.
_VICTIM_BLANK: dict[str, Any] = {
    "victim": "",
    "activity": "Technology",
    "attackdate": "2026-07-19T00:00:00.000000+00:00",
    "claim_url": "http://exampleonionaddr7777777777777777777777777777777777777.onion/blank",
    "country": "US",
    "description": "N/A",
    "domain": "blank.example",
    "group": "somegroup",
    "infostealer": "",
    "url": "https://www.ransomware.live/id/ZmFrZS1ibGFuaw==",
}
# Neither victim- nor group-shaped (no "victim" key, no "name" key) — must fail-soft to [].
_NEITHER_SHAPE_RECORD: dict[str, Any] = {"foo": "bar", "baz": 123}

# Recentvictims collect() traversal fixture: a flat JSON array, 3 well-formed elements spanning
# BOTH documented `infostealer` shapes.
_RECENTVICTIMS_FEED: list[dict[str, Any]] = [
    _VICTIM_FULL,
    _VICTIM_STR_INFOSTEALER,
    _VICTIM_EMPTY_DOMAIN,
]
# Hostile variant (spec §6/§4): a non-dict array element must be skipped, never raised.
_HOSTILE_RECENTVICTIMS_FEED: list[Any] = [123, "not-a-dict", _VICTIM_FULL]

# --- groups fixtures --------------------------------------------------------------------------

# Rich group record — every mappable field populated (name/altname/description/locations/url);
# `url` last path segment is "braincipher" (the convergence-pin fixture's groups-side half).
_GROUP_RICH: dict[str, Any] = {
    "added_date": "2026-01-28",
    "altname": "Brain Cipher Collective",
    "description": (
        "The group appears active; claims high-profile victims across multiple sectors."
    ),
    "locations": [
        {
            "available": True,
            "enabled": True,
            "fqdn": "braincipheronionaddr888888888888888888888888888888888.onion",
            "slug": "http://braincipheronionaddr888888888888888888888888888888888.onion",
            "title": "BrainCipher Leak Site",
            "type": "DLS",
        }
    ],
    "name": "BrainCipher",
    "tools": ["Cobalt Strike"],
    "ttps": [
        {
            "tactic_id": "TA0001",
            "tactic_name": "Initial Access",
            "techniques": [
                {
                    "technique_details": "",
                    "technique_id": "T1078",
                    "technique_name": "Valid Accounts",
                }
            ],
        }
    ],
    "url": "https://www.ransomware.live/group/braincipher",
}
# Minimal group record — altname/description null (both omitted), tools/ttps empty; distinct
# url-slug ("0daysyndicate") from the rich fixture for the collect() traversal test.
_GROUP_MINIMAL: dict[str, Any] = {
    "added_date": None,
    "altname": None,
    "description": None,
    "locations": [
        {
            "available": True,
            "enabled": True,
            "fqdn": "zerodayonionaddr9999999999999999999999999999999999999.onion",
            "slug": "http://zerodayonionaddr9999999999999999999999999999999999999.onion",
            "title": "0day | Command Ops",
            "type": "DLS",
        }
    ],
    "name": "0day Syndicate",
    "tools": [],
    "ttps": [],
    "url": "https://www.ransomware.live/group/0daysyndicate",
}

_GROUPS_FEED: list[dict[str, Any]] = [_GROUP_RICH, _GROUP_MINIMAL]
_HOSTILE_GROUPS_FEED: list[Any] = [None, 42, _GROUP_MINIMAL]

_ISO_DATE_ATTACKDATE = "2026-07-23T07:58:08.356507+00:00"  # _VICTIM_FULL["attackdate"]


# --------------------------------------------------------------------------------------------------
# Independent id oracles (spec §3.1) — computed here via stdlib `re`/`hashlib` ONLY, never
# imported from (or otherwise derived from) the connector's own implementation.
# --------------------------------------------------------------------------------------------------


def _slug_oracle(value: str) -> str:
    """``_slug(x) = re.sub(r"[^a-z0-9]+", "", x.lower())`` — spec §3.1, verbatim."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _h_oracle(value: str) -> str:
    """``_h(s) = sha1(s.encode("utf-8")).hexdigest()[:16]`` — spec §3.1, verbatim."""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _group_id_oracle(slug_or_name: str) -> str:
    return f"ransomware-live-group-{_slug_oracle(slug_or_name)}"


def _victim_id_oracle(permalink_url: str) -> str:
    return f"ransomware-live-victim-{_h_oracle(permalink_url)}"


def _claim_id_oracle(group_id: str, victim_id: str, permalink_url: str) -> str:
    joined = group_id + chr(10) + victim_id + chr(10) + permalink_url
    return f"ransomware-live-claim-{_h_oracle(joined)}"


def _url_slug(url: str) -> str:
    """The raw last path segment of a ``groups[].url`` value (test-side helper, not the oracle
    itself — mirrors spec §3.1's "last path segment of groups[].url" definition)."""
    return url.rstrip("/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------------------------------
# Hermetic-HTTP helpers (mirror tests/unit/test_urlhaus_connector.py / test_threatfox_connector.py).
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
    """Wrap a raw Ransomware.live entry dict as the JSON ``RawRecord`` that ``map()`` consumes."""
    return RawRecord(
        key=key,
        data=json.dumps(entry).encode("utf-8"),
        retrieved_at=_PROV_VICTIMS.retrieved_at,
        content_type="application/json",
    )


def _only(entities: list[FtmEntity], schema_name: str) -> FtmEntity:
    """Assert ``entities`` carries EXACTLY one entity of ``schema_name`` and return it."""
    matches = [e for e in entities if e.schema.name == schema_name]
    assert len(matches) == 1, (
        f"expected exactly one {schema_name!r} entity, found {len(matches)} among schemas "
        f"{[e.schema.name for e in entities]!r}"
    )
    return matches[0]


# --------------------------------------------------------------------------------------------------
# 1. Manifest
# --------------------------------------------------------------------------------------------------


def test_manifest_is_external_import_passive_and_implemented() -> None:
    manifest = _RansomwareLiveConnector().manifest
    assert manifest.connector_id == "ransomware_live"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# 2. Config schema — closed schema, required dataset enum, api_key secret.
# --------------------------------------------------------------------------------------------------


def test_config_schema_valid_dataset_validates_and_rejects_missing_or_smuggled_keys() -> None:
    connector = _RansomwareLiveConnector()
    schema = connector.config_schema
    assert schema["additionalProperties"] is False
    assert "dataset" in schema.get("required", [])

    connector.validate_config({"dataset": "groups"})  # minimal valid config
    connector.validate_config(
        {
            "dataset": "recentvictims",
            "url": _TEST_RECENTVICTIMS_URL,
            "limit": 5,
            "api_key": _API_KEY,
        }
    )

    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({})  # missing required `dataset`

    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"dataset": "groups", "bogus": 1})  # smuggled key


def test_config_schema_dataset_enum_is_enforced() -> None:
    connector = _RansomwareLiveConnector()
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"dataset": "not-a-real-dataset"})


def test_config_schema_api_key_is_marked_secret() -> None:
    schema = _RansomwareLiveConnector().config_schema
    assert schema["properties"]["api_key"]["secret"] is True


# --------------------------------------------------------------------------------------------------
# 3. collect() — flat-array traversal (both datasets), keying, limit, non-dict skip, 16 MiB cap,
#    api_key header value, per-dataset default url, non-list top-level body.
# --------------------------------------------------------------------------------------------------


def test_collect_recentvictims_yields_one_record_per_element_keyed_by_permalink_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(transport=_transport_serving(_RECENTVICTIMS_FEED, calls))

    records = list(connector.collect({"dataset": "recentvictims", "url": _TEST_RECENTVICTIMS_URL}))

    assert calls, "collect() never issued a request through the transport"
    assert len(records) == 3, "collect() must yield ONE RawRecord per array element"
    assert [r.key for r in records] == [entry["url"] for entry in _RECENTVICTIMS_FEED], (
        "RawRecord.key must be the permalink `url` for recentvictims"
    )
    parsed = [json.loads(r.data) for r in records]
    assert parsed == _RECENTVICTIMS_FEED, (
        "collect() must preserve every record byte-for-byte, including BOTH infostealer "
        "shapes ('' string and a nested dict) without ever crashing or dropping either"
    )


def test_collect_recentvictims_skips_non_dict_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(
        transport=_transport_serving(_HOSTILE_RECENTVICTIMS_FEED, calls)
    )

    records = list(
        connector.collect({"dataset": "recentvictims", "url": _TEST_RECENTVICTIMS_URL})
    )  # must not raise

    assert len(records) == 1, (
        f"expected ONLY the well-formed dict element to yield, got {len(records)} records"
    )
    assert json.loads(records[0].data) == _VICTIM_FULL


def test_collect_recentvictims_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(transport=_transport_serving(_RECENTVICTIMS_FEED, calls))

    records = list(
        connector.collect({"dataset": "recentvictims", "url": _TEST_RECENTVICTIMS_URL, "limit": 2})
    )

    assert len(records) == 2
    parsed = [json.loads(r.data) for r in records]
    assert parsed == [_VICTIM_FULL, _VICTIM_STR_INFOSTEALER], "limit must hard-cap in array order"


def test_collect_groups_yields_one_record_per_element_keyed_by_url_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(transport=_transport_serving(_GROUPS_FEED, calls))

    records = list(connector.collect({"dataset": "groups", "url": _TEST_GROUPS_URL}))

    assert calls, "collect() never issued a request through the transport"
    assert len(records) == 2
    assert [r.key for r in records] == [
        _url_slug(_GROUP_RICH["url"]),
        _url_slug(_GROUP_MINIMAL["url"]),
    ], "RawRecord.key must be the url-slug (last path segment of groups[].url) for groups"
    parsed = [json.loads(r.data) for r in records]
    assert parsed == _GROUPS_FEED


def test_collect_groups_skips_non_dict_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(transport=_transport_serving(_HOSTILE_GROUPS_FEED, calls))

    records = list(
        connector.collect({"dataset": "groups", "url": _TEST_GROUPS_URL})
    )  # must not raise

    assert len(records) == 1
    assert json.loads(records[0].data) == _GROUP_MINIMAL


def test_collect_fails_closed_on_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body larger than the 16 MiB cap raises (fail-closed) — never an unbounded read."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    oversized = b"A" * (_MAX_FEED_BYTES + 4096)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"content-type": "application/json"})

    connector = _RansomwareLiveConnector(transport=httpx.MockTransport(_handler))

    with pytest.raises(ValueError):
        list(connector.collect({"dataset": "groups", "url": _TEST_GROUPS_URL}))


def test_collect_sends_api_key_value_in_a_header_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AMBIGUITY NOTED #1 (module docstring): the header NAME is unpinned by the spec, so this
    asserts on header VALUES rather than a specific header key."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(transport=_transport_serving(_GROUPS_FEED, calls))

    list(connector.collect({"dataset": "groups", "url": _TEST_GROUPS_URL, "api_key": _API_KEY}))

    assert calls, "collect() never issued a request"
    header_values = list(calls[0].headers.values())
    assert _API_KEY in header_values, (
        f"expected the configured api_key to ride as SOME request header's value, "
        f"got headers={dict(calls[0].headers)!r}"
    )


def test_collect_omits_api_key_value_from_headers_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(transport=_transport_serving(_GROUPS_FEED, calls))

    list(connector.collect({"dataset": "groups", "url": _TEST_GROUPS_URL}))

    assert calls, "collect() never issued a request"
    header_values = list(calls[0].headers.values())
    assert _API_KEY not in header_values, (
        f"the api_key value must be ABSENT from every header when not configured, "
        f"got headers={dict(calls[0].headers)!r}"
    )


@pytest.mark.parametrize(
    ("dataset", "expected_url", "feed"),
    [
        ("recentvictims", _DEFAULT_RECENTVICTIMS_URL, _RECENTVICTIMS_FEED),
        ("groups", _DEFAULT_GROUPS_URL, _GROUPS_FEED),
    ],
    ids=["recentvictims", "groups"],
)
def test_collect_uses_pinned_default_url_per_dataset_when_url_omitted(
    monkeypatch: pytest.MonkeyPatch, dataset: str, expected_url: str, feed: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(transport=_transport_serving(feed, calls))

    list(connector.collect({"dataset": dataset}))  # `url` omitted entirely

    assert calls, "collect() never issued a request"
    assert str(calls[0].url) == expected_url, (
        f"dataset={dataset!r} with `url` omitted must default to {expected_url!r} (spec §7), "
        f"got {calls[0].url!r}"
    )


def test_collect_top_level_non_list_returns_zero_records_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = _RansomwareLiveConnector(
        transport=_transport_serving({"not": "a list", "message": "throttled"}, calls)
    )

    records = list(
        connector.collect({"dataset": "recentvictims", "url": _TEST_RECENTVICTIMS_URL})
    )  # must not raise

    assert records == [], "a non-list top-level body must be treated as zero records, never raise"


# --------------------------------------------------------------------------------------------------
# 4. map() victim path — Company + thin Organization + UnknownLink edge, oracle ids, provenance.
# --------------------------------------------------------------------------------------------------


def test_map_victim_path_emits_exactly_three_entities_with_oracle_ids_and_provenance() -> None:
    connector = _RansomwareLiveConnector()
    record = _record(_VICTIM_FULL, key=_VICTIM_FULL["url"])

    entities = list(connector.map(record, provenance=_PROV_VICTIMS))

    assert len(entities) == 3, (
        f"a victim record must map to EXACTLY 3 entities (Company + thin Organization + "
        f"UnknownLink edge), got {len(entities)}: {[e.schema.name for e in entities]!r}"
    )
    schemas = sorted(e.schema.name for e in entities)
    assert schemas == ["Company", "Organization", "UnknownLink"]

    company = _only(entities, "Company")
    group_org = _only(entities, "Organization")
    edge = _only(entities, "UnknownLink")

    expected_victim_id = _victim_id_oracle(_VICTIM_FULL["url"])
    expected_group_id = _group_id_oracle(_VICTIM_FULL["group"])
    expected_edge_id = _claim_id_oracle(expected_group_id, expected_victim_id, _VICTIM_FULL["url"])

    # -- victim Company -----------------------------------------------------------------------
    assert company.id == expected_victim_id
    assert company.get("name") == ["Indigo Energy"]
    assert company.get("website") == ["indigoenergy.example"]
    assert company.get("country") == ["US"]
    assert company.get("sector") == ["Energy & Utilities"]
    assert company.get("topics", quiet=True) == [], "victim Company must NEVER carry a risk topic"

    # -- thin group Organization ----------------------------------------------------------------
    assert group_org.id == expected_group_id
    assert group_org.get("name") == ["moneymessage"]
    assert group_org.get("weakAlias") == ["moneymessage"]
    assert group_org.get("topics") == ["crime.cyber"]

    # -- UnknownLink edge -------------------------------------------------------------------------
    assert edge.id == expected_edge_id
    assert edge.get("subject") == [expected_group_id]
    assert edge.get("object") == [expected_victim_id]
    assert edge.get("role") == ["ransomware victim (claimed by group)"]
    assert edge.get("date") == ["2026-07-23T07:58:08"], (
        "attackdate must be added via entity.add() (FtM date cleaning)"
    )
    assert edge.get("sourceUrl") == [_VICTIM_FULL["claim_url"]]
    assert edge.get("summary", quiet=True) == [], (
        '"[AI generated] N/A" must omit the edge summary (still emit the edge)'
    )

    # -- provenance round-trips on ALL THREE -------------------------------------------------------
    for entity in entities:
        prov = get_provenance(entity)
        assert prov is not None, f"{entity.schema.name} {entity.id} lost its provenance"
        assert prov == _PROV_VICTIMS
        assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])


def test_map_victim_path_omits_website_when_domain_blank() -> None:
    connector = _RansomwareLiveConnector()
    entities = list(connector.map(_record(_VICTIM_EMPTY_DOMAIN), provenance=_PROV_VICTIMS))

    assert len(entities) == 3, "the Company must still be emitted when domain is blank"
    company = _only(entities, "Company")
    assert company.get("website", quiet=True) == []
    assert company.get("country") == ["AR"], "other fields must still map normally"


def test_map_victim_path_omits_country_when_blank() -> None:
    connector = _RansomwareLiveConnector()
    entities = list(connector.map(_record(_VICTIM_EMPTY_COUNTRY), provenance=_PROV_VICTIMS))

    assert len(entities) == 3, "the Company must still be emitted when country is blank"
    company = _only(entities, "Company")
    assert company.get("country", quiet=True) == []
    assert company.get("website") == ["infinahealth.example"]


def test_map_victim_path_omits_sector_when_activity_is_not_found_sentinel() -> None:
    connector = _RansomwareLiveConnector()
    entities = list(connector.map(_record(_VICTIM_NOTFOUND_ACTIVITY), provenance=_PROV_VICTIMS))

    assert len(entities) == 3
    company = _only(entities, "Company")
    assert company.get("sector", quiet=True) == [], '"Not Found" activity must omit sector'


def test_map_victim_path_omits_summary_when_description_is_plain_na() -> None:
    connector = _RansomwareLiveConnector()
    entities = list(connector.map(_record(_VICTIM_STR_INFOSTEALER), provenance=_PROV_VICTIMS))

    edge = _only(entities, "UnknownLink")
    assert edge.get("summary", quiet=True) == [], 'a plain "N/A" description must omit the summary'


def test_map_victim_path_maps_a_real_description_as_edge_summary() -> None:
    connector = _RansomwareLiveConnector()
    entities = list(connector.map(_record(_VICTIM_EMPTY_DOMAIN), provenance=_PROV_VICTIMS))

    edge = _only(entities, "UnknownLink")
    assert edge.get("summary") == [_VICTIM_EMPTY_DOMAIN["description"]], (
        "a non-placeholder description must map verbatim to the edge summary"
    )


# --------------------------------------------------------------------------------------------------
# 5. map() group path — one rich Organization; added_date/tools/ttps never leak onto ANY property.
# --------------------------------------------------------------------------------------------------


def test_map_group_path_emits_one_rich_organization_with_expected_fields_and_provenance() -> None:
    connector = _RansomwareLiveConnector()
    entities = list(
        connector.map(
            _record(_GROUP_RICH, key=_url_slug(_GROUP_RICH["url"])), provenance=_PROV_GROUPS
        )
    )

    assert len(entities) == 1, (
        f"a group record must map to exactly ONE Organization, got {entities!r}"
    )
    org = entities[0]
    assert org.schema.name == "Organization"

    expected_id = _group_id_oracle(_url_slug(_GROUP_RICH["url"]))
    assert org.id == expected_id
    assert org.get("name") == ["BrainCipher"]
    assert org.get("alias") == ["Brain Cipher Collective"]
    assert org.get("description") == [_GROUP_RICH["description"]]
    assert org.get("website") == [_GROUP_RICH["locations"][0]["slug"]]
    assert org.get("sourceUrl") == [_GROUP_RICH["url"]]
    assert org.get("topics") == ["crime.cyber"]
    assert org.get("weakAlias") == [_url_slug(_GROUP_RICH["url"])], (
        "weakAlias must be the RAW url-slug, not a re-normalized value"
    )

    prov = get_provenance(org)
    assert prov is not None
    assert prov == _PROV_GROUPS
    assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])


def test_map_group_path_never_maps_added_date_tools_or_ttps() -> None:
    """`added_date`/`tools`/`ttps` are raw-only (landing zone) — this asserts they never leak onto
    ANY FtM property of the mapped Organization (a whitelist of the entire populated-property set,
    not merely that three specific property NAMES are absent — FtM silently drops unknown property
    keys, so a naive "no `added_date` property" check would pass vacuously)."""
    connector = _RansomwareLiveConnector()
    entities = list(connector.map(_record(_GROUP_RICH), provenance=_PROV_GROUPS))

    assert len(entities) == 1
    org = entities[0]
    populated = {name for name, values in org.properties.items() if values}
    expected = {"name", "alias", "description", "website", "sourceUrl", "topics", "weakAlias"}
    assert populated == expected, (
        f"expected populated properties {expected}, got {populated} — added_date/tools/ttps "
        f"(raw-only per spec §3.3) must never surface on the mapped Organization"
    )


def test_map_group_path_omits_alias_and_description_when_null() -> None:
    connector = _RansomwareLiveConnector()
    entities = list(
        connector.map(
            _record(_GROUP_MINIMAL, key=_url_slug(_GROUP_MINIMAL["url"])), provenance=_PROV_GROUPS
        )
    )

    assert len(entities) == 1
    org = entities[0]
    assert org.get("name") == ["0day Syndicate"]
    assert org.get("alias", quiet=True) == [], "null altname must omit alias"
    assert org.get("description", quiet=True) == [], "null description must omit description"
    assert org.get("website") == [_GROUP_MINIMAL["locations"][0]["slug"]]
    assert org.get("topics") == ["crime.cyber"]


# --------------------------------------------------------------------------------------------------
# 6. Convergence pin — victim-side and groups-side group ids are byte-identical.
# --------------------------------------------------------------------------------------------------


def test_group_id_converges_between_victim_side_and_groups_side() -> None:
    connector = _RansomwareLiveConnector()

    victim_entities = list(
        connector.map(_record(_VICTIM_CLAIMED_BY_BRAINCIPHER), provenance=_PROV_VICTIMS)
    )
    thin_org = _only(victim_entities, "Organization")

    group_entities = list(
        connector.map(
            _record(_GROUP_RICH, key=_url_slug(_GROUP_RICH["url"])), provenance=_PROV_GROUPS
        )
    )
    assert len(group_entities) == 1
    rich_org = group_entities[0]
    assert rich_org.schema.name == "Organization"

    assert thin_org.id == rich_org.id, (
        f"victim-side group={_VICTIM_CLAIMED_BY_BRAINCIPHER['group']!r} thin org id "
        f"{thin_org.id!r} must equal groups-side url={_GROUP_RICH['url']!r} rich org id "
        f"{rich_org.id!r} (spec §3.1 convergence pin)"
    )
    assert thin_org.id == _group_id_oracle("braincipher")
    assert rich_org.id == _group_id_oracle("braincipher")


# --------------------------------------------------------------------------------------------------
# 7. Fail-soft — blank victim, neither-shape record, id determinism on re-map.
# --------------------------------------------------------------------------------------------------


def test_map_blank_victim_returns_empty_list() -> None:
    connector = _RansomwareLiveConnector()
    assert list(connector.map(_record(_VICTIM_BLANK), provenance=_PROV_VICTIMS)) == []


def test_map_neither_victim_nor_group_shaped_record_returns_empty_list() -> None:
    connector = _RansomwareLiveConnector()
    assert list(connector.map(_record(_NEITHER_SHAPE_RECORD), provenance=_PROV_VICTIMS)) == []


def test_map_is_deterministic_on_remap() -> None:
    connector = _RansomwareLiveConnector()
    victim_record = _record(_VICTIM_FULL, key=_VICTIM_FULL["url"])

    first = list(connector.map(victim_record, provenance=_PROV_VICTIMS))
    second = list(connector.map(victim_record, provenance=_PROV_VICTIMS))
    assert len(first) == len(second) == 3
    assert sorted(e.id for e in first) == sorted(e.id for e in second), (
        "re-mapping the identical raw record must yield IDENTICAL ids (idempotent re-ingest)"
    )

    group_record = _record(_GROUP_RICH, key=_url_slug(_GROUP_RICH["url"]))
    first_group = list(connector.map(group_record, provenance=_PROV_GROUPS))
    second_group = list(connector.map(group_record, provenance=_PROV_GROUPS))
    assert len(first_group) == len(second_group) == 1
    assert first_group[0].id == second_group[0].id


# --------------------------------------------------------------------------------------------------
# 8. Victims never get a risk topic; thin AND rich group Orgs always carry crime.cyber.
# --------------------------------------------------------------------------------------------------


def test_victim_company_never_carries_a_topics_value() -> None:
    connector = _RansomwareLiveConnector()
    for victim_record in (
        _VICTIM_FULL,
        _VICTIM_STR_INFOSTEALER,
        _VICTIM_EMPTY_DOMAIN,
        _VICTIM_EMPTY_COUNTRY,
        _VICTIM_NOTFOUND_ACTIVITY,
    ):
        entities = list(connector.map(_record(victim_record), provenance=_PROV_VICTIMS))
        company = _only(entities, "Company")
        assert company.get("topics", quiet=True) == [], (
            f"victim Company for victim={victim_record['victim']!r} must carry NO topics value "
            f"at all — the allegation lives on the disclaimed edge, never a risk tag on the "
            f"victim node"
        )


def test_thin_and_rich_group_organizations_always_carry_crime_cyber_topic() -> None:
    connector = _RansomwareLiveConnector()

    thin_entities = list(connector.map(_record(_VICTIM_FULL), provenance=_PROV_VICTIMS))
    thin_org = _only(thin_entities, "Organization")
    assert thin_org.get("topics") == ["crime.cyber"], "the victim-side thin Organization"

    rich_entities = list(
        connector.map(
            _record(_GROUP_RICH, key=_url_slug(_GROUP_RICH["url"])), provenance=_PROV_GROUPS
        )
    )
    rich_org = _only(rich_entities, "Organization")
    assert rich_org.get("topics") == ["crime.cyber"], "the groups-side rich Organization"

    minimal_entities = list(
        connector.map(
            _record(_GROUP_MINIMAL, key=_url_slug(_GROUP_MINIMAL["url"])), provenance=_PROV_GROUPS
        )
    )
    minimal_org = _only(minimal_entities, "Organization")
    assert minimal_org.get("topics") == ["crime.cyber"], (
        "even a minimal (mostly-null) group record must carry crime.cyber — every group Org is "
        "sensitive, unconditionally"
    )
