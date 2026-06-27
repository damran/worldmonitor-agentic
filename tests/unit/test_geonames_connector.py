"""Unit tests for the GeoNames connector (on the real Vatican dump fixture)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from worldmonitor.ontology.anchors import get_anchors
from worldmonitor.plugins.base import Capability, Mode
from worldmonitor.plugins.connectors.geonames import GeoNamesConnector
from worldmonitor.provenance.model import Provenance, get_provenance
from worldmonitor.settings import get_settings

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "geonames"
_FIXTURE = str(_FIXTURES_DIR / "VA.txt")
_PROV = Provenance(
    source_id="geonames:VA",
    retrieved_at="2026-06-21T00:00:00Z",
    reliability="A",
    source_record="s3://landing/geonames/VA/3164670.json",
)


@pytest.fixture(autouse=True)
def _allow_fixtures_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Allowlist wiring ONLY (Gate H-6/H-7, ADR 0052 §9): under default-deny the local ``path``
    override is rejected unless ``geonames_allowed_path_dir`` is set, so point it at the
    fixtures dir (env + ``get_settings.cache_clear()``, the existing suite-wide pattern). No
    output assertion below is changed — they remain the FROZEN byte-identity guard.
    """
    monkeypatch.setenv("GEONAMES_ALLOWED_PATH_DIR", str(_FIXTURES_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


def test_collected_record_bytes_are_byte_identical_to_splitlines_oracle() -> None:
    """FROZEN byte-identity (ADR 0052 §9): the emitted RawRecord bytes must equal exactly what the
    legacy ``read_text().splitlines()`` read produced.

    This is the negative-space guard for the streaming rewrite: the builder's lazy line iteration
    (``rstrip("\\n")``) must reproduce ``splitlines()`` byte-for-byte. The oracle is computed here
    INDEPENDENTLY of the connector, so it pins the contract regardless of how ``collect()`` reads.
    """
    raw_text = (_FIXTURES_DIR / "VA.txt").read_text("utf-8")
    expected_payloads = [line.encode("utf-8") for line in raw_text.splitlines() if line.strip()]
    assert expected_payloads, "fixture must be non-empty"

    records = list(GeoNamesConnector().collect({"country": "VA", "path": _FIXTURE}))

    # Same count and same bytes, in the same order.
    assert [record.data for record in records] == expected_payloads
    # Pin the canonical Vatican line exactly (no trailing newline, full TSV row).
    vatican_line = next(
        line for line in raw_text.splitlines() if line.split("\t", 1)[0] == "3164670"
    )
    by_key = {record.key: record for record in records}
    assert by_key["3164670"].data == vatican_line.encode("utf-8")
    assert not by_key["3164670"].data.endswith(b"\n")
