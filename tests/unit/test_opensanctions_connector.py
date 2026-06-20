"""Unit tests for the OpenSanctions connector (on a captured fixture line)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from worldmonitor.plugins.base import Capability, Mode, RawRecord
from worldmonitor.plugins.connectors.opensanctions import OpenSanctionsConnector
from worldmonitor.provenance.model import Provenance, get_provenance

_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "opensanctions_entity.json").read_text("utf-8")
)
_PROV = Provenance(
    source_id="opensanctions:ie_unlawful_organizations",
    retrieved_at="2026-06-20T00:00:00Z",
    reliability="B",
    source_record="s3://landing/test-tenant/opensanctions/x.json",
)


def test_manifest() -> None:
    manifest = OpenSanctionsConnector().manifest
    assert manifest.connector_id == "opensanctions"
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE


def test_config_schema_and_validation() -> None:
    connector = OpenSanctionsConnector()
    assert connector.config_schema["required"] == ["dataset"]
    connector.validate_config({"dataset": "us_ofac_sdn"})
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({})  # dataset is required


def test_map_validates_against_ftm_and_stamps_provenance() -> None:
    connector = OpenSanctionsConnector()
    record = RawRecord(
        key=_FIXTURE["id"],
        data=json.dumps(_FIXTURE).encode("utf-8"),
        retrieved_at=_PROV.retrieved_at,
    )
    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    # Validated against the FtM schema (id + resolvable schema).
    assert entity.id == _FIXTURE["id"]
    assert entity.schema.name == _FIXTURE["schema"]
    # All provenance fields present on the output.
    prov = get_provenance(entity)
    assert prov is not None
    assert prov == _PROV
    assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])
