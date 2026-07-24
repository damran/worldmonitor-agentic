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

Gate F-1 slice 1 additions (ADR 0123, bottom of file): the derived
``worldmonitor_connector_freshness{connector_id, instance, state}`` gauge, sourced from the new
``observability.freshness.compute_instance_freshness`` shared helper. These tests construct the
collector with the two NEW keyword-only budgets (``stale_after_seconds`` /
``very_stale_after_seconds``) that the constructor must gain (spec §4.1) — a RED
``TypeError: unexpected keyword argument`` until the builder adds them (with defaults, so the
EXISTING calls above that omit them keep working unmodified).
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

# RED import (Gate F-1 slice 1): the observability package does not exist yet.
from worldmonitor.observability.freshness import FRESHNESS_STATES


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
    "worldmonitor_connector_last_success_timestamp",
}

# Gate F-1 slice 1 defaults (ADR 0123 D4) — used to construct the collector explicitly in the
# freshness gauge tests below so they do not depend on whatever default the builder wires in.
_STALE_AFTER = 14400
_VERY_STALE_AFTER = 86400


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


def test_connector_last_success_timestamp_per_instance() -> None:
    """Freshness gauge (re-review 2026-07-11 #9): the LATEST successful ingest per instance,
    from task_run — an error-only history reports 0 (ConnectorInstance.last_run stamps every
    attempt and would make a forever-failing feed look fresh)."""
    sessions = _sqlite_sessions()
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-a", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-b", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                TaskRun(
                    id="t-old",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-a",
                    started_at=_T - timedelta(hours=2),
                    finished_at=_T - timedelta(hours=2),
                ),
                TaskRun(
                    id="t-new",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-a",
                    started_at=_T,
                    finished_at=_T,
                ),
                TaskRun(
                    id="t-fail",
                    kind="ingest",
                    status="error",
                    connector_instance_id="ci-b",
                    started_at=_T,
                    finished_at=_T,
                ),
            ]
        )
        session.commit()

    collector = DriverMetricsCollector(
        session_factory=sessions, neo4j=_StubNeo4j(nodes=0, edges=0), skip_counter=lambda: 0
    )
    _names, values = _collect(collector)

    def g(**labels: str) -> float:
        key = ("worldmonitor_connector_last_success_timestamp", frozenset(labels.items()))
        return values[key]

    # Compare against the SAME read path (SQLite hands back naive datetimes; Postgres aware —
    # the integration suite covers the aware path).
    with sessions() as session:
        newer = session.execute(
            select(TaskRun.finished_at).where(TaskRun.id == "t-new")
        ).scalar_one()
        older = session.execute(
            select(TaskRun.finished_at).where(TaskRun.id == "t-old")
        ).scalar_one()
    assert newer is not None and older is not None
    assert g(connector_id="feeds", instance="ci-a") == newer.timestamp()
    assert newer.timestamp() > older.timestamp(), "MAX must have picked the newer run"
    assert g(connector_id="feeds", instance="ci-b") == 0, "error-only history reports 0"


# ================================================================================================
# Gate F-1 slice 1 (ADR 0123) — the derived worldmonitor_connector_freshness{state} gauge.
# ================================================================================================


def test_connector_freshness_gauge_emitted() -> None:
    """AC-3 / spec §6.3: the derived state gauge is emitted per instance, closed 6-state
    alphabet, value 1 for the active series. RED: DriverMetricsCollector does not yet accept
    ``stale_after_seconds``/``very_stale_after_seconds`` (TypeError), and the gauge does not
    exist yet either way."""
    sessions = _sqlite_sessions()
    now_ref = datetime.now(UTC)
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-fresh", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                TaskRun(
                    id="t-fresh",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-fresh",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(minutes=2),
                ),
            ]
        )
        session.commit()

    collector = DriverMetricsCollector(
        session_factory=sessions,
        neo4j=_StubNeo4j(nodes=0, edges=0),
        skip_counter=lambda: 0,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    names, values = _collect(collector)

    assert "worldmonitor_connector_freshness" in names

    key = (
        "worldmonitor_connector_freshness",
        frozenset({("connector_id", "feeds"), ("instance", "ci-fresh"), ("state", "fresh")}),
    )
    assert values[key] == 1.0


def test_connector_freshness_gauge_closed_cardinality() -> None:
    """An instance in EACH of the 6 states produces exactly the expected {instance: state}
    labels — one active series per instance, no unbounded/extra label combination (mirrors the
    ``_RESOLVE_STOPPED_REASONS`` closed-cardinality discipline)."""
    sessions = _sqlite_sessions()
    now_ref = datetime.now(UTC)
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-disabled", connector_id="a", config_encrypted="x", status="disabled"
                ),
                ConnectorInstance(
                    id="ci-error", connector_id="b", config_encrypted="x", status="error"
                ),
                ConnectorInstance(
                    id="ci-no-data", connector_id="c", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-fresh", connector_id="d", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-stale", connector_id="e", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-very-stale", connector_id="f", config_encrypted="x", status="enabled"
                ),
                TaskRun(
                    id="t-fresh",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-fresh",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(minutes=2),
                ),
                TaskRun(
                    id="t-stale",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-stale",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(hours=6),
                ),
                TaskRun(
                    id="t-very-stale",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-very-stale",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(hours=30),
                ),
            ]
        )
        session.commit()

    collector = DriverMetricsCollector(
        session_factory=sessions,
        neo4j=_StubNeo4j(nodes=0, edges=0),
        skip_counter=lambda: 0,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    _, values = _collect(collector)

    expected = {
        "ci-disabled": "disabled",
        "ci-error": "error",
        "ci-no-data": "no_data",
        "ci-fresh": "fresh",
        "ci-stale": "stale",
        "ci-very-stale": "very_stale",
    }
    freshness_samples = {
        labels: value
        for (name, labels), value in values.items()
        if name == "worldmonitor_connector_freshness"
    }
    assert len(freshness_samples) == 6, (
        f"expected exactly 6 worldmonitor_connector_freshness series, got "
        f"{len(freshness_samples)}: {freshness_samples}"
    )

    seen_states: set[str] = set()
    for instance_id, expected_state in expected.items():
        matches = [
            (labels, value)
            for labels, value in freshness_samples.items()
            if ("instance", instance_id) in labels
        ]
        assert len(matches) == 1, f"{instance_id}: expected exactly 1 series, got {len(matches)}"
        labels, value = matches[0]
        assert value == 1.0
        state = dict(labels)["state"]
        assert state == expected_state, f"{instance_id}: expected {expected_state}, got {state!r}"
        assert state in FRESHNESS_STATES
        seen_states.add(state)

    assert seen_states == set(FRESHNESS_STATES), (
        f"the full closed 6-set must be exercised: {seen_states} != {set(FRESHNESS_STATES)}"
    )


def test_last_success_timestamp_gauge_unchanged() -> None:
    """AC-3 regression pin (spec §9 item 8): adding worldmonitor_connector_freshness must NOT
    alter worldmonitor_connector_last_success_timestamp's emitted values — including for a
    DISABLED instance, proving the raw timestamp gauge stays status-blind even though the new
    gauge is status-aware."""
    sessions = _sqlite_sessions()
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-a", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-b", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-c", connector_id="feeds", config_encrypted="x", status="disabled"
                ),
                TaskRun(
                    id="t-old",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-a",
                    started_at=_T - timedelta(hours=2),
                    finished_at=_T - timedelta(hours=2),
                ),
                TaskRun(
                    id="t-new",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-a",
                    started_at=_T,
                    finished_at=_T,
                ),
                TaskRun(
                    id="t-fail",
                    kind="ingest",
                    status="error",
                    connector_instance_id="ci-b",
                    started_at=_T,
                    finished_at=_T,
                ),
            ]
        )
        session.commit()

    collector = DriverMetricsCollector(
        session_factory=sessions,
        neo4j=_StubNeo4j(nodes=0, edges=0),
        skip_counter=lambda: 0,
        stale_after_seconds=_STALE_AFTER,
        very_stale_after_seconds=_VERY_STALE_AFTER,
    )
    _, values = _collect(collector)

    def g(**labels: str) -> float:
        key = ("worldmonitor_connector_last_success_timestamp", frozenset(labels.items()))
        return values[key]

    with sessions() as session:
        newer = session.execute(
            select(TaskRun.finished_at).where(TaskRun.id == "t-new")
        ).scalar_one()
    assert newer is not None
    assert g(connector_id="feeds", instance="ci-a") == newer.timestamp()
    assert g(connector_id="feeds", instance="ci-b") == 0, "error-only history still reports 0"
    assert g(connector_id="feeds", instance="ci-c") == 0, (
        "a disabled instance with no ingest history is UNCHANGED by the status-aware new gauge "
        "-- the raw timestamp gauge stays status-blind (it iterates every ConnectorInstance "
        "regardless of status)"
    )
