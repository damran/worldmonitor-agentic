# Prometheus configuration — WorldMonitor (ADR 0078)

## Quick start

Boot the **opt-in** reference Prometheus service (default-off behind the `monitoring` profile):

```sh
docker compose -f deploy/compose.yaml --profile monitoring up -d prometheus
```

Prometheus will be available at http://localhost:9090 (loopback-only).

## Files

| File | Purpose |
|------|---------|
| `prometheus.yml` | Global scrape config + job `worldmonitor-driver` → `driver:9108` |
| `alerts/worldmonitor.rules.yml` | 7 alert rules (2 critical + 5 warning); see ADR 0078 D2 |
| `tests/worldmonitor.rules.test.yml` | `promtool test rules` fixture (operator-run) |

## DRIVER_METRICS_PORT coupling

The scrape target `driver:9108` tracks `DRIVER_METRICS_PORT` (default `9108`, ADR 0076 §4 /
`Settings.driver_metrics_port`). If you override `DRIVER_METRICS_PORT` in `.env`, update the
target in `prometheus.yml` to match. The unit test
`tests/unit/test_prometheus_alerts.py::test_worldmonitor_driver_scrape_job_targets_correct_host_and_port`
asserts this coupling so the two cannot drift silently.

## Alertmanager

Alertmanager wiring is **out of scope** (ADR 0078 / external/ops). When it lands, add an
`alerting:` block to `prometheus.yml` (a commented stub is included in the file).

## Grafana

Grafana dashboards are **out of scope** (ADR 0078 / external/ops). The metric inventory for
building dashboards is documented in ADR 0076 and `src/worldmonitor/metrics/collector.py`.

## Running the promtool rule tests (manual / operator)

```sh
promtool test rules deploy/prometheus/tests/worldmonitor.rules.test.yml
```

Requires `promtool` on PATH (ships with the Prometheus binary). This is NOT wired into CI
(out of scope, ADR 0078 §Consequences). The Python unit tests in
`tests/unit/test_prometheus_alerts.py` are the enforced gate.

## External / managed Prometheus

If you run a managed or central Prometheus, copy `prometheus.yml` and `alerts/` to its config
directory — no compose service needed. The alert rules are identical.
