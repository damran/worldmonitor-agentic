"""Integration tests: the driver Prometheus /metrics exposition (ADR 0076, gate H-8c).

Against ephemeral Postgres + Neo4j (testcontainers): seed the H-8a/H-8b signals
(instances-in-error, the queue/parked/dead-letter/task/graph health, two finished resolves
carrying ``stopped_reason``), build the on-scrape collector against the REAL session factory +
Neo4j client + a skip accessor, render the Prometheus text exposition over an in-process
``CollectorRegistry`` (no socket), and assert:

* the headline gauges (``instances_in_error``, ``resolve_consecutive_lock_skips``,
  ``resolve_last_stopped_reason``) appear with the seeded values;
* INV-5 parity — every reused DB-derived gauge equals ``smoke_metrics.snapshot()`` for the SAME
  DB state (no drift between the CLI snapshot and /metrics);
* INV-7 — counts only: no row/graph string content (a sentinel name/value) leaks into the text;
* INV-6 — ``start_metrics_exporter`` binds NO server when the port is 0 (the opt-out), and starts
  it once on a positive port (``start_http_server`` is patched, so no real socket is bound).

RED until the gate lands: ``prometheus_client`` + the ``worldmonitor.metrics`` package do not
exist yet (import fails at collection — the right RED reason).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    ConnectorInstance,
    ErQueueItem,
    IngestDeadLetter,
    MergeAudit,
    TaskRun,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.metrics.collector import DriverMetricsCollector
from worldmonitor.metrics.exporter import start_metrics_exporter
from worldmonitor.runner import smoke_metrics
from worldmonitor.settings import Settings

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
# A distinctive marker placed in row + graph data the collector READS but MUST NOT expose.
_SENTINEL = "Zzz-Sentinel-Person-Name-Do-Not-Leak-7a3f"


def _seed_postgres(sessions) -> None:  # noqa: ANN001 - sessionmaker passthrough (house style)
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-err", connector_id="c", config_encrypted="x", status="error"
                ),
                ConnectorInstance(
                    id="ci-en", connector_id="c", config_encrypted="x", status="enabled"
                ),
                ErQueueItem(
                    id="q1",
                    connector_id="c",
                    raw_entity={"id": "q1", "leak": _SENTINEL},
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
                    canonical_id="cid",
                    source_ids=["a"],
                    score=0.9,
                    decision="pending_review",
                ),
                IngestDeadLetter(
                    id="d1",
                    connector_id="c",
                    source_key="k",
                    stage="map",
                    error=f"boom {_SENTINEL}",
                ),
                TaskRun(id="t-i-ok", kind="ingest", status="ok", started_at=_NOW),
                TaskRun(id="t-i-err", kind="ingest", status="error", started_at=_NOW),
                TaskRun(
                    id="t-r-old",
                    kind="resolve",
                    status="ok",
                    stats={"stopped_reason": "exhausted"},
                    started_at=_NOW - timedelta(seconds=100),
                ),
                TaskRun(
                    id="t-r-new",
                    kind="resolve",
                    status="ok",
                    stats={"stopped_reason": "timeout"},
                    started_at=_NOW - timedelta(seconds=10),
                ),
                TaskRun(id="t-r-run", kind="resolve", status="running", started_at=_NOW),
            ]
        )
        session.commit()


def _exposition_values(
    collector: DriverMetricsCollector,
) -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    values: dict[tuple[str, frozenset[tuple[str, str]]], float] = {}
    for family in collector.collect():
        for sample in family.samples:
            values[(sample.name, frozenset(sample.labels.items()))] = sample.value
    return values


def test_metrics_exposition_reflects_seeded_state(
    clean_graph: Neo4jClient, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rendered text exposition carries the headline H-8a/H-8b gauges, matches
    smoke_metrics.snapshot() on every reused DB-derived gauge (INV-5), and leaks no row/graph
    string content (INV-7)."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    _seed_postgres(sessions)
    # 2 :Entity nodes + 1 edge; the Person node carries the sentinel NAME the collector must
    # count but never expose.
    clean_graph.execute_write(
        "CREATE (a:Entity:Person {id:'p1', name:$name})-[:ASSOCIATE]->"
        "(b:Entity:Company {id:'c1', name:'Acme'})",
        name=_SENTINEL,
    )

    collector = DriverMetricsCollector(
        session_factory=sessions, neo4j=clean_graph, skip_counter=lambda: 4
    )
    registry = CollectorRegistry()
    registry.register(collector)
    text = generate_latest(registry).decode("utf-8")

    # The headline H-8a/H-8b signals, as scrapeable gauges (raw counts, ``.0`` float render).
    assert "worldmonitor_instances_in_error 1.0" in text
    assert "worldmonitor_resolve_consecutive_lock_skips 4.0" in text
    assert 'worldmonitor_resolve_last_stopped_reason{reason="timeout"} 1.0' in text

    # INV-7: counts only — no row/graph string content leaks into the exposition.
    assert _SENTINEL not in text

    # INV-5 parity: point snapshot()'s settings at the SAME containers (it builds its own
    # engine + Neo4j client from get_settings) and compare gauge-for-gauge.
    container_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        environment="test",
        postgres_dsn=postgres_dsn,
        neo4j_uri=clean_graph.uri,
        neo4j_user=clean_graph.user,
        neo4j_password=clean_graph.password,
    )
    monkeypatch.setattr(smoke_metrics, "get_settings", lambda: container_settings)
    snap = smoke_metrics.snapshot()

    values = _exposition_values(collector)

    def g(name: str, **labels: str) -> float:
        return values[(name, frozenset(labels.items()))]

    # Explicit expected values pin both sides (neither can be silently zero) AND the parity.
    assert g("worldmonitor_er_queue_pending") == 2.0 == snap["queue_pending"]
    assert g("worldmonitor_er_queue_pending_review") == 1.0 == snap["queue_pending_review"]
    assert g("worldmonitor_parked_merges") == 1.0 == snap["parked_merges"]
    assert g("worldmonitor_dead_letters") == 1.0 == snap["dead_letter"]
    assert g("worldmonitor_graph_nodes") == 2.0 == snap["graph_nodes"]
    assert g("worldmonitor_graph_edges") == 1.0 == snap["graph_edges"]
    for kind in ("ingest", "resolve"):
        for status in ("ok", "error", "running"):
            assert (
                g("worldmonitor_task_runs", kind=kind, status=status)
                == snap[f"task_{kind}_{status}"]
            ), f"task_runs{{{kind},{status}}} must equal the smoke_metrics snapshot"

    engine.dispose()


class _NoopCollector:
    """A do-nothing collector for the exporter-lifecycle test (yields no metric families)."""

    def collect(self):  # noqa: ANN201 - prometheus_client Collector protocol (yields families)
        return iter(())


def test_start_metrics_exporter_is_a_noop_when_port_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-6: ``driver_metrics_port == 0`` starts NO server (no thread, no bound port) — today's
    behaviour — while a positive port starts the HTTP server exactly once on that port. The real
    ``start_http_server`` is patched on the exporter module so no socket is ever bound.

    Builder contract: ``exporter.py`` must do ``from prometheus_client import start_http_server``
    (so the name is patchable on the module) AND ``start_metrics_exporter`` must short-circuit on
    a falsy port — see the test-author report.
    """
    from worldmonitor.metrics import exporter as exporter_mod

    started: list[int] = []
    monkeypatch.setattr(
        exporter_mod, "start_http_server", lambda port, *a, **k: started.append(port)
    )

    noop_zero = _NoopCollector()
    start_metrics_exporter(0, noop_zero)
    assert started == [], "port=0 must NOT bind/serve (the exporter opt-out)"

    noop_on = _NoopCollector()
    try:
        start_metrics_exporter(9108, noop_on)
        assert started == [9108], "a positive port starts the HTTP server once on that port"
    finally:
        # Hygiene: drop whatever got registered to the global REGISTRY (the no-op collectors
        # name nothing, so this never conflicts; both cleanups are best-effort).
        for collector in (noop_on, noop_zero):
            with contextlib.suppress(KeyError):
                REGISTRY.unregister(collector)
