# Gate H-8c — Prometheus `/metrics` exporter on the driver process

- Gate: **H-8c** (Stage-4 hardening; the LAST H-8 remaining half — the metrics/alerting transport)
- Branch: `feat/h8c-prometheus-metrics` (off `master` @ the H-8b merge)
- ADR: `docs/decisions/0076-prometheus-metrics-exporter.md` (accepted)
- Transport DECIDED upstream: Prometheus `/metrics` (ADR 0054 tail; GATE_LEDGER §Stage-1, CLOSED) — not a fork.
- Scope contract: `.claude/gate.scope` (INV-1..8)
- Person-affecting: **NO** (read-only operational counts; no ER/merge/score/guard write). No human sign-off.
- Migration: **NONE** (no `db/models.py` change).

---

## 1. GAP
The H-8a/H-8b signals surface only in logs/DB and cannot be scraped/paged: `instances-in-error`
(`ConnectorInstance.status=='error'`, ADR 0074), `ResolveStats.stopped_reason` (rides `task_run.stats`,
ADR 0075 D2), and `IngestDriver._consecutive_resolve_skips` (**in-memory on the driver only**, ADR 0075
D3). `smoke_metrics.py` computes 11 DB-derived health counts but only as a one-shot CLI log line and does
not count instances-in-error. No `/metrics`, no `prometheus-client` dep exist yet.

## 2. BUILD

### 2.1 Dependency
`uv add prometheus-client` (regenerates `pyproject.toml` + `uv.lock`; the Docker build is `uv sync
--frozen`, so the lock MUST be committed). Verify the resolved version at build time.

### 2.2 Settings (`src/worldmonitor/settings.py`, driver-supervision block ~138-142)
- `driver_metrics_port: int = Field(default=9108, ge=0)` — `0` DISABLES the exporter (no thread, no
  port; today's behaviour). Doc comment in house style; `.env.example` gets `DRIVER_METRICS_PORT=9108`.

### 2.3 Collector (`src/worldmonitor/metrics/collector.py`)
A `prometheus_client`-registered custom **Collector** (a class with `collect()` yielding metric families)
computing **at scrape time** (so it is fresh even if the asyncio loop is wedged). Constructed with the
driver's `session_factory` (a `Callable[[], Session]` ctx-mgr), `Neo4jClient`, and a zero-arg
`skip_counter: Callable[[], int]` accessor onto the live driver. **Read-only** (no Postgres/Neo4j
writes — mirror the `smoke_metrics` contract). Metrics (all `GaugeMetricFamily`, `worldmonitor_` prefix):
- Reuse the `smoke_metrics` queries (refactor the shared counting into a function both call, OR have the
  collector import/call a `snapshot`-style helper): `worldmonitor_er_queue_pending`,
  `worldmonitor_er_queue_pending_review`, `worldmonitor_parked_merges`, `worldmonitor_dead_letters`,
  `worldmonitor_task_runs{kind,status}` (6 series), `worldmonitor_graph_nodes`, `worldmonitor_graph_edges`.
- `worldmonitor_instances_in_error` = `COUNT(ConnectorInstance WHERE status=='error')` (NEW; ADR 0074).
- `worldmonitor_resolve_consecutive_lock_skips` = `skip_counter()` (the in-memory ADR-0075 D3 value).
- `worldmonitor_resolve_last_stopped_reason{reason}` = `1` on the reason of the latest **finished**
  resolve `task_run`, read portably as a Python dict (`stats.get("stopped_reason")`, NOT a Postgres
  `->>`). Select the most-recent `TaskRun` `kind=='resolve'`, `status in (ok,error)`, `ORDER BY
  started_at DESC` (the absolute latest finished row — do NOT filter on `stats is not None`).
  `reason="unknown"` when there is no finished row, that latest row's `stats` is None / lacks the key,
  or the value is outside the closed set `{exhausted, timeout}` (closed-cardinality label, INV-7).

### 2.4 Exporter wiring (`src/worldmonitor/metrics/exporter.py` + `runner/driver.py`)
- `exporter.py`: a thin `start_metrics_exporter(port, collector) -> None` that registers the collector to
  the default `REGISTRY` and calls `prometheus_client.start_http_server(port)` (daemon WSGI thread).
- `driver.py run_forever`: at the top, **after** `self.recover_stale()` and **before** `while True:`,
  `if self._settings.driver_metrics_port: start_metrics_exporter(self._settings.driver_metrics_port,
  <collector built from self>)`. `run_forever` is `# pragma: no cover` glue, so keep the testable logic
  (the collector + a pure "should we start" guard) out of it. The `--healthcheck` path (`main()` before
  `build_driver`) MUST NOT start the server.

### 2.5 Compose (`deploy/compose.yaml`, driver service)
- Add `DRIVER_METRICS_PORT=9108` to the driver `environment:`. Add a **commented** host-publish example
  (`# - "127.0.0.1:9108:9108"`) — default is in-network only (no host publish; a Prometheus scraper on
  the compose network hits `driver:9108`). Do NOT add a Prometheus server service (external/ops).

## 3. LOAD-BEARING INVARIANTS — see `.claude/gate.scope` INV-1..8.

## 4. FAILING-TEST-FIRST (RED → GREEN)

- **`tests/unit/test_settings.py`** — `driver_metrics_port` default(9108)/override/`allows_zero`/reject-negative.
- **`tests/unit/test_metrics_collector.py`** — construct the collector with FAKE session_factory + fake
  neo4j + a `lambda: 7` skip accessor (no Docker where possible; if the queries need a real session, make
  it an integration test instead). Assert the produced metric families include every name above with the
  right values for a seeded state; assert `resolve_last_stopped_reason` → `"unknown"` with no finished
  resolve row and the actual reason when one exists; assert the collector issues **no writes**.
- **`tests/integration/test_metrics_exporter.py`** (testcontainers) — seed Postgres (an `error`
  instance, some queue/dead-letter/task rows) + the graph; build the collector against the real
  session_factory/neo4j + a skip accessor returning N; scrape via `prometheus_client.generate_latest(REGISTRY)`
  (no real socket needed) and assert the exposition text contains `worldmonitor_instances_in_error 1.0`,
  `worldmonitor_resolve_consecutive_lock_skips <N>.0`, the queue/dead-letter gauges matching
  `smoke_metrics.snapshot()` for the same state (INV-5 parity), and NO person/entity strings. Also a
  guard test: `driver_metrics_port=0` ⇒ the start helper does not bind/serve.

## 5. ACCEPTANCE CRITERIA
- `prometheus-client` in `pyproject.toml` + `uv.lock`; `driver_metrics_port` setting + `.env.example` + compose env.
- The collector yields every §2.3 metric, read-only, computed on scrape; `instances_in_error`,
  `resolve_consecutive_lock_skips`, `resolve_last_stopped_reason` correct; DB-derived gauges match `smoke_metrics`.
- The exporter starts only when `driver_metrics_port>0`, once, from `run_forever`; `--healthcheck` stays server-free.
- All §4 tests green; existing suites untouched & green; `ruff format --check .` (repo-wide) + `ruff check` + `pyright` clean.

## 6. FROZEN (KEEP-GREEN)
- ADR-0054/0074/0075 logic (retry/backoff, hard-disable, recover_stale startup-only, maintenance cadence,
  resolve timeout, lock-skip escalation) — H-8c only READS these signals; byte-identical.
- `smoke_metrics` read-only contract + its existing metric meanings (reuse, don't change semantics).
- `db/models.py` (no schema change); `tests/integration/test_migrations.py`; AuthMiddleware/API auth
  (the exporter is NOT on the API process, so the API auth allowlist is untouched).

## 7. PERSON-AFFECTING ASSESSMENT
**NOT person-affecting → no human sign-off** (`human_fork: false`). Read-only integer counts/gauges, no
ER/merge/canonical-id/provenance/sensitivity invariant touched (no `@given` mandated). Pull/inbound only;
counts only — no entity/person data is exposed (mirrors `smoke_metrics`). Sovereignty-consistent.

## 8. OUT OF SCOPE
- A Prometheus server / Alertmanager / scrape config / alert rules in-repo (external/ops, ADR 0054).
- `/metrics` on the API process; API request metrics; multi-replica aggregation (HA lease, ADR 0029 X2).
- Any change to the ADR-0054/0074/0075 scheduling/resolve logic; a resolve watchdog/kill (ADR 0075 limit).

## 9. VERDICT
Reversible (set `driver_metrics_port=0` to fully disable), non-person-affecting observability transport
executing an already-locked decision. One PR; checker reproduces INV-1..8; judge gates. `human_fork: false`.
