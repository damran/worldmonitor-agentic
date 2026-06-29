# 0078 — Prometheus scrape job + alert rules for the driver `/metrics` exporter

- **Status:** accepted
- **Date:** 2026-06-29
- **Gate:** H-8c follow-up (Stage-4 hardening). The **named** follow-up that ADR
  [0076](0076-prometheus-metrics-exporter.md) §5 and ADR [0054](0054-driver-connector-retry-backoff.md)
  deliberately left "external/ops": H-8c shipped the **exporter** (`driver:9108/metrics`, PR #134) but
  **no scrape config and no alert rules**, so the H-8a/H-8b signals are scrapeable yet **not pageable**.
  This gate fills exactly that gap, in-repo.
- **Touches:** a new `deploy/prometheus/` tree (`prometheus.yml`, `alerts/worldmonitor.rules.yml`,
  `tests/worldmonitor.rules.test.yml`), an **opt-in** `prometheus` service in `deploy/compose.yaml`
  (default-off behind a `monitoring` profile), `.env.example` (doc note), and unit tests under
  `tests/unit/test_prometheus_*.py`. **Not person-affecting** — observability/ops config only; no
  ER/merge/score/guard/provenance/graph path, no schema change, no migration (`human_fork: false`).
  No new dependency (PyYAML already present in the test env; `prometheus-client` already a dep, ADR 0076).

## Context

ADR 0076 / H-8c put the H-8a/H-8b signals on a Prometheus text endpoint via a read-only on-scrape
`DriverMetricsCollector` (`src/worldmonitor/metrics/collector.py`), served by
`start_http_server(driver_metrics_port)` on the **driver** process (port `9108`, `0` disables). ADR 0076
§5 and ADR 0054 explicitly scoped the **Prometheus server + alert rules** *out* as "external/ops" — so
today nothing scrapes `driver:9108` and nothing pages on its signals. The escalations built by ADR 0074
(auto-hard-disable → `worldmonitor_instances_in_error`) and ADR 0075 (resolve lock-skip streak →
`worldmonitor_resolve_consecutive_lock_skips`; wall-clock timeout →
`worldmonitor_resolve_last_stopped_reason{reason="timeout"}`) are therefore visible to a human only if
someone curls the endpoint. ADR 0075's revisit trigger named this directly: once the `/metrics`
transport lands, "the WARN can be paged."

The full collector metric set (all `worldmonitor_`-prefixed, the **only** names that exist —
re-enumerated here so the parity invariant below is concrete):
`er_queue_pending`, `er_queue_pending_review`, `parked_merges`, `dead_letters`,
`task_runs{kind=ingest|resolve, status=ok|error|running}`, `graph_nodes`, `graph_edges`,
`instances_in_error` (ADR 0074), `resolve_consecutive_lock_skips` (ADR 0075 D3),
`resolve_last_stopped_reason{reason=exhausted|timeout|unknown}` (ADR 0075 D2; value always `1`). Plus
the Prometheus synthetic `up{job="worldmonitor-driver"}`. An alert expression that names anything else is
the **drift bug** this gate exists to catch.

## Decision

Ship the scrape config + alert rules **in-repo** under `deploy/prometheus/`, plus a default-off
reference Prometheus service so the config is bootable/testable. Three files + one opt-in service.

### D1 — `deploy/prometheus/prometheus.yml` (the scrape config)
- `global`: `scrape_interval` and `evaluation_interval` of **30s** — strictly **below**
  `resolve_cadence_seconds` (300s, the resolve tick) so the live in-memory
  `resolve_consecutive_lock_skips` gauge is sampled multiple times per resolve cadence (it is read at
  scrape time, ADR 0075 D3) and a wedge is never missed between ticks.
- one `scrape_configs` job **`worldmonitor-driver`** with a single static target **`driver:9108`** —
  the in-network compose hostname:port of the driver exporter. **Coupling (documented):** the `9108` is
  the `driver_metrics_port` default (ADR 0076 §4); the scrape-config test asserts equality against
  `Settings().driver_metrics_port` so the two cannot drift.
- `rule_files: ["alerts/*.rules.yml"]` (relative to the config dir `/etc/prometheus`), loading D2.
- **No `alerting:`/`alertmanagers:` block** — Alertmanager wiring is out of scope (below); a commented
  stub may document where it would attach.

### D2 — `deploy/prometheus/alerts/worldmonitor.rules.yml` (the alert rules)
One group `worldmonitor-driver`. Every rule carries `expr`, `for`, `labels.severity ∈ {critical,
warning}`, and `annotations.{summary,description}` (the description names its source ADR). The set:

| Alert | Expr | for | Severity | Rationale / ADR |
|---|---|---|---|---|
| `DriverDown` | `up{job="worldmonitor-driver"} == 0` | 2m | **critical** | Exporter/driver unreachable; every other signal goes stale. ADR 0076. |
| `ResolutionWedged` | `worldmonitor_resolve_consecutive_lock_skips >= 3` | 10m | **critical** | A prior resolve pass still holds the lock — resolution starved. Threshold **= `resolve_lock_skip_alert_threshold` (3)**; the driver also escalates to WARNING log at exactly this count. ADR 0075 D3. |
| `ConnectorInstanceHardDisabled` | `worldmonitor_instances_in_error > 0` | 5m | warning | A connector auto-hard-disabled after `ingest_max_consecutive_failures` (10) failures; operator re-enables from the UI. ADR 0074. |
| `ResolvePassTimingOut` | `worldmonitor_resolve_last_stopped_reason{reason="timeout"} == 1` | 30m | warning | Latest finished resolve hit `resolve_timeout_seconds` (600) and stopped early; sustained ⇒ backlog never clears in one pass. ADR 0075 D2. |
| `ErQueueBacklogHigh` | `worldmonitor_er_queue_pending > 10000` | 1h | warning | Persistent pending backlog ⇒ resolution not keeping up (pair with the two above). Threshold is **operator-tunable** (no setting backs it). |
| `IngestDeadLettersPresent` | `worldmonitor_dead_letters > 0` | 15m | warning | Quarantined ingest/resolution rows accumulating; triage needed. ADR 0027/0053. |
| `MergesParkedForReview` | `worldmonitor_parked_merges > 0` | 15m | warning | Catastrophic-merge guard parked merges in `pending_review`; human review required. ADR 0024/0031. |

Two **critical** (page now: pipeline down / resolution silently stopped), five **warning** (ticket:
needs operator attention soon). The `for:` windows are deliberately ≥ a couple of the relevant cadences
(resolve 300s, ingest cadence) so a single transient tick never pages.

### D3 — `deploy/prometheus/tests/worldmonitor.rules.test.yml` (promtool rule unit test)
A `promtool test rules` fixture: input series + alert assertions for at least the four headline alerts
(`DriverDown`, `ResolutionWedged`, `ConnectorInstanceHardDisabled`, `ResolvePassTimingOut`). It ships
in-repo for operators to run manually (`promtool test rules deploy/prometheus/tests/...`); wiring
promtool into CI is out of scope (below), so our pytest suite (§Test plan) is the enforced gate.

### D4 — opt-in reference Prometheus service in `deploy/compose.yaml`
A `prometheus` service **behind `profiles: ["monitoring"]`** so a bare `docker compose up` (and
`--profile core`) does **not** start it — default-off, exactly like the rest of the optional surface.
Pin a current `prom/prometheus` tag (verify at build time, per the compose house rule). It mounts
`./prometheus/prometheus.yml` and `./prometheus/alerts/` **read-only** (`:ro`), publishes a
**loopback-only** host port `127.0.0.1:9090:9090`, and sits on the **`default`** network (so it can
reach `driver:9108`) and **not** on `sandbox_net` (ADR 0077 egress isolation). It is **not** a
`depends_on` of any core service.

### D5 — the invariant this gate protects (the primary test)
**Metric-name PARITY (anti-drift):** every metric an alert `expr` references is one
`DriverMetricsCollector` actually emits (plus the synthetic `up`), with valid label keys/values
(`kind∈{ingest,resolve}`, `status∈{ok,error,running}`, `reason∈{exhausted,timeout,unknown}`,
`job=="worldmonitor-driver"`). This mirrors ADR 0076 INV-5 (CLI↔/metrics parity) one layer up
(rules↔exporter). It is enforced by deriving the emitted set **dynamically** from the collector (not a
hand-copied list), so a renamed/removed gauge breaks the alert test immediately.

## Alternatives considered

- **Leave the server + rules fully external (status quo of ADR 0076/0054):** rejected for *this* gate —
  it is precisely the named follow-up; "external" should not mean "undocumented and untested." Shipping
  config-as-code with a parity test makes the rules reviewable and drift-proof; an operator may still
  point a managed Prometheus at the same files.
- **Always-on Prometheus core service:** rejected — Prometheus is an operator choice (many deployments
  use a managed/central one); a default-off profile keeps the bare stack lean and avoids a second
  host-port surface unless opted in.
- **Hand-maintained list of valid metric names in the parity test:** rejected — it would itself drift
  from the collector. Deriving the set by instantiating the collector (SQLite + stub Neo4j, the existing
  `test_metrics_collector.py` pattern) makes the exporter the single source of truth.
- **Bake Alertmanager + receivers in now:** rejected/deferred — routing/silences/secrets are a separate,
  deployment-specific concern (below).

## Consequences

- The H-8a/H-8b signals are now **pageable**: drop these files next to a Prometheus (the reference
  service or a managed one) and `instances_in_error`, the lock-skip wedge, and the resolve-timeout
  signal raise alerts — closing ADR 0075's revisit trigger.
- **Sovereignty-safe:** Prometheus *pulls* `driver:9108` in-network; the reference service publishes
  loopback-only and holds counts/gauges only — no person/entity data leaves.
- **Drift-proof:** the parity test fails the moment a collector rename/removal orphans an alert expr.
- **No schema change, not person-affecting** (`human_fork: false`); no `@given` property fleet (no
  ER/merge/canonical-id/provenance/sensitivity invariant touched) — structural + parity unit tests +
  the adversarial checker gate the self-merge, per CLAUDE.md.
- **Out of scope (recorded as follow-up backlog):** Alertmanager receivers/routing/silences; Grafana
  dashboards; promtool-in-CI (the `.test.yml` ships but is operator-run for now); multi-replica
  scrape aggregation / per-replica labelling (rides the HA lease, ADR 0029 fork X2); booting the
  `monitoring` profile in CI `compose-boot`.

## Reversibility

Reversible (observability/ops config). **Reversal cost: low** — delete the `prometheus` service block
from `deploy/compose.yaml` and remove the `deploy/prometheus/` directory; nothing in the app/driver
depends on either (the exporter is unchanged and independently toggled by `driver_metrics_port`). This
is **not** a human fork (no data-shape lock-in, nothing public-facing, no person impact) — per the
CLAUDE.md reversible-decision discipline the sensible default (a default-off in-repo reference service +
config-as-code) is picked and we proceed. **Revisit triggers:** (1) if an **external/managed Prometheus
becomes the system of record**, drop the reference compose service and keep only the config files (or
relocate them to that system's repo); (2) when **Alertmanager** lands, add the `alerting:` block +
routing in a follow-up gate; (3) when the **HA/multi-replica lease** (ADR 0029 fork X2) lands, revisit
per-replica scrape labelling and aggregation rules.

## Slice plan (independently mergeable)

- **Slice 1 — alert rules + parity (the heart).** `deploy/prometheus/alerts/worldmonitor.rules.yml` +
  `deploy/prometheus/tests/worldmonitor.rules.test.yml` + `tests/unit/test_prometheus_rules_metric_parity.py`
  (PRIMARY, the D5 invariant) + `tests/unit/test_prometheus_rules_structure.py`. Stands alone.
- **Slice 2 — scrape config.** `deploy/prometheus/prometheus.yml` +
  `tests/unit/test_prometheus_scrape_config.py` (job→`driver:9108`, port-couples to
  `driver_metrics_port`, scrape_interval ≤ `resolve_cadence_seconds`, `rule_files` glob). Order-independent.
- **Slice 3 — opt-in compose service + docs.** `prometheus` service in `deploy/compose.yaml` (D4) +
  `tests/unit/test_prometheus_compose_service.py` + `.env.example` note + `docs/GATE_LEDGER.md` +
  `docs/40_ROADMAP.md` + flip this ADR to `accepted` on merge.

## Test plan (what test-author writes — all Docker-free)

- **`test_prometheus_rules_metric_parity.py` (PRIMARY / D5):** instantiate `DriverMetricsCollector`
  against in-memory SQLite + a stub Neo4j (reuse the `test_metrics_collector.py` fixtures) and call
  `collect()` to derive the authoritative `{family-name → label-keys}` and observed label values; parse
  `worldmonitor.rules.yml`, extract every `worldmonitor_[A-Za-z0-9_]+` token (and any bare metric
  identifier) from each `expr`, and assert each is in the emitted set — the only non-`worldmonitor_`
  identifier allowed is `up` (and only with `job="worldmonitor-driver"`). Assert label matchers use
  only valid keys/values per metric. A deliberately-misspelled metric in a fixture must fail.
- **`test_prometheus_rules_structure.py`:** valid YAML with a `groups:` list; every alert has
  `alert`/`expr`/`for`, `labels.severity ∈ {critical, warning}`, non-empty `annotations.summary` +
  `annotations.description`; `ResolutionWedged`'s threshold literal equals
  `Settings().resolve_lock_skip_alert_threshold` (coupling, import settings).
- **`test_prometheus_scrape_config.py`:** a `worldmonitor-driver` job targets `driver:<port>` where
  `<port> == Settings().driver_metrics_port`; `global.scrape_interval`/`evaluation_interval` parse to
  ≤ `Settings().resolve_cadence_seconds`; `rule_files` references the shipped alerts glob.
- **`test_prometheus_compose_service.py`:** a `prometheus` service exists with `profiles` containing
  `monitoring` (default-off); host port bound loopback-only (`127.0.0.1`); config + alerts mounted
  `:ro`; on `default` and **not** `sandbox_net`; not a `depends_on` of core services. Reuse the
  `test_compose_sandbox_topology.py` YAML helpers.
