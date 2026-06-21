"""Unit tests: connector-input safety hardening (WS5; ARCHITECTURE_REVIEW H5/H6).

H6 — the connector-controlled landing key must not escape its tenant prefix.
H5 — config values interpolated into outbound URLs must be pattern-constrained.
"""

from __future__ import annotations

import jsonschema
import pytest

from worldmonitor.plugins.connectors.geonames.connector import GeoNamesConnector
from worldmonitor.plugins.connectors.opensanctions.connector import OpenSanctionsConnector
from worldmonitor.runner.ingest import _safe_segment


@pytest.mark.parametrize("hostile", ["../../etc/passwd", "a/b", "..", "/abs", "x\\y", "", "."])
def test_safe_segment_neutralizes_separators_and_traversal(hostile: str) -> None:
    out = _safe_segment(hostile)
    assert "/" not in out and "\\" not in out
    assert not out.startswith("."), "no leading dot -> no '..' traversal"
    assert out, "never empty (would collapse the key path)"


def test_safe_segment_preserves_a_normal_key() -> None:
    assert _safe_segment("NK-abc_123") == "NK-abc_123"
    assert _safe_segment("ofac-12345") == "ofac-12345"


def test_opensanctions_config_rejects_unsafe_dataset() -> None:
    connector = OpenSanctionsConnector()
    connector.validate_config({"dataset": "ie_unlawful_organizations"})  # valid slug, no raise
    for bad in ["../evil", "a/b", "Up_Per", "x.y", "with space", ".."]:
        with pytest.raises(jsonschema.ValidationError):
            connector.validate_config({"dataset": bad})


def test_geonames_config_rejects_unsafe_country() -> None:
    connector = GeoNamesConnector()
    connector.validate_config({"country": "VA"})  # valid
    connector.validate_config({"country": "mc"})  # valid (connector upper-cases at use)
    for bad in ["..", "a/", "USA", "1A", "/."]:
        with pytest.raises(jsonschema.ValidationError):
            connector.validate_config({"country": bad})
