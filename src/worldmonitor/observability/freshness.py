"""The derived 6-state source-freshness machine + its ONE shared query (Gate F-1, ADR 0123).

Spec: ``docs/reviews/GATE_F1_FRESHNESS_SURFACE_SPEC.md`` §2/§2.1. Two things live here:

1. :func:`freshness_status` — a pure, total, deterministic function mapping
   ``(status, last_success, now, budgets) -> FreshnessState``. Priority order (first match
   wins): ``disabled`` (status) > ``error`` (status) > ``no_data`` (active + never succeeded) >
   ``very_stale`` (``age >= very_stale_after``) > ``stale`` (``age >= stale_after``) > ``fresh``.
   Any status other than the literal strings ``"disabled"``/``"error"`` is treated as ACTIVE
   (defense-in-depth over hostile/unexpected input — the function never raises and never returns
   outside the closed :data:`FRESHNESS_STATES` alphabet).
2. :func:`compute_instance_freshness` — the ONE query+derivation point both the REST route
   (``worldmonitor.api.freshness``) and the driver's Prometheus collector
   (``worldmonitor.metrics.collector``) call, so the two surfaces cannot report different states
   for the same instance (the ``collect_snapshot`` / ADR 0076 INV-5 shared-helper idiom). It reads
   the SAME last-success predicate as the existing ``worldmonitor_connector_last_success_timestamp``
   gauge (``task_run`` where ``kind='ingest' AND status='ok'``) — deliberately NOT
   ``ConnectorInstance.last_run``, which stamps every *attempt* and would make a forever-failing
   feed read ``fresh`` (ADR 0123 D2 / Alternative A5).

Read-only: both functions only ``SELECT``; no write, no new table, no migration. All datetimes are
tz-aware UTC; a naive datetime handed back by SQLite (the unit-test session factory) is interpreted
as UTC, never the process's local timezone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ConnectorInstance, TaskRun

FreshnessState = Literal["fresh", "stale", "very_stale", "no_data", "error", "disabled"]

# The closed 6-set — single source of truth for the label alphabet (Prometheus + REST both draw
# from this, mirroring the collector's ``_RESOLVE_STOPPED_REASONS`` closed-cardinality discipline).
FRESHNESS_STATES: tuple[str, ...] = (
    "fresh",
    "stale",
    "very_stale",
    "no_data",
    "error",
    "disabled",
)


def freshness_status(
    *,
    status: str,
    last_success: datetime | None,
    now: datetime,
    stale_after_seconds: int,
    very_stale_after_seconds: int,
) -> FreshnessState:
    """The pure, total, deterministic 6-state derivation (spec §2 — the load-bearing truth table).

    Status matching is an EXACT literal comparison (``status == "disabled"`` / ``status ==
    "error"``) — no case-folding, no whitespace-trimming. Any other status string (including
    ``"enabled"``, ``"running"``, an empty string, or hostile/garbled input) falls into the
    active branch. ``age = (now - last_success).total_seconds()``; the boundary is
    ``age < stale_after_seconds`` -> ``fresh``, ``stale_after_seconds <= age <
    very_stale_after_seconds`` -> ``stale``, ``age >= very_stale_after_seconds`` -> ``very_stale``.
    """
    if status == "disabled":
        return "disabled"
    if status == "error":
        return "error"
    if last_success is None:
        return "no_data"
    age_seconds = (now - last_success).total_seconds()
    if age_seconds >= very_stale_after_seconds:
        return "very_stale"
    if age_seconds >= stale_after_seconds:
        return "stale"
    return "fresh"


@dataclass(frozen=True)
class InstanceFreshness:
    """One connector instance's derived freshness (spec §2.1) — read-only, opaque ids only."""

    instance_id: str
    connector_id: str
    status: str
    freshness_status: FreshnessState
    last_run: datetime | None
    last_success_at: datetime | None
    age_seconds: float | None


def _as_utc(value: datetime | None) -> datetime | None:
    """Interpret a naive datetime (SQLite hands one back for ``DateTime(timezone=True)``) as UTC.

    An already-aware datetime (the real Postgres path) is returned unchanged. This is the ONLY
    place naive-vs-aware ambiguity is resolved, so it can never silently shift by the host's local
    UTC offset (spec §9 item 2 / the ``TZ=America/New_York`` regression test).
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def compute_instance_freshness(
    session: Session,
    *,
    now: datetime,
    stale_after_seconds: int,
    very_stale_after_seconds: int,
) -> list[InstanceFreshness]:
    """The ONE shared query+derivation (spec §2.1/AC-2): one row per ``ConnectorInstance``.

    Left-joins the max ``task_run.finished_at`` where ``kind='ingest' AND status='ok'`` — the
    SAME predicate the existing ``worldmonitor_connector_last_success_timestamp`` gauge uses —
    then applies :func:`freshness_status`. Both the REST route and the collector gauge are thin
    consumers of this function; neither re-derives state.
    """
    rows = session.execute(
        select(
            ConnectorInstance.id,
            ConnectorInstance.connector_id,
            ConnectorInstance.status,
            ConnectorInstance.last_run,
            func.max(TaskRun.finished_at),
        )
        .join(
            TaskRun,
            (TaskRun.connector_instance_id == ConnectorInstance.id)
            & (TaskRun.kind == "ingest")
            & (TaskRun.status == "ok"),
            isouter=True,
        )
        .group_by(
            ConnectorInstance.id,
            ConnectorInstance.connector_id,
            ConnectorInstance.status,
            ConnectorInstance.last_run,
        )
    ).all()

    results: list[InstanceFreshness] = []
    for instance_id, connector_id, status, last_run, last_success_raw in rows:
        last_success_at = _as_utc(last_success_raw)
        age_seconds = (
            (now - last_success_at).total_seconds() if last_success_at is not None else None
        )
        state = freshness_status(
            status=status,
            last_success=last_success_at,
            now=now,
            stale_after_seconds=stale_after_seconds,
            very_stale_after_seconds=very_stale_after_seconds,
        )
        results.append(
            InstanceFreshness(
                instance_id=instance_id,
                connector_id=connector_id,
                status=status,
                freshness_status=state,
                last_run=_as_utc(last_run),
                last_success_at=last_success_at,
                age_seconds=age_seconds,
            )
        )
    return results
