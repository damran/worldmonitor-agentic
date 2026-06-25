"""Integration tests: the long-running ingest driver (ADR 0029, Gate A).

Beyond count-based checks (the user's verification bar): cadence + task_run trail,
idempotent re-ingest (no double-enqueue on a restart/re-run), failure handling (a
task records error and the instance is not left stuck running), a VISIBLE refusal of
ACTIVE-capability connectors, stale-running recovery on startup, and resolution
no-overlap. Postgres (+ Neo4j for the resolution pass) come from testcontainers; the
connector + landing zone are in-memory fakes.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ConnectorInstance, ErQueueItem, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.plugins.base import Capability, Connector, Kind, Manifest, Mode, RawRecord
from worldmonitor.plugins.registry import Registry
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.runner.driver import IngestDriver

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


class _FakeConnector(Connector):
    def __init__(
        self,
        connector_id: str,
        *,
        records: int = 2,
        capability: Capability = Capability.PASSIVE,
        raise_on_collect: bool = False,
    ) -> None:
        self._id = connector_id
        self._records = records
        self._capability = capability
        self._raise = raise_on_collect

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id=self._id,
            name=self._id,
            version="0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=self._capability,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        if self._raise:
            raise RuntimeError("source unreachable")
        for i in range(self._records):
            yield RawRecord(key=f"r{i}", data=b'{"n":1}', retrieved_at="2026-06-21T00:00:00Z")

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        entity = make_entity(
            {"id": record.key, "schema": "Company", "properties": {"name": [f"Co {record.key}"]}}
        )
        return [stamp(entity, provenance)]


class _FakeLanding:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        self.objects[key] = data
        return f"s3://landing/{key}"


def _harness(postgres_dsn: str, clean_graph: Neo4jClient, connector: Connector):
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


def _add_instance(sessions, *, connector_id: str, cipher: ConfigCipher) -> str:
    instance_id = str(uuid.uuid4())
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id=instance_id,
                connector_id=connector_id,
                config_encrypted=cipher.encrypt(json.dumps({"dataset": "d"})),
                status="enabled",
            )
        )
        session.commit()
    return instance_id


def test_driver_runs_due_instance_and_records_task(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, _FakeConnector("fake-a"))
    instance_id = _add_instance(sessions, connector_id="fake-a", cipher=cipher)

    # The driver runs ALL due instances (global, by design); the shared session-scoped
    # Postgres holds other tests' instances too, so assert membership, not equality.
    ran = driver.run_due_ingests(now=_NOW)
    assert instance_id in ran

    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None
        assert instance.status == "enabled"  # back to enabled after a clean run
        assert instance.last_run == _NOW
        assert instance.next_run is not None and instance.next_run > _NOW  # cadence advanced

        task = session.execute(select(TaskRun).where(TaskRun.kind == "ingest")).scalar_one()
        assert task.status == "ok"
        assert task.stats is not None and task.stats["queued"] == 2

        queued = session.execute(select(func.count()).select_from(ErQueueItem)).scalar_one()
        assert queued == 2
    engine.dispose()


def test_driver_reingest_is_idempotent_no_double_enqueue(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A6: re-running the same instance does not double-enqueue (restart-safe)."""
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, _FakeConnector("fake-b"))
    _add_instance(sessions, connector_id="fake-b", cipher=cipher)

    driver.run_due_ingests(now=_NOW)
    later = _NOW + timedelta(seconds=10_000)  # past next_run, so the instance is due again
    driver.run_due_ingests(now=later)

    with sessions() as session:
        queued = session.execute(select(func.count()).select_from(ErQueueItem)).scalar_one()
        assert queued == 2, "re-ingest must not double-enqueue the same records"

        # The second ingest enqueued nothing new (all conflicts).
        second = session.execute(
            select(TaskRun)
            .where(TaskRun.kind == "ingest")
            .order_by(TaskRun.started_at.desc())
            .limit(1)
        ).scalar_one()
        assert second.stats is not None and second.stats["queued"] == 0
    engine.dispose()


def test_driver_records_error_and_does_not_leave_instance_running(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    connector = _FakeConnector("fake-c", raise_on_collect=True)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    instance_id = _add_instance(sessions, connector_id="fake-c", cipher=cipher)

    driver.run_due_ingests(now=_NOW)

    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None
        assert instance.status == "error"  # not stuck in "running"
        assert (
            instance.next_run is not None and instance.next_run > _NOW
        )  # still retries next cycle

        task = session.execute(select(TaskRun)).scalar_one()
        assert task.status == "error"
        assert "source unreachable" in task.error
    engine.dispose()


def test_driver_refuses_active_connector_visibly(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """An ACTIVE-capability connector is refused with a recorded error, never run silently."""
    connector = _FakeConnector("fake-active", capability=Capability.ACTIVE)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    _add_instance(sessions, connector_id="fake-active", cipher=cipher)

    driver.run_due_ingests(now=_NOW)

    with sessions() as session:
        task = session.execute(select(TaskRun)).scalar_one()
        assert task.status == "error"
        assert "ACTIVE" in task.error  # the refusal reason is recorded, not silent

        # Nothing was ingested.
        queued = session.execute(select(func.count()).select_from(ErQueueItem)).scalar_one()
        assert queued == 0
    engine.dispose()


def test_driver_recovers_stale_running_on_startup(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, _FakeConnector("fake-d"))
    instance_id = _add_instance(sessions, connector_id="fake-d", cipher=cipher)
    # Simulate a crash: instance + task left "running".
    with sessions() as session:
        session.get(ConnectorInstance, instance_id).status = "running"  # type: ignore[union-attr]
        session.add(TaskRun(id=str(uuid.uuid4()), kind="ingest", status="running"))
        session.commit()

    reset = driver.recover_stale()
    assert reset == 1

    with sessions() as session:
        assert session.get(ConnectorInstance, instance_id).status == "enabled"  # type: ignore[union-attr]
        task = session.execute(select(TaskRun)).scalar_one()
        assert task.status == "error"
    engine.dispose()


def test_driver_resolution_pass_resolves_and_does_not_overlap(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine, sessions, driver, _cipher = _harness(
        postgres_dsn, clean_graph, _FakeConnector("fake-e")
    )

    def _candidate(entity_id: str, name: str, jurisdiction: str) -> ErQueueItem:
        # Distinct names + jurisdictions so the two do NOT merge (we expect 2 nodes).
        entity = stamp(
            make_entity(
                {
                    "id": entity_id,
                    "schema": "Company",
                    "properties": {"name": [name], "jurisdiction": [jurisdiction]},
                }
            ),
            Provenance("src", "2026-06-21T00:00:00Z", "A", f"s3://landing/{entity_id}.json"),
        )
        return ErQueueItem(
            id=str(uuid.uuid4()),
            connector_id="fake-e",
            entity_id=entity_id,
            raw_entity=entity.to_dict(),
            source_record=f"s3://landing/{entity_id}.json",
            status="pending",
        )

    with sessions() as session:
        session.add_all(
            [_candidate("e1", "Alpha Industries", "us"), _candidate("e2", "Beta Holdings", "gb")]
        )
        session.commit()

    # No-overlap: while a resolution "holds" the lock, a second pass is skipped.
    assert driver._resolve_lock.acquire(blocking=False)
    try:
        assert driver.run_resolution(now=_NOW) == []
    finally:
        driver._resolve_lock.release()

    # Now it runs: both candidates resolve and land in the graph.
    resolved = driver.run_resolution(now=_NOW)
    assert driver._RESOLVED_MARKER in resolved

    with sessions() as session:
        pending = session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.status == "pending")
        ).scalar_one()
        assert pending == 0
        task = session.execute(select(TaskRun).where(TaskRun.kind == "resolve")).scalar_one()
        assert task.status == "ok"

    nodes = clean_graph.execute_read("MATCH (n:Company) RETURN count(n) AS n")[0]["n"]
    assert nodes == 2  # two distinct companies, both written
    engine.dispose()


def test_prune_task_runs_removes_old_finished_only(
    postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """Pruning deletes finished task_run rows past the retention window (default 30d);
    recent rows and any ``running`` row are kept (WS5 hardening)."""
    engine, sessions, driver, _ = _harness(postgres_dsn, clean_graph, _FakeConnector("stub"))
    old = _NOW - timedelta(days=60)
    with sessions() as session:
        session.add(TaskRun(id="prune-old-ok", kind="ingest", status="ok", finished_at=old))
        session.add(TaskRun(id="prune-old-running", kind="ingest", status="running"))
        session.add(
            TaskRun(
                id="prune-recent-ok",
                kind="resolve",
                status="ok",
                finished_at=_NOW,
            )
        )
        session.commit()

    driver.prune_task_runs(now=_NOW)

    with sessions() as session:
        remaining = {row.id for row in session.execute(select(TaskRun)).scalars()}
    assert "prune-old-ok" not in remaining, "an old finished row is pruned"
    assert {"prune-old-running", "prune-recent-ok"} <= remaining, "running + recent rows are kept"
    engine.dispose()


def test_run_due_ingests_survives_a_crashing_instance(
    postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """One instance crashing must never abort the tick or crash the driver loop (B1)."""
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, _FakeConnector("stub"))
    id1 = _add_instance(sessions, connector_id="stub", cipher=cipher)
    id2 = _add_instance(sessions, connector_id="stub", cipher=cipher)

    attempted: list[str] = []

    def _boom(instance_id: str, *, now: datetime) -> None:
        attempted.append(instance_id)
        if instance_id == id1:
            raise RuntimeError("instance boom")

    driver._ingest_instance = _boom  # type: ignore[method-assign]
    driver.run_due_ingests(now=_NOW)  # must NOT raise despite id1 crashing

    assert id1 in attempted and id2 in attempted, "both instances attempted; the crash was isolated"
    engine.dispose()
