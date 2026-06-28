"""Primary invariant tests (RED) for the G8 cursor field + the StreamConnector base (ADR 0070).

These pin the *plugin* half of the cursor/resume protocol the builder must satisfy:

* ``RawRecord`` gains ``cursor: str | None = None`` (frozen field, default ``None``) — additive, so
  every existing batch connector is unaffected (it never sets it).
* ``IngestStats`` gains ``last_cursor: str | None = None`` (default ``None``).
* ``worldmonitor.plugins.stream.StreamConnector`` is a ``Connector`` subclass whose ``collect()`` is
  SELF-BOUNDING: it reads ``config["_cursor"]`` (resume position), calls an INJECTABLE
  ``event_source(cursor, window_seconds, max_events) -> Iterator[dict]`` seam, yields ONE RawRecord
  per event (carrying that event's cursor), and RETURNS after ``max_events`` — it is NOT a
  forever-blocking iterator the driver has to interrupt. A subclass extracts ``(key, cursor, data)``
  from one event via the ``_extract_event`` hook.

RED today: ``worldmonitor.plugins.stream`` does not exist, so the top-level import raises
``ModuleNotFoundError`` and the whole module errors at collection (the correct RED:
"plugins.stream missing"). The ``RawRecord.cursor`` / ``IngestStats.last_cursor`` fields are also
absent. GREEN once the builder lands the base + the two optional fields.

Locked seams (reported to the builder — match these exactly):
* event_source signature: ``(cursor: str | None, window_seconds: int, max_events: int)`` called
  POSITIONALLY by ``collect()``; returns an ``Iterator[dict]``.
* subclass hook: ``_extract_event(self, event) -> tuple[str, str | None, bytes]`` returning
  ``(key, cursor, data)``; ``collect()`` wraps these in a ``RawRecord``.
* ``collect()`` reads ``config["_cursor"]`` / ``"window_seconds"`` / ``"max_events"`` and caps the
  records it yields at ``max_events`` (self-bounding even if the source overruns).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import (
    Capability,
    Kind,
    Manifest,
    Mode,
    RawRecord,
    Status,
)

# Top-level import of the not-yet-built base — ModuleNotFoundError today (the correct RED).
from worldmonitor.plugins.stream import StreamConnector
from worldmonitor.provenance.model import Provenance
from worldmonitor.runner.ingest import IngestStats


def _event(key: str, cursor: str) -> dict[str, Any]:
    """A minimal source event the in-test ``_extract_event`` hook understands."""
    return {"key": key, "cursor": cursor, "payload": f"payload-{key}"}


def _source_of(
    events: Iterable[dict[str, Any]],
    *,
    calls: list[dict[str, Any]] | None = None,
) -> Any:
    """An ``event_source`` seam returning ``events`` verbatim, recording each call's args.

    The source IGNORES its ``max_events`` argument (it always yields every event) so the
    self-bounding assertion below proves ``collect()`` itself caps the window — not the source.
    """

    captured = list(events)

    def _source(
        cursor: str | None, window_seconds: int, max_events: int
    ) -> Iterator[dict[str, Any]]:
        if calls is not None:
            calls.append(
                {"cursor": cursor, "window_seconds": window_seconds, "max_events": max_events}
            )
        return iter(captured)

    return _source


class _FakeStream(StreamConnector):
    """A tiny concrete StreamConnector for exercising the self-bounding base."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="fake-stream",
            name="Fake Stream",
            version="0",
            kind=Kind.CONNECTOR,
            mode=Mode.STREAM,
            capability=Capability.PASSIVE,
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def _extract_event(self, event: Mapping[str, Any]) -> tuple[str, str | None, bytes]:
        return (str(event["key"]), str(event["cursor"]), json.dumps(dict(event)).encode("utf-8"))

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        return []


# --------------------------------------------------------------------------------------------------
# The optional cursor fields — additive, default None (batch is unaffected)
# --------------------------------------------------------------------------------------------------


def test_raw_record_gains_optional_cursor_field() -> None:
    """RawRecord gains ``cursor`` (default ``None``); set it explicitly and it round-trips."""
    rec = RawRecord(key="k", data=b"{}", retrieved_at="2026-06-28T00:00:00Z", cursor="x")
    assert rec.cursor == "x"
    # A batch connector that never sets it leaves it None (backward compatible).
    assert RawRecord(key="k", data=b"{}", retrieved_at="2026-06-28T00:00:00Z").cursor is None


def test_ingest_stats_gains_optional_last_cursor_default_none() -> None:
    """IngestStats gains ``last_cursor`` defaulting to ``None`` (a cursor-less run reports None)."""
    stats = IngestStats(
        collected=0,
        landed=0,
        queued=0,
        dead_lettered=0,
        windows=0,
        stopped_reason="exhausted",
    )
    assert stats.last_cursor is None


# --------------------------------------------------------------------------------------------------
# StreamConnector.collect() — windowed, self-bounding, resuming
# --------------------------------------------------------------------------------------------------


def test_collect_yields_one_record_per_event_carrying_cursor() -> None:
    """One RawRecord per event, each carrying THAT event's cursor (key/cursor + round-trip data)."""
    events = [_event(f"k{i}", f"cur{i}") for i in range(4)]
    connector = _FakeStream(event_source=_source_of(events))

    records = list(connector.collect({"window_seconds": 20, "max_events": 100}))

    assert [r.key for r in records] == ["k0", "k1", "k2", "k3"]
    assert [r.cursor for r in records] == ["cur0", "cur1", "cur2", "cur3"]
    # The event survives into RawRecord.data so a subclass map() can re-parse it.
    assert json.loads(records[0].data)["key"] == "k0"


def test_collect_is_self_bounded_by_max_events() -> None:
    """A source of 10 events with max_events=3 yields EXACTLY 3 records (collect caps the run)."""
    events = [_event(f"k{i}", f"cur{i}") for i in range(10)]
    connector = _FakeStream(event_source=_source_of(events))

    records = list(connector.collect({"window_seconds": 20, "max_events": 3}))

    assert len(records) == 3, "collect() must self-bound at max_events even if the source overruns"
    assert [r.cursor for r in records] == ["cur0", "cur1", "cur2"]


def test_collect_resumes_from_saved_cursor() -> None:
    """collect() passes ``config['_cursor']`` to the source (resume); absent it -> None (tail)."""
    calls: list[dict[str, Any]] = []
    connector = _FakeStream(event_source=_source_of([_event("k0", "cur0")], calls=calls))

    list(connector.collect({"_cursor": "C0", "window_seconds": 20, "max_events": 100}))
    assert calls and calls[0]["cursor"] == "C0"

    calls.clear()
    list(connector.collect({"window_seconds": 20, "max_events": 100}))
    assert calls and calls[0]["cursor"] is None


def test_collect_returns_finite_iterator() -> None:
    """``list(collect(...))`` TERMINATES — collect is not a forever-blocking iterator."""
    events = (_event(f"k{i}", f"cur{i}") for i in range(5))  # a lazy generator source
    connector = _FakeStream(event_source=_source_of(events))

    records = list(connector.collect({"window_seconds": 20, "max_events": 100}))

    assert len(records) == 5
