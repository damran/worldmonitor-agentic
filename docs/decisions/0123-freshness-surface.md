# 0123 — Source-freshness surface (F-1 slice 1): a derived 6-state machine over connector run-metadata

- **Status:** PROPOSED (flips to ACCEPTED at the gate-completing PR — the 0117–0122 convention)
- **Date:** 2026-07-24
- **human_fork:** false — a reversible, additive, read-only **derivation** over run-metadata we already
  persist and already partly expose (`ConnectorInstance.status` + `task_run` + the shipped
  `worldmonitor_connector_last_success_timestamp` gauge / `ConnectorSuccessStale` alert). No
  product/architecture fork. The scoping calls (MCP tool deferred; no new alert; global-budget defaults)
  each have a sensible default, a cheap reversal, and a revisit trigger (below). Not marked OPEN.
- **person_affecting:** false — see "Person-affecting reasoning" below. The surface exposes **operational
  metadata about connectors** (which source last succeeded, and how stale it is), never data about a
  person. It makes **no** change to the live system (no ER threshold, guard mode, sensitivity park,
  score, or model/param promotion), performs **no** inference/scoring/attribution, has **zero** egress,
  and writes **nothing** (no table, no migration, no graph write).
- **human_cosign:** not required — reversible, non-person-affecting, read-only observability (per the cost
  directive: reserve cosign for irreversible / person-affecting changes).
- **Backlog/roadmap:** `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-1** (P0 / S, 1 gate — **this
  is the first slice only**). Closes re-review 2026-07-11 finding **9** (no staleness metric).
- **Spec:** `docs/reviews/GATE_F1_FRESHNESS_SURFACE_SPEC.md`.
- **Builds on:** ADR 0076 (H-8c driver Prometheus collector, `collect_snapshot` parity), 0078 (alert
  rules + parity test), 0088 (`alert-rules` promtool CI), 0074 (H-8a auto-hard-disable → `status="error"`),
  0054 (ingest backoff), 0062 (REST read routes + `get_db`/`get_principal` DI), 0069 (app.state DI),
  0042 (single-tenant), 0122 (F-3 — the MCP-has-no-DB-session precedent this ADR follows).

## Context

Backlog row F-1 asks for a "6-state freshness machine (fresh/stale/very_stale/no_data/error/disabled)…
derived `freshness_status` per instance + labeled Prometheus gauge + 1 alert + `GET /sources/freshness` +
matching read-only MCP tool", on "substrate we already have (`ConnectorInstance.last_run` + Prometheus
exporter)". This ADR governs the **first slice**; per-manifest `max_stale_min`, `requiredForRisk` gating,
the intelligence-gap report, and the UI are out of scope.

**What already exists (verified — a precursor to this row).** The continuation stretch already landed a
`worldmonitor_connector_last_success_timestamp{connector_id, instance}` gauge and a `ConnectorSuccessStale`
warning alert (with promtool tests) — the collector source tags them `re-review 2026-07-11 #9 /
OG-harvest F-1 slice 1`. Crucially, the gauge derives from the **latest successful ingest**
(`task_run` where `kind='ingest' AND status='ok'`), **not** `ConnectorInstance.last_run` — because
`last_run` stamps every *attempt* and would make a forever-failing feed read fresh. So the backlog's
"`last_run`" phrasing is a (harmless) inaccuracy: the correct derivation input is `last_success`.

**What this slice adds.** The **derived 6-state layer** on top of that raw signal — a pure state machine
mapping `(status, last_success, now, budgets) → freshness_status` — exposed as (1) a new labeled gauge
`worldmonitor_connector_freshness{connector_id, instance, state}` and (2) `GET /sources/freshness`. Both
call **one** shared helper so they cannot drift (the `collect_snapshot` idiom, ADR 0076 INV-5).

**The status vocabulary (verified).** `ConnectorInstance.status ∈ {disabled, enabled, running, error}`
(there is no `enabled` boolean; `status` is the state). `disabled` = administratively off; `error` =
auto-hard-disabled after 10 consecutive failures (ADR 0074); `enabled`/`running` = active. This maps
cleanly onto the 6-state machine (see the Decision).

**The MCP finding (verified — mirrors F-3's).** Freshness lives **entirely in Postgres**. The stdio/HTTP
MCP server takes only a `Neo4jClient`; the deployed `mcp` compose service has **only** `NEO4J_*` env and
**no** `depends_on: postgres`. ADR 0122 (F-3, one day earlier) **explicitly rejected** wiring Postgres
into the MCP server (its A1). Unlike F-3 — which could record its one Postgres-only field (`merge_history`)
as absent while shipping the graph parts — an F-1 MCP tool would be **entirely** a Postgres read, so
"recorded absence" cannot apply: the tool either reads Postgres or does not ship.

## Decision

Ship the derived freshness surface on **REST + Prometheus**, from **one shared pure helper**; **defer the
MCP tool**; **add no new alert**.

**D1 — One pure, total, deterministic state machine.** `observability/freshness.py::freshness_status(*,
status, last_success, now, stale_after_seconds, very_stale_after_seconds) -> FreshnessState`, priority
order: `disabled` (status) → `error` (status) → `no_data` (active AND `last_success is None`) →
`very_stale` (`age >= very_stale_after`) → `stale` (`age >= stale_after`) → `fresh`. `age = now -
last_success`. Any status not in `{disabled, error}` is treated as active (total over hostile input). All
tz-aware UTC.

**D2 — One shared derivation both surfaces consume (lockstep).**
`observability/freshness.py::compute_instance_freshness(session, *, now, stale_after_seconds,
very_stale_after_seconds)` runs the query (every `ConnectorInstance` left-joined to its max
`task_run.finished_at` where `kind='ingest' AND status='ok'` — the **same** last-success predicate as the
existing gauge — plus `status`) and applies D1. The REST route (api process) and the collector gauge
(driver process) are thin consumers; a parity test pins that they report the same state per instance
(the ADR-0076 INV-5 idiom). The derivation input is **`last_success`, not `last_run`** (a forever-failing
feed must not read fresh).

**D3 — Two surfaces.** (a) Prometheus gauge `worldmonitor_connector_freshness{connector_id, instance,
state}` on the existing driver collector — one active series per instance (value `1`), following the
`worldmonitor_resolve_last_stopped_reason{reason}` single-active idiom, with `state` drawn from the closed
6-set `FRESHNESS_STATES` (defense-in-depth, like `_RESOLVE_STOPPED_REASONS`); emitted **alongside** the
unchanged `last_success_timestamp` gauge. (b) `GET /sources/freshness` — auth-gated (`get_principal`),
DB-backed (`get_db`), read-only JSON: a per-instance list (`instance_id`, `connector_id`, raw `status`,
derived `freshness_status`, `last_run` display-only, `last_success_at`, `age_seconds`) + a `summary`
count-by-state + the `budget`. **Opaque ids only** — no connector config/secret/name/URL/person field
(INV-7). `generated_at` is included (freshness is inherently time-dependent; no byte-parity requirement
this slice).

**D4 — Global budgets now; per-manifest is slice 2.** Two `Settings` fields,
`freshness_stale_after_seconds` (default **14400** = 4× the default hourly `ingest_cadence_seconds`) and
`freshness_very_stale_after_seconds` (default **86400** = 24h), with a validator asserting
`very_stale > stale`. Absolute seconds (not cadence multipliers) so the Python "stale" boundary and the
fixed-literal `ConnectorSuccessStale` PromQL alert cannot drift; a coupling test pins
`freshness_stale_after_seconds == ` the alert's `14400` literal (mirroring
`test_resolution_wedged_threshold_matches_settings`). Slice 2's per-manifest `max_stale_min` will
override these globals; they become the fallback.

**D5 — The MCP tool is a RECORDED DEFERRAL.** `get_source_freshness` does **not** ship in slice 1.
Rationale: freshness is entirely a Postgres read; the MCP server is deliberately Neo4j-only (ADR 0063)
and ADR 0122 (A1, one day earlier) already rejected wiring Postgres into it; doing so requires new
compose infra (`POSTGRES_*` env + `depends_on: postgres` on the `mcp` service) and widens what the MCP
process *is*. That is an architecture question worth its own deliberate ADR, not a rider on the smallest
observability slice. **Reversible + non-person-affecting → pick-and-record, not a human fork** (a human
may veto at accept time). The REST surface fully serves the operator/UI need now; Hermes (the MCP
consumer) is not yet host-operational.

**D6 — No new alert.** The backlog's "1 alert" is the already-shipped `ConnectorSuccessStale`. Every
plausible alert over the new state gauge duplicates an existing alert (`stale`/`very_stale` ↔
`ConnectorSuccessStale`; `error` ↔ `ConnectorInstanceHardDisabled`). The new gauge is a **visibility**
surface (Grafana can show `disabled`/`no_data` distributions the raw-ts gauge cannot), not an alerting
one. Anti-drift is met by the D4 coupling test, not a new rule.

This ADR flips to **ACCEPTED** at the gate-completing PR.

## Person-affecting reasoning (recorded either way)

The surface reports, per connector instance: its `status`, when it last **succeeded**, and a derived
staleness bucket. Reasoned out:

1. **Operational metadata, not personal data.** The payload is about *sources/pipelines* (`connector_id`,
   opaque `instance_id`, timestamps, an enum) — never about a Person, and never the resolved-graph
   entities themselves. Opaque ids only (D3, INV-7); no config/secret/name is read.
2. **No new data class.** Every field derives from `ConnectorInstance` + `task_run`, already visible to
   the same authorized operator via the Integrations UI + the existing Prometheus gauge. The 6-state is a
   mechanical bucketing of an already-exposed timestamp — no new field, inference, score, or attribution.
3. **No change to the live system.** CLAUDE.md's person-affecting sign-off gates cover *changes* affecting
   a real person (ER thresholds, individual-affecting scores, model/param promotion). A read view of
   connector health persists nothing, decides nothing, mutates no threshold/guard/score.
4. **Zero egress, read-only.** No LLM, no external transmission, no write (no table, no migration, no graph
   write).
5. **Same gate, same audience.** `get_principal` / single-tenant (D1, ADR 0042) — neither lowers the auth
   bar nor widens who can see it.

**Conclusion:** not person-affecting in the CLAUDE.md sense. **Revisit** if a future slice (i) joins
freshness to person-level data, (ii) exposes connector config/secrets/names, (iii) adds egress, or (iv)
begins *gating* a person-affecting decision (e.g. `requiredForRisk` feeding a person-affecting risk score).

## Alternatives considered

- **A1 — Ship the `get_source_freshness` MCP tool now by wiring Postgres into the MCP server.** Rejected
  (D5): contradicts ADR 0122 A1 (one day earlier), needs new compose infra + widens the MCP trust
  boundary from Neo4j-only, and belongs in its own deliberate ADR. Deferred with a revisit trigger.
- **A2 — Serve the MCP tool by scraping the driver `/metrics` or calling our own REST.** Rejected: the
  driver metrics port is opt-out-able (`driver_metrics_port=0`), scrape-text parsing is brittle, and
  making the MCP an HTTP client of `api` (with its own bearer) is *more* plumbing than a read-only
  session — no cleaner than A1, so it doesn't beat the deferral.
- **A3 — Add a new `very_stale`/`no_data` alert over the state gauge.** Rejected (D6): duplicates
  `ConnectorSuccessStale` / `ConnectorInstanceHardDisabled` semantics. Recorded as a revisit trigger (a
  `very_stale`→critical escalation) if operators want a distinct higher threshold.
- **A4 — Derive `freshness_status` in PromQL/Grafana from the existing timestamp gauge (no new gauge).**
  Rejected: PromQL over a timestamp cannot express `disabled`/`error`/`no_data` (those need `status`, not
  age), so the 6-state is not derivable without the status join — a materialized state gauge is required.
- **A5 — Use `ConnectorInstance.last_run` as the derivation input (per the backlog wording).** Rejected:
  `last_run` stamps every *attempt*, so a forever-failing feed would read fresh — the exact bug the
  existing gauge's comment calls out. Use `last_success` (D2), consistent with the shipped gauge.
- **A6 — Cadence-multiplier budgets (`stale = N × ingest_cadence_seconds`).** Rejected for slice 1:
  `ConnectorSuccessStale` uses a fixed `14400` literal, so a multiplier that rescales with cadence would
  drift from the alert. Absolute-seconds budgets (D4) with a coupling test keep them lockstep; per-manifest
  budgets are slice 2.
- **A7 — A new table caching `freshness_status`.** Rejected: derivable on read from
  `ConnectorInstance`+`task_run`; a cache would violate the "read-only, no new store" posture and add a
  staleness-of-the-staleness problem.
- **A8 — Fold slice 2 (per-manifest `max_stale_min`) in now.** Rejected: the backlog splits them; the
  global budget is the honest slice-1 fallback and slice 2 layers cleanly on top.

## Reversibility

**Reversible** (`human_fork` = false, `person_affecting` = false).

- **Reversal cost:** revert one new module (`observability/freshness.py`), two `Settings` fields + a
  validator, one collector gauge + two ctor args, one driver construction line, one REST router + its
  `main.py` mount, and the new tests. **No** data migration, **no** schema/store change, **no** new table,
  **no** stored artifacts, **no** change to the existing gauge/alert. The one soft lock-in is the REST
  response **shape** (`/sources/freshness`) — a new additive, auth-gated, single-tenant surface with zero
  locked-in consumers yet; adding fields later is backward-compatible.
- **Revisit triggers:**
  - (a) **Per-manifest `max_stale_min` (slice 2)** — the global budgets become the fallback when a
    manifest declares no budget; `compute_instance_freshness` gains a per-instance budget lookup.
  - (b) **The `get_source_freshness` MCP tool** — when Hermes is host-operational and needs freshness
    in-agent, OR a second Postgres-backed MCP need arises: do the "MCP reads operational metadata over a
    deliberate DB-session context" ADR **once**, then the 5→6 tool sweep (spec §6.7) + compose infra.
  - (c) **A `very_stale`→critical alert** — if operators want a distinct escalation beyond
    `ConnectorSuccessStale`'s 4h warning; that gate adds the `worldmonitor_connector_freshness` `state`
    entry to the alert parity test's closed-label map.
  - (d) **`requiredForRisk` gating / the intelligence-gap report** — separate gates consuming the
    `summary` substrate; (d) re-runs the person-affecting analysis if freshness begins gating a
    person-affecting decision.

## Consequences

- Operators get a first-class, derived staleness view (`GET /sources/freshness` + a Grafana-friendly
  6-state gauge incl. `disabled`/`no_data`), closing re-review finding 9 — without a new store, migration,
  or alert.
- The REST route and the gauge stay lockstep (one shared helper); the Python "stale" boundary and the
  shipped `ConnectorSuccessStale` alert stay lockstep (one budget + a coupling test).
- No CLAUDE.md invariant is touched (read-only operational metadata; no graph write, no provenance, no
  resolution). A property test is added by decision (a pure total state machine), not by mandate.
- The MCP surface stays deliberately Neo4j-only (consistent with ADR 0122); the "matching read-only MCP
  tool" is an honest, reversible deferral with a named revisit trigger — not silently dropped.
