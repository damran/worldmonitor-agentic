"""Unit tests for provenance: stamping round-trips and every map() output carries it."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

from worldmonitor.ontology.ftm import make_entity
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord
from worldmonitor.plugins.ftm_bulk import FtmBulkConnector
from worldmonitor.provenance.model import Provenance, get_provenance, stamp

PROV = Provenance(
    source_id="opensanctions:us_ofac_sdn",
    retrieved_at="2026-06-20T00:00:00Z",
    reliability="A",
    source_record="s3://landing/us_ofac_sdn/ofac-1.json",
)


def test_stamp_roundtrip() -> None:
    entity = make_entity(
        {"id": "ofac-1", "schema": "Person", "properties": {"name": ["Jane"]}, "datasets": ["t"]}
    )
    stamp(entity, PROV)
    assert get_provenance(entity) == PROV
    # Provenance survives the serialization round-trip (raw landing / ER queue).
    assert get_provenance(make_entity(entity.to_dict())) == PROV


def test_unstamped_entity_has_no_provenance() -> None:
    entity = make_entity({"id": "x", "schema": "Person", "properties": {}, "datasets": ["t"]})
    assert get_provenance(entity) is None


class _StubBulkConnector(FtmBulkConnector):
    """FtM-native stub yielding one canned entity record."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="stub-bulk",
            name="Stub Bulk",
            version="0.0.1",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        payload = {
            "id": "ofac-1",
            "schema": "Person",
            "properties": {"name": ["Jane Doe"]},
            "datasets": ["us_ofac_sdn"],
        }
        yield RawRecord(
            key="ofac-1", data=json.dumps(payload).encode(), retrieved_at=PROV.retrieved_at
        )


def test_bulk_map_stamps_every_output() -> None:
    connector = _StubBulkConnector()
    outputs = [
        entity
        for record in connector.collect({})
        for entity in connector.map(record, provenance=PROV)
    ]
    assert outputs, "expected at least one mapped entity"
    assert all(get_provenance(entity) == PROV for entity in outputs)
    assert outputs[0].id == "ofac-1"
