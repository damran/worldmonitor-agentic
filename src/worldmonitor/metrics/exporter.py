"""Start the driver's Prometheus ``/metrics`` HTTP exporter (Gate H-8c / ADR 0076).

A thin lifecycle helper: register the on-scrape collector to the default ``prometheus_client``
registry and start the daemon WSGI thread serving the text exposition. The driver calls this ONCE
at the top of ``run_forever`` (after ``recover_stale``, before the loop); a falsy port (``0``) is
the opt-out — it binds NO server (no thread, no port), today's behaviour and the ADR-0076 lever.

``start_http_server`` is imported at module level so a test can ``monkeypatch.setattr`` it on this
module (no real socket bound under test).
"""

from __future__ import annotations

from prometheus_client import REGISTRY, start_http_server

from worldmonitor.metrics.collector import DriverMetricsCollector


def start_metrics_exporter(port: int, collector: DriverMetricsCollector) -> None:
    """Register ``collector`` and serve ``/metrics`` on ``port`` (no-op when ``port`` is falsy)."""
    if not port:
        # The opt-out: driver_metrics_port=0 starts no server at all (ADR 0076 INV-6).
        return
    REGISTRY.register(collector)
    # Daemon WSGI thread bound 0.0.0.0:port inside the container; coexists with the asyncio loop.
    start_http_server(port)
