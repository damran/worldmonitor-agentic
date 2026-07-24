"""The driver's on-scrape Prometheus collector (Gate H-8c / ADR 0076).

A ``prometheus_client`` custom collector that computes every ``worldmonitor_`` gauge **at scrape
time** (so the numbers stay fresh even if the asyncio loop is wedged — the metrics thread is
independent). It is **read-only**: it reads the driver's session factory + Neo4j client + a zero-arg
accessor onto the live in-memory ``_consecutive_resolve_skips`` (ADR 0075 D3) and performs NO write
to Postgres/Neo4j and never closes the injected Neo4j client (the driver owns it) — mirroring the
``smoke_metrics`` read-only contract. It exposes ONLY integer counts/labels — never entity names,
raw rows, or person fields (INV-7).

The DB/graph-derived gauges reuse :func:`worldmonitor.runner.smoke_metrics.collect_snapshot`, the
shared counting both the CLI snapshot and this collector call, so the scrape surface and the CLI
snapshot cannot drift (INV-5 parity).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.models import ConnectorInstance, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.observability.freshness import compute_instance_freshness
from worldmonitor.runner.smoke_metrics import collect_snapshot

if TYPE_CHECKING:
    from worldmonitor.resolution.divergence import ProjectionDivergence
    from worldmonitor.runner.gc import GcStats

# The worldmonitor_task_runs{kind,status} cross-product: every series is emitted on each scrape,
# including the zero combinations, so a gap reads as 0 rather than a missing time-series.
_TASK_KINDS = ("ingest", "resolve")
_TASK_STATUSES = ("ok", "error", "running")
# The CLOSED set of resolve stopped_reason label values (ADR 0075 D2; the only values
# ``ResolveStats.stopped_reason`` is ever set to). Any other / missing value collapses to
# ``_UNKNOWN_REASON`` so the label can never carry an unbounded or hostile string — closed
# cardinality, defense-in-depth even though the only writer today is our own ResolveStats.
_RESOLVE_STOPPED_REASONS = ("exhausted", "timeout")
_UNKNOWN_REASON = "unknown"

# Gate F-1 slice 1 (ADR 0123 D4) — the SAME defaults as ``Settings.freshness_stale_after_seconds``
# / ``freshness_very_stale_after_seconds``, so a caller (e.g. the pre-existing unit tests) that
# constructs the collector without the new keywords keeps working unmodified. The live driver
# never relies on these — it always passes the settings values explicitly (``driver.py``).
_DEFAULT_STALE_AFTER_SECONDS = 14400
_DEFAULT_VERY_STALE_AFTER_SECONDS = 86400


def _gauge(name: str, documentation: str, value: float) -> GaugeMetricFamily:
    """A single-sample (unlabelled) gauge family."""
    return GaugeMetricFamily(name, documentation, value=value)


class DriverMetricsCollector:
    """Read-only, on-scrape Prometheus collector over the driver's stores + live counters."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        neo4j: Neo4jClient,
        skip_counter: Callable[[], int],
        gc_stats: Callable[[], GcStats | None] | None = None,
        projection_divergence: Callable[[], ProjectionDivergence | None] | None = None,
        projection_diff_refusals: Callable[[], int] | None = None,
        stale_after_seconds: int = _DEFAULT_STALE_AFTER_SECONDS,
        very_stale_after_seconds: int = _DEFAULT_VERY_STALE_AFTER_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._neo4j = neo4j
        self._skip_counter = skip_counter
        # Zero-arg accessor onto the driver's cached GcStats (ADR 0083 / M-6).
        # ``None`` when the GC has never run or the collector is created without a GC
        # accessor (e.g. in tests). Exposes the disk-growth signal (orphan count + bytes)
        # WITHOUT performing an expensive bucket list on every scrape.
        self._gc_stats = gc_stats
        # Zero-arg accessor onto the driver's cached ProjectionDivergence (ADR 0102 / Gate
        # 3a-ii-B). ``None`` when the projection-diff guard has never run (dormant/disabled) or
        # the collector is created without this accessor (e.g. the existing collector tests) —
        # the gauge reports the ``-1`` sentinel in that case so the alert's ``> 0`` never fires.
        self._projection_divergence = projection_divergence
        # Zero-arg accessor onto the driver's cumulative ProjectionDiffMisconfiguredError
        # refusal counter (Gate 3b LOW-2, ADR 0114 D-7) — same pattern as ``skip_counter``.
        # ``None`` (the existing construction shape) reports 0.
        self._projection_diff_refusals = projection_diff_refusals
        # Gate F-1 slice 1 (ADR 0123 D4) — the derived freshness gauge's budgets.
        self._stale_after_seconds = stale_after_seconds
        self._very_stale_after_seconds = very_stale_after_seconds

    def collect(self) -> Iterator[GaugeMetricFamily]:
        """Yield every ``worldmonitor_`` gauge family, computed fresh from the stores."""
        with self._session_factory() as session:
            snap = collect_snapshot(session, self._neo4j)
            instances_in_error = session.execute(
                select(func.count())
                .select_from(ConnectorInstance)
                .where(ConnectorInstance.status == "error")
            ).scalar_one()
            stopped_reason = self._latest_stopped_reason(session)
            last_success_rows = session.execute(
                select(
                    ConnectorInstance.id,
                    ConnectorInstance.connector_id,
                    func.max(TaskRun.finished_at),
                )
                .join(
                    TaskRun,
                    (TaskRun.connector_instance_id == ConnectorInstance.id)
                    & (TaskRun.kind == "ingest")
                    & (TaskRun.status == "ok"),
                    isouter=True,
                )
                .group_by(ConnectorInstance.id, ConnectorInstance.connector_id)
            ).all()
            # Gate F-1 slice 1 (ADR 0123): the ONE shared derivation both this gauge and
            # ``GET /sources/freshness`` consume (AC-2) — computed inside the same session/scrape
            # so it reflects the exact same instant as every other gauge in this collect().
            freshness_rows = compute_instance_freshness(
                session,
                now=datetime.now(UTC),
                stale_after_seconds=self._stale_after_seconds,
                very_stale_after_seconds=self._very_stale_after_seconds,
            )
        # Read the live in-memory driver counter via the injected accessor (no driver mutation).
        skips = self._skip_counter()

        # DB-derived gauges (reuse of the smoke_metrics queries via collect_snapshot — INV-5).
        yield _gauge(
            "worldmonitor_er_queue_pending",
            "ER-queue candidates awaiting resolution (status='pending').",
            snap["queue_pending"],
        )
        yield _gauge(
            "worldmonitor_er_queue_pending_review",
            "ER-queue candidates parked for human review (status='pending_review').",
            snap["queue_pending_review"],
        )
        yield _gauge(
            "worldmonitor_parked_merges",
            "Merges parked in pending_review by the catastrophic-merge guard.",
            snap["parked_merges"],
        )
        yield _gauge(
            "worldmonitor_dead_letters",
            "Quarantined ingest/resolution dead-letter rows.",
            snap["dead_letter"],
        )

        task_runs = GaugeMetricFamily(
            "worldmonitor_task_runs",
            "Driver task_run rows by kind and status.",
            labels=["kind", "status"],
        )
        for kind in _TASK_KINDS:
            for status in _TASK_STATUSES:
                task_runs.add_metric([kind, status], snap[f"task_{kind}_{status}"])
        yield task_runs

        yield _gauge(
            "worldmonitor_graph_nodes",
            "Entity nodes in the graph.",
            snap["graph_nodes"],
        )
        yield _gauge(
            "worldmonitor_graph_edges",
            "Edges in the graph.",
            snap["graph_edges"],
        )

        # New H-8a/H-8b headline gauges.
        yield _gauge(
            "worldmonitor_instances_in_error",
            "Connector instances hard-disabled to status='error' (ADR 0074).",
            instances_in_error,
        )
        yield _gauge(
            "worldmonitor_resolve_consecutive_lock_skips",
            "Consecutive non-blocking resolve-lock skips on the live driver (ADR 0075 D3).",
            skips,
        )

        # Per-instance freshness (re-review 2026-07-11 #9 / OG-harvest F-1 slice 1): the Unix
        # timestamp of each connector instance's latest SUCCESSFUL ingest, from task_run
        # (kind='ingest', status='ok') — NOT ConnectorInstance.last_run, which stamps every
        # attempt and would make a forever-failing feed look fresh. 0 = no success within the
        # task_run retention window (task_run_retention_days). Labels are opaque ids only
        # (connector_id + the server-minted instance uuid) — never a URL/name/person field
        # (INV-7); cardinality is bounded by the instance count.
        last_success = GaugeMetricFamily(
            "worldmonitor_connector_last_success_timestamp",
            "Unix timestamp of the latest successful ingest per connector instance; "
            "0 = never succeeded within the task_run retention window.",
            labels=["connector_id", "instance"],
        )
        for instance_id, connector_id, finished_at in last_success_rows:
            last_success.add_metric(
                [connector_id, instance_id],
                finished_at.timestamp() if finished_at is not None else 0,
            )
        yield last_success

        # The derived 6-state freshness gauge (Gate F-1 slice 1, ADR 0123 D3): ONE active series
        # per instance, value 1, ``state`` drawn from the CLOSED FRESHNESS_STATES alphabet (the
        # totality of ``freshness_status`` guarantees this — mirrors the
        # ``worldmonitor_resolve_last_stopped_reason`` single-active-series idiom). Emitted
        # ALONGSIDE ``worldmonitor_connector_last_success_timestamp`` above — not a replacement.
        freshness = GaugeMetricFamily(
            "worldmonitor_connector_freshness",
            "Derived source-freshness state per connector instance (ADR 0123): one active "
            "series per instance, value 1, state in {fresh,stale,very_stale,no_data,error,"
            "disabled}.",
            labels=["connector_id", "instance", "state"],
        )
        for row in freshness_rows:
            freshness.add_metric([row.connector_id, row.instance_id, row.freshness_status], 1)
        yield freshness

        last_reason = GaugeMetricFamily(
            "worldmonitor_resolve_last_stopped_reason",
            "The stopped_reason of the latest finished resolve pass, set to 1 (ADR 0075 D2).",
            labels=["reason"],
        )
        last_reason.add_metric([stopped_reason], 1)
        yield last_reason

        # Landing-zone GC gauges (ADR 0083 / audit M-6): expose the disk-growth signal from the
        # CACHED GcStats of the latest periodic pass — no on-scrape bucket list.
        # All three report 0 until the first GC pass runs (landing_gc_enabled=False by default).
        gc = self._gc_stats() if self._gc_stats is not None else None
        yield _gauge(
            "worldmonitor_landing_objects",
            "Total landing-zone objects scanned in the latest GC pass (ADR 0083 / M-6). "
            "0 until the first GC pass runs (landing_gc_enabled=False by default).",
            gc.scanned if gc is not None else 0,
        )
        yield _gauge(
            "worldmonitor_landing_orphans",
            "Unreferenced landing-zone orphans found in the latest GC pass (ADR 0083 / M-6). "
            "0 until the first GC pass runs.",
            gc.orphaned if gc is not None else 0,
        )
        yield _gauge(
            "worldmonitor_landing_orphan_bytes",
            "Total bytes of unreferenced landing-zone orphans (disk-growth signal, ADR 0083). "
            "Computed even in report-only mode; 0 until the first GC pass runs.",
            gc.orphan_bytes if gc is not None else 0,
        )

        # Projection rebuild-and-diff guard gauges (ADR 0102 / Gate 3a-ii-B): the CACHED
        # ProjectionDivergence from the latest scheduled full-fold divergence measure — no
        # on-scrape Neo4j fold. -1 sentinel = never-run/disabled (dormant guard), so the
        # ProjectionDivergenceHigh alert's `> 0` expr never fires while dormant.
        div = self._projection_divergence() if self._projection_divergence is not None else None
        yield _gauge(
            "worldmonitor_projection_divergence",
            "Live-graph elements the whole-log fold cannot explain (ADR 0102). "
            "-1 = the projection-diff guard has never run / is disabled (never fires the alert).",
            div.total if div is not None else -1,
        )
        yield _gauge(
            "worldmonitor_projection_divergence_last_run_timestamp",
            "Unix timestamp of the latest projection-diff guard run (ADR 0102); "
            "0 = never run. A liveness signal so a stuck divergence value is detectable.",
            div.computed_at.timestamp() if div is not None else 0,
        )
        yield _gauge(
            "worldmonitor_projection_diff_refusals",
            "Cumulative ProjectionDiffMisconfiguredError refusals since driver start "
            "(Gate 3b LOW-2 / ADR 0114 D-7): the guard declined to wipe a target it could not "
            "prove distinct from the live graph — distinct from a generic diff failure. "
            "0 when the accessor is absent.",
            self._projection_diff_refusals() if self._projection_diff_refusals is not None else 0,
        )

    def _latest_stopped_reason(self, session: Session) -> str:
        """The ``stopped_reason`` of the latest FINISHED resolve task_run, else ``"unknown"``.

        Reads ``task_run.stats`` as a Python ``dict`` (``stats.get("stopped_reason")``) — NOT a
        Postgres-only ``->>`` operator — so the same path runs on SQLite (the unit suite) and
        Postgres. The most-recent ``kind='resolve'`` row with ``status in (ok, error)`` wins
        (a still-running resolve is excluded, INV-4); ``"unknown"`` when there is no finished row,
        its ``stats`` is None, it lacks the key, or the value is outside the closed
        ``_RESOLVE_STOPPED_REASONS`` set (so the label is always closed-cardinality, INV-7).
        """
        stats = (
            session.execute(
                select(TaskRun.stats)
                .where(TaskRun.kind == "resolve", TaskRun.status.in_(("ok", "error")))
                .order_by(TaskRun.started_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if not isinstance(stats, dict):
            return _UNKNOWN_REASON
        reason = stats.get("stopped_reason")
        return reason if reason in _RESOLVE_STOPPED_REASONS else _UNKNOWN_REASON
