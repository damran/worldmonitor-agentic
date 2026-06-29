"""Unit tests for the driver Prometheus collector (ADR 0076, gate H-8c).

The on-scrape collector (``worldmonitor.metrics.collector.DriverMetricsCollector``) computes
every ``worldmonitor_`` gauge at scrape time, **read-only**, from the driver's session factory +
Neo4j client + a zero-arg accessor onto the live ``_consecutive_resolve_skips`` (ADR 0075 D3).

These run Docker-free: a real in-memory SQLite session (the relational COUNT queries are
portable) + a stub Neo4j client (canned node/edge counts; ``execute_write`` is fatal). The
``stopped_reason`` value is read from ``TaskRun.stats`` the portable way (a Python ``dict``
lookup, not a Postgres-only ``->>`` SQL operator) — the integration suite re-checks it on real
Postgres.

RED until the gate lands: the ``worldmonitor.metrics`` package + its ``prometheus_client``
dependency do not exist yet, so importing the collector fails at collection time (the right RED
reason). Constructor contract these tests pin (for the builder):

    DriverMetricsCollector(session_factory=<sessionmaker>, neo4j=<Neo4jClient>,
                           skip_counter=<Callable[[], int]>)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.db.models import (
    Base,
    ConnectorInstance,
    ErQueueItem,
    IngestDeadLetter,
    MergeAudit,
    TaskRun,
)

# RED import: the collector module (and its prometheus_client dependency) do not exist yet.
from worldmonitor.metrics.collector import DriverMetricsCollector


# Test-only dialect shim: the ORM models use Postgres ``JSONB``; render it as SQLite ``JSON`` so
# the portable COUNT/ORM queries can run against an in-memory DB without Docker. Keyed to the
# "sqlite" dialect only — the real Postgres path (the integration suite) is untouched.
@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


_T = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)

# Every gauge name the collector must yield (ADR 0076 §2.3 / spec §2.3), all ``worldmonitor_``.
_EXPECTED_NAMES = {
    "worldmonitor_er_queue_pending",
    "worldmonitor_er_queue_pending_review",
    "worldmonitor_parked_merges",
    "worldmonitor_dead_letters",
    "worldmonitor_task_runs",
    "worldmonitor_graph_nodes",
    "worldmonitor_graph_edges",
    "worldmonitor_instances_in_error",
    "worldmonitor_resolve_consecutive_lock_skips",
    "worldmonitor_resolve_last_stopped_reason",
}


class _StubNeo4j:
    """A read-only Neo4j stand-in: canned counts; any write is fatal, close is observable."""

    def __init__(self, *, nodes: int, edges: int) -> None:
        self._nodes = nodes
        self._edges = edges
        self.closed = False

    def execute_read(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        is_edges = "[r]" in query or "->()" in query or "count(r)" in query
        return [{"n": self._edges if is_edges else self._nodes}]

    def execute_write(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        raise AssertionError("collector must be read-only — execute_write must never be called")

    def close(self) -> None:
        # The driver owns the client; the collector must never close an injected client.
        self.closed = True


def _sqlite_sessions() -> sessionmaker[Session]:
    """A real session factory over a single shared in-memory SQLite DB (Docker-free)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _seed_full(sessions: sessionmaker[Session]) -> None:
    """A representative DB state with a known, non-trivial value for every gauge."""
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-error", connector_id="c", config_encrypted="x", status="error"
                ),
                ConnectorInstance(
                    id="ci-enabled", connector_id="c", config_encrypted="x", status="enabled"
                ),
                ErQueueItem(
                    id="q1",
                    connector_id="c",
                    raw_entity={"id": "q1"},
                    source_record="s3://l/q1",
                    status="pending",
                ),
                ErQueueItem(
                    id="q2",
                    connector_id="c",
                    raw_entity={"id": "q2"},
                    source_record="s3://l/q2",
                    status="pending",
                ),
                ErQueueItem(
                    id="q3",
                    connector_id="c",
                    raw_entity={"id": "q3"},
                    source_record="s3://l/q3",
                    status="pending_review",
                ),
                MergeAudit(
                    id="m1",
                    canonical_id="cid-1",
                    source_ids=["a", "b"],
                    score=0.9,
                    decision="pending_review",
                ),
                MergeAudit(
                    id="m2", canonical_id="cid-2", source_ids=["c"], score=0.99, decision="merged"
                ),
                IngestDeadLetter(
                    id="d1", connector_id="c", source_key="k1", stage="map", error="x"
                ),
                TaskRun(id="t-i-ok", kind="ingest", status="ok", started_at=_T),
                TaskRun(id="t-i-err", kind="ingest", status="error", started_at=_T),
                # Two FINISHED resolves (exhausted earlier, timeout later) + one RUNNING resolve
                # that the latest-finished selection must exclude (INV-4).
                TaskRun(
                    id="t-r-old",
                    kind="resolve",
                    status="ok",
                    stats={"stopped_reason": "exhausted"},
                    started_at=_T - timedelta(seconds=100),
                ),
                TaskRun(
                    id="t-r-new",
                    kind="resolve",
                    status="ok",
                    stats={"stopped_reason": "timeout"},
                    started_at=_T - timedelta(seconds=10),
                ),
                TaskRun(id="t-r-run", kind="resolve", status="running", started_at=_T),
            ]
        )
        session.commit()


def _collect(
    collector: DriverMetricsCollector,
) -> tuple[set[str], dict[tuple[str, frozenset[tuple[str, str]]], float]]:
    """Flatten one ``collect()`` scrape into (family-names, {(name, labels) -> value})."""
    names: set[str] = set()
    values: dict[tuple[str, frozenset[tuple[str, str]]], float] = {}
    for family in collector.collect():
        names.add(family.name)
        for sample in family.samples:
            values[(sample.name, frozenset(sample.labels.items()))] = sample.value
    return names, values


def test_collector_yields_every_metric_with_seeded_values() -> None:
    """INV-1 (complete) + INV-2 + INV-3 + INV-4: one scrape yields every declared gauge with the
    seeded value, the in-memory skip counter verbatim, and the latest finished resolve reason."""
    sessions = _sqlite_sessions()
    _seed_full(sessions)
    neo4j = _StubNeo4j(nodes=7, edges=3)
    collector = DriverMetricsCollector(
        session_factory=sessions, neo4j=neo4j, skip_counter=lambda: 7
    )

    names, values = _collect(collector)

    assert names >= _EXPECTED_NAMES, f"missing metric families: {_EXPECTED_NAMES - names}"

    def g(name: str, **labels: str) -> float:
        return values[(name, frozenset(labels.items()))]

    # INV-2: COUNT(ConnectorInstance status=='error') — the enabled instance is filtered out.
    assert g("worldmonitor_instances_in_error") == 1.0
    # DB-derived reuse of the smoke_metrics queries.
    assert g("worldmonitor_er_queue_pending") == 2.0
    assert g("worldmonitor_er_queue_pending_review") == 1.0
    assert g("worldmonitor_parked_merges") == 1.0  # only decision=='pending_review'
    assert g("worldmonitor_dead_letters") == 1.0
    assert g("worldmonitor_graph_nodes") == 7.0
    assert g("worldmonitor_graph_edges") == 3.0

    # INV-3: the live driver counter, surfaced verbatim through the zero-arg accessor.
    assert g("worldmonitor_resolve_consecutive_lock_skips") == 7.0

    # task_runs cross-product — all six kind x status series, including the zero combinations.
    assert g("worldmonitor_task_runs", kind="ingest", status="ok") == 1.0
    assert g("worldmonitor_task_runs", kind="ingest", status="error") == 1.0
    assert g("worldmonitor_task_runs", kind="ingest", status="running") == 0.0
    assert g("worldmonitor_task_runs", kind="resolve", status="ok") == 2.0
    assert g("worldmonitor_task_runs", kind="resolve", status="error") == 0.0
    assert g("worldmonitor_task_runs", kind="resolve", status="running") == 1.0

    # INV-4: the latest FINISHED resolve's reason wins; the running row is excluded; exactly one
    # series is emitted (the current reason set to 1).
    assert g("worldmonitor_resolve_last_stopped_reason", reason="timeout") == 1.0
    reason_series = {
        labels for (name, labels) in values if name == "worldmonitor_resolve_last_stopped_reason"
    }
    assert reason_series == {frozenset({("reason", "timeout")})}, (
        "exactly one resolve_last_stopped_reason series, on the latest finished reason"
    )


def test_resolve_last_stopped_reason_unknown_when_no_finished_resolve() -> None:
    """INV-4 default: with no FINISHED resolve row (only a running resolve + an ingest row),
    ``resolve_last_stopped_reason`` reports ``reason="unknown"`` set to 1, and nothing else."""
    sessions = _sqlite_sessions()
    with sessions() as session:
        session.add(TaskRun(id="r-run", kind="resolve", status="running", started_at=_T))
        session.add(TaskRun(id="i-ok", kind="ingest", status="ok", started_at=_T))
        session.commit()
    collector = DriverMetricsCollector(
        session_factory=sessions, neo4j=_StubNeo4j(nodes=0, edges=0), skip_counter=lambda: 0
    )

    _, values = _collect(collector)

    key = ("worldmonitor_resolve_last_stopped_reason", frozenset({("reason", "unknown")}))
    assert values[key] == 1.0
    reason_series = {
        labels for (name, labels) in values if name == "worldmonitor_resolve_last_stopped_reason"
    }
    assert reason_series == {frozenset({("reason", "unknown")})}


def test_collector_performs_no_writes() -> None:
    """INV-1 (read-only): scraping twice mutates neither Postgres (row counts invariant) nor
    Neo4j (the stub's execute_write is fatal), and never closes the injected Neo4j client."""
    sessions = _sqlite_sessions()
    _seed_full(sessions)
    neo4j = _StubNeo4j(nodes=2, edges=1)
    collector = DriverMetricsCollector(
        session_factory=sessions, neo4j=neo4j, skip_counter=lambda: 5
    )

    def _row_counts() -> dict[str, int]:
        with sessions() as session:
            return {
                "ci": session.execute(
                    select(func.count()).select_from(ConnectorInstance)
                ).scalar_one(),
                "eq": session.execute(select(func.count()).select_from(ErQueueItem)).scalar_one(),
                "ma": session.execute(select(func.count()).select_from(MergeAudit)).scalar_one(),
                "dl": session.execute(
                    select(func.count()).select_from(IngestDeadLetter)
                ).scalar_one(),
                "tr": session.execute(select(func.count()).select_from(TaskRun)).scalar_one(),
            }

    before = _row_counts()
    # On-scrape collection must be idempotent + side-effect-free: scrape twice.
    _collect(collector)
    _collect(collector)
    after = _row_counts()

    assert after == before, "the collector must not write to Postgres (read-only contract)"
    assert neo4j.closed is False, "the collector must not close the injected Neo4j client"
