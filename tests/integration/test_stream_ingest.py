"""Primary invariant tests (RED) for the G8 cursor/resume protocol end-to-end (ADR 0070).

The driver + ingest + schema half of the STREAM path, against a testcontainer Postgres (and the
shared Neo4j only because ``IngestDriver`` requires a client — the ingest pass never touches it).
The transport is an INJECTED fake (canned cursor-tagged records) — NO live firehose.

What is pinned (and why it is RED on the current tree):

* ``run_ingest`` reports the last COMMITTED record's cursor on ``IngestStats.last_cursor`` — and a
  batch run whose records carry no cursor reports ``None`` (batch path unchanged). RED now:
  ``RawRecord`` has no ``cursor`` / ``IngestStats`` has no ``last_cursor``.
* The DRIVER cursor protocol: an instance with ``stream_cursor="C0"`` -> the connector's ``collect``
  receives ``config["_cursor"]=="C0"`` (INJECT); after a run reporting ``last_cursor="C1"`` the
  instance's ``stream_cursor`` is persisted ``"C1"`` (PERSIST); a ``Mode.STREAM`` instance gets
  ``next_run == now`` (re-run ASAP), while a BATCH (``EXTERNAL_IMPORT``) instance keeps
  ``next_run == now + cadence`` and persists NO cursor (UNCHANGED). RED now:
  ``ConnectorInstance`` has no ``stream_cursor`` column and the driver does not inject/persist.
* MIGRATION: ``migrate_to_head`` adds the nullable ``connector_instance.stream_cursor`` column and
  the alembic drift guard (model == head) still passes. RED now: the column does not exist.

The existing batch ``run_ingest`` / driver / migration tests are FROZEN — this module only ADDs
cases over a self-contained fake stream connector (``mode=Mode.STREAM``), decoupled from the
``StreamConnector`` base internals so it pins the driver/ingest CONTRACT, not an implementation.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect, make_url, select, text

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import (
    _alembic_config,
    create_all,
    make_engine,
    migrate_to_head,
    session_factory,
)
from worldmonitor.db.models import ConnectorInstance, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.plugins.base import Capability, Connector, Kind, Manifest, Mode, RawRecord
from worldmonitor.plugins.registry import Registry
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.runner.driver import IngestDriver
from worldmonitor.runner.ingest import run_ingest

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)


def _rec(key: str, cursor: str | None = None) -> RawRecord:
    """A RawRecord carrying the new ``cursor`` field (``None`` for a batch record)."""
    return RawRecord(key=key, data=b'{"n":1}', retrieved_at="2026-06-28T00:00:00Z", cursor=cursor)


class _ScriptedConnector(Connector):
    """A connector with scripted records + a declared mode; records the config its collect saw."""

    def __init__(
        self,
        connector_id: str,
        records: list[RawRecord],
        *,
        mode: Mode = Mode.STREAM,
        received: dict[str, Any] | None = None,
    ) -> None:
        self._id = connector_id
        self._records = records
        self._mode = mode
        self._received = received

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id=self._id,
            name=self._id,
            version="0",
            kind=Kind.CONNECTOR,
            mode=self._mode,
            capability=Capability.PASSIVE,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        if self._received is not None:
            # Capture the injected resume cursor for an after-the-run assertion.
            self._received["cursor"] = config.get("_cursor")
        yield from self._records

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        entity = make_entity(
            {"id": record.key, "schema": "Company", "properties": {"name": [f"Co {record.key}"]}}
        )
        return [stamp(entity, provenance)]


class _FakeLanding:
    """In-memory stand-in for LandingStore (duck-typed: ensure_bucket + put)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        self.objects[key] = data
        return f"s3://landing/{key}"


def _sessions(postgres_dsn: str):  # noqa: ANN202 - test helper
    engine = make_engine(postgres_dsn)
    create_all(engine)
    return engine, session_factory(engine)


def _harness(postgres_dsn: str, clean_graph: Neo4jClient, connector: Connector):  # noqa: ANN202
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    registry = Registry()
    registry.register(connector)
    cipher = ConfigCipher(ConfigCipher.generate_key())
    driver = IngestDriver(
        sessions=sessions,
        landing=_FakeLanding(),  # type: ignore[arg-type]
        neo4j=clean_graph,
        registry=registry,
        cipher=cipher,
    )
    return engine, sessions, driver, cipher


def _add_instance(
    sessions,  # noqa: ANN001 - sessionmaker
    *,
    connector_id: str,
    cipher: ConfigCipher,
    stream_cursor: str | None = None,
) -> str:
    instance_id = str(uuid.uuid4())
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id=instance_id,
                connector_id=connector_id,
                config_encrypted=cipher.encrypt(json.dumps({"dataset": "d"})),
                status="enabled",
                stream_cursor=stream_cursor,
            )
        )
        session.commit()
    return instance_id


def _create_fresh_database(postgres_dsn: str) -> str:
    """Create a uniquely-named empty database on the test server; return its DSN."""
    url = make_url(postgres_dsn)
    name = f"stream_cursor_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


# --------------------------------------------------------------------------------------------------
# run_ingest — reports the last COMMITTED record's cursor (None for a cursor-less batch run)
# --------------------------------------------------------------------------------------------------


def test_run_ingest_reports_last_committed_record_cursor(postgres_dsn: str) -> None:
    """``IngestStats.last_cursor`` is the cursor of the last record in the last committed window."""
    engine, sessions = _sessions(postgres_dsn)
    records = [_rec(f"r{i}", cursor=f"c{i}") for i in range(5)]
    connector = _ScriptedConnector("cur-stream", records, mode=Mode.STREAM)

    with sessions() as session:
        stats = run_ingest(
            connector,
            {"dataset": "s"},
            landing=_FakeLanding(),  # type: ignore[arg-type]
            session=session,
            commit_every=2,
        )

    assert stats.collected == 5
    assert stats.last_cursor == "c4"  # all five committed; the last record's cursor is reported
    engine.dispose()


def test_run_ingest_batch_with_no_cursors_reports_none(postgres_dsn: str) -> None:
    """A batch run whose records carry no cursor reports ``last_cursor is None`` (batch as-is)."""
    engine, sessions = _sessions(postgres_dsn)
    records = [_rec(f"b{i}") for i in range(3)]  # cursor defaults None
    connector = _ScriptedConnector("cur-batch", records, mode=Mode.EXTERNAL_IMPORT)

    with sessions() as session:
        stats = run_ingest(
            connector,
            {"dataset": "b"},
            landing=_FakeLanding(),  # type: ignore[arg-type]
            session=session,
        )

    assert stats.collected == 3
    assert stats.last_cursor is None
    engine.dispose()


# --------------------------------------------------------------------------------------------------
# Driver — inject the saved cursor, persist the new one, re-run a STREAM instance ASAP
# --------------------------------------------------------------------------------------------------


def test_driver_injects_persists_stream_cursor_and_reschedules_now(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """STREAM: inject ``stream_cursor``->``_cursor``; persist ``last_cursor``->``stream_cursor``;
    ``next_run == now`` (keep the stream warm)."""
    received: dict[str, Any] = {}
    records = [_rec("s0", cursor="C0a"), _rec("s1", cursor="C1")]
    connector = _ScriptedConnector("bsky-fake", records, mode=Mode.STREAM, received=received)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    instance_id = _add_instance(
        sessions, connector_id="bsky-fake", cipher=cipher, stream_cursor="C0"
    )

    driver._ingest_instance(instance_id, now=_NOW)

    # INJECT: the connector's collect saw the saved resume cursor.
    assert received["cursor"] == "C0"

    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None
        # PERSIST: the last committed record's cursor is written back onto the instance.
        assert instance.stream_cursor == "C1"
        # KEEP WARM: a STREAM instance re-runs immediately (not now + cadence).
        assert instance.next_run == _NOW
        assert instance.status == "enabled"

        # The cursor also flows into the run-history trail (asdict(IngestStats) -> task_run.stats).
        task = session.execute(
            select(TaskRun).where(
                TaskRun.kind == "ingest",
                TaskRun.connector_instance_id == instance_id,
            )
        ).scalar_one()
        assert task.status == "ok"
        assert task.stats is not None
        assert task.stats["last_cursor"] == "C1"
        assert task.stats["queued"] == 2
    engine.dispose()


def test_driver_batch_instance_keeps_cadence_and_persists_no_cursor(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """BATCH (EXTERNAL_IMPORT) unchanged: next_run == now + cadence and stream_cursor stays None."""
    records = [_rec("b0"), _rec("b1")]  # no cursors
    connector = _ScriptedConnector("batch-fake", records, mode=Mode.EXTERNAL_IMPORT)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    instance_id = _add_instance(
        sessions, connector_id="batch-fake", cipher=cipher, stream_cursor=None
    )

    driver._ingest_instance(instance_id, now=_NOW)

    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None
        expected = _NOW + timedelta(seconds=driver._settings.ingest_cadence_seconds)
        assert instance.next_run == expected  # batch cadence unchanged
        assert instance.stream_cursor is None  # no cursor persisted for a batch run
        assert instance.status == "enabled"
    engine.dispose()


# --------------------------------------------------------------------------------------------------
# Migration — 0007 adds the nullable stream_cursor column; the drift guard still passes
# --------------------------------------------------------------------------------------------------


def test_migration_adds_stream_cursor_column_and_no_drift(postgres_dsn: str) -> None:
    """``migrate_to_head`` adds a nullable ``connector_instance.stream_cursor`` and model==head."""
    engine = make_engine(_create_fresh_database(postgres_dsn))
    migrate_to_head(engine)

    columns = {c["name"]: c for c in inspect(engine).get_columns("connector_instance")}
    assert "stream_cursor" in columns, "migration 0007 must add connector_instance.stream_cursor"
    assert columns["stream_cursor"]["nullable"] is True, "stream_cursor must be nullable"

    # Drift guard: the ORM model and the migration head must agree (no autogenerate diff).
    command.check(_alembic_config(engine))
    engine.dispose()
