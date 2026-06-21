"""Unit tests for the GeoNames connector (on the real Vatican dump fixture)."""

from __future__ import annotations

from pathlib import Path

from worldmonitor.ontology.anchors import get_anchors
from worldmonitor.plugins.base import Capability, Mode
from worldmonitor.plugins.connectors.geonames import GeoNamesConnector
from worldmonitor.provenance.model import Provenance, get_provenance

_FIXTURE = str(Path(__file__).parent.parent / "fixtures" / "geonames" / "VA.txt")
_PROV = Provenance(
    source_id="geonames:VA",
    retrieved_at="2026-06-21T00:00:00Z",
    reliability="A",
    source_record="s3://landing/geonames/VA/3164670.json",
)


def test_manifest() -> None:
    manifest = GeoNamesConnector().manifest
    assert manifest.connector_id == "geonames"
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE


def test_maps_known_place_to_geonames_id() -> None:
    connector = GeoNamesConnector()
    records = list(connector.collect({"country": "VA", "path": _FIXTURE}))
    assert len(records) >= 100  # Vatican dump is ~130 rows

    entities = [entity for record in records for entity in connector.map(record, provenance=_PROV)]
    by_geonames_id = {get_anchors(entity)["geonames_id"]: entity for entity in entities}

    # 3164670 = "State of the Vatican City"
    assert "3164670" in by_geonames_id
    vatican = by_geonames_id["3164670"]
    assert vatican.schema.name == "Address"
    assert "State of the Vatican City" in vatican.get("name")
    assert vatican.get("country") == ["va"]
    assert get_provenance(vatican) == _PROV
