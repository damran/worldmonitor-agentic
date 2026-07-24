# Gate F-1 (slice 1) — source-freshness surface: derived 6-state machine + gauge + REST

> Backlog: `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-1** — "Source-freshness surface +
> intelligence-gap reporting — their 6-state freshness machine
> (fresh/stale/very_stale/no_data/error/disabled, per-source max-stale budgets, requiredForRisk gating,
> 'what analysts can't see' reporting) on substrate we already have (`ConnectorInstance.last_run` +
> Prometheus exporter). Closes re-review finding 9 (no staleness metric). First slice: derived
> `freshness_status` per instance + labeled Prometheus gauge + 1 alert + `GET /sources/freshness` +
> matching read-only MCP tool; per-manifest `max_stale_min` = slice 2. P0 / S (1 gate)."
> **THIS GATE = the first slice ONLY.** Hard NON-goals (§7): per-manifest `max_stale_min` (slice 2),
> `requiredForRisk` gating, the intelligence-gap report, any UI.
> ADR: `docs/decisions/0123-freshness-surface.md` (PROPOSED → ACCEPTED at the merging PR).
> Builds on: ADR 0076 (H-8c driver Prometheus collector), ADR 0078 (alert rules + parity test), ADR
> 0088 (`alert-rules` promtool CI job), ADR 0074 (H-8a auto-hard-disable → `status="error"`), ADR 0054
> (ingest backoff), ADR 0062 (REST read routes + `get_db`/`get_principal` DI), ADR 0042 (single-tenant).

## 0. What this gate is (and is NOT)

**Is:** ONE derived, read-only **6-state freshness projection** over connector run-metadata we already
persist (`ConnectorInstance.status` + `task_run`), exposed on **two** surfaces from **one shared pure
helper**:
1. a labeled Prometheus gauge `worldmonitor_connector_freshness{connector_id, instance, state}` on the
   existing driver collector (ADR 0076); and
2. `GET /sources/freshness` (auth-gated JSON, DB-backed).

Zero graph read/write, zero resolution, zero egress, zero new table/migration. It reads two Postgres
tables and derives a total, deterministic enum.

**Is NOT** (explicit non-goals — §7): per-manifest `max_stale_min` budgets (**slice 2** — this gate
ships a single global budget pair as the fallback slice 2 will override); `requiredForRisk` gating; the
"what analysts can't see" **intelligence-gap report**; any UI; **the `get_source_freshness` MCP tool**
(deferred with justification — §3.6 / §6.7); a new alert (the freshness alert **already shipped** —
§1.1 / §3.4).

---

## 1. The freshness substrate that ALREADY EXISTS (verified — do NOT re-spec)

Two freshness-adjacent artifacts already landed during the continuation stretch as a **precursor to
this row** (the collector source literally tags them `re-review 2026-07-11 #9 / OG-harvest F-1 slice 1`).
This gate builds the **derived-state layer on top of them** and must not duplicate them.

| Artifact | Where | What it is | F-1's relation |
|---|---|---|---|
| `worldmonitor_connector_last_success_timestamp{connector_id, instance}` gauge | `metrics/collector.py` (`last_success_rows` query) | The Unix ts of each instance's **latest SUCCESSFUL** ingest (`task_run` `kind='ingest' AND status='ok'`); `0` = never within the `task_run` retention window. Deliberately **NOT** `ConnectorInstance.last_run` (which stamps every *attempt*, so a forever-failing feed would read fresh). | F-1's state machine consumes the **same `last_success` signal** (not `last_run`). The new state gauge is emitted **alongside** this raw-ts gauge — it is NOT a rename/replacement. |
| `ConnectorSuccessStale` alert | `deploy/prometheus/alerts/worldmonitor.rules.yml` | Warning; fires `time() - last_success > 14400 (4h) AND last_success > 0` for `30m`. Never-succeeded (`0`) is excluded (fresh deploys); the terminal failure-streak case is `ConnectorInstanceHardDisabled`'s job (ADR 0074). Promtool tests exist (3 cases). | This **IS** the backlog's "1 alert" line-item — **already delivered**. F-1 does **not** add a duplicate (§3.4). The Python "stale" boundary is pinned equal to this alert's `14400` literal by a coupling test (§6.6) so the two derivations of "stale" cannot drift. |
| `worldmonitor_instances_in_error` gauge + `ConnectorInstanceHardDisabled` alert | `metrics/collector.py` + rules | Count of instances hard-disabled to `status='error'` (ADR 0074) + its warning alert. | Feeds the `error` state (§2). No change. |

**Honest correction to the backlog wording.** The row says "on substrate we already have
(`ConnectorInstance.last_run` …)". The correct derivation input is **last successful ingest**
(`task_run`), **not** `ConnectorInstance.last_run` — using `last_run` would make a forever-failing feed
look fresh (the exact bug the existing gauge's comment calls out). This gate uses `last_success` and
surfaces `last_run` as a display-only field.

### 1.1 The status vocabulary (verified from `runner/driver.py` + `api/integrations.py`)

`ConnectorInstance.status ∈ {"disabled", "enabled", "running", "error"}` (there is **no** `enabled`
boolean column; `status` is the state):

- `"disabled"` — administratively off (default; toggled from the Integrations UI, `integrations.py:301`).
  The driver due-query selects only `"enabled"`, so a disabled instance never runs.
- `"enabled"` — active/retryable (set on run completion `driver.py:764`, on operator enable
  `integrations.py:290`, on stale-recovery `driver.py:241`).
- `"running"` — transient, mid-run (`driver.py:495`).
- `"error"` — terminal hard-disable after `ingest_max_consecutive_failures` (10) consecutive failures
  (ADR 0074, `driver.py:789`); stays out of the due-query until an operator re-enables.

---

## 2. The 6-state machine — mapped to REAL fields (the load-bearing artifact)

A **pure, total, deterministic** function. Inputs: the raw `status`, the `last_success` datetime (or
`None`), `now`, and the two budgets. Priority order (first match wins):

| # | Derived `freshness_status` | Condition (on REAL fields) | Rationale |
|---|---|---|---|
| 1 | `disabled` | `status == "disabled"` | Administratively off — not an availability gap (age-invariant). |
| 2 | `error` | `status == "error"` | Auto-hard-disabled (ADR 0074) — a *known* failed source (age-invariant); `ConnectorInstanceHardDisabled` already alerts it. |
| 3 | `no_data` | active (`status ∉ {disabled, error}`) **AND** `last_success is None` | Enabled but **never** succeeded within the `task_run` retention window — a genuine "no data" gap (age-invariant). |
| 4 | `very_stale` | active, `last_success` present, `age >= very_stale_after_seconds` | Beyond the escalated budget. |
| 5 | `stale` | active, `last_success` present, `stale_after_seconds <= age < very_stale_after_seconds` | Past the stale budget; matches `ConnectorSuccessStale` by construction. |
| 6 | `fresh` | active, `last_success` present, `age < stale_after_seconds` | Recently succeeded. |

where `age = (now - last_success).total_seconds()`. `"running"` and any unexpected status fall into the
**active branch** (defense-in-depth: any status not in `{disabled, error}` is treated as active, so the
function is total over hostile input). All datetimes are timezone-aware UTC (`task_run.finished_at` is
`DateTime(timezone=True)`; `now = datetime.now(UTC)`).

### 2.1 Exact signatures (new module `src/worldmonitor/observability/freshness.py`)

```python
FreshnessState = Literal["fresh", "stale", "very_stale", "no_data", "error", "disabled"]
FRESHNESS_STATES: tuple[str, ...]  # the closed 6-set, single source of truth for the label alphabet

def freshness_status(
    *,
    status: str,
    last_success: datetime | None,
    now: datetime,
    stale_after_seconds: int,
    very_stale_after_seconds: int,
) -> FreshnessState: ...   # the pure state machine (the @given target, §6.5)

@dataclass(frozen=True)
class InstanceFreshness:
    instance_id: str
    connector_id: str
    status: str                     # raw ConnectorInstance.status
    freshness_status: FreshnessState  # derived
    last_run: datetime | None       # ConnectorInstance.last_run (last ATTEMPT — display only)
    last_success_at: datetime | None  # derivation input (latest successful ingest)
    age_seconds: float | None       # now - last_success_at; None when last_success_at is None

def compute_instance_freshness(
    session: Session,
    *,
    now: datetime,
    stale_after_seconds: int,
    very_stale_after_seconds: int,
) -> list[InstanceFreshness]: ...
```

`compute_instance_freshness` is the **one shared query+derivation** both surfaces call (the
`collect_snapshot` parity idiom, ADR 0076 INV-5): for every `ConnectorInstance` it left-joins the max
`task_run.finished_at` where `kind='ingest' AND status='ok'` (identical predicate to the existing
`last_success_rows` query — extended only by also selecting `ConnectorInstance.status`), then applies
`freshness_status`. The REST route (api process) and the collector (driver process) both import it, so
the two surfaces **cannot** report different states for the same instance.

### 2.2 Budgets — settings + defaults (slice-1 global; slice-2 makes them the fallback)

New in `settings.py` (absolute seconds — NOT cadence multipliers — so the Python boundary and the
fixed-literal PromQL alert cannot drift; `model_validator` is already imported):

```python
# --- Source-freshness surface (F-1 slice 1 / ADR 0123) ---
freshness_stale_after_seconds: int = Field(default=14400, gt=0)       # 4h = 4x default ingest cadence
freshness_very_stale_after_seconds: int = Field(default=86400, gt=0)  # 24h
# + a model_validator asserting very_stale > stale (keeps the state machine monotone under
#   operator misconfiguration).
```

- `freshness_stale_after_seconds` default **`14400`** is chosen to **equal `ConnectorSuccessStale`'s
  `> 14400` literal** (4× the default hourly `ingest_cadence_seconds`), so the REST/gauge "stale"
  boundary and the alert fire at the same age. A coupling test pins the equality (§6.6).
- `freshness_very_stale_after_seconds` default **`86400`** (24h) is the escalated budget — a full day
  with no success. No alert references it in slice 1 (see §3.4).
- **Reversal cost:** both are env-tunable (`FRESHNESS_STALE_AFTER_SECONDS` /
  `FRESHNESS_VERY_STALE_AFTER_SECONDS`) — pure config, no code change. **Revisit trigger:** slice 2
  introduces per-manifest `max_stale_min`; these globals become the fallback when a manifest declares no
  budget (§7).

---

## 3. Locked invariants + design decisions every change must hold

### 3.1 CLAUDE.md invariants — which apply (honest calculus)

Freshness is **derived, read-only, operational metadata about connectors** — not graph data. It writes
nothing, resolves nothing, stamps no provenance. So the three "locked invariants" the coordinator names
map as:

- **G1 provenance on every node AND edge — NOT ON THIS PATH.** The gate creates no node and no edge and
  performs no graph write. There is no provenance to stamp. (Provenance stamping remains untouched
  elsewhere.) The surface-analogue obligation here is **label/field hygiene**, §3.3.
- **Append-only — trivially held (READ-ONLY).** No new table, no migration, no write of any kind
  (Postgres or graph). `compute_instance_freshness` and both surfaces issue `SELECT` only. The
  recording-fake / read-only test discipline applies (§6).
- **Canonical-canonical only via the guard — NOT ON THIS PATH.** No resolution, merge, or
  canonicalisation. The catastrophic-merge guard is not on this path.

Because the gate touches **none** of the invariant-bearing subsystems, a property test is **not
gate-mandatory** under CLAUDE.md's "any gate that touches an invariant MUST add a `@given`". We add one
anyway as a **decision** (§3.5) — a pure total state machine is the cheapest, highest-value `@given`
target there is.

### 3.2 One shared helper — REST + gauge lockstep (the ADR-0076 INV-5 idiom)

There is **exactly one** derivation point: `observability/freshness.py::compute_instance_freshness`
(query) wrapping `freshness_status` (the enum). The REST route and the collector state gauge are **thin
consumers** — neither re-derives the state. A parity test (§6.4) pins that, for the same seeded DB
state + frozen `now` + budgets, the collector's emitted `{state}` label per instance **equals** the
REST body's `freshness_status` for that instance. Mirrors `collect_snapshot` (ADR 0076) and F-3's
shared-helper lockstep.

### 3.3 Field/label hygiene — opaque ids only (INV-7)

Both surfaces expose **only** `instance_id` (server-minted uuid) + `connector_id` — the same opaque
labels the existing gauge uses. **No** connector config, secret, URL, dataset name, or person field is
read or exposed (config is Fernet-encrypted; decrypting it here would be a leak). A human-friendly
display name is a **UI / slice-2 concern**, recorded as a non-goal (§7). This is the freshness analogue
of the collector's INV-7 "integer counts/labels — never entity names/raw rows/person fields".

### 3.4 The alert is ALREADY shipped — NO new alert (avoid duplicate semantics)

The backlog's "1 alert" is `ConnectorSuccessStale`, which **already exists** (§1.1). Adding a second
alert over the new state gauge would **duplicate** existing semantics:
- `state="stale"/"very_stale"` overlaps `ConnectorSuccessStale` (age-of-last-success),
- `state="error"` overlaps `ConnectorInstanceHardDisabled` (`instances_in_error > 0`).

**Decision:** F-1 adds **no** alert. The "1 alert" line-item is satisfied by the precursor
`ConnectorSuccessStale`. The new state gauge is a **visibility** surface (Grafana can show the full
6-state distribution incl. `disabled`/`no_data` — which the raw-ts gauge structurally cannot), not an
alerting surface. The anti-drift obligation is met by the §6.6 coupling test, not by a new rule.

*Revisit trigger (recorded, not built):* a **`very_stale` → critical** escalation (a genuinely distinct
threshold beyond `ConnectorSuccessStale`'s 4h warning) could later be added over
`worldmonitor_connector_freshness{state="very_stale"}`; that gate must add the
`_VALID_LABEL_VALUES["worldmonitor_connector_freshness"] = {"state": FRESHNESS_STATES}` entry to the
parity test (§6.6). Out of scope here.

### 3.5 Property-test discipline — DECISION: YES, one `@given` (not mandatory, but cheap+high-value)

Recorded as a decision, not an omission (mirror F-3 §3.5, inverse of F-2). Freshness touches no
CLAUDE.md invariant (§3.1), so a property test is not required — but `freshness_status` is a pure total
function with three checkable algebraic properties, so we include `tests/property/test_prop_freshness_state.py`
(§6.5): **totality**, **determinism**, and **monotonicity in age** (age-increase never yields a fresher
state), plus **age-invariance** of `disabled`/`error`/`no_data`.

### 3.6 The MCP tool — RECORDED DEFERRAL (locked decision, ADR 0123 D5)

The backlog asks for a "matching read-only MCP tool" (`get_source_freshness`). **It does not ship in
slice 1.** Honest finding:

- Freshness lives **entirely in Postgres** (`ConnectorInstance` + `task_run`). Unlike F-3's dossier
  (graph data + one Postgres-only field it recorded absent), there is **no graph substrate** to serve —
  the whole tool is a Postgres read.
- The **stdio MCP server has no DB session**: `build_server`/`build_http_app` take only a `Neo4jClient`;
  the deployed `mcp` compose service has **only** `NEO4J_*` env and **no** `depends_on: postgres`. Its
  trust boundary / 12-factor story is deliberately Neo4j-only (ADR 0063).
- **ADR 0122 (F-3, 2026-07-23) just rejected wiring Postgres into the MCP server** (its Alternative A1:
  "needs a `Session` in the shared helper, which the stdio MCP surface lacks — wiring Postgres into the
  stdio MCP server is new plumbing that breaks the graph-only lockstep and expands the gate"). Doing it
  one day later, as a rider on the smallest observability slice, would contradict that fresh decision.

**Decision (D5):** deliver **REST + gauge in slice 1**; record `get_source_freshness` as **deferred to a
follow-up gate that deliberately decides whether/how the MCP surface reads operational metadata from
Postgres** (an architecture question worth its own ADR — it changes what the MCP process *is*, from a
graph-read gateway to a graph + operational-metadata gateway, and requires compose infra: Postgres env +
`depends_on: postgres` on the `mcp` service). This is **reversible** and **non-person-affecting**, so
per CLAUDE.md build-discipline it is a *pick-and-record*, **not** a human fork; a human may still veto at
ADR-accept time.

- **Reversal cost of the deferral:** LOW — adding the tool later is an additive change (an injected
  session factory symmetric to the existing `Neo4jClient` injection, the tool body, the 5→6 pin sweep in
  §6.7, and the compose env). No data/schema change.
- **Revisit trigger:** (i) Hermes goes host-operational (it is currently BLOCKED on operator host
  verify) and demonstrably needs freshness in-agent; OR (ii) a second Postgres-backed MCP need arises —
  then do the "MCP reads operational metadata" ADR **once**, for both.
- **Why not option (b) (MCP scrapes the driver `/metrics` or calls our REST)?** Rejected: the driver
  metrics port is opt-out-able (`driver_metrics_port=0`), text-parsing a scrape is brittle, and making
  the MCP an HTTP client of `api` (with its own bearer) is *more* plumbing than a read-only session — no
  cleaner than (a), so it doesn't beat the deferral.

---

## 4. Surface designs

### 4.1 Prometheus gauge (collector — `metrics/collector.py`)

Add `worldmonitor_connector_freshness{connector_id, instance, state}` following the existing
`worldmonitor_resolve_last_stopped_reason{reason}` single-active-series idiom: for each instance emit
**exactly one** series with `state = <derived FreshnessState>` and value `1`. Cardinality is bounded by
the instance count; `state` is a **closed** 6-value alphabet (`FRESHNESS_STATES`) — defense-in-depth
against an unbounded label, mirroring the `_RESOLVE_STOPPED_REASONS` discipline.

- Sourced from `compute_instance_freshness(session, now=…, stale_after_seconds=…,
  very_stale_after_seconds=…)` inside `collect()` (the collector already opens a `session`).
- The collector `__init__` gains `stale_after_seconds: int` + `very_stale_after_seconds: int`; the
  driver passes `self._settings.freshness_stale_after_seconds` /
  `self._settings.freshness_very_stale_after_seconds` at construction (`driver.py:844`).
- The existing `worldmonitor_connector_last_success_timestamp` gauge is **unchanged** in emitted values.
  The builder MAY (optional, not required) source both gauges from the single shared helper result to
  de-duplicate the `last_success` query; if so, the timestamp gauge's emitted values MUST stay identical
  (its existing collector test must stay green).

### 4.2 REST — `GET /sources/freshness` (new router `api/freshness.py`, mounted in `main.py`)

Auth-gated (`get_principal`, mirroring `api/graph.py`) + DB-backed (`get_db` → `app.state.db_sessions`,
the standard generator dependency in `api/deps.py`). Read-only; no path params; no injection surface
(no id input). Response:

```jsonc
{
  "generated_at": "<ISO8601 UTC>",            // snapshot wall-clock (freshness IS time-dependent — honest, not a determinism break)
  "budget": { "stale_after_seconds": 14400, "very_stale_after_seconds": 86400 },
  "summary": { "fresh": N, "stale": N, "very_stale": N, "no_data": N, "error": N, "disabled": N, "total": N },
  "sources": [
    {
      "instance_id": "<uuid>",
      "connector_id": "<opaque>",
      "status": "enabled",                    // raw ConnectorInstance.status
      "freshness_status": "fresh",            // derived (the 6-state)
      "last_run": "<ISO8601|null>",           // last ATTEMPT (display only)
      "last_success_at": "<ISO8601|null>",    // derivation input
      "age_seconds": 123.0                    // null when last_success_at is null
    }
  ]
}
```

- `summary` counts per state + `total` — the headline for a later intelligence-gap report (the report
  itself is a non-goal, §7).
- `generated_at` is included (unlike F-3's dossier, which omitted `assembled_at` for byte-determinism):
  freshness is *inherently* a function of `now`, and there is no MCP byte-parity requirement this slice.
  Tests freeze `now` and do not assert equality on `generated_at`.
- Return type `dict[str, Any]` (consistent with the existing routes; no typed Pydantic model — that is
  F-7 territory).

---

## 5. Acceptance criteria (crisp)

- **AC-1 pure state machine.** `freshness_status(...)` returns exactly one of the 6 `FRESHNESS_STATES`
  for **all** inputs; the §2 truth table holds (unit + property).
- **AC-2 shared derivation.** `compute_instance_freshness` is the ONLY query+derivation site; it uses
  the `kind='ingest' AND status='ok'` last-success predicate (NOT `last_run`); the REST route and the
  collector gauge both call it and neither re-derives state.
- **AC-3 Prometheus gauge.** `worldmonitor_connector_freshness{connector_id, instance, state}` is
  emitted with exactly one series per instance, value `1`, `state` in the closed 6-set; the existing
  `last_success_timestamp` gauge's values are unchanged; the collector parity test
  (`test_prometheus_alerts.py::_emitted_metric_names`) still passes (new gauge widens the emitted set;
  no alert references it).
- **AC-4 REST route.** `GET /sources/freshness` returns 200 + the §4.2 body for an authenticated caller;
  401 without a token; an empty `sources[]` + zeroed `summary` when there are no instances; the derived
  states match §2 for seeded rows; opaque ids only (no config/secret/name — §3.3).
- **AC-5 budget alignment (no drift).** `Settings().freshness_stale_after_seconds` equals the numeric
  literal in `ConnectorSuccessStale`'s expr (§6.6); `freshness_very_stale_after_seconds >
  freshness_stale_after_seconds` (validator).
- **AC-6 read-only.** No write of any kind (Postgres or graph); no new table; no migration; the
  recording-fake/read-only guard holds on both surfaces.
- **AC-7 property invariant.** The `@given` holds: totality, determinism, monotonicity-in-age, and
  age-invariance of `disabled`/`error`/`no_data` (§6.5).
- **AC-8 MCP deferral recorded.** No `get_source_freshness` tool ships; the MCP tool count/set pins
  (§6.7) are **unchanged**; ADR 0123 D5 records the deferral + revisit trigger.
- **AC-9 no regression.** All existing collector/alert/promtool/REST tests stay green; the existing
  gauge + `ConnectorSuccessStale` alert are unchanged.

---

## 6. Named tests

### 6.1 `tests/unit/test_freshness.py` (NEW) — state machine truth table + shared query
- `test_state_disabled` / `test_state_error` — status precedence beats age (age-invariant).
- `test_state_no_data_when_never_succeeded` — active + `last_success is None` → `no_data`.
- `test_state_fresh_stale_very_stale_boundaries` — exact boundary behaviour at `age == stale_after`
  (→ `stale`) and `age == very_stale_after` (→ `very_stale`); `age < stale_after` → `fresh`.
- `test_running_status_treated_as_active` — `"running"` + recent success → `fresh`.
- `test_unknown_status_is_total` — an unexpected status string still yields a valid state (active branch).
- `test_compute_instance_freshness_uses_last_success_not_last_run` — seed an instance whose `last_run`
  is recent but whose only `task_run` rows are `status='error'`: the derived state is `no_data`/`stale`,
  NOT `fresh` (the load-bearing correctness point). Uses an injected **in-memory SQLite** session
  factory (models are SQLite-compatible; no Docker).
- `test_compute_instance_freshness_shape` — returns one `InstanceFreshness` per instance with
  `age_seconds` computed from `last_success_at`.

### 6.2 `tests/unit/test_api_freshness.py` (NEW) — REST route
- `test_freshness_returns_sources_and_summary` — 200; body has `generated_at`, `budget`, `summary`
  (counts sum to `total`), `sources[]` with the §4.2 shape; states match seeded rows (frozen `now`).
- `test_freshness_requires_auth_401` — no principal → 401 (mirror `read_entity`).
- `test_freshness_empty_when_no_instances` — 200; `sources == []`; `summary.total == 0`.
- `test_freshness_exposes_only_opaque_ids` — no config/secret/name/URL key anywhere in the body (§3.3).
- Uses the app `TestClient` with an injected SQLite session factory on `app.state.db_sessions` (the ADR
  0069 DI pattern the existing REST tests use).

### 6.3 `tests/unit/test_metrics_collector.py` (EXTEND) — the state gauge
- `test_connector_freshness_gauge_emitted` — with a seeded session + budgets, `collect()` yields
  `worldmonitor_connector_freshness` with one sample per instance, value `1`, `state` in
  `FRESHNESS_STATES`.
- `test_connector_freshness_gauge_closed_cardinality` — an instance in each of the 6 states produces
  exactly the expected `{state}` labels; no unbounded label.
- `test_last_success_timestamp_gauge_unchanged` — the existing gauge's emitted values are byte-identical
  to before (regression guard if the builder consolidates the query).

### 6.4 `tests/unit/test_freshness.py` (parity) — REST ↔ gauge lockstep
- `test_rest_and_gauge_agree_per_instance` — for the same seeded session + frozen `now` + budgets, the
  `{state}` label the collector emits for each instance **equals** the REST body's `freshness_status`
  for that instance (pins §3.2; both are thin consumers of the one helper).

### 6.5 `tests/property/test_prop_freshness_state.py` (NEW `@given`)
- `test_prop_state_is_total_and_deterministic` — `@given` over `status` (a strategy incl. the 4 real
  values + arbitrary strings), `last_success` (None or a datetime), `now`, and budgets with
  `very_stale > stale`: the output is always one of `FRESHNESS_STATES`, and a second call with identical
  inputs returns the identical value.
- `test_prop_state_monotone_in_age` — for a fixed **active** status + present `last_success` + fixed
  budgets, and two ages `a1 <= a2`, `rank(state(a1)) <= rank(state(a2))` for the order
  `fresh < stale < very_stale` (age never moves the state fresher).
- `test_prop_terminal_states_age_invariant` — `disabled`/`error` (and `no_data` when `last_success is
  None`) are independent of `now`/age.
- Pure in-process function — **no DB, no container** (no connection-leak footgun).

### 6.6 `tests/unit/test_prometheus_alerts.py` (EXTEND) — budget↔alert coupling (anti-drift)
- `test_freshness_stale_budget_matches_connector_success_stale_alert` — parse the numeric literal from
  `ConnectorSuccessStale`'s expr (`> (\d+)`) and assert it equals
  `Settings().freshness_stale_after_seconds` (default 14400). Mirrors
  `test_resolution_wedged_threshold_matches_settings` verbatim in spirit — pins the two "stale"
  derivations equal so REST/gauge and the alert cannot drift.
- `test_freshness_very_stale_greater_than_stale` — `Settings()` validator holds
  (`very_stale_after_seconds > stale_after_seconds`).
- **No change** to `_emitted_metric_names` expectations beyond the new gauge appearing in the emitted
  set (the one-way parity — alerts→emitted — is unaffected; the new gauge is unalerted).

### 6.7 MCP pin loci — enumerated for the FUTURE gate; **NOT edited here**

Because the MCP tool is deferred (§3.6), **none** of the following change in this gate. Enumerated so the
follow-up MCP gate has the full 5→6 sweep ready (this list is the deliverable the coordinator asked for;
it is a reference, not a sanctioned edit surface for F-1's test-author):

| # | File : locus | Current |
|---|---|---|
| M-1 | `src/worldmonitor/mcp/server.py:3-8` (module docstring — "exactly five … tools" list) | 5 tools |
| M-2 | `src/worldmonitor/mcp/server.py` `_register_read_tools` + `_ALL_TOOL_NAMES`-style registration (the 5 `add_tool` calls) | 5 tools |
| M-3 | `tests/unit/test_mcp_server.py:15` (docstring) + `test_tool_set_is_exactly_the_five` (~199) + the `_ALL_TOOL_NAMES` frozenset + `test_all_tools_have_output_schema` | 5-set |
| M-4 | `tests/unit/test_mcp_http_auth.py` — the two HTTP tool-set assertions + parity `expected_names` (the `..._five_tools` assertions) | 5-set |
| M-5 | `tests/integration/test_mcp_stdio.py` — module docstring enumeration + the two wire `tools/list` set assertions + the AC-4 annotations set | 5-set |
| M-6 | `tests/property/test_mcp_auth_boundary.py` — the per-tool boundary drivers (add a `get_source_freshness` driver) | 5 tools |
| M-7 | `tests/property/test_prop_mcp_stdout_purity.py` — extend to the new stdio tool | 5 tools |
| M-8 | `deploy/hermes/config.yaml:11` `tools.include: [..5..]  # EXACTLY the 5 read tools` | 5-set |
| M-9 | `deploy/compose.yaml:571-572` MCP service comment (still lists only 4 tools — already stale) + **add `POSTGRES_*`/`DATABASE_URL` env + `depends_on: postgres`** to the `mcp` service | Neo4j-only env |
| M-10 | `src/worldmonitor/mcp/server.py` `build_server`/`build_http_app` — add an injected `db_sessions` factory (symmetric to `neo4j_client`) | Neo4j-only |
| M-11 | `src/worldmonitor/mcp/__main__.py` / `main()` — build the session factory from settings | n/a |

---

## 7. NON-goals (explicit — do NOT build here)

- **Per-manifest `max_stale_min`** — **slice 2**. This gate ships the two GLOBAL budgets as the fallback
  slice 2 will override per-manifest. Revisit trigger recorded in ADR 0123.
- **`requiredForRisk` gating** — the "this source is required for risk scoring" flag + its consumption is
  a later concern (needs the risk-scoring surface).
- **The intelligence-gap report** ("what analysts can't see") — the `summary` counts are the substrate; the
  narrative report is out of scope.
- **The `get_source_freshness` MCP tool** — deferred with justification (§3.6 / ADR 0123 D5).
- **A new alert** — the freshness alert (`ConnectorSuccessStale`) already shipped (§3.4).
- **Any UI** (a freshness panel / Integrations badge) — no template/HTML change.
- **A human display name / dataset label** on sources — opaque ids only (§3.3).
- **A typed Pydantic response model / OpenAPI artifact** — F-7 territory; keep `dict[str, Any]`.

---

## 8. Slice breakdown

**ONE slice.** The state machine, the shared helper, the gauge, the REST route, the budget settings, and
all tests are tightly coupled and individually small; the parity test (§6.4) needs both the gauge and the
REST route to exist. The MCP tool — the only piece that would force a second slice + an architectural
change — is **deferred** (§3.6), so there is no second builder slice in this gate.

- **Slice 1 — derived source-freshness surface (state machine + gauge + REST).** Production: (a)
  `observability/freshness.py` (`FreshnessState`/`FRESHNESS_STATES`, `freshness_status`,
  `InstanceFreshness`, `compute_instance_freshness`); (b) `settings.py` (two budget fields +
  `very_stale > stale` validator); (c) `metrics/collector.py` (the `worldmonitor_connector_freshness`
  gauge + the two budget ctor args); (d) `runner/driver.py` (pass the budgets at collector
  construction); (e) `api/freshness.py` (`GET /sources/freshness`) + `api/main.py` (mount it). Tests: all
  of §6.1–§6.6. ADR 0123 → **ACCEPTED** at the merging PR; regenerate the ADR index. Individually
  mergeable; closes the backlog's gauge + REST + alert(-precursor) line-items; MCP recorded deferred.

---

## 9. Open items for the test-author / builder

1. **`last_success`, not `last_run`.** The derivation input is the latest **successful** ingest from
   `task_run` (`kind='ingest' AND status='ok'`), NOT `ConnectorInstance.last_run`. `test_..._uses_last_
   success_not_last_run` (§6.1) is the load-bearing regression — a forever-failing feed must NOT read
   `fresh`.
2. **tz-aware UTC everywhere.** `task_run.finished_at` is `DateTime(timezone=True)`; use `datetime.now(UTC)`
   for `now`; guard against naive/aware subtraction.
3. **Closed `state` label alphabet.** The collector's `state` label must draw only from `FRESHNESS_STATES`
   (defense-in-depth, mirroring `_RESOLVE_STOPPED_REASONS`); the property test proves totality so no
   hostile status can leak an unbounded label.
4. **Budget coupling.** Keep `freshness_stale_after_seconds` default `== ConnectorSuccessStale`'s `14400`
   literal; the §6.6 coupling test fails if they drift. If an operator raises the budget, that is a
   deployment choice — the coupling test asserts only the **default** matches the shipped alert.
5. **The MCP tool is deferred — do NOT touch any MCP file or the 5-tool pins.** §6.7 is a reference for the
   FUTURE gate. If a reviewer asks "where's the MCP tool?", the answer is ADR 0123 D5 (recorded deferral
   + revisit trigger), consistent with ADR 0122 A1.
6. **No new table / migration.** Read-only; do not add a model or migration; do not decrypt connector
   config.
7. **Run `pytest -m "not integration"` (incl. the new property + collector + REST tests) locally; if
   promtool is on PATH, `promtool test rules deploy/prometheus/tests/worldmonitor.rules.test.yml` still
   passes (no rules change). Run `ruff format --check .` repo-wide before push.**
