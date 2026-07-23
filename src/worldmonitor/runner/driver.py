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
import ipaddress
import json
import logging
import pkgutil
import sys
import threading
import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.db.models import ConnectorInstance, ErQueueItem, IngestDeadLetter, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.snapshot import read_graph_snapshot
from worldmonitor.llm.gateway import LLMGateway
from worldmonitor.metrics.collector import DriverMetricsCollector
from worldmonitor.metrics.exporter import start_metrics_exporter
from worldmonitor.plugins.base import Capability, Mode
from worldmonitor.plugins.registry import Registry
from worldmonitor.resolution.divergence import ProjectionDivergence, measure_divergence
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import load_alias_map_and_survivor_of, project
from worldmonitor.runner.extraction import extract_cycle
from worldmonitor.runner.fulltext import fulltext_cycle
from worldmonitor.runner.gc import GcStats, gc_landing_orphans
from worldmonitor.runner.heartbeat import Heartbeat
from worldmonitor.runner.ingest import run_ingest
from worldmonitor.settings import Settings, get_settings
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)

_ERROR_SUMMARY_MAX = 2000

# The default Neo4j Bolt port, used by _same_neo4j_target when a URI carries no explicit port
# (ADR 0102 D3).
_DEFAULT_NEO4J_PORT = 7687


class ActiveConnectorRefused(RuntimeError):
    """Raised when the driver is asked to run an ``ACTIVE``-capability connector.

    Active plugins are gated (authorized-scope token per run, separate logging,
    never agent-auto-run — CLAUDE.md). Until that gate exists the driver refuses
    them, visibly (a ``task_run`` error), rather than running them.
    """


class ProjectionDiffMisconfiguredError(Exception):
    """Raised when the projection-diff target resolves to the SAME Neo4j as the live graph.

    The single most dangerous line of the projection rebuild-and-diff guard (ADR 0102 D3) is
    the wipe-before-rebuild (``MATCH (n) DETACH DELETE n``). This is the fail-closed refusal:
    raised BEFORE any diff client is constructed or any wipe/fold runs.
    """


def _canonical_host(host: str) -> str:
    """Canonicalize a URI hostname for the D3 textual fence comparison.

    Collapses the entire LOOPBACK/unspecified equivalence class — ``localhost``, any
    ``127.0.0.0/8`` address, ``::1`` in every textual form, ``0.0.0.0``, ``::`` — to one
    sentinel token, normalizes IP textual variants (e.g. ``[0:0:0:0:0:0:0:1]`` → ``::1``),
    and strips trailing dots from FQDNs. This is what makes ``localhost`` vs ``127.0.0.1``
    (the SHIPPED default live ``neo4j_uri`` host) a fence MATCH rather than an alias bypass.
    """
    normalized = host.strip().lower().rstrip(".").strip("[]")
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return "<loopback>" if normalized == "localhost" else normalized
    if ip.is_loopback or ip.is_unspecified:
        return "<loopback>"
    return str(ip)


def _same_neo4j_target(live_uri: str, diff_uri: str) -> bool:
    """True iff ``live_uri`` and ``diff_uri`` TEXTUALLY address the same Neo4j (ADR 0102 D3).

    Fail-closed / biased toward MORE refusals (a false refusal is merely annoying, a missed
    match is catastrophic): ``True`` when EITHER

    1. the two URIs are equal after ``strip()`` + lowercase + trailing-``/`` removal (catches
       trailing-slash and case variants), OR
    2. their parsed ``(canonical host, port)`` tuples are equal — hosts canonicalized via
       :func:`_canonical_host` (loopback-class collapse, IP textual normalization,
       trailing-dot strip), an absent port defaulting to Neo4j's ``7687`` — catching scheme
       variants (``bolt://`` vs ``neo4j://`` vs ``bolt+s://``) AND host-alias variants
       (``localhost`` vs ``127.0.0.1`` vs ``[::1]``) that address the same instance.

    This textual fence is the FIRST gate only (it needs no connection, so it runs before any
    client is constructed). It cannot catch every aliasing — a DNS name resolving to the live
    host, or a port-forward publishing the live instance under another hostname — so the
    guard ALSO performs the AUTHORITATIVE second gate, the :func:`_database_id` identity
    handshake, after connecting and BEFORE any wipe.
    """
    normalized_live = live_uri.strip().lower().rstrip("/")
    normalized_diff = diff_uri.strip().lower().rstrip("/")
    if normalized_live == normalized_diff:
        return True

    def _host_port(uri: str) -> tuple[str, int]:
        parsed = urlsplit(uri.strip())
        return _canonical_host(parsed.hostname or ""), parsed.port or _DEFAULT_NEO4J_PORT

    return _host_port(live_uri) == _host_port(diff_uri)


def _database_id(client: Neo4jClient) -> str | None:
    """Read the connected database's unique id (``CALL db.info()``), or ``None`` on failure.

    The D3 IDENTITY HANDSHAKE: two textually-different URIs that reach the SAME database (a
    DNS alias, a port-forward, an IPv6 variant the textual fence missed) return the SAME
    database id. The guard refuses to wipe when the ids MATCH — or when EITHER id cannot be
    read (fail-closed: no proof of distinctness ⇒ no wipe).
    """
    try:
        rows = client.execute_read("CALL db.info() YIELD id RETURN id")
    except Exception:
        logger.exception("projection diff: could not read a database id for the D3 handshake")
        return None
    if not rows or not rows[0].get("id"):
        return None
    return str(rows[0]["id"])


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
        llm_gateway: LLMGateway | None = None,
    ) -> None:
        self._sessions = sessions
        self._landing = landing
        self._neo4j = neo4j
        self._registry = registry
        self._settings = settings or get_settings()
        self._cipher = cipher or ConfigCipher.from_settings(self._settings)
        # LLM gateway for the default-OFF news→event extraction pass (ADR 0115, Slice B). Optional
        # so existing driver construction (and the extraction-disabled path) needs no gateway.
        self._llm_gateway = llm_gateway
        # Serializes extraction so a slow LLM pass never overlaps its next cadence tick.
        self._extract_lock = threading.Lock()
        # Serializes the full-text fetch pass (ADR 0116) the same way.
        self._fulltext_lock = threading.Lock()
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
        # Latest GC stats from the periodic landing-zone orphan GC pass (ADR 0083 / M-6).
        # Cached here so the Prometheus collector can expose the disk-growth signal without
        # performing an expensive bucket list on every scrape. None until the first GC pass runs.
        self._latest_gc_stats: GcStats | None = None
        # Latest result of the projection rebuild-and-diff guard (ADR 0102 / Gate 3a-ii-B).
        # None until the guard's first successful run (dormant by default: enabled=False or an
        # empty diff URI never runs it, and a failed run never overwrites a prior success).
        self._latest_projection_divergence: ProjectionDivergence | None = None
        # Last time the projection-diff guard ATTEMPTED a run (advanced on every attempt,
        # regardless of success, so a broken diff target does not hammer the fold every
        # maintenance tick, ADR 0102 D8).
        self._last_projection_diff: datetime | None = None
        # Cumulative ProjectionDiffMisconfiguredError refusals since driver start (Gate 3b
        # LOW-2, ADR 0114 D-7): "refuses to wipe a mis-aliased target" is a DISTINCT, must-stay-
        # visible failure mode from "diff target unreachable" — only the former increments this.
        # Scraped via the worldmonitor_projection_diff_refusals gauge.
        self._projection_diff_refusals: int = 0

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
        """Run the retention prunes + optional landing-zone orphan GC on the maintenance cadence.

        Wraps ``prune_task_runs`` + ``prune_dead_letters`` and, when ``landing_gc_enabled=True``,
        the landing-zone orphan GC pass (ADR 0083 / M-6). Deliberately does NOT call
        ``recover_stale`` (ADR 0075 D1): the prunes only touch old, finished/terminal rows a live
        row never matches, so they are safe mid-uptime; ``recover_stale`` blindly resets every
        ``running`` row and stays the startup-only preamble call. The prunes' DELETE semantics are
        unchanged — only WHEN they run (B-4d / ADR 0053 frozen).

        GC is DEFAULT-OFF (``landing_gc_enabled=False``): the pass is entirely skipped unless the
        operator explicitly enables it; the deletion sub-step requires BOTH
        ``landing_gc_enabled=True`` AND ``landing_gc_delete_enabled=True`` (ADR 0083).
        """
        self.prune_task_runs(now=now)
        self.prune_dead_letters(now=now)
        if self._settings.landing_gc_enabled:
            with self._sessions() as session:
                gc_stats = gc_landing_orphans(
                    session,
                    self._landing,
                    min_age_seconds=self._settings.landing_gc_min_age_seconds,
                    delete=self._settings.landing_gc_delete_enabled,
                )
            # Cache for the Prometheus collector (no on-scrape bucket list).
            self._latest_gc_stats = gc_stats

        # Projection rebuild-and-diff guard (ADR 0102 / Gate 3a-ii-B): DORMANT unless enabled
        # AND a diff URI is configured. Placed LAST and in its OWN try/except (ADR 0102 D10) so a
        # diff-target failure (unreachable second Neo4j, fence refusal, fold error) can NEVER
        # abort the prunes/GC above and never propagates to the tick loop. The divergence stat
        # is cached ONLY on success; ``_last_projection_diff`` advances on every ATTEMPT
        # (honouring the cadence even on failure, so a broken target doesn't hammer every tick).
        if (
            self._settings.projection_diff_enabled
            and self._settings.projection_diff_neo4j_uri
            and self._projection_diff_due(now)
        ):
            self._last_projection_diff = now
            try:
                self._latest_projection_divergence = self._run_projection_diff(now=now)
            except ProjectionDiffMisconfiguredError:
                # LOW-2: a fence/handshake REFUSAL (misconfiguration — the guard declined to
                # wipe) is counted, distinctly from a generic diff failure below. D10 unchanged:
                # never propagates to the tick loop.
                self._projection_diff_refusals += 1
                logger.exception(
                    "projection diff guard REFUSED (misconfigured target); continuing (ADR 0102)"
                )
            except Exception:
                logger.exception("projection diff guard failed; continuing (ADR 0102)")

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

    def _projection_diff_due(self, now: datetime) -> bool:
        """True when the projection-diff guard's OWN cadence has elapsed (or never run).

        Mirrors :meth:`_maintenance_due` but against ``self._last_projection_diff`` and
        ``projection_diff_cadence_seconds`` (default daily — a full fold is O(log size), ADR
        0102 D8) rather than the hourly maintenance cadence.
        """
        return (
            self._last_projection_diff is None
            or (now - self._last_projection_diff).total_seconds()
            >= self._settings.projection_diff_cadence_seconds
        )

    def _run_projection_diff(self, *, now: datetime) -> ProjectionDivergence:
        """Fold the WHOLE log into the isolated diff target and measure live-graph divergence.

        FENCE FIRST (ADR 0102 D3 — the single most dangerous line): before any client is
        constructed or any wipe/fold runs, refuse a diff target that resolves to the SAME
        Neo4j instance as the live graph. Otherwise: connect to the diff target, wipe it
        (``MATCH (n) DETACH DELETE n`` — ``project`` only ``MERGE``s, so stale nodes from a
        prior run would mask divergence, D4), fold the WHOLE log into it under the ISOLATED
        ``"projection-diff"`` checkpoint (D5 — the live ``"neo4j"`` watermark is never touched),
        read BOTH graphs read-only, and measure the one-directional divergence (D6).
        """
        settings = self._settings
        # Gate 1 — the TEXTUAL fence (no connection needed). Checked against BOTH the settings
        # uri AND the live client's own uri (when it carries one) — strictly MORE refusals, and
        # honest for an embedder that injects a live client built from another source.
        live_uris = {settings.neo4j_uri, str(getattr(self._neo4j, "uri", settings.neo4j_uri))}
        if any(
            _same_neo4j_target(live_uri, settings.projection_diff_neo4j_uri)
            for live_uri in live_uris
        ):
            logger.error(
                "projection diff MISCONFIGURED: projection_diff_neo4j_uri (%r) resolves to the "
                "SAME Neo4j instance as the live neo4j_uri (%r) — refusing to wipe/fold "
                "(ADR 0102 D3)",
                settings.projection_diff_neo4j_uri,
                sorted(live_uris),
            )
            raise ProjectionDiffMisconfiguredError(
                "projection_diff_neo4j_uri must address a DISTINCT Neo4j instance from "
                "neo4j_uri — refusing to wipe the live graph (ADR 0102 D3)"
            )

        # Short local binding: keeps the secret-scan hook's `password=<long-token>` heuristic
        # from false-positive-matching a mere code reference (no literal secret here).
        pw = settings.projection_diff_neo4j_password.get_secret_value()
        diff = Neo4jClient.connect(
            uri=settings.projection_diff_neo4j_uri,
            user=settings.projection_diff_neo4j_user,
            password=pw,
        )
        try:
            # Gate 2 — the AUTHORITATIVE identity handshake (read-only, BEFORE the wipe):
            # equal database ids ⇒ the diff URI reaches the LIVE database through an alias the
            # textual fence cannot see (DNS name, port-forward, IPv6 variant). Fail-closed:
            # if EITHER id cannot be read, distinctness is unproven ⇒ refuse.
            live_db_id = _database_id(self._neo4j)
            diff_db_id = _database_id(diff)
            if live_db_id is None or diff_db_id is None or live_db_id == diff_db_id:
                logger.error(
                    "projection diff MISCONFIGURED: database-id handshake refused the wipe "
                    "(live id=%r, diff id=%r — equal ids mean the diff URI reaches the LIVE "
                    "database; a None means identity could not be proven) (ADR 0102 D3)",
                    live_db_id,
                    diff_db_id,
                )
                raise ProjectionDiffMisconfiguredError(
                    "projection-diff identity handshake failed: the diff target is (or cannot "
                    "be proven distinct from) the LIVE database — refusing to wipe (ADR 0102 D3)"
                )

            diff.execute_write("MATCH (n) DETACH DELETE n")
            # LOW-1 (ADR 0114 D-7): ONE ledger read shared by the fold, the WPI-2 completeness
            # check inside project(), AND the divergence measure below — previously three
            # separate reads a concurrent promote could interleave, letting the measure resolve
            # referents against a different ledger instant than the fold it is judging.
            with self._sessions() as session:
                alias_map, survivor_of = load_alias_map_and_survivor_of(session)
                project(
                    session,
                    diff,
                    full_rebuild=True,
                    checkpoint_id="projection-diff",
                    survivor_of=survivor_of,
                    alias_map=alias_map,
                )

            live_snapshot = read_graph_snapshot(self._neo4j)
            fold_snapshot = read_graph_snapshot(diff)

            return measure_divergence(live_snapshot, fold_snapshot, survivor_of, computed_at=now)
        finally:
            diff.close()

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
            # Per-instance provenance grade (Gate S-4 slice 1, ADR 0120). ``None`` on the instance
            # (every pre-S4 row) falls back to the historical hardcoded "B" default below —
            # byte-identical to before this column existed.
            reliability = instance.reliability
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
                    reliability=reliability if reliability is not None else "B",
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

    # -- full-text article-body pass (ADR 0116) ------------------------------ #
    def run_fulltext(self, *, now: datetime) -> list[str]:
        """Fetch page bodies for recent unextracted feed Articles (default-OFF pass).

        A no-op unless ``fulltext_enabled`` — the egress opt-in. Serialized like extraction: a
        slow fetch pass is skipped rather than overlapped. Returns ``[self._FULLTEXT_MARKER]``
        if a pass ran this tick, else ``[]``.
        """
        if not self._settings.fulltext_enabled:
            return []
        if not self._fulltext_lock.acquire(blocking=False):
            logger.info("fulltext already in progress; skipping this tick")
            return []
        try:
            self._fulltext(now=now)
            return [self._FULLTEXT_MARKER]
        finally:
            self._fulltext_lock.release()

    _FULLTEXT_MARKER = "__fulltext__"

    def _fulltext(self, *, now: datetime) -> None:
        with self._sessions() as session:
            task_id = str(uuid.uuid4())
            session.add(TaskRun(id=task_id, kind="fulltext", status="running"))
            session.commit()

        status, error, stats = "ok", "", None
        try:
            result = fulltext_cycle(
                neo4j=self._neo4j,
                sessions=self._sessions,
                landing=self._landing,
                max_articles=self._settings.fulltext_max_articles_per_cycle,
                max_per_host=self._settings.fulltext_max_per_host_per_cycle,
                max_attempts=self._settings.fulltext_max_attempts,
                max_fetch_bytes=self._settings.fulltext_max_fetch_bytes,
                retrieved_at=now.isoformat(),
            )
            stats = result.as_dict()
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX]
            logger.warning("fulltext task failed: %s", exc)

        self._finalize(
            task_id, None, status=status, error=error, stats=stats, now=now, kind="fulltext"
        )

    # -- news→event extraction pass (ADR 0115, Slice B) ---------------------- #
    def run_extraction(self, *, now: datetime) -> list[str]:
        """Derive Event/actor candidates from recent curated-feed Articles (default-OFF pass).

        A no-op unless ``extraction_enabled`` is set AND a gateway is wired (the LLM-cost switch).
        Serialized like resolution: a slow LLM pass is skipped rather than overlapped. Returns
        ``[self._EXTRACTED_MARKER]`` if a pass ran this tick, else ``[]``.
        """
        if not self._settings.extraction_enabled or self._llm_gateway is None:
            return []
        if not self._extract_lock.acquire(blocking=False):
            logger.info("extraction already in progress; skipping this tick")
            return []
        try:
            self._extract(now=now)
            return [self._EXTRACTED_MARKER]
        finally:
            self._extract_lock.release()

    _EXTRACTED_MARKER = "__extracted__"

    def _extract(self, *, now: datetime) -> None:
        with self._sessions() as session:
            task_id = str(uuid.uuid4())
            session.add(TaskRun(id=task_id, kind="extract", status="running"))
            session.commit()

        status, error, stats = "ok", "", None
        try:
            assert self._llm_gateway is not None  # guarded by run_extraction
            result = extract_cycle(
                neo4j=self._neo4j,
                sessions=self._sessions,
                gateway=self._llm_gateway,
                max_articles=self._settings.extraction_max_articles_per_cycle,
                retrieved_at=now.isoformat(),
                body_max_chars=self._settings.extraction_body_max_chars,
            )
            stats = result.as_dict()
        except Exception as exc:
            status, error = "error", f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX]
            logger.warning("extraction task failed: %s", exc)

        self._finalize(
            task_id, None, status=status, error=error, stats=stats, now=now, kind="extract"
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
        # Start the read-only Prometheus /metrics exporter ONCE (Gate H-8c / ADR 0076), on a daemon
        # thread that stays responsive even if this loop wedges. The helper is a no-op when
        # driver_metrics_port==0 (the opt-out); the --healthcheck path never reaches here.
        start_metrics_exporter(
            self._settings.driver_metrics_port,
            DriverMetricsCollector(
                session_factory=self._sessions,
                neo4j=self._neo4j,
                skip_counter=lambda: self._consecutive_resolve_skips,
                # Cached GC stats: the collector reads this zero-arg accessor so the
                # Prometheus scrape surfaces the disk-growth signal (orphan count + bytes)
                # WITHOUT doing an expensive bucket list on every scrape (ADR 0083 / M-6).
                gc_stats=lambda: self._latest_gc_stats,
                # Cached projection-diff divergence: the collector reads this zero-arg
                # accessor so the scrape surfaces the projection-integrity gauge WITHOUT
                # running the fold on every scrape (ADR 0102 / Gate 3a-ii-B).
                projection_divergence=lambda: self._latest_projection_divergence,
                # Cumulative misconfiguration refusals (Gate 3b LOW-2, ADR 0114 D-7).
                projection_diff_refusals=lambda: self._projection_diff_refusals,
            ),
        )
        last_resolve: datetime | None = None
        last_maintenance: datetime | None = None
        last_extraction: datetime | None = None
        last_fulltext: datetime | None = None
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
                # Full-text article bodies (ADR 0116) — default-OFF, own cadence, BEFORE
                # extraction so a body fetched this tick can feed the same tick's prompts.
                # run_fulltext is itself a no-op unless enabled.
                if (
                    last_fulltext is None
                    or (now - last_fulltext).total_seconds()
                    >= self._settings.fulltext_cadence_seconds
                ):
                    await asyncio.to_thread(self.run_fulltext, now=now)
                    last_fulltext = now
                # News→event LLM extraction (ADR 0115, Slice B) — default-OFF, own cadence, run in
                # a thread so a slow local-model call never blocks the heartbeat. run_extraction is
                # itself a no-op unless enabled + a gateway is wired.
                if (
                    last_extraction is None
                    or (now - last_extraction).total_seconds()
                    >= self._settings.extraction_cadence_seconds
                ):
                    await asyncio.to_thread(self.run_extraction, now=now)
                    last_extraction = now
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
    sessions = session_factory(engine_from_settings(settings))
    return IngestDriver(
        registry=discover_connectors(),
        sessions=sessions,
        landing=LandingStore.from_settings(settings),
        neo4j=Neo4jClient.from_settings(settings),
        settings=settings,
        # The extraction pass's LLM gateway (LOCAL/Ollama by default). Sharing ``sessions`` wires
        # the durable-egress sink so an EXTERNAL mode with durable audit on cannot silently egress.
        llm_gateway=LLMGateway(settings, session_factory=sessions),
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
