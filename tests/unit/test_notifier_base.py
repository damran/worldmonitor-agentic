"""Primary invariant tests (RED) for the Notifier plugin type — ADR 0067.

These pin the contract the builder must satisfy when establishing the **Notifier plugin type**
alongside the existing **Connector** one (Phase-2 Stage-3 slice 3):

* ``Notification`` — a frozen dataclass payload (``title`` / ``body`` required; ``severity`` default
  ``"info"``; ``context`` default an empty mapping); assignment to a field raises (frozen).
* ``Notifier(ABC)`` — mirrors ``Connector``: ``manifest`` / ``config_schema`` properties + a
  ``send`` method (a *sink* — NO ``collect`` / ``map``); concrete ``validate_config`` raises
  ``jsonschema.ValidationError`` on a bad config. An incomplete subclass cannot instantiate.
* ``Manifest.mode`` / ``Manifest.capability`` become Optional (default ``None``) so a notifier
  manifest need not supply connector-only fields — BACKWARD-COMPATIBLE: an existing connector's
  manifest STILL carries its Mode / Capability.
* ``Registry`` gains ADDITIVE notifier methods (``register_notifier`` / ``get_notifier`` /
  ``all_notifiers`` / ``notifier_manifests`` / a combined ``all_manifests``) and ``discover_module``
  ALSO registers concrete ``Notifier`` subclasses. The two namespaces DO NOT cross (a notifier id is
  not served by ``get()`` and a connector id is not served by ``get_notifier()``). The CONNECTOR
  path (``register`` / ``get`` / ``all`` / ``manifests``) behaves exactly as before.

RED today: ``worldmonitor.plugins.base`` exports neither ``Notification`` nor ``Notifier`` — the
top-level import raises ``ImportError`` and the whole module errors at collection (the correct RED).
GREEN once the builder lands the Notifier base + the additive registry surface.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import FrozenInstanceError
from types import ModuleType
from typing import Any

import jsonschema
import pytest

from worldmonitor.ontology.ftm import FtmEntity

# Top-level import of the not-yet-built Notifier base — ImportError today (the correct RED): base
# currently exports only Connector / Manifest, not Notification / Notifier.
from worldmonitor.plugins.base import (
    Capability,
    Connector,
    Kind,
    Manifest,
    Mode,
    Notification,
    Notifier,
    RawRecord,
)

# The OpenCorporates connector is imported to prove the Manifest change is backward-compatible.
from worldmonitor.plugins.connectors.opencorporates import OpenCorporatesConnector
from worldmonitor.plugins.registry import (
    DuplicateConnectorError,
    Registry,
    UnknownConnectorError,
)
from worldmonitor.provenance.model import Provenance

# --------------------------------------------------------------------------------------------------
# In-test plugin doubles — a minimal concrete Notifier + Connector exercising the registry.
# --------------------------------------------------------------------------------------------------


class _FakeNotifier(Notifier):
    """A minimal concrete notifier: manifest + config_schema + send (a sink, no collect/map)."""

    def __init__(self, notifier_id: str = "fake-notifier") -> None:
        self._id = notifier_id
        self.sent: list[Notification] = []

    @property
    def manifest(self) -> Manifest:
        # A notifier manifest: kind=NOTIFIER, no Mode / Capability (those are connector concepts).
        return Manifest(
            connector_id=self._id,
            name="Fake Notifier",
            version="0.0.1",
            kind=Kind.NOTIFIER,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"channel": {"type": "string"}},
            "required": ["channel"],
            "additionalProperties": False,
        }

    def send(self, config: Mapping[str, Any], notification: Notification) -> None:
        self.sent.append(notification)


class _FakeConnector(Connector):
    """A minimal concrete connector (mirrors the registry's existing StubConnector shape)."""

    def __init__(self, connector_id: str = "fake-connector") -> None:
        self._id = connector_id

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id=self._id,
            name="Fake Connector",
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


def _module_with_notifier(notifier_id: str) -> ModuleType:
    """A throwaway module whose ONLY class is a concrete Notifier — for discover_module()."""
    mod = ModuleType("wm_test_throwaway_notifier_mod")

    class _DiscoverableNotifier(Notifier):
        @property
        def manifest(self) -> Manifest:
            return Manifest(
                connector_id=notifier_id,
                name="Discoverable",
                version="0.0.1",
                kind=Kind.NOTIFIER,
            )

        @property
        def config_schema(self) -> dict[str, Any]:
            return {"type": "object", "properties": {}, "additionalProperties": True}

        def send(self, config: Mapping[str, Any], notification: Notification) -> None:
            return None

    # discover_module only counts classes *defined in* the module
    # (obj.__module__ == module.__name__).
    _DiscoverableNotifier.__module__ = mod.__name__
    mod._DiscoverableNotifier = _DiscoverableNotifier  # type: ignore[attr-defined]
    return mod


# --------------------------------------------------------------------------------------------------
# Notification — frozen payload with defaults
# --------------------------------------------------------------------------------------------------


def test_notification_dataclass_defaults() -> None:
    """title/body required; severity defaults "info"; context empty; the value is frozen."""
    note = Notification(title="Alert", body="rule X fired")
    assert note.title == "Alert"
    assert note.body == "rule X fired"
    assert note.severity == "info"  # default
    assert dict(note.context) == {}  # default is an empty mapping
    assert len(note.context) == 0

    # title + body are required positional/keyword fields (no defaults).
    with pytest.raises(TypeError):
        Notification(title="only title")  # type: ignore[call-arg]  # body missing

    # Non-default severity + context are carried through.
    note2 = Notification(
        title="Down", body="host unreachable", severity="critical", context={"rule": "r1"}
    )
    assert note2.severity == "critical"
    assert dict(note2.context) == {"rule": "r1"}

    # Frozen — assignment to a field raises (immutability of the payload is the contract).
    with pytest.raises(FrozenInstanceError):
        note.title = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------------------------------
# Notifier ABC — manifest + config_schema + send; validate_config
# --------------------------------------------------------------------------------------------------


def test_notifier_abc_requires_send_manifest_schema() -> None:
    """A complete concrete Notifier instantiates; an incomplete one cannot; config validates."""
    notifier = _FakeNotifier()
    assert isinstance(notifier, Notifier)
    assert notifier.manifest.kind is Kind.NOTIFIER

    # A notifier is a SINK: it must NOT carry connector source methods.
    assert not hasattr(notifier, "collect")
    assert not hasattr(notifier, "map")

    # validate_config: a good config passes, a bad one raises jsonschema.ValidationError.
    notifier.validate_config({"channel": "ops"})  # must not raise
    with pytest.raises(jsonschema.ValidationError):
        notifier.validate_config({})  # missing required "channel"

    # send() delivers the payload (the in-test double records it).
    note = Notification(title="T", body="B")
    notifier.send({"channel": "ops"}, note)
    assert notifier.sent == [note]

    # An incomplete subclass (missing the abstract send) CANNOT be instantiated.
    class _NoSend(Notifier):
        @property
        def manifest(self) -> Manifest:
            return Manifest(connector_id="x", name="x", version="0", kind=Kind.NOTIFIER)

        @property
        def config_schema(self) -> dict[str, Any]:
            return {"type": "object"}

        # send deliberately omitted — must keep the class abstract.

    with pytest.raises(TypeError):
        _NoSend()  # type: ignore[abstract]


# --------------------------------------------------------------------------------------------------
# Manifest — mode / capability optional (notifier) yet unchanged for connectors
# --------------------------------------------------------------------------------------------------


def test_manifest_mode_and_capability_optional() -> None:
    """A NOTIFIER Manifest constructs with no Mode / Capability (they default to None)."""
    implicit = Manifest(
        connector_id="telegram", name="Telegram", version="0.1.0", kind=Kind.NOTIFIER
    )
    assert implicit.mode is None
    assert implicit.capability is None
    assert implicit.kind is Kind.NOTIFIER

    explicit = Manifest(
        connector_id="telegram2",
        name="Telegram2",
        version="0.1.0",
        kind=Kind.NOTIFIER,
        mode=None,
        capability=None,
    )
    assert explicit.mode is None
    assert explicit.capability is None


def test_connector_manifest_still_requires_nothing_new() -> None:
    """Backward-compatible: an existing connector's manifest STILL carries its Mode / Capability."""
    manifest = OpenCorporatesConnector().manifest
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE


# --------------------------------------------------------------------------------------------------
# Registry — additive notifier surface; the two namespaces never cross
# --------------------------------------------------------------------------------------------------


def test_registry_discovers_and_serves_notifiers() -> None:
    """register_notifier / get_notifier / all_notifiers / notifier_manifests / all_manifests work,
    discover_module registers a Notifier subclass, and notifier ids never cross into the connector
    namespace (nor connector ids into the notifier namespace)."""
    registry = Registry()
    connector = _FakeConnector("conn-x")
    notifier = _FakeNotifier("noti-y")
    registry.register(connector)
    registry.register_notifier(notifier)

    # Notifier lookup / listing serves the notifier (and ONLY the notifier).
    assert registry.get_notifier("noti-y") is notifier
    assert registry.all_notifiers() == [notifier]
    assert [m.connector_id for m in registry.notifier_manifests()] == ["noti-y"]

    # The combined catalog includes BOTH a connector and a notifier manifest.
    all_ids = {m.connector_id for m in registry.all_manifests()}
    assert {"conn-x", "noti-y"} <= all_ids

    # The namespaces DO NOT cross: a notifier id is not served by get(); a connector id is not
    # served by get_notifier(). A missing id raises a KeyError-family error (mirroring
    # UnknownConnectorError, which subclasses KeyError).
    with pytest.raises(KeyError):
        registry.get("noti-y")
    with pytest.raises(KeyError):
        registry.get_notifier("conn-x")
    with pytest.raises(KeyError):
        registry.get_notifier("does-not-exist")

    # discover_module ALSO registers a concrete Notifier subclass defined in a module.
    module = _module_with_notifier("disc-noti")
    registry.discover_module(module)
    assert registry.get_notifier("disc-noti").manifest.connector_id == "disc-noti"


def test_connector_registry_path_unchanged() -> None:
    """The CONNECTOR path (register / get / all / manifests / duplicate / unknown) is untouched."""
    registry = Registry()
    connector = _FakeConnector("stub")
    registry.register(connector)

    assert registry.get("stub") is connector
    assert registry.all() == [connector]
    assert [m.connector_id for m in registry.manifests()] == ["stub"]
    assert registry.manifests()[0].mode is Mode.EXTERNAL_IMPORT

    with pytest.raises(DuplicateConnectorError):
        registry.register(_FakeConnector("stub"))
    with pytest.raises(UnknownConnectorError):
        registry.get("does-not-exist")

    # A registered connector is NOT exposed through the notifier listing.
    assert registry.all_notifiers() == []
