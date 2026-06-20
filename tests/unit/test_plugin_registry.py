"""Unit tests for the plugin framework: registry + config validation."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import jsonschema
import pytest

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import (
    Capability,
    Connector,
    Kind,
    Manifest,
    Mode,
    RawRecord,
)
from worldmonitor.plugins.registry import (
    DuplicateConnectorError,
    Registry,
    UnknownConnectorError,
)
from worldmonitor.provenance.model import Provenance


class StubConnector(Connector):
    """A minimal connector used to exercise the registry."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="stub",
            name="Stub Connector",
            version="0.0.1",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"dataset": {"type": "string"}},
            "required": ["dataset"],
        }

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        yield RawRecord(key="1", data=b"{}", retrieved_at="2026-01-01T00:00:00Z")

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        return []


def test_register_get_and_manifests() -> None:
    registry = Registry()
    registry.register(StubConnector())
    assert registry.get("stub").manifest.name == "Stub Connector"
    manifests = registry.manifests()
    assert [m.connector_id for m in manifests] == ["stub"]
    assert manifests[0].mode is Mode.EXTERNAL_IMPORT


def test_duplicate_registration_raises() -> None:
    registry = Registry()
    registry.register(StubConnector())
    with pytest.raises(DuplicateConnectorError):
        registry.register(StubConnector())


def test_unknown_connector_raises() -> None:
    with pytest.raises(UnknownConnectorError):
        Registry().get("does-not-exist")


def test_discover_module_finds_connector() -> None:
    registry = Registry()
    found = registry.discover_module(sys.modules[__name__])
    assert found == 1
    assert registry.get("stub").manifest.connector_id == "stub"


def test_validate_config_accepts_and_rejects() -> None:
    connector = StubConnector()
    connector.validate_config({"dataset": "us_ofac_sdn"})
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"wrong": "no dataset"})
