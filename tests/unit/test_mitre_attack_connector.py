"""Primary invariant tests (RED) for the `mitre_attack` connector (Gate S-3, ADR 0117).

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/mitre_attack/`` (an ``EXTERNAL_IMPORT`` / ``PASSIVE``
connector mirroring the ``opensanctions``/``feeds`` package shape):

* MANIFEST: ``connector_id="mitre_attack"``, ``kind=CONNECTOR``, ``mode=EXTERNAL_IMPORT``,
  ``capability=PASSIVE``, ``status=IMPLEMENTED``.
* CONFIG SCHEMA: an empty config validates (``url``/``limit`` both optional, the connector falls
  back to the pinned default url like ``feeds`` falls back to ``_DEFAULT_MAX_ITEMS``);
  ``additionalProperties: false`` rejects a smuggled extra key.
* ``collect()``: over an in-test STIX 2.1 bundle (2 eligible ``intrusion-set`` objects, 1
  ``revoked``, 1 ``x_mitre_deprecated``, 1 non-``intrusion-set`` object) served via
  ``httpx.MockTransport`` (NO live HTTP), yields ONE ``RawRecord`` PER ELIGIBLE intrusion-set,
  keyed by its G-id; revoked/deprecated/non-intrusion-set objects are never yielded; ``limit`` is
  honored (hard cap, bundle order).
* ``map()``: one intrusion-set record -> ONE FtM ``Organization``: deterministic
  ``id=f"mitre-{gid}"``, ``name``/``alias`` (the PRIMARY name is never duplicated into the alias
  list), ``datasets={"mitre_attack"}``, the ``mitre_gid`` anchor present in the entity context
  (the raw ``wm_anchor_mitre_gid`` key ``ontology.anchors.set_anchor`` writes), provenance that
  round-trips via ``get_provenance``, and NO ``topics`` (catalog membership is not a risk
  verdict). A record with a malformed/absent G-id maps to ``[]`` (fail-soft), never raising.

AMBIGUITY RESOLVED (test-author choice, spec is silent on the exact wire shape): ``collect()``
yields ``RawRecord.data`` as the intrusion-set's OWN STIX object JSON verbatim (the shape
``map()`` — per the spec text "the G-id is extracted from external_references" — parses
directly), mirroring how ``FeedConnector.collect()`` normalizes one JSON object per entry and
``map()`` consumes that same shape. ``collect()``/``map()`` are tested independently below so a
builder choosing a different (but still G-id-keyed, revoked/deprecated-filtering) ``collect()``
JSON envelope only needs to adjust the ``map()`` parse, not the invariants themselves.

RED today: ``worldmonitor.plugins.connectors.mitre_attack`` does not exist, so the top-level
import raises ``ModuleNotFoundError`` and the whole module errors at collection — the correct RED.
GREEN once the builder lands the connector (ADR 0117 / gate S-3 spec D2).

No live network: ``httpx.MockTransport`` is injected through the connector ``transport=`` ctor
kwarg (mirrors ``FeedConnector``/``tests/unit/test_feed_connector.py``), and
``socket.getaddrinfo`` is monkeypatched so the SSRF host check runs with no real DNS.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any

import httpx
import jsonschema
import pytest

from worldmonitor.plugins.base import Capability, Kind, Mode, RawRecord, Status

# Top-level import of the not-yet-built connector — ModuleNotFoundError today (correct RED).
from worldmonitor.plugins.connectors.mitre_attack import MitreAttackConnector
from worldmonitor.provenance.model import Provenance, get_provenance

_TEST_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/test/enterprise-attack.json"
)

_PROV = Provenance(
    source_id="mitre_attack:enterprise",
    retrieved_at="2026-07-18T00:00:00Z",
    reliability="B",
    source_record="s3://landing/mitre_attack/g0201.json",
)

# --------------------------------------------------------------------------------------------------
# A minimal, hand-built STIX 2.1 bundle: 2 eligible intrusion-sets (one with the primary NAME
# repeated inside its own `aliases` — the alias-minus-primary-name case), 1 revoked, 1
# x_mitre_deprecated, and 1 non-intrusion-set object (must never be yielded).
# --------------------------------------------------------------------------------------------------

_IS_SILENT_FALCON: dict[str, Any] = {
    "type": "intrusion-set",
    "spec_version": "2.1",
    "id": "intrusion-set--0000000000000000000000000000001",
    "name": "Silent Falcon",
    "aliases": ["Silent Falcon", "SF Group", "Falcon Team"],
    "external_references": [
        {
            "source_name": "mitre-attack",
            "external_id": "G0201",
            "url": "https://attack.mitre.org/groups/G0201",
        }
    ],
    "revoked": False,
    "x_mitre_deprecated": False,
}
_IS_NIGHTSHADE_PANDA: dict[str, Any] = {
    "type": "intrusion-set",
    "spec_version": "2.1",
    "id": "intrusion-set--0000000000000000000000000000002",
    "name": "Nightshade Panda",
    "aliases": ["Panda Ops"],
    "external_references": [{"source_name": "mitre-attack", "external_id": "G0305"}],
}
_IS_REVOKED: dict[str, Any] = {
    "type": "intrusion-set",
    "spec_version": "2.1",
    "id": "intrusion-set--0000000000000000000000000000003",
    "name": "Ghost Legion",
    "aliases": [],
    "external_references": [{"source_name": "mitre-attack", "external_id": "G0999"}],
    "revoked": True,
}
_IS_DEPRECATED: dict[str, Any] = {
    "type": "intrusion-set",
    "spec_version": "2.1",
    "id": "intrusion-set--0000000000000000000000000000004",
    "name": "Deprecated Wolf",
    "aliases": [],
    "external_references": [{"source_name": "mitre-attack", "external_id": "G0888"}],
    "x_mitre_deprecated": True,
}
_MALWARE_OBJECT: dict[str, Any] = {
    "type": "malware",
    "spec_version": "2.1",
    "id": "malware--00000000000000000000000000000005",
    "name": "SomeMalware",
}

_BUNDLE: dict[str, Any] = {
    "type": "bundle",
    "id": "bundle--test-0001",
    "objects": [
        _IS_SILENT_FALCON,
        _IS_NIGHTSHADE_PANDA,
        _IS_REVOKED,
        _IS_DEPRECATED,
        _MALWARE_OBJECT,
    ],
}


# --------------------------------------------------------------------------------------------------
# Hermetic-HTTP helpers (mirror tests/unit/test_feed_connector.py): a fake getaddrinfo so the SSRF
# guard resolves the bundle host with NO real DNS, plus a MockTransport serving the bundle bytes.
# --------------------------------------------------------------------------------------------------


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in resolving EVERY host to ``ip`` (one IPv4 5-tuple)."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def _bundle_transport(bundle: dict[str, Any], calls: list[httpx.Request]) -> httpx.MockTransport:
    """Serve ``bundle`` (JSON-encoded) verbatim for any request; record every request."""
    body = json.dumps(bundle).encode("utf-8")

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    return httpx.MockTransport(_handler)


def _record(obj: dict[str, Any], *, key: str | None = None) -> RawRecord:
    """Wrap a raw STIX object dict as the JSON ``RawRecord`` that ``map()`` consumes."""
    return RawRecord(
        key=key or str(obj.get("id", "")),
        data=json.dumps(obj).encode("utf-8"),
        retrieved_at=_PROV.retrieved_at,
        content_type="application/json",
    )


# --------------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------------


def test_manifest_is_external_import_and_passive() -> None:
    manifest = MitreAttackConnector().manifest
    assert manifest.connector_id == "mitre_attack"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — an empty config validates (built-in pinned defaults); the schema is closed.
# --------------------------------------------------------------------------------------------------


def test_config_schema_validates_default_and_rejects_additional_properties() -> None:
    connector = MitreAttackConnector()
    schema = connector.config_schema
    assert schema["additionalProperties"] is False

    connector.validate_config({})  # must not raise: url/limit both fall back to pinned defaults
    connector.validate_config({"url": _TEST_URL, "limit": 5})  # explicit override must not raise
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"url": _TEST_URL, "bogus": 1})


# --------------------------------------------------------------------------------------------------
# collect() — one RawRecord per ELIGIBLE intrusion-set, revoked/deprecated skipped, limit honored.
# --------------------------------------------------------------------------------------------------


def test_collect_yields_one_record_per_eligible_intrusion_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = MitreAttackConnector(transport=_bundle_transport(_BUNDLE, calls))

    records = list(connector.collect({"url": _TEST_URL}))

    assert calls, "collect() never issued a request through the transport"
    assert len(records) == 2, "revoked + x_mitre_deprecated + non-intrusion-set must be skipped"
    assert {r.key for r in records} == {"G0201", "G0305"}

    by_key = {r.key: json.loads(r.data) for r in records}
    assert by_key["G0201"]["name"] == "Silent Falcon"
    assert by_key["G0305"]["name"] == "Nightshade Panda"


def test_collect_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = MitreAttackConnector(transport=_bundle_transport(_BUNDLE, calls))

    records = list(connector.collect({"url": _TEST_URL, "limit": 1}))

    assert len(records) == 1
    assert records[0].key == "G0201"  # bundle order — the FIRST eligible intrusion-set


# --------------------------------------------------------------------------------------------------
# map() — FtM Organization: deterministic id, aliases minus the primary name, anchor, provenance.
# --------------------------------------------------------------------------------------------------


def test_map_intrusion_set_emits_organization_with_anchor_and_provenance() -> None:
    connector = MitreAttackConnector()
    record = _record(_IS_SILENT_FALCON, key="G0201")

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]

    assert entity.schema.name == "Organization"
    assert entity.id == "mitre-G0201"  # deterministic, gid-derived

    assert entity.get("name") == ["Silent Falcon"]
    # aliases minus the primary name — "Silent Falcon" is NOT duplicated into the alias list.
    assert set(entity.get("alias")) == {"SF Group", "Falcon Team"}
    assert "Silent Falcon" not in entity.get("alias")

    assert entity.datasets == {"mitre_attack"}

    # Catalog membership is not a risk verdict — no FtM topics are stamped.
    assert entity.get("topics", quiet=True) == []

    # The mitre_gid anchor, stored the way ontology.anchors.set_anchor writes it.
    assert entity.context.get("wm_anchor_mitre_gid") == ["G0201"]

    # Provenance round-trips intact (the non-negotiable invariant).
    prov = get_provenance(entity)
    assert prov is not None
    assert prov == _PROV
    assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])

    # Deterministic: re-mapping the same record yields the same id (idempotent re-ingest).
    again = list(connector.map(record, provenance=_PROV))
    assert again[0].id == entity.id


def test_map_intrusion_set_without_alias_repeated_name() -> None:
    """Nightshade Panda's aliases never included its own name — the alias list is untouched."""
    connector = MitreAttackConnector()
    record = _record(_IS_NIGHTSHADE_PANDA, key="G0305")

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    assert entity.id == "mitre-G0305"
    assert entity.get("name") == ["Nightshade Panda"]
    assert set(entity.get("alias")) == {"Panda Ops"}


def test_map_malformed_gid_returns_empty_without_raising() -> None:
    """external_id fails the G-id shape (`G12`, not `G####`) -> [] (fail-soft), never raises."""
    obj: dict[str, Any] = {
        "type": "intrusion-set",
        "spec_version": "2.1",
        "id": "intrusion-set--00000000000000000000000000000bad",
        "name": "Bad Gid Group",
        "aliases": [],
        "external_references": [{"source_name": "mitre-attack", "external_id": "G12"}],
    }
    connector = MitreAttackConnector()
    assert list(connector.map(_record(obj), provenance=_PROV)) == []


def test_map_missing_mitre_external_reference_returns_empty() -> None:
    """No `mitre-attack`-sourced external_reference at all -> [] (fail-soft), never raises."""
    obj: dict[str, Any] = {
        "type": "intrusion-set",
        "spec_version": "2.1",
        "id": "intrusion-set--00000000000000000000000000000ng",
        "name": "No Gid Group",
        "aliases": [],
        "external_references": [{"source_name": "some-other-source", "external_id": "X1"}],
    }
    connector = MitreAttackConnector()
    assert list(connector.map(_record(obj), provenance=_PROV)) == []
