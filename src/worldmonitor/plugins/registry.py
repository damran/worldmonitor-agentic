"""Plugin registry — discovers connectors and serves their manifests.

The Integrations page and the pipeline both read the graph of available plugins
through here. Connectors can be registered explicitly (tests, programmatic use)
or discovered by scanning a package — entry-point discovery slots in later
without changing callers.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType

from worldmonitor.plugins.base import Connector, Manifest, Notifier


class DuplicateConnectorError(ValueError):
    """Raised when two connectors claim the same ``connector_id``."""


class UnknownConnectorError(KeyError):
    """Raised when a requested ``connector_id`` is not registered."""


class DuplicateNotifierError(ValueError):
    """Raised when two notifiers claim the same id."""


class UnknownNotifierError(KeyError):
    """Raised when a requested notifier id is not registered."""


class Registry:
    """An in-memory catalog of connector instances keyed by ``connector_id``."""

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}
        self._notifiers: dict[str, Notifier] = {}

    def register(self, connector: Connector) -> None:
        """Add a connector, rejecting a duplicate id."""
        connector_id = connector.manifest.connector_id
        if connector_id in self._connectors:
            raise DuplicateConnectorError(connector_id)
        self._connectors[connector_id] = connector

    def get(self, connector_id: str) -> Connector:
        """Return the connector for ``connector_id`` or raise."""
        try:
            return self._connectors[connector_id]
        except KeyError as exc:
            raise UnknownConnectorError(connector_id) from exc

    def all(self) -> list[Connector]:
        """All registered connectors, ordered by id for stable output."""
        return [self._connectors[key] for key in sorted(self._connectors)]

    def manifests(self) -> list[Manifest]:
        """All connector manifests (what the catalog/UI consumes)."""
        return [connector.manifest for connector in self.all()]

    def register_notifier(self, notifier: Notifier) -> None:
        """Add a notifier, rejecting a duplicate id (a separate namespace from connectors)."""
        notifier_id = notifier.manifest.connector_id
        if notifier_id in self._notifiers:
            raise DuplicateNotifierError(notifier_id)
        self._notifiers[notifier_id] = notifier

    def get_notifier(self, notifier_id: str) -> Notifier:
        """Return the notifier for ``notifier_id`` or raise :class:`UnknownNotifierError`."""
        try:
            return self._notifiers[notifier_id]
        except KeyError as exc:
            raise UnknownNotifierError(notifier_id) from exc

    def all_notifiers(self) -> list[Notifier]:
        """All registered notifiers, ordered by id for stable output."""
        return [self._notifiers[key] for key in sorted(self._notifiers)]

    def notifier_manifests(self) -> list[Manifest]:
        """All notifier manifests (the Integrations-UI notifier catalog)."""
        return [notifier.manifest for notifier in self.all_notifiers()]

    def all_manifests(self) -> list[Manifest]:
        """The combined catalog: connector manifests + notifier manifests."""
        return self.manifests() + self.notifier_manifests()

    def discover_module(self, module: ModuleType) -> int:
        """Register every concrete :class:`Connector` and :class:`Notifier` in ``module``.

        Returns the number newly registered (connectors + notifiers). Classes imported into the
        module from elsewhere are skipped (only those *defined* there count).
        """
        found = 0
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__ or inspect.isabstract(obj):
                continue
            if issubclass(obj, Connector) and obj is not Connector:
                self.register(obj())
                found += 1
            elif issubclass(obj, Notifier) and obj is not Notifier:
                self.register_notifier(obj())
                found += 1
        return found

    def discover_package(self, package: str | ModuleType) -> int:
        """Import every submodule of ``package`` and register the connectors found."""
        pkg = importlib.import_module(package) if isinstance(package, str) else package
        if not hasattr(pkg, "__path__"):
            return self.discover_module(pkg)
        found = 0
        for info in pkgutil.iter_modules(pkg.__path__, prefix=f"{pkg.__name__}."):
            found += self.discover_module(importlib.import_module(info.name))
        return found
