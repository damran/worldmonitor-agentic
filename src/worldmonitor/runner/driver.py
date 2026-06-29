"""The long-running ingest driver (ADR 0029).

Turns the call-once primitives (``run_ingest``, ``resolve_pending``) into a running
system: it reads the ``ConnectorInstance`` registry, runs each enabled connector on a
cadence, and resolves the queue on its own independent cadence.

Scope (Gate A, batch sources on a timer, single-node):
* Runs **EXTERNAL_IMPORT** connectors via the bounded/windowed ``run_ingest``.
* **Refuses ``Capability.ACTIVE`` connectors visibly** — records a ``task_run`` error
  with a reason (never a silent skip, never agent-auto-run) until the authorized-
  scope-token system exists.
* Config is **decrypted at use** (``ConfigCipher``), never cached in plaintext.
* Resolution runs on ``RESOLVE_CADENCE_SECONDS`` and is **serialized** — the driver
  never overlaps its own ``resolve_pending`` runs (single-node; the multi-replica
  lease is deferred, ADR 0029 fork X2).
* Every run is recorded in ``task_run``; rows left ``running`` by a crash are reset to
  ``error`` on startup (single-node recovery, replaced by the lease under HA).

The core passes are synchronous and take an injected ``now`` so they are
deterministically testable; :meth:`IngestDriver.run_forever` is the thin asyncio loop.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import pkgutil
import sys
import threading
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.db.models import ConnectorInstance, ErQueueItem, IngestDeadLetter, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.plugins.base import Capability, Mode
from worldmonitor.plugins.registry import Registry
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.runner.heartbeat import Heartbeat
from worldmonitor.runner.ingest import run_ingest
from worldmonitor.settings import Settings, get_settings
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)

_ERROR_SUMMARY_MAX = 2000


class ActiveConnectorRefused(RuntimeError):
    """Raised when the driver is asked to run an ``ACTIVE``-capability connector.

    Active plugins are gated (authorized-scope token per run, separate logging,
    never agent-auto-run — CLAUDE.md). Until that gate exists the driver refuses
    them, visibly (a ``task_run`` error), rather than running them.
    """


class IngestDriver:
    """Cadence-driven runner over the connector-instance registry + the ER queue."""

    def __init__(
        self,
        *,
        sessions: sessionmaker[Session],
        landing: LandingStore,
        neo4j: Neo4jClient,
        registry: Registry,
        cipher: ConfigCipher | None = None,
        settings: Settings | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self._sessions = sessions
        self._landing = landing
        self._neo4j = neo4j
        self._registry = registry
        self._settings = settings or get_settings()
        self._cipher = cipher or ConfigCipher.from_settings(self._settings)
        # Last-tick heartbeat FILE (Gate B-4c / ADR 0051): touched once per loop iteration so a
        # stalled pipeline is detectable via --healthcheck even while /health still echoes ok.
        self._heartbeat = heartbeat or Heartbeat(
            Path(self._settings.driver_heartbeat_path),
            self._settings.driver_heartbeat_stale_seconds,
        )
        # Serializes resolution so a slow run never overlaps the next cadence tick.
        self._resolve_lock = threading.Lock()
        # Consecutive non-blocking lock-skips (ADR 0075 D3): incremented when a tick finds the
        # resolve lock held, reset to 0 on a successful acquire. Escalates info->WARNING at the
        # configured threshold so a wedged pass surfaces instead of silently starving resolution.
        self._consecutive_resolve_skips = 0

    # -- startup recovery ---------------------------------------------------- #
    def recover_stale(self) -> int:
        """Reset rows left ``running`` by a crashed prior run (single-node startup).

        A crash can leave a ``task_run`` and its ``ConnectorInstance`` in
        ``running`` forever. Reset the tasks to ``error`` and the instances to
        ``enabled`` so the next tick re-runs them. Returns the number of stale tasks
        reset. (Under HA this is replaced by a lease/heartbeat — deferred, fork X2.)
        """
        now = datetime.now(UTC)
        with self._sessions() as session:
            stale_tasks = list(
                session.execute(select(TaskRun).where(TaskRun.status == "running")).scalars()
            )
            for task in stale_tasks:
                task.status = "error"
                task.error = "reset on driver startup (left running by a prior crash)"
                task.finished_at = now
            stale_instances = list(
                session.execute(
                    select(ConnectorInstance).where(ConnectorInstance.status == "running")
                ).scalars()
            )
            for instance in stale_instances:
                instance.status = "enabled"
            session.commit()
        if stale_tasks:
            logger.warning("driver startup: reset %d stale 'running' task(s)", len(stale_tasks))
        return len(stale_tasks)

    def prune_task_runs(self, *, now: datetime | None = None) -> int:
        """Delete finished (``ok``/``error``) task_run rows older than the retention window.

        Keeps the run-history table bounded (``TASK_RUN_RETENTION_DAYS``, ADR 0029
        follow-up). ``running`` rows are never pruned; a retention of 0 disables it.
        Returns the number deleted.
        """
        retention = self._settings.task_run_retention_days
        if retention <= 0:
            return 0
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention)
        with self._sessions() as session:
            stale_ids = list(
                session.execute(
                    select(TaskRun.id).where(
                        TaskRun.status.in_(("ok", "error")),
                        TaskRun.finished_at.is_not(None),
                        TaskRun.finished_at < cutoff,
                    )
                ).scalars()
            )
            if stale_ids:
                session.execute(delete(TaskRun).where(TaskRun.id.in_(stale_ids)))
                session.commit()
        deleted = len(stale_ids)
        if deleted:
            logger.info("pruned %d finished task_run row(s) older than %dd", deleted, retention)
        return deleted

    def prune_dead_letters(self, *, now: datetime | None = None) -> int:
        """Delete ``ingest_dead_letter`` rows older than the retention window.

        Bounds the replayable error-audit table (``DEAD_LETTER_RETENTION_DAYS``, M-6 /
        ADR 0053) so it does not grow without bound. Unlike ``prune_task_runs`` there is
        NO status/finished_at filter — dead-letters are terminal (written once, never
        mutated), so ALL rows with ``created_at < cutoff`` are pruned. A retention of 0
        disables it. Returns the number deleted.
        """
        retention = self._settings.dead_letter_retention_days
        if retention <= 0:
            return 0
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention)
        with self._sessions() as session:
            stale_ids = list(
                session.execute(
                    select(IngestDeadLetter.id).where(IngestDeadLetter.created_at < cutoff)
                ).scalars()
            )
            if stale_ids:
                session.execute(delete(IngestDeadLetter).where(IngestDeadLetter.id.in_(stale_ids)))
                session.commit()
        deleted = len(stale_ids)
        if deleted:
            logger.info("pruned %d dead_letter row(s) older than %dd", deleted, retention)
        return deleted

    # -- periodic maintenance (ADR 0075 D1) ---------------------------------- #
    def run_maintenance(self, *, now: datetime) -> None:
        """Run the two retention prunes on the maintenance cadence.

        Wraps ``prune_task_runs`` + ``prune_dead_letters`` and ONLY those — it deliberately does
        NOT call ``recover_stale`` (ADR 0075 D1): the prunes only touch old, finished/terminal rows
        a live row never matches, so they are safe to run mid-uptime; ``recover_stale`` blindly
        resets every ``running`` row and would clobber a live/abandoned resolve worker, so it stays
        the startup-only preamble call. The prunes' DELETE semantics are unchanged — only WHEN they
        run (B-4d / ADR 0053 frozen).
        """
        self.prune_task_runs(now=now)
        self.prune_dead_letters(now=now)

    def _maintenance_due(self, now: datetime, last_maintenance: datetime | None) -> bool:
        """True when the maintenance cadence has elapsed (or has never run).

        ``last_maintenance is None`` makes the FIRST tick fire, so the boot-time prune is preserved
        and the cadence is a strict superset of today's startup-only behaviour (ADR 0075 D1).
        """
        return (
            last_maintenance is None
            or (now - last_maintenance).total_seconds()
            >= self._settings.maintenance_cadence_seconds
        )

    # -- ingest pass --------------------------------------------------------- #
    def run_due_ingests(self, *, now: datetime) -> list[str]:
        """Run every enabled connector instance whose ``next_run`` has arrived."""
        with self._sessions() as session:
            due_ids = [
                instance.id
                for instance in session.execute(
                    select(ConnectorInstance).where(
                        ConnectorInstance.status == "enabled",
                        or_(
                            ConnectorInstance.next_run.is_(None),
                            ConnectorInstance.next_run <= now,
                        ),
                    )
                ).scalars()
            ]
        for instance_id in due_ids:
            try:
                self._ingest_instance(instance_id, now=now)
            except Exception:
                # One instance's failure must never abort the tick or crash the driver.
                logger.exception("ingest instance %s crashed; continuing", instance_id)
        return due_ids

    def _ingest_instance(self, instance_id: str, *, now: datetime) -> None:
        # 1. Claim the instance and open a running task (committed before the work,
        #    so a crash mid-run leaves a recoverable trail).
        with self._sessions() as session:
            instance = session.get(ConnectorInstance, instance_id)
            if instance is None or instance.status != "enabled":
                return
            instance.status = "running"
            task_id = str(uuid.uuid4())
            session.add(
                TaskRun(
                    id=task_id,
                    connector_instance_id=instance_id,
                    kind="ingest",
                    status="running",
                )
            )
            connector_id, config_token = (
                instance.connector_id,
                instance.config_encrypted,
            )
            # G8 resume (ADR 0070): read the saved stream cursor in the claim txn so it can be
            # injected before the run. ``None`` for every batch connector — nothing is injected.
            stream_cursor = instance.stream_cursor
            session.commit()

        # 2. Run the connector (its own session; run_ingest commits per window).
        status, error, stats = "ok", "", None
        # Whether this connector keeps the stream warm (Mode.STREAM -> next_run=now); stays False on
        # any failure (e.g. an unknown connector) so the unchanged backoff path is used.
        is_stream = False
        try:
            config = json.loads(self._cipher.decrypt(config_token))
            connector = self._registry.get(connector_id)
            is_stream = connector.manifest.mode is Mode.STREAM
            if connector.manifest.capability is Capability.ACTIVE:
                raise ActiveConnectorRefused(
                    f"connector '{connector_id}' is ACTIVE-capability; refused — active plugins "
                    "need an authorized-scope token and are never agent-auto-run"
                )
            # INJECT the saved cursor only when one exists (ADR 0070); a stream collect() reads it
            # via ``config.get("_cursor")``. Absent it, a stream tails live. Batch is untouched.
            if stream_cursor is not None:
                config["_cursor"] = stream_cursor
            with self._sessions() as work:
                result = run_ingest(
                    connector,
                    config,
                    landing=self._landing,
                    session=work,
                )
            stats = asdict(result)
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX]
            logger.warning("ingest task failed [%s]: %s", connector_id, exc)

        # 3. Finalize task + instance (status, cadence, stream cursor).
        self._finalize(
            task_id,
            instance_id,
            status=status,
            error=error,
            stats=stats,
            now=now,
            kind="ingest",
            stream=is_stream,
        )

    # -- resolution pass ----------------------------------------------------- #
    # Marker returned by ``run_resolution`` when a single-tenant resolution pass ran
    # (there was a pending backlog). The per-tenant id list of the multi-tenant design
    # collapses to this one constant under D1 (ADR 0042); an empty list still means
    # "no work / skipped this tick".
    _RESOLVED_MARKER = "__all__"

    def run_resolution(self, *, now: datetime) -> list[str]:
        """Resolve pending candidates in a SINGLE pass when there is a backlog.

        Serialized: if a resolution is already running (a slow prior tick), this
        tick is skipped rather than overlapping it. Returns ``[_RESOLVED_MARKER]`` if a
        resolution pass ran this tick, or ``[]`` if there was no backlog or the tick was
        skipped (single-tenant, D1 / ADR 0042).
        """
        if not self._resolve_lock.acquire(blocking=False):
            # Skip-rather-than-overlap is unchanged; D3 only ADDS escalation on REPEATED contention.
            self._consecutive_resolve_skips += 1
            if self._consecutive_resolve_skips >= self._settings.resolve_lock_skip_alert_threshold:
                logger.warning(
                    "resolution wedged: %d consecutive lock-skips — a prior pass still holds the "
                    "lock (resolution starving; ADR 0075 D3)",
                    self._consecutive_resolve_skips,
                )
            else:
                logger.info("resolution already in progress; skipping this tick")
            return []
        try:
            # A successful acquire breaks any skip streak, so escalation fires only on genuinely
            # repeated contention (ADR 0075 D3).
            self._consecutive_resolve_skips = 0
            with self._sessions() as session:
                has_backlog = session.execute(
                    select(ErQueueItem.id).where(ErQueueItem.status == "pending").limit(1)
                ).first()
            if has_backlog is None:
                return []
            self._resolve(now=now)
            return [self._RESOLVED_MARKER]
        finally:
            self._resolve_lock.release()

    def _resolve(self, *, now: datetime) -> None:
        with self._sessions() as session:
            task_id = str(uuid.uuid4())
            session.add(TaskRun(id=task_id, kind="resolve", status="running"))
            session.commit()

        status, error, stats = "ok", "", None
        try:
            with self._sessions() as work:
                result = resolve_pending(
                    session=work,
                    neo4j=self._neo4j,
                    timeout=self._settings.resolve_timeout_seconds,
                )
            stats = asdict(result)
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX]
            logger.warning("resolve task failed: %s", exc)

        self._finalize(
            task_id, None, status=status, error=error, stats=stats, now=now, kind="resolve"
        )

    def _resolve_wait_timeout(self) -> float | None:
        """The loop-liveness backstop bound for the resolve ``to_thread`` await (ADR 0075 D3).

        ``None`` when ``resolve_timeout_seconds <= 0`` (disabled → today's awaited behaviour: a
        wedged pass is caught coarsely by the heartbeat-staleness healthcheck), else
        ``resolve_timeout_seconds + driver_tick_seconds`` — strictly LOOSER than the D2 cooperative
        deadline (one tick of grace), so in the normal multi-batch case D2 breaks cleanly between
        batches and no thread is abandoned; the backstop only engages for a single batch that hangs
        *inside* DuckDB/Neo4j (never reaching the between-batch check).
        """
        if self._settings.resolve_timeout_seconds <= 0:
            return None
        return self._settings.resolve_timeout_seconds + self._settings.driver_tick_seconds

    # -- shared finalize ----------------------------------------------------- #
    def _finalize(
        self,
        task_id: str,
        instance_id: str | None,
        *,
        status: str,
        error: str,
        stats: dict[str, object] | None,
        now: datetime,
        kind: str,
        stream: bool = False,
    ) -> None:
        with self._sessions() as session:
            task = session.get(TaskRun, task_id)
            if task is not None:
                task.status = status
                task.error = error
                task.stats = stats
                task.finished_at = now
            if instance_id is not None:
                instance = session.get(ConnectorInstance, instance_id)
                if instance is not None:
                    # Default: keep the instance retryable ("enabled"). The due-query selects only
                    # "enabled", so on success we reschedule on the normal cadence and on a
                    # transient failure an exponential backoff (ADR 0054; resets on the next
                    # success, when the consecutive-error streak breaks). A failure ALWAYS stays
                    # visible via the task_run row above. The one exception is a SUSTAINED streak:
                    # the failure branch below hard-disables to "error" after N fails (ADR 0074).
                    instance.status = "enabled"
                    instance.last_run = now
                    # PERSIST the G8 stream cursor (ADR 0070) transactionally, only when the run
                    # reported one. A batch run reports last_cursor=None -> a no-op for batch.
                    last_cursor = stats.get("last_cursor") if stats is not None else None
                    if isinstance(last_cursor, str):
                        instance.stream_cursor = last_cursor
                    if status == "ok":
                        # KEEP STREAMS WARM: a Mode.STREAM instance re-runs immediately (continuous
                        # windowed consumption); a batch instance keeps the normal cadence.
                        if stream:
                            instance.next_run = now
                        else:
                            instance.next_run = now + timedelta(
                                seconds=self._settings.ingest_cadence_seconds
                            )
                    else:
                        failures = self._consecutive_ingest_failures(session, instance_id)
                        max_failures = self._settings.ingest_max_consecutive_failures
                        if max_failures and failures >= max_failures:
                            # HARD-DISABLE (ADR 0074): a terminal "error" status — the due-query
                            # selects only "enabled", so the instance stops retrying rather than
                            # backing off forever (ADR 0054's named follow-up). The failure stays
                            # visible (the task_run row above); an operator re-enables it from the
                            # Integrations UI (status → "enabled"), and a success resets the streak.
                            instance.status = "error"
                            logger.error(
                                "instance %s hard-disabled after %d consecutive ingest failures",
                                instance_id,
                                failures,
                            )
                        else:
                            delay = self._backoff_seconds(failures)
                            instance.next_run = now + timedelta(seconds=delay)
            session.commit()

    def _consecutive_ingest_failures(self, session: Session, instance_id: str) -> int:
        """Count the most-recent CONSECUTIVE ``kind="ingest"`` ``task_run`` errors for this instance
        (ADR 0054), INCLUDING the run just finalized. The current task row's ``status="error"`` is
        already set on the session above; ``flush`` makes it visible so a first failure counts as 1.

        One source of truth drives BOTH the ADR-0054 backoff and the ADR-0074 hard-disable threshold
        — the streak is losslessly encoded in run history, so neither needs a schema change. A
        success (a non-error row) breaks the streak, so a recovered instance is never hard-disabled
        off stale failures.
        """
        session.flush()
        statuses = session.execute(
            select(TaskRun.status)
            .where(
                TaskRun.kind == "ingest",
                TaskRun.connector_instance_id == instance_id,
            )
            .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
        ).scalars()
        failures = 0
        for row_status in statuses:
            if row_status != "error":
                break
            failures += 1
        return failures

    def _backoff_seconds(self, consecutive_failures: int) -> int:
        """Exponential backoff for a just-failed ingest streak (ADR 0054): a first failure backs off
        ``base``, a second ``base*2``, …, capped at ``ingest_retry_max_seconds``."""
        consecutive_failures = max(consecutive_failures, 1)
        backoff = self._settings.ingest_retry_base_seconds * 2 ** (consecutive_failures - 1)
        return min(backoff, self._settings.ingest_retry_max_seconds)

    # -- the loop ------------------------------------------------------------ #
    async def run_forever(self) -> None:  # pragma: no cover - thin asyncio glue
        """Drive ingests + resolution on cadence until cancelled."""
        # recover_stale STAYS startup-only (ADR 0075 D1) — the prunes moved into the periodic
        # maintenance cadence below (the first tick fires, so the boot-time prune is preserved).
        self.recover_stale()
        last_resolve: datetime | None = None
        last_maintenance: datetime | None = None
        while True:
            now = datetime.now(UTC)
            try:
                # Last-tick heartbeat: every tick, before any work, so an idle driver still
                # proves it is alive (Gate B-4c). Additive — does not touch cadence/serialization.
                self._heartbeat.touch(now)
                await asyncio.to_thread(self.run_due_ingests, now=now)
                # Periodic maintenance (ADR 0075 D1): prune on cadence, not only at startup.
                if self._maintenance_due(now, last_maintenance):
                    await asyncio.to_thread(self.run_maintenance, now=now)
                    last_maintenance = now
                if (
                    last_resolve is None
                    or (now - last_resolve).total_seconds()
                    >= self._settings.resolve_cadence_seconds
                ):
                    # Loop-liveness backstop (ADR 0075 D3): bound the resolve await so a single
                    # batch that hangs INSIDE DuckDB/Neo4j (never reaching the D2 between-batch
                    # check) cannot wedge the whole loop. On timeout the worker is abandoned (not
                    # killed — a pooled thread cannot be force-cancelled) and the loop continues;
                    # ingest + heartbeat stay alive and the lock-skip escalation surfaces it.
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(self.run_resolution, now=now),
                            timeout=self._resolve_wait_timeout(),
                        )
                    except TimeoutError:
                        # asyncio.TimeoutError IS TimeoutError on 3.11+ (the wait_for raise).
                        logger.error(
                            "resolve pass exceeded the loop-liveness backstop (%ss); abandoning "
                            "the worker and continuing — ingest/heartbeat stay alive (ADR 0075 D3)",
                            self._resolve_wait_timeout(),
                        )
                    last_resolve = now
            except Exception:
                # A tick failure (transient DB error, etc.) must never kill the loop.
                logger.exception("driver tick failed; continuing")
            await asyncio.sleep(self._settings.driver_tick_seconds)


def discover_connectors() -> Registry:
    """A registry of every connector under ``worldmonitor.plugins.connectors``.

    Connectors live two levels down (``connectors/<name>/connector.py``), so walk the
    package recursively and register each connector class in the modules found.
    """
    registry = Registry()
    package = importlib.import_module("worldmonitor.plugins.connectors")
    for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
        registry.discover_module(importlib.import_module(info.name))
    return registry


def build_driver(settings: Settings | None = None) -> IngestDriver:
    """Wire an :class:`IngestDriver` from settings + the discovered connector registry.

    The process entry point for the smoke run (docs/runbooks/smoke-run.md): builds the
    real Postgres sessions, landing store, Neo4j client, and a registry auto-discovered
    from ``worldmonitor.plugins.connectors``.
    """
    settings = settings or get_settings()
    # Fail closed: a non-development boot with a placeholder secret halts loud here, before any
    # store client is built (ADR 0061). Development is unaffected (placeholders allowed locally).
    # The cheap --healthcheck path in main() never reaches here, so it stays connection-free.
    settings.validate_production_secrets()
    return IngestDriver(
        registry=discover_connectors(),
        sessions=session_factory(engine_from_settings(settings)),
        landing=LandingStore.from_settings(settings),
        neo4j=Neo4jClient.from_settings(settings),
        settings=settings,
    )


def _run_healthcheck(settings: Settings) -> int:  # pragma: no cover - exercised via subprocess
    """The container HEALTHCHECK: read ONLY the heartbeat file; exit 0 alive / 1 down.

    Must NOT construct the full driver (no store connections) — a stalled-but-up pipeline must
    still be reportable, and a healthcheck must stay cheap. Builds the ``Heartbeat`` from
    settings (``DRIVER_HEARTBEAT_PATH`` / ``DRIVER_HEARTBEAT_STALE_SECONDS``) and checks it.
    """
    heartbeat = Heartbeat(
        Path(settings.driver_heartbeat_path),
        settings.driver_heartbeat_stale_seconds,
    )
    return 0 if heartbeat.is_alive(datetime.now(UTC)) else 1


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - process entry point
    """Run the ingest driver forever (Ctrl-C to stop). ``python -m worldmonitor.runner.driver``.

    With ``--healthcheck`` it instead reads the last-tick heartbeat and exits 0 (alive) /
    1 (missing-or-stale) — the container HEALTHCHECK; it never connects to any store.
    """
    args = sys.argv[1:] if argv is None else argv
    settings = get_settings()
    if "--healthcheck" in args:
        return _run_healthcheck(settings)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    driver = build_driver(settings)
    logger.info(
        "ingest driver starting: guard_mode=%s ingest_cadence=%ss resolve_cadence=%ss tick=%ss",
        settings.merge_guard_mode,
        settings.ingest_cadence_seconds,
        settings.resolve_cadence_seconds,
        settings.driver_tick_seconds,
    )
    try:
        asyncio.run(driver.run_forever())
    except KeyboardInterrupt:
        logger.info("ingest driver stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
