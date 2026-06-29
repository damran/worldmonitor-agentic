# 0076 — Prometheus `/metrics` exporter on the driver process

- **Status:** accepted
- **Date:** 2026-06-29
- **Gate:** H-8c (Stage-4 hardening). The **last** of the H-8 remaining halves; the alerting/metrics
  **transport** for the signals landed by ADR [0074](0074-auto-hard-disable-after-n-failures.md) (H-8a)
  and ADR [0075](0075-periodic-maintenance-and-resolve-liveness.md) (H-8b). The transport choice itself
  was already **decided** — Prometheus `/metrics` (ADR 0054 tail; GATE_LEDGER §Stage-1 "H-8 transport =
  Prometheus /metrics", CLOSED) — over a notifiers push / structured-logs-only.
- **Touches:** new `metrics/` exporter module, `runner/driver.py` (`run_forever` start hook),
  `settings.py`, `.env.example`, `deploy/compose.yaml`, `pyproject.toml` + `uv.lock` (new dep). **Not
  person-affecting** — read-only operational counts; no ER/merge/score/guard/graph write, no person data
  (`human_fork: false`).

## Context

H-8a/H-8b made the driver's failure + liveness signals **bounded and escalating**, but they surface
**only in logs / in the DB**: the auto-hard-disable count is `ConnectorInstance.status=='error'` (a DB
state, ADR 0074), the resolve outcome is `ResolveStats.stopped_reason` riding `task_run.stats`, and the
resolve-lock-skip streak is `IngestDriver._consecutive_resolve_skips` — an **in-memory int on the live
driver process, persisted nowhere**. Nothing can be scraped or paged. ADR 0075's revisit trigger names
exactly this gate: *"when H-8c / ADR 0076 lands the `/metrics` transport, the in-memory skip counter +
`ResolveStats.stopped_reason` become the gauge/counter source and the WARN can be paged."*

`smoke_metrics.py` already computes 11 DB-derived health counts (queue backlog, parked merges,
dead-letters, the `task_{ingest,resolve}_{ok,error,running}` cross-product, graph node/edge counts) but
only as a one-shot operator CLI log line — not a scrape surface, and it does **not** count
instances-in-error.

**The one real design fact** (not a preference): the API and driver are **separate processes**
(`compose.yaml`). The DB-derived counts are readable by either, but `_consecutive_resolve_skips` lives
in the **driver** process and is unreadable from the API. So a *complete* metrics surface must run **in
the driver process** — which also has DB sessions for everything else.

## Decision

1. **Add `prometheus-client`** (the standard exposition library) and expose a Prometheus text
   `/metrics` endpoint **from the driver process** via `prometheus_client.start_http_server(port)` — a
   daemon WSGI thread that coexists with the asyncio loop (`threading` is already imported). Started
   **once** at the top of `run_forever` (after `recover_stale`, before the `while True`), guarded by the
   new port setting; the cheap `--healthcheck` path never reaches it and stays connection-free.
2. **A custom on-scrape collector** (`prometheus_client` `Collector` registered to the default
   registry) computes every metric **at scrape time** — so the numbers stay fresh even if the asyncio
   loop is wedged (the metrics thread is independent). It is constructed with the driver's
   `session_factory` + `Neo4jClient` + a zero-arg accessor for the live skip counter. It must stay
   **read-only** (the `smoke_metrics` contract): counts/gauges only, **never** entity or person data.
3. **Metric set** (all `worldmonitor_`-prefixed gauges; raw counts — rates/slopes left to PromQL per
   ADR 0054 "rules external"):
   - DB-derived (reuse the `smoke_metrics` queries): `er_queue_pending`, `er_queue_pending_review`,
     `parked_merges`, `dead_letters`, `task_runs{kind,status}` (6 series), `graph_nodes`, `graph_edges`.
   - **New** `instances_in_error` — `COUNT(ConnectorInstance WHERE status=='error')` (the ADR-0074
     hard-disable gauge, the headline ask).
   - **New** `resolve_consecutive_lock_skips` — the in-memory driver counter (ADR 0075 D3).
   - **New** `resolve_last_stopped_reason{reason}` — `1` on the reason of the latest *finished* resolve
     `task_run` (`exhausted`/`timeout`, ADR 0075 D2; `unknown` when no finished row / `stats is None`).
4. **New settings:** `driver_metrics_port: int = Field(default=9108, ge=0)` (`0` disables the exporter
   entirely — today's behaviour; `9108` avoids the node_exporter/Prometheus defaults 9100/9090). Bind
   `0.0.0.0` **inside the container** only; the `driver` compose service publishes **no host port**, so
   the endpoint is reachable **in-network only** (a Prometheus scraper on the compose network hits
   `driver:9108`). `.env.example` documents `DRIVER_METRICS_PORT`; compose gets the env mapping and a
   commented host-publish example.
5. **The Prometheus server + alert rules are external/ops** (ADR 0054) — H-8c ships the **exporter**
   only, not a scraper service or in-code thresholds.

## Alternatives considered

- **Expose `/metrics` on the FastAPI API process:** rejected as the *primary* home — the API cannot see
  the driver's in-memory skip counter (separate process); it would only serve the DB-derived subset, and
  duplicating gauge definitions across two scraped processes risks double counting.
- **Persist `_consecutive_resolve_skips` to the heartbeat file / DB so the API can read it:** rejected —
  an extra write path + staleness for a value the driver already holds; the driver-side exporter is
  simpler and idiomatic (each process exposes its own `/metrics`).
- **Hand-rolled text exposition + `http.server`:** rejected — re-implements the format/escaping + a tiny
  HTTP server; `prometheus-client`'s `start_http_server` + registry is the battle-tested standard for
  one small, well-maintained dep.
- **Push (Pushgateway / notifiers):** rejected — pull is sovereignty-consistent (the scraper reaches
  *in*; our data never leaves), and ADR 0054 already chose pull.
- **Per-tick gauge updates:** rejected in favour of the on-scrape collector, which stays fresh under a
  wedged loop (the H-8b failure mode this gate exists to observe).

## Consequences

- The H-8a/H-8b signals are now **scrapeable and pageable**: `instances_in_error`,
  `resolve_consecutive_lock_skips`, `resolve_last_stopped_reason`, plus the existing queue/dead-letter/
  task/graph health — all on `driver:9108/metrics`. Alert rules live in external Prometheus/Alertmanager.
- **Sovereignty-safe:** an inbound pull endpoint, in-network by default (no host publish), counts/gauges
  only — no person/entity data leaves (consistent with the pull-only data-sovereignty principle).
- **Robust to a wedged loop:** the metrics thread + on-scrape collection are independent of the asyncio
  loop, so a hung resolve still serves fresh numbers (the abandon-not-kill case from ADR 0075 D3).
- **New dependency** (`prometheus-client`) + `uv.lock` regen — the Docker build is `uv sync --frozen`, so
  the lock must be regenerated (this dev box can't build images → push and let CI build).
- **No schema change**, **not person-affecting** (`human_fork: false`), no `@given` invariant mandated
  (no ER/merge/canonical-id/provenance/sensitivity invariant touched) — example tests + green
  quality+security CI gate the self-merge, per CLAUDE.md.
- **Out of scope (recorded):** a Prometheus server / Alertmanager / scrape config in-repo (ops/external);
  API-process request metrics; multi-replica aggregation (per-replica `/metrics`; HA lease deferred,
  ADR 0029 fork X2); a true resolve watchdog/kill (ADR 0075 limitation).

## Reversibility

Reversible (observability transport). **Reversal cost: low** — set `driver_metrics_port=0` to disable
the exporter (no endpoint, no thread; exactly today's behaviour), or revert the `run_forever` start hook
+ collector module and drop the dependency. **Revisit triggers:** (1) if API-layer metrics (request
latency, auth failures) are wanted, add a second exporter on the API process with a disjoint metric
namespace; (2) if multi-replica scraping lands, revisit per-replica labelling alongside the HA lease
(ADR 0029 fork X2); (3) if `prometheus-client` becomes a burden, the on-scrape collector can be re-backed
by a hand-rolled text exposition behind the same endpoint.
