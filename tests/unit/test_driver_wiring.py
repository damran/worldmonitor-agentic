"""Unit test: the smoke-run harness discovers the real connectors (WS2).

``build_driver`` (the smoke-run entry point) wires an ``IngestDriver`` around a registry
auto-discovered from ``worldmonitor.plugins.connectors``. This pins that the discovery
actually finds the connectors the runbook seeds, without needing the live stack.
"""

from __future__ import annotations

from worldmonitor.runner.driver import discover_connectors


def test_discover_connectors_finds_opensanctions_and_geonames() -> None:
    """The connectors live two levels deep, so discovery must walk the package
    recursively — a one-level scan finds nothing."""
    connector_ids = {connector.manifest.connector_id for connector in discover_connectors().all()}
    assert {"opensanctions", "geonames"} <= connector_ids
