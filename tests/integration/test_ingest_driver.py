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


def test_driver_failed_instance_stays_retryable_with_backoff(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A failed ingest must NOT park the instance in a dead end (ADR 0054, Phase-B #1).

    The pre-fix bug: ``_finalize`` set a failed instance ``status="error"`` while the
    due-query selects only ``status=="enabled"`` — so one transient failure parked the
    connector forever. The corrected contract: a failed instance is left **retryable**
    (``status=="enabled"``) with ``next_run`` pushed forward by a backoff, while the
    failure stays visible in run history (the ``task_run`` row is still ``status=="error"``).
    """
    connector = _FakeConnector("fake-c", raise_on_collect=True)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    instance_id = _add_instance(sessions, connector_id="fake-c", cipher=cipher)

    driver.run_due_ingests(now=_NOW)

    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None
        assert instance.status != "running"  # not stuck mid-run
        assert instance.status == "enabled"  # retryable — NOT "error" (the bug)
        assert (
            instance.next_run is not None and instance.next_run > _NOW
        )  # still scheduled forward for the next attempt

        # The failure is NOT hidden: the run-history row stays an error with a reason.
        task = session.execute(
            select(TaskRun).where(
                TaskRun.kind == "ingest",
                TaskRun.connector_instance_id == instance_id,
            )
        ).scalar_one()
        assert task.status == "error"
        assert task.error and "source unreachable" in task.error
    engine.dispose()


def test_failed_connector_becomes_due_again_after_backoff(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """The retry loop actually closes: a failed instance comes due again (ADR 0054).

    Proves the core invariant H-8 fixes — a connector that fails is re-selected by the
    due-query after its backoff elapses (instead of being parked in ``error`` forever).
    First failure schedules ``next_run == now + ingest_retry_base_seconds``; the instance
    is NOT re-run while still inside that window, but IS re-run once it has elapsed; a
    SECOND consecutive failure escalates to a longer backoff (``base*2``, capped at
    ``ingest_retry_max_seconds``). Backoff bounds are read from settings via the driver,
    never hardcoded.
    """
    connector = _FakeConnector("fake-retry", raise_on_collect=True)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    instance_id = _add_instance(sessions, connector_id="fake-retry", cipher=cipher)

    base = driver._settings.ingest_retry_base_seconds
    cap = driver._settings.ingest_retry_max_seconds

    # First tick: the connector fails -> retryable, scheduled at exactly the base backoff.
    ran1 = driver.run_due_ingests(now=_NOW)
    assert instance_id in ran1
    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None
        assert instance.status == "enabled"
        assert instance.next_run == _NOW + timedelta(seconds=base)  # first failure -> base backoff

    # A second tick at the SAME instant: still inside the backoff window -> NOT re-run.
    ran_same = driver.run_due_ingests(now=_NOW)
    assert instance_id not in ran_same, "must not re-run while still inside the backoff window"

    # Once the backoff has elapsed: the instance is DUE again and is re-selected (it fails again).
    now2 = _NOW + timedelta(seconds=base + 1)
    ran2 = driver.run_due_ingests(now=now2)
    assert instance_id in ran2, "must re-run once the backoff window elapsed (retry loop closes)"

    # Second consecutive failure -> escalated (longer) backoff, capped at the max.
    expected_backoff = min(base * 2, cap)
    assert expected_backoff > base, "the second failure must back off longer than the first"
    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None
        assert instance.status == "enabled"
        assert instance.next_run == now2 + timedelta(seconds=expected_backoff)

        # Both failures stay visible in run history (failure is never silently swallowed).
        errors = (
            session.execute(
                select(TaskRun).where(
                    TaskRun.kind == "ingest",
                    TaskRun.connector_instance_id == instance_id,
                    TaskRun.status == "error",
                )
            )
            .scalars()
            .all()
        )
        assert len(errors) == 2
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


# -- H-8a: auto-hard-disable after N consecutive failures (ADR 0074, extends ADR 0054) ---------- #


def test_instance_hard_disabled_after_max_consecutive_failures(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """INV-1: after exactly ``ingest_max_consecutive_failures`` consecutive ingest failures the
    instance is hard-disabled (``status=="error"``) and is NEVER re-selected by the due-query —
    closing ADR 0054's 'retry forever' tail. Failures ``1..N-1`` stay ``enabled`` + backoff (INV-2);
    every attempt stays visible as an error ``task_run`` (INV-5)."""
    connector = _FakeConnector("fake-hd", raise_on_collect=True)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    max_failures = 3
    driver._settings = driver._settings.model_copy(
        update={"ingest_max_consecutive_failures": max_failures}
    )
    instance_id = _add_instance(sessions, connector_id="fake-hd", cipher=cipher)

    now = _NOW
    for k in range(1, max_failures + 1):
        ran = driver.run_due_ingests(now=now)
        assert instance_id in ran, f"instance must be due on attempt {k}"
        with sessions() as session:
            instance = session.get(ConnectorInstance, instance_id)
            assert instance is not None
            if k < max_failures:
                assert instance.status == "enabled", f"attempt {k} (< N) stays retryable (ADR 0054)"
            else:
                assert instance.status == "error", "the Nth consecutive failure hard-disables"
            nxt = instance.next_run
        now = (nxt + timedelta(seconds=1)) if nxt is not None else now

    # INV-1: a hard-disabled instance is never re-selected, even far in the future.
    far_future = _NOW + timedelta(days=365)
    assert instance_id not in driver.run_due_ingests(now=far_future), (
        "a hard-disabled instance must not be re-selected by the due-query"
    )

    # INV-5: every attempt stays visible in run history as an error (failure never swallowed).
    with sessions() as session:
        n_errors = session.execute(
            select(func.count())
            .select_from(TaskRun)
            .where(
                TaskRun.kind == "ingest",
                TaskRun.connector_instance_id == instance_id,
                TaskRun.status == "error",
            )
        ).scalar_one()
    assert n_errors == max_failures
    engine.dispose()


def test_max_failures_zero_disables_hard_disable(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """INV-3 opt-out: ``ingest_max_consecutive_failures == 0`` never hard-disables; the instance
    stays retryable (the exact ADR-0054 retry-forever behaviour) however often it fails."""
    connector = _FakeConnector("fake-forever", raise_on_collect=True)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    driver._settings = driver._settings.model_copy(update={"ingest_max_consecutive_failures": 0})
    instance_id = _add_instance(sessions, connector_id="fake-forever", cipher=cipher)

    now = _NOW
    for _ in range(6):  # well past any plausible default threshold
        driver.run_due_ingests(now=now)
        with sessions() as session:
            instance = session.get(ConnectorInstance, instance_id)
            assert instance is not None
            assert instance.status == "enabled", "max=0 must never hard-disable"
            nxt = instance.next_run
        now = (nxt + timedelta(seconds=1)) if nxt is not None else now
    engine.dispose()


def test_success_resets_streak_before_hard_disable(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """INV-4: a SUCCESS breaks the consecutive-failure streak, so an instance that fails ``N-1``
    times then succeeds is NOT one failure from death — a later failure restarts the streak at 1."""
    connector = _FakeConnector("fake-recover", raise_on_collect=True)
    engine, sessions, driver, cipher = _harness(postgres_dsn, clean_graph, connector)
    max_failures = 3
    driver._settings = driver._settings.model_copy(
        update={"ingest_max_consecutive_failures": max_failures}
    )
    instance_id = _add_instance(sessions, connector_id="fake-recover", cipher=cipher)

    now = _NOW
    # Fail N-1 times (one short of hard-disable).
    for _ in range(max_failures - 1):
        driver.run_due_ingests(now=now)
        with sessions() as session:
            nxt = session.get(ConnectorInstance, instance_id).next_run  # type: ignore[union-attr]
        now = nxt + timedelta(seconds=1) if nxt is not None else now

    # Succeed once: the streak resets to 0.
    connector._raise = False
    driver.run_due_ingests(now=now)
    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None and instance.status == "enabled"
        nxt = instance.next_run
    now = nxt + timedelta(seconds=1) if nxt is not None else now

    # A single failure AFTER the success must NOT hard-disable (streak is 1, not N).
    connector._raise = True
    driver.run_due_ingests(now=now)
    with sessions() as session:
        instance = session.get(ConnectorInstance, instance_id)
        assert instance is not None
        assert instance.status == "enabled", "one failure after a success must not hard-disable"
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


def test_prune_dead_letters_removes_old_only(postgres_dsn: str, clean_graph: Neo4jClient) -> None:
    """Gate B-4d (ADR 0053): ``prune_dead_letters`` bounds the replayable dead-letter
    error-audit table (M-6). It deletes every ``ingest_dead_letter`` row whose
    ``created_at < now - dead_letter_retention_days`` (default 30d), KEEPS rows inside
    the window AND the row exactly at the cutoff (``<`` semantics, off-by-one), returns
    the deleted count, and treats ``retention <= 0`` as disabled (deletes nothing).

    Dead-letters are terminal (written once, never mutated) — so, unlike
    ``prune_task_runs``, there is NO status/finished_at filter: ALL rows older than the
    window are pruned. Deterministic via an injected ``now`` + straddling ``created_at``
    seeds (mirrors ``test_prune_task_runs_removes_old_finished_only``)."""
    from worldmonitor.db.models import IngestDeadLetter

    engine, sessions, driver, _ = _harness(postgres_dsn, clean_graph, _FakeConnector("stub"))

    cutoff_at = _NOW - timedelta(days=30)  # default retention window edge
    old = _NOW - timedelta(days=60)  # outside the window -> pruned
    with sessions() as session:
        session.add(
            IngestDeadLetter(
                id="dl-old",
                connector_id="stub",
                source_key="r-old",
                stage="map",
                error="boom",
                created_at=old,
            )
        )
        session.add(
            IngestDeadLetter(
                id="dl-boundary",
                connector_id="stub",
                source_key="r-boundary",
                stage="map",
                error="boom",
                created_at=cutoff_at,  # exactly at cutoff -> KEPT (created_at < cutoff is False)
            )
        )
        session.add(
            IngestDeadLetter(
                id="dl-recent",
                connector_id="stub",
                source_key="r-recent",
                stage="land",
                error="boom",
                created_at=_NOW,  # inside the window -> kept
            )
        )
        session.commit()

    deleted = driver.prune_dead_letters(now=_NOW)
    assert deleted == 1, "exactly the one row older than the 30d window is pruned"

    with sessions() as session:
        remaining = {row.id for row in session.execute(select(IngestDeadLetter)).scalars()}
    assert "dl-old" not in remaining, "a row older than the retention window is pruned"
    assert {"dl-boundary", "dl-recent"} <= remaining, (
        "the row exactly at the cutoff and the in-window row are kept"
    )

    # Retention <= 0 disables pruning entirely: an ancient row survives, return is 0.
    driver._settings = driver._settings.model_copy(update={"dead_letter_retention_days": 0})
    with sessions() as session:
        session.add(
            IngestDeadLetter(
                id="dl-ancient-disabled",
                connector_id="stub",
                source_key="r-ancient",
                stage="resolve-row",
                error="boom",
                created_at=_NOW - timedelta(days=3650),
            )
        )
        session.commit()

    assert driver.prune_dead_letters(now=_NOW) == 0, "retention <= 0 deletes nothing"
    with sessions() as session:
        survived = session.get(IngestDeadLetter, "dl-ancient-disabled")
    assert survived is not None, "with retention disabled even an ancient row survives"
    engine.dispose()
