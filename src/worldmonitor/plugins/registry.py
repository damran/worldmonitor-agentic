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

from worldmonitor.plugins.base import Connector, Manifest


class DuplicateConnectorError(ValueError):
    """Raised when two connectors claim the same ``connector_id``."""


class UnknownConnectorError(KeyError):
    """Raised when a requested ``connector_id`` is not registered."""


class Registry:
    """An in-memory catalog of connector instances keyed by ``connector_id``."""

    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

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

    def discover_module(self, module: ModuleType) -> int:
        """Register every concrete :class:`Connector` defined in ``module``.

        Returns the number newly registered. Connectors imported into the module
        from elsewhere are skipped (only those *defined* there count).
        """
        found = 0
        for _name, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, Connector)
                and obj is not Connector
                and not inspect.isabstract(obj)
                and obj.__module__ == module.__name__
            ):
                self.register(obj())
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
