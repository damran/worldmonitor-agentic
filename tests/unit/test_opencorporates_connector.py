"""Primary invariant tests (RED) for the OpenCorporates connector — ADR 0065.

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/opencorporates/`` (a ``RestApiConnector`` subclass):

* MANIFEST: ``kind=CONNECTOR``, ``mode=EXTERNAL_IMPORT``, ``capability=PASSIVE`` (the driver
  refuses ``ACTIVE``).
* CONFIG SCHEMA: ``api_token`` is a SECRET (``"secret": true``); ``required`` lists both
  ``api_token`` AND ``q``; ``additionalProperties: false``; ``per_page`` ``<=100``;
  ``max_pages`` ``>=1``.
* ``map()``: over a REAL-API-shaped fixture (the inner ``company`` object) emits ONE FtM ``Company``
  with name / registrationNumber / jurisdiction, the ``opencorporates_id`` anchor
  ``"{jurisdiction_code}/{company_number}"``, and PROVENANCE round-trips via ``get_provenance``.
  An identity-less record maps to ``[]`` (fail-soft on one row).
* ``collect()``: paginates ``>=2`` pages over ``httpx.MockTransport`` (NO live HTTP), yields ONE
  ``RawRecord`` per company (key ``"{jurisdiction_code}/{company_number}"``, content-type
  ``application/json``), STOPS at ``total_pages``, and is HARD-bounded by ``max_pages`` (a payload
  claiming 999 pages still stops at ``max_pages``).
* SSRF: every fetch goes through ``net.ssrf.guarded_stream`` — a host that resolves to a private
  address is blocked BEFORE any request leaves.
* SECRET: the ``api_token`` (and the token-bearing request URL) is NEVER written to ANY logger —
  not the connector's ``worldmonitor`` tree AND not ``httpx`` (which logs the full request URL,
  ``"HTTP Request: GET <url> ..."``, at INFO).

RED today: ``worldmonitor.plugins.connectors.opencorporates`` does not exist (nor
``worldmonitor.plugins.rest_api``), so the top-level import raises ``ModuleNotFoundError`` and the
whole module errors at collection — the correct RED. GREEN once the builder lands the connector.

No live network: ``httpx.MockTransport`` is injected through the connector ``transport=`` ctor kwarg
(forwarded to ``guarded_stream``), and ``socket.getaddrinfo`` is monkeypatched to a public IP so the
SSRF host check passes with no real DNS — the pattern from ``tests/unit/test_ssrf_guard.py``.
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest

from worldmonitor.net.ssrf import BlockedAddressError
from worldmonitor.ontology.anchors import get_anchors
from worldmonitor.plugins.base import Capability, Kind, Mode, RawRecord, Status

# Top-level import of the not-yet-built connector — ModuleNotFoundError today (correct RED).
from worldmonitor.plugins.connectors.opencorporates import OpenCorporatesConnector
from worldmonitor.provenance.model import Provenance, get_provenance

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "opencorporates"

_PROV = Provenance(
    source_id="opencorporates:gb/acme",
    retrieved_at="2026-06-28T00:00:00Z",
    reliability="B",
    source_record="s3://landing/opencorporates/gb_01234567.json",
)


# --------------------------------------------------------------------------------------------------
# Hermetic-HTTP helpers (mirror tests/unit/test_ssrf_guard.py): a fake getaddrinfo so the guard's
# assert_public_host resolves the API host to a chosen IP with NO real DNS, plus a MockTransport
# that serves the captured page fixtures by ``page`` query param (NO real HTTP).
# --------------------------------------------------------------------------------------------------


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in resolving EVERY host to ``ip`` (one IPv4 5-tuple)."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def _load(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text("utf-8"))


def _page_fixture_transport(calls: list[httpx.Request]) -> httpx.MockTransport:
    """Serve companies_page{1,2}.json keyed on the ``page`` query param; record every request."""
    page_body = {
        "1": (_FIXTURES / "companies_page1.json").read_bytes(),
        "2": (_FIXTURES / "companies_page2.json").read_bytes(),
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        page = request.url.params.get("page", "1")
        body = page_body.get(page)
        if body is None:  # an out-of-range page must never be requested
            return httpx.Response(200, json={"results": {"companies": [], "total_pages": 2}})
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    return httpx.MockTransport(_handler)


# --------------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------------


def test_manifest_is_external_import_and_passive() -> None:
    """The connector is a PASSIVE EXTERNAL_IMPORT CONNECTOR (the driver refuses ACTIVE)."""
    manifest = OpenCorporatesConnector().manifest
    assert manifest.connector_id == "opencorporates"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — secret token + required q + bounds
# --------------------------------------------------------------------------------------------------


def test_config_schema_marks_api_token_secret_and_requires_q() -> None:
    """api_token is a UI secret; q + api_token are required; schema is closed + bounded."""
    schema = OpenCorporatesConnector().config_schema

    props = schema["properties"]
    # The api_token is a SECRET field (drives the UI password input + vault encryption at rest).
    assert props["api_token"].get("secret") is True
    assert props["api_token"]["type"] == "string"

    # Both the secret token AND the search term are required.
    assert "api_token" in schema["required"]
    assert "q" in schema["required"]

    # Closed schema (no smuggled extra keys) and the documented numeric bounds.
    assert schema["additionalProperties"] is False
    assert props["per_page"]["type"] == "integer"
    assert props["per_page"]["maximum"] == 100
    assert props["max_pages"]["type"] == "integer"
    assert props["max_pages"]["minimum"] >= 1


def test_validate_config_rejects_missing_secret_or_query_and_accepts_valid() -> None:
    """A config missing api_token or q is rejected; a complete config validates."""
    connector = OpenCorporatesConnector()

    connector.validate_config({"api_token": "tok", "q": "acme"})  # must not raise

    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"q": "acme"})  # api_token missing
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"api_token": "tok"})  # q missing
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"api_token": "tok", "q": "acme", "per_page": 999})  # > 100


# --------------------------------------------------------------------------------------------------
# map() — FtM Company + anchor + provenance round-trip
# --------------------------------------------------------------------------------------------------


def test_map_emits_ftm_company_with_anchor_and_provenance() -> None:
    """The inner company object maps to ONE FtM Company with its anchor + stamped provenance."""
    company = _load("company.json")
    record = RawRecord(
        key="gb/01234567",
        data=json.dumps(company).encode("utf-8"),
        retrieved_at=_PROV.retrieved_at,
        content_type="application/json",
    )

    entities = list(OpenCorporatesConnector().map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]

    # Validated FtM Company with the mapped business properties.
    assert entity.schema.name == "Company"
    assert entity.id == "opencorporates-gb-01234567"
    assert entity.get("name") == ["ACME LIMITED"]
    assert entity.get("registrationNumber") == ["01234567"]
    assert entity.get("jurisdiction") == ["gb"]

    # Canonical anchor: opencorporates_id == "{jurisdiction_code}/{company_number}".
    assert get_anchors(entity)["opencorporates_id"] == "gb/01234567"

    # Provenance round-trips intact (the non-negotiable invariant: every mapped entity is stamped).
    prov = get_provenance(entity)
    assert prov is not None
    assert prov == _PROV
    assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])


def test_map_skips_identityless_record() -> None:
    """A record with no company_number/name is dropped (fail-soft on one row), not raised."""
    record = RawRecord(
        key="gb/",
        data=json.dumps({"jurisdiction_code": "gb"}).encode("utf-8"),
        retrieved_at=_PROV.retrieved_at,
    )
    assert list(OpenCorporatesConnector().map(record, provenance=_PROV)) == []


# --------------------------------------------------------------------------------------------------
# collect() — paginates over MockTransport, one record per company, bounded
# --------------------------------------------------------------------------------------------------


def test_collect_paginates_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """collect() walks both pages over MockTransport, yielding one RawRecord per company.

    The api_token + page are carried on the request URL (asserted from the captured requests);
    every company across the 2 pages becomes its own RawRecord keyed
    ``"{jurisdiction_code}/{company_number}"`` with content-type ``application/json``.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = OpenCorporatesConnector(transport=_page_fixture_transport(calls))

    records = list(
        connector.collect({"api_token": "T", "q": "acme", "per_page": 2, "max_pages": 5})
    )

    # 4 companies across 2 pages -> 4 raw records, one per company.
    assert len(records) == 4
    assert {r.key for r in records} == {
        "gb/01234567",
        "gb/02345678",
        "gb/03456789",
        "gb/04567890",
    }
    assert all(r.content_type == "application/json" for r in records)

    # Each record's bytes are the inner company object (what map() consumes).
    by_key = {r.key: json.loads(r.data) for r in records}
    assert by_key["gb/01234567"]["name"] == "ACME LIMITED"
    assert by_key["gb/01234567"]["company_number"] == "01234567"

    # The request URL carried the secret token + the page param (page-based pagination).
    assert calls, "collect() never issued a request through the transport"
    assert all(req.url.params.get("api_token") == "T" for req in calls)
    assert {req.url.params.get("page") for req in calls} == {"1", "2"}
    # Stopped at total_pages (=2): never requested a third page.
    assert len(calls) == 2


def test_collect_is_hard_bounded_by_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload claiming 999 total_pages still stops at max_pages — the HARD bound."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        page = request.url.params.get("page", "1")
        # Every page advertises 999 total_pages and 2 companies — an attacker-sized total.
        body = {
            "results": {
                "companies": [
                    {
                        "company": {
                            "name": f"CO {page}-A",
                            "company_number": f"{page}001",
                            "jurisdiction_code": "gb",
                        }
                    },
                    {
                        "company": {
                            "name": f"CO {page}-B",
                            "company_number": f"{page}002",
                            "jurisdiction_code": "gb",
                        }
                    },
                ],
                "page": int(page),
                "per_page": 2,
                "total_count": 1998,
                "total_pages": 999,
            }
        }
        return httpx.Response(200, json=body)

    connector = OpenCorporatesConnector(transport=httpx.MockTransport(_handler))

    records = list(
        connector.collect({"api_token": "T", "q": "acme", "per_page": 2, "max_pages": 2})
    )

    # HARD bound: exactly max_pages (2) fetches and 2*2 records — NOT 999 pages.
    assert len(calls) == 2, f"max_pages cap was not enforced (saw {len(calls)} page fetches)"
    assert len(records) == 4


# --------------------------------------------------------------------------------------------------
# SECRET — the api_token (and token-bearing URL) is never logged, by ANY logger
# --------------------------------------------------------------------------------------------------


def test_collect_does_not_log_api_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The api_token (and any token-bearing request URL) is NEVER written to ANY logger.

    Regression scope-fix: the earlier version of this test captured ONLY the ``worldmonitor``
    logger tree, so it MISSED the real leak — ``guarded_stream`` drives an ``httpx.Client`` and
    ``httpx`` logs the FULL request URL (``"HTTP Request: GET <url> ..."``) at INFO, and that URL
    carries the ``api_token`` query param. ``caplog.set_level(logging.INFO)`` (note: NO ``logger=``
    argument) raises the ROOT logger + caplog handler to INFO and captures PROPAGATED records from
    EVERY logger — including ``httpx``, whose own level is NOTSET so its effective level then
    inherits INFO from root. We deliberately do NOT touch the ``httpx`` logger level: the test only
    decides what is *captured*; whether ``httpx`` actually leaks is up to its (inherited) effective
    level, and the production fix is the thing that must stop it (e.g. quiet the httpx logger /
    redact the URL). The widened capture SUBSUMES the original "connector's own logs" intent.
    """
    token = "SUPERSECRET_TOKEN_XYZ"
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = OpenCorporatesConnector(transport=_page_fixture_transport(calls))

    # Capture EVERY logger at INFO via the ROOT logger (httpx inherits this effective level). Do
    # NOT set the httpx logger's own level — let its effective level decide whether it leaks.
    caplog.set_level(logging.INFO)
    list(connector.collect({"api_token": token, "q": "acme", "per_page": 2, "max_pages": 5}))

    # Guard against a vacuous pass: collect() must have actually issued page requests, so the
    # httpx request-line INFO log path was genuinely exercised.
    assert calls, "collect() never issued a request — the no-leak assertion would be vacuous"

    # The secret must appear in NONE of the captured output, across ALL loggers — both the
    # aggregated caplog.text AND each record's formatted message AND its raw %-args (so a token
    # embedded in httpx's "HTTP Request: GET <url>" INFO line, supplied via %-args, is caught).
    assert token not in caplog.text, (
        "api_token leaked into the aggregated log text "
        "(the httpx request-URL INFO line carries the token-bearing query string)"
    )
    leaked = [rec for rec in caplog.records if token in rec.getMessage() or token in str(rec.args)]
    assert not leaked, "api_token leaked into logs via " + "; ".join(
        f"{r.name}[{r.levelname}]: {r.getMessage()}" for r in leaked
    )


# --------------------------------------------------------------------------------------------------
# SSRF — every fetch goes through guarded_stream; a private-resolving host is blocked
# --------------------------------------------------------------------------------------------------


def test_collect_uses_guarded_stream_and_blocks_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetch path goes through net.ssrf.guarded_stream: a host resolving to a private/metadata
    address is refused BEFORE any request leaves (a bare-httpx connector would NOT block)."""
    # Resolve the API host to the cloud-metadata IP -> assert_public_host must reject it.
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("169.254.169.254"))

    calls: list[httpx.Request] = []
    connector = OpenCorporatesConnector(transport=_page_fixture_transport(calls))

    with pytest.raises(BlockedAddressError):
        list(connector.collect({"api_token": "T", "q": "acme", "per_page": 2, "max_pages": 5}))

    # The guard fired BEFORE connecting — the transport never saw a request.
    assert calls == [], "collect() connected to a blocked host — SSRF guard was bypassed"
