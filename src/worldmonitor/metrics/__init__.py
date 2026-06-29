"""Driver-process Prometheus ``/metrics`` exporter (Gate H-8c / ADR 0076).

A read-only, on-scrape Prometheus surface that makes the H-8a/H-8b operational signals
(instances-in-error, the in-memory resolve-lock-skip counter, the latest resolve ``stopped_reason``,
plus the existing queue/dead-letter/task/graph health) scrapeable + pageable. It runs on the DRIVER
process — the only process that can read the in-memory ``_consecutive_resolve_skips`` — served by
``prometheus_client.start_http_server`` on a daemon thread started once at the top of
``IngestDriver.run_forever``. Counts/gauges only — never entity names or person fields.
"""

from worldmonitor.metrics.collector import DriverMetricsCollector
from worldmonitor.metrics.exporter import start_metrics_exporter

__all__ = ["DriverMetricsCollector", "start_metrics_exporter"]
