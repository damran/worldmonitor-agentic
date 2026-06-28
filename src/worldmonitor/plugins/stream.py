"""StreamConnector — a self-bounding windowed connector base (ADR 0070, the G8 STREAM path).

A :class:`~worldmonitor.plugins.base.Connector` whose ``collect()`` consumes a real-time source for
one BOUNDED window (at most ``window_seconds`` or ``max_events``) and then RETURNS — it is NOT a
forever-blocking iterator the driver has to interrupt. Each window is a bounded, checkpointed
``run_ingest`` pass; the driver re-runs a STREAM instance immediately (``next_run=now``) to keep the
stream warm, and persists the last committed cursor so the next window resumes where this one left
off (the G8 resume protocol).

The transport sits behind an INJECTABLE seam — ``event_source(cursor, window_seconds, max_events)``
— so tests inject canned events (no live network, no deep WebSocket mocking) while production wires
the real consumer. A subclass implements:

* :meth:`_extract_event` — ``(key, cursor, data)`` from one source event;
* :meth:`_default_event_source` — the real transport (the base raises until a subclass supplies it);
* ``manifest`` / ``config_schema`` / ``map`` — the usual :class:`Connector` surface.

Safety: ``collect()`` is bounded by the window (``run_ingest``'s ``timeout`` / ``max_records``
remain a backstop); each event's bytes are treated as hostile and only validated in ``map()``.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

from worldmonitor.plugins.base import Connector, RawRecord

# The injectable transport seam: resume ``cursor`` (``None`` -> tail), the window length, and the
# event cap, returning the window's events. Called POSITIONALLY by :meth:`StreamConnector.collect`.
EventSource = Callable[[str | None, int, int], Iterator[dict[str, Any]]]

# Defensive defaults when a config omits the bounds (the connector's JSON-Schema default is the real
# source of truth; jsonschema does not inject defaults, so ``collect`` falls back here).
_DEFAULT_WINDOW_SECONDS = 20
_DEFAULT_MAX_EVENTS = 500

# Runtime-injected resume key (the driver sets ``config["_cursor"]``); not a user-facing config
# field, so it is stripped before validating against the (possibly closed) schema.
_CURSOR_KEY = "_cursor"


class StreamConnector(Connector):
    """A windowed, self-bounding :class:`Connector` base for real-time STREAM sources."""

    def __init__(self, *, event_source: EventSource | None = None) -> None:
        """Wire the transport seam — an injected ``event_source`` (tests) or the real one."""
        self._event_source: EventSource = event_source or self._default_event_source

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Consume one bounded window from the source, one RawRecord per event, then RETURN.

        Validates the user-facing config (the injected ``_cursor`` is stripped first — it is a
        runtime resume key, not a schema field), resumes from ``config["_cursor"]`` (``None`` ->
        live tail), and yields a :class:`RawRecord` per event carrying that event's cursor. It
        SELF-BOUNDS at ``max_events`` even if the source over-produces, so ``list(collect(...))``
        always terminates.
        """
        self.validate_config({k: v for k, v in config.items() if k != _CURSOR_KEY})
        cursor = config.get(_CURSOR_KEY)
        window_seconds = int(config.get("window_seconds", _DEFAULT_WINDOW_SECONDS))
        max_events = int(config.get("max_events", _DEFAULT_MAX_EVENTS))
        retrieved_at = datetime.now(UTC).isoformat()

        for emitted, event in enumerate(self._event_source(cursor, window_seconds, max_events)):
            if emitted >= max_events:
                break  # self-bound: cap the window even if the source overruns
            key, event_cursor, data = self._extract_event(event)
            yield RawRecord(
                key=key,
                data=data,
                retrieved_at=retrieved_at,
                content_type="application/json",
                cursor=event_cursor,
            )

    @abstractmethod
    def _extract_event(self, event: Mapping[str, Any]) -> tuple[str, str | None, bytes]:
        """Extract ``(key, cursor, data)`` from one source event (subclass-specific shape)."""

    def _default_event_source(
        self, cursor: str | None, window_seconds: int, max_events: int
    ) -> Iterator[dict[str, Any]]:
        """The real transport — provided by a concrete subclass (the base has none).

        Not abstract (so a test subclass that always injects ``event_source`` need not define it),
        but raises if ever reached without an injected seam.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no real event_source; inject one or override "
            "_default_event_source"
        )
