"""Integration tests: bounded + windowed + dead-lettering run_ingest (ADR 0027, G8).

run_ingest must (a) commit progress in windows, (b) stop a collect() that never
returns via a record cap or a wall-clock deadline, and (c) dead-letter a record
that fails to land or map instead of aborting the whole run. These use in-memory
fakes for the connector and landing zone, so they need only Postgres (no MinIO /
Neo4j); marked ``integration`` because Postgres comes from testcontainers.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, IngestDeadLetter
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.plugins.base import (
    Capability,
    Connector,
    Kind,
    Manifest,
    Mode,
    RawRecord,
)
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.runner.ingest import run_ingest

pytestmark = pytest.mark.integration

_MANIFEST = Manifest(
    connector_id="fake",
    name="Fake",
    version="0",
    kind=Kind.CONNECTOR,
    mode=Mode.EXTERNAL_IMPORT,
    capability=Capability.PASSIVE,
)


def _record(key: str) -> RawRecord:
    return RawRecord(key=key, data=b'{"n":1}', retrieved_at="2026-06-21T00:00:00Z")


class _FakeConnector(Connector):
    """A connector whose collection is fully scripted for the test."""

    def __init__(
        self,
        records: list[RawRecord] | None = None,
        *,
        raise_at_end: bool = False,
        infinite: bool = False,
        per_record_sleep: float = 0.0,
        fail_map_on: frozenset[str] = frozenset(),
    ) -> None:
        self._records = records or []
        self._raise_at_end = raise_at_end
        self._infinite = infinite
        self._sleep = per_record_sleep
        self._fail_map_on = fail_map_on

    @property
    def manifest(self) -> Manifest:
        return _MANIFEST

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        if self._infinite:
            i = 0
            while True:
                if self._sleep:
                    time.sleep(self._sleep)
                yield _record(f"r{i}")
                i += 1
        else:
            yield from self._records
            if self._raise_at_end:
                raise RuntimeError("source dropped mid-stream")

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        if record.key in self._fail_map_on:
            raise ValueError(f"unmappable record {record.key}")
        entity = make_entity(
            {"id": record.key, "schema": "Company", "properties": {"name": [f"Co {record.key}"]}}
        )
        return [stamp(entity, provenance)]


class _FakeLanding:
    """In-memory stand-in for LandingStore (duck-typed: ensure_bucket + put)."""

    def __init__(self, *, fail_on: frozenset[str] = frozenset()) -> None:
        self._fail_on = fail_on
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:  # noqa: D401 - trivial fake
        pass

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        if any(token in key for token in self._fail_on):
            raise RuntimeError(f"landing zone unavailable for {key}")
        self.objects[key] = data
        return f"s3://landing/{key}"


def _sessions(postgres_dsn: str):
    engine = make_engine(postgres_dsn)
    create_all(engine)
    return engine, session_factory(engine)


def _count(sessions, model, **filters: object) -> int:
    with sessions() as session:
        stmt = select(func.count()).select_from(model)
        for attr, value in filters.items():
            stmt = stmt.where(getattr(model, attr) == value)
        return session.execute(stmt).scalar_one()


def test_windowed_commits_persist_progress_before_a_source_failure(postgres_dsn: str) -> None:
    """Records committed in earlier windows survive a later collect() failure."""
    engine, sessions = _sessions(postgres_dsn)
    connector = _FakeConnector([_record("r0"), _record("r1"), _record("r2")], raise_at_end=True)
    landing = _FakeLanding()

    # collect() raises after 3 records; with commit_every=1 those 3 are already committed.
    with pytest.raises(RuntimeError), sessions() as session:
        run_ingest(
            connector,
            {"dataset": "d"},
            landing=landing,  # type: ignore[arg-type]
            session=session,
            commit_every=1,
        )

    assert _count(sessions, ErQueueItem) == 3, "committed windows must persist"
    engine.dispose()


def test_full_run_commits_in_windows(postgres_dsn: str) -> None:
    engine, sessions = _sessions(postgres_dsn)
    connector = _FakeConnector([_record(f"r{i}") for i in range(5)])
    landing = _FakeLanding()

    with sessions() as session:
        stats = run_ingest(
            connector,
            {"dataset": "d"},
            landing=landing,  # type: ignore[arg-type]
            session=session,
            commit_every=2,
        )

    assert stats.collected == 5
    assert stats.landed == 5
    assert stats.queued == 5
    assert stats.dead_lettered == 0
    assert stats.windows == 3  # [2, 2, 1]
    assert stats.stopped_reason == "exhausted"
    assert _count(sessions, ErQueueItem) == 5
    engine.dispose()


def test_max_records_caps_an_unbounded_collect(postgres_dsn: str) -> None:
    """An infinite collect() is stopped by the record cap."""
    engine, sessions = _sessions(postgres_dsn)
    connector = _FakeConnector(infinite=True)
    landing = _FakeLanding()

    with sessions() as session:
        stats = run_ingest(
            connector,
            {"dataset": "d"},
            landing=landing,  # type: ignore[arg-type]
            session=session,
            max_records=3,
        )

    assert stats.collected == 3
    assert stats.queued == 3
    assert stats.stopped_reason == "max_records"
    assert _count(sessions, ErQueueItem) == 3
    engine.dispose()


def test_wall_clock_timeout_terminates_an_unbounded_collect(postgres_dsn: str) -> None:
    """An infinite collect() that keeps yielding is stopped by the deadline (no hang)."""
    engine, sessions = _sessions(postgres_dsn)
    connector = _FakeConnector(infinite=True, per_record_sleep=0.05)
    landing = _FakeLanding()

    with sessions() as session:
        stats = run_ingest(
            connector,
            {"dataset": "d"},
            landing=landing,  # type: ignore[arg-type]
            session=session,
            timeout=0.2,
        )

    # The run returned (did not hang) because the wall-clock deadline fired.
    assert stats.stopped_reason == "timeout"
    assert stats.collected >= 1
    assert stats.collected < 100  # sanity: it stopped quickly, not after thousands
    engine.dispose()


def test_map_failure_is_dead_lettered_and_run_continues(postgres_dsn: str) -> None:
    """A record that fails to map is recorded in ingest_dead_letter; others still enqueue."""
    engine, sessions = _sessions(postgres_dsn)
    connector = _FakeConnector(
        [_record("r0"), _record("r1"), _record("r2")], fail_map_on=frozenset({"r1"})
    )
    landing = _FakeLanding()

    with sessions() as session:
        stats = run_ingest(
            connector,
            {"dataset": "d"},
            landing=landing,  # type: ignore[arg-type]
            session=session,
        )

    assert stats.collected == 3
    assert stats.landed == 3  # all landed (map is what failed)
    assert stats.queued == 2  # r0, r2
    assert stats.dead_lettered == 1
    assert stats.stopped_reason == "exhausted"
    assert _count(sessions, ErQueueItem) == 2

    with sessions() as session:
        dl = session.execute(select(IngestDeadLetter)).scalar_one()
        assert dl.stage == "map"
        assert dl.source_key == "r1"
        assert dl.source_record is not None
        assert dl.source_record.startswith("s3://landing/")  # map-stage failure: raw landed
        assert "unmappable" in dl.error
    engine.dispose()


def test_land_failure_is_dead_lettered_with_no_landing_pointer(postgres_dsn: str) -> None:
    """A record that fails to land is dead-lettered with a null source_record."""
    engine, sessions = _sessions(postgres_dsn)
    connector = _FakeConnector([_record("r0"), _record("r1"), _record("r2")])
    landing = _FakeLanding(fail_on=frozenset({"r1.json"}))

    with sessions() as session:
        stats = run_ingest(
            connector,
            {"dataset": "d"},
            landing=landing,  # type: ignore[arg-type]
            session=session,
        )

    assert stats.collected == 3
    assert stats.landed == 2  # r1 failed to land
    assert stats.queued == 2
    assert stats.dead_lettered == 1

    with sessions() as session:
        dl = session.execute(select(IngestDeadLetter)).scalar_one()
        assert dl.stage == "land"
        assert dl.source_key == "r1"
        assert dl.source_record is None  # nothing landed
    engine.dispose()
