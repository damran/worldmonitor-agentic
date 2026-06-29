"""Smoke-run metrics snapshot (WS2 harness — see docs/runbooks/smoke-run.md).

Prints a single one-line snapshot of a running ingest driver so a sustained, real-data
smoke run can be watched over time: queue backlog, task_run outcomes, dead-letter
counts, block-mode parking (parked merges), and graph size. Pair it with the OS process
RSS to watch for memory growth.

    python -m worldmonitor.runner.smoke_metrics              # one snapshot
    watch -n 30 python -m worldmonitor.runner.smoke_metrics  # every 30s

Read-only: it never writes to Postgres or the graph.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.db.models import ErQueueItem, IngestDeadLetter, MergeAudit, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.settings import get_settings

logger = logging.getLogger(__name__)


def _count(session: Session, model: type[Any], *conditions: ColumnElement[bool]) -> int:
    stmt = select(func.count()).select_from(model)
    for condition in conditions:
        stmt = stmt.where(condition)
    return session.execute(stmt).scalar_one()


def collect_snapshot(session: Session, neo4j: Neo4jClient) -> dict[str, int]:
    """The shared read-only health counts across Postgres + the graph.

    Single source of truth for the DB/graph-derived metrics, consumed by BOTH :func:`snapshot`
    (the CLI one-liner) and the H-8c driver Prometheus collector
    (:class:`worldmonitor.metrics.collector.DriverMetricsCollector`), so the scrape surface and the
    CLI snapshot cannot drift (ADR 0076 INV-5). Takes an already-open ``session`` + an injected
    ``neo4j`` and only READS — the caller owns both clients' lifecycles (this never disposes the
    engine or closes the Neo4j client). Keys are the existing, frozen ``smoke_metrics`` names.
    """
    metrics: dict[str, int] = {}
    metrics["queue_pending"] = _count(session, ErQueueItem, ErQueueItem.status == "pending")
    metrics["queue_pending_review"] = _count(
        session, ErQueueItem, ErQueueItem.status == "pending_review"
    )
    metrics["parked_merges"] = _count(session, MergeAudit, MergeAudit.decision == "pending_review")
    metrics["dead_letter"] = _count(session, IngestDeadLetter)
    for kind in ("ingest", "resolve"):
        for status in ("ok", "error", "running"):
            metrics[f"task_{kind}_{status}"] = _count(
                session, TaskRun, TaskRun.kind == kind, TaskRun.status == status
            )
    metrics["graph_nodes"] = neo4j.execute_read("MATCH (n:Entity) RETURN count(n) AS n")[0]["n"]
    metrics["graph_edges"] = neo4j.execute_read("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
    return metrics


def snapshot() -> dict[str, int]:
    """One read-only metrics snapshot across Postgres + the graph.

    Builds its own engine + Neo4j client from the process settings, delegates the counting to
    :func:`collect_snapshot`, and tears both clients down. Behaviour-preserving: the returned dict
    + key set are unchanged (ADR 0076 reuses the queries via ``collect_snapshot`` rather than
    forking them).
    """
    settings = get_settings()
    engine = engine_from_settings(settings)
    sessions = session_factory(engine)
    neo4j = Neo4jClient.from_settings(settings)
    try:
        with sessions() as session:
            return collect_snapshot(session, neo4j)
    finally:
        neo4j.close()
        engine.dispose()


def main() -> int:  # pragma: no cover - process entry point
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    metrics = snapshot()
    logger.info("smoke-metrics  %s", "  ".join(f"{key}={value}" for key, value in metrics.items()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
