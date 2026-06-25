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
import threading
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.db.models import ConnectorInstance, ErQueueItem, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.plugins.base import Capability
from worldmonitor.plugins.registry import Registry
from worldmonitor.resolution.pipeline import resolve_pending
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
    ) -> None:
        self._sessions = sessions
        self._landing = landing
        self._neo4j = neo4j
        self._registry = registry
        self._settings = settings or get_settings()
        self._cipher = cipher or ConfigCipher.from_settings(self._settings)
        # Serializes resolution so a slow run never overlaps the next cadence tick.
        self._resolve_lock = threading.Lock()

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
            session.commit()

        # 2. Run the connector (its own session; run_ingest commits per window).
        status, error, stats = "ok", "", None
        try:
            config = json.loads(self._cipher.decrypt(config_token))
            connector = self._registry.get(connector_id)
            if connector.manifest.capability is Capability.ACTIVE:
                raise ActiveConnectorRefused(
                    f"connector '{connector_id}' is ACTIVE-capability; refused — active plugins "
                    "need an authorized-scope token and are never agent-auto-run"
                )
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

        # 3. Finalize task + instance (status, cadence).
        self._finalize(
            task_id, instance_id, status=status, error=error, stats=stats, now=now, kind="ingest"
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
            logger.info("resolution already in progress; skipping this tick")
            return []
        try:
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
                result = resolve_pending(session=work, neo4j=self._neo4j)
            stats = asdict(result)
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX]
            logger.warning("resolve task failed: %s", exc)

        self._finalize(
            task_id, None, status=status, error=error, stats=stats, now=now, kind="resolve"
        )

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
                    instance.status = "enabled" if status == "ok" else "error"
                    instance.last_run = now
                    instance.next_run = now + timedelta(
                        seconds=self._settings.ingest_cadence_seconds
                    )
            session.commit()

    # -- the loop ------------------------------------------------------------ #
    async def run_forever(self) -> None:  # pragma: no cover - thin asyncio glue
        """Drive ingests + resolution on cadence until cancelled."""
        self.recover_stale()
        self.prune_task_runs()
        last_resolve: datetime | None = None
        while True:
            now = datetime.now(UTC)
            try:
                await asyncio.to_thread(self.run_due_ingests, now=now)
                if (
                    last_resolve is None
                    or (now - last_resolve).total_seconds()
                    >= self._settings.resolve_cadence_seconds
                ):
                    await asyncio.to_thread(self.run_resolution, now=now)
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
    return IngestDriver(
        registry=discover_connectors(),
        sessions=session_factory(engine_from_settings(settings)),
        landing=LandingStore.from_settings(settings),
        neo4j=Neo4jClient.from_settings(settings),
        settings=settings,
    )


def main() -> int:  # pragma: no cover - process entry point
    """Run the ingest driver forever (Ctrl-C to stop). ``python -m worldmonitor.runner.driver``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
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
