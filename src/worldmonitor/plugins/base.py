"""Plugin framework v0 — base interfaces every connector implements.

A plugin is a *manifest* + a JSON-Schema *config* + an *impl* + *tests*
(CLAUDE.md, ``docs/30_PLUGIN_FRAMEWORK.md``). Connectors declare a **mode**
(EXTERNAL_IMPORT / INTERNAL_ENRICHMENT / STREAM) and a **capability**
(passive / active). They ``collect()`` raw records (honoring passive/active +
rate limits) and ``map()`` them to FtM/STIX entities **with provenance** — they
never write to the graph or resolve; raw goes to the landing zone, candidates to
the ER queue.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import jsonschema

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.provenance.model import Provenance


class Kind(StrEnum):
    """The plugin family (a method is added as a plugin of one of these kinds)."""

    CONNECTOR = "connector"
    MAPPER = "mapper"
    RESOLVER = "resolver"
    ENRICHER = "enricher"
    RULE = "rule"
    SCORER = "scorer"
    NOTIFIER = "notifier"
    TOOL = "tool"


class Mode(StrEnum):
    """How a connector relates to the graph."""

    EXTERNAL_IMPORT = "EXTERNAL_IMPORT"
    INTERNAL_ENRICHMENT = "INTERNAL_ENRICHMENT"
    STREAM = "STREAM"


class Capability(StrEnum):
    """Whether a connector touches targets actively (gated) or only passively."""

    PASSIVE = "passive"
    ACTIVE = "active"


class Status(StrEnum):
    """Lifecycle tag carried by every component (CLAUDE.md scope discipline)."""

    RESEARCHED = "researched"
    SCAFFOLDED = "scaffolded"
    IMPLEMENTED = "implemented"
    TESTED = "tested"
    OPERATIONAL = "operational"


@dataclass(frozen=True, slots=True)
class Manifest:
    """Static description of a plugin (drives the catalog + Integrations UI).

    ``mode`` and ``capability`` are **connector-only** concepts (how a source relates to the graph
    + whether it touches targets actively). A notifier — a sink, not a source — leaves them
    ``None``. They keep their field positions so every existing connector still constructs (each
    passes both by keyword); the ``None`` defaults are backward-compatible.
    """

    connector_id: str
    name: str
    version: str
    kind: Kind
    mode: Mode | None = None
    capability: Capability | None = None
    description: str = ""
    status: Status = Status.SCAFFOLDED


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One unit of collected data, destined for the landing zone verbatim."""

    key: str
    """Stable identifier for the record within its source (used as a landing key)."""
    data: bytes
    """The raw bytes exactly as collected — treated as hostile until validated."""
    retrieved_at: str
    """ISO-8601 timestamp of collection."""
    content_type: str = "application/json"


class Connector(ABC):
    """Base class for all connectors: manifest + config schema + collect + map."""

    @property
    @abstractmethod
    def manifest(self) -> Manifest:
        """Static description of this connector."""

    @property
    @abstractmethod
    def config_schema(self) -> dict[str, Any]:
        """JSON Schema for this connector's instance config (drives the UI form)."""

    @abstractmethod
    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Yield raw records from the source, honoring passive/active + rate limits."""

    @abstractmethod
    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Transform a raw record into FtM entities, each stamped with provenance."""

    def validate_config(self, config: Mapping[str, Any]) -> None:
        """Validate an instance config against :attr:`config_schema`.

        Raises :class:`jsonschema.ValidationError` on a bad config.
        """
        jsonschema.validate(dict(config), self.config_schema)


@dataclass(frozen=True, slots=True)
class Notification:
    """A channel-agnostic alert payload a :class:`Notifier` renders + delivers.

    The deterministic "rule fired / run complete" message a future trigger hands to ``send()``.
    Immutable (frozen) — a payload is fixed once built. ``severity`` is one of info/warning/critical
    (free-form string, the channel decides how to render it); ``context`` carries optional string
    key/values (e.g. the firing rule id) and defaults to an empty mapping.
    """

    title: str
    body: str
    severity: str = "info"
    context: Mapping[str, str] = field(default_factory=dict[str, str])


class Notifier(ABC):
    """Base class for all notifiers: manifest + config schema + ``send`` (a sink — no collect/map).

    A notifier is the mirror of :class:`Connector` on the *output* side: it delivers a
    :class:`Notification` to an external channel (Telegram, Slack, email, …). It never collects,
    maps, resolves, or writes the graph. Egress (when a notifier reaches the network) goes through
    the SSRF guard, exactly like a connector's fetch.
    """

    @property
    @abstractmethod
    def manifest(self) -> Manifest:
        """Static description of this notifier (``kind=NOTIFIER``; no Mode / Capability)."""

    @property
    @abstractmethod
    def config_schema(self) -> dict[str, Any]:
        """JSON Schema for this notifier's instance config (drives the UI form)."""

    @abstractmethod
    def send(self, config: Mapping[str, Any], notification: Notification) -> None:
        """Deliver ``notification`` over the configured channel; raise on delivery failure."""

    def validate_config(self, config: Mapping[str, Any]) -> None:
        """Validate an instance config against :attr:`config_schema`.

        Raises :class:`jsonschema.ValidationError` on a bad config.
        """
        jsonschema.validate(dict(config), self.config_schema)
