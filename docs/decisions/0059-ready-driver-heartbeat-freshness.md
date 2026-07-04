# 0059 — Surface driver-heartbeat freshness on `/ready` (non-fatal)

- **Status:** ACCEPTED
- **Date:** 2026-06-27
- **Gate:** Phase-D (`gate/ready-driver-heartbeat`) — a focused, LOW enhancement off `master`.
- **Addresses:** the cross-workflow review's one A→B port candidate — the sibling's `/ready` includes a
  driver-heartbeat-freshness check; B's `/ready` (ADR 0051) probes only store reachability. This is the
  "shared durable heartbeat surfaced on `/ready`" that ADR 0051 D3 explicitly **deferred** as a named
  future enhancement. Phase D delivers it.

## Context

B's `/ready` (`api/readiness.py`, ADR 0051) is fail-closed over the three stores (Postgres/Neo4j/MinIO)
— it answers "can the API serve the graph?". It does **not** reflect whether the ingest **driver** is
alive. B already has a driver heartbeat: a per-container **file** (`runner/heartbeat.py`,
`driver_heartbeat_path`) the driver `touch`es every tick, read by the driver's own
`--healthcheck` (the driver container's HEALTHCHECK). So a dead/stalled driver is already visible in
`docker compose ps` for the **driver** container — but not on the API's `/ready`.

The sibling (A) keeps its heartbeat in **Postgres** and reads it inside `/ready`, treating `driver` as a
readiness check. B's heartbeat is a **file** (ADR 0051 D3 deliberately rejected a Postgres heartbeat
table — migration + conflates API-readiness with driver-liveness — and a shared volume — "fragile"),
so B cannot read it from the API container without new cross-service plumbing. ADR 0051 D3 named exactly
this ("A shared durable heartbeat surfaced on `/ready` is a NAMED future enhancement") and deferred it.

## Decision

Add a **non-fatal** `driver` component to `/ready`:

1. **Non-fatal (the key divergence from A).** `/ready` stays **200 as long as the three stores are
   reachable** — the API can serve the graph regardless of the driver. The `driver` result is reported in
   the response **body** (`checks["driver"] ∈ {"ok","stale","unknown"}`) as **observability**, and does
   **not** flip `ready`/the HTTP status. Rationale: the API and the driver are **separate services**;
   making a dead driver 503 the API would pull a healthy API out of an orchestrator's rotation — wrong.
   A makes its `driver` check fatal; for B's separate-service topology, non-fatal is the correct adaptation.
   (A stalled driver is still hard-signalled by the **driver container's own** HEALTHCHECK, ADR 0051.)
2. **Read B's existing file heartbeat, not a new store.** `build_default_readiness` adds a driver probe
   that reads `driver_heartbeat_path` via the existing `runner.heartbeat.Heartbeat(...).is_alive(now)`
   (fresh ⇒ `"ok"`, missing/stale/unparseable ⇒ `"stale"`, path unset/unreadable ⇒ `"unknown"`). No new
   table, no migration; reuses `driver_heartbeat_path` + `driver_heartbeat_stale_seconds` (settings exist).
3. **Cross-container visibility via a shared read-only volume** (overturns ADR 0051 D3's shared-volume
   deferral, with reason). Compose mounts a named `driver-heartbeat` volume at the heartbeat dir —
   read-write on the `driver` service (the sole writer), **read-only** on the `api` service (the reader).
   D3 rejected a shared volume as "fragile cross-container writable runtime volume"; that concern is
   mitigated here: it is **one writer / one read-only reader**, writes are atomic (temp + `os.replace`,
   `heartbeat.py`), and the signal is **non-fatal** (a missing/stale read degrades to `"stale"`/`"unknown"`,
   never a false outage). The fragility D3 worried about (two writers, fatal coupling) does not apply.

## Alternatives considered

- **Fatal `driver` check (mirror A exactly).** Rejected — conflates API readiness with driver liveness;
  a dead driver would 503 a healthy API and remove it from rotation. Non-fatal observability is correct
  for separate services.
- **Postgres-backed heartbeat (adopt A's table).** The cross-service-correct way *if* fatal/durable
  liveness were needed, but it needs a migration and overturns ADR 0051 D3's "no heartbeat table". Kept
  as the **upgrade trigger** (below), not built now — the file + shared RO volume is sufficient for a
  non-fatal signal.
- **Leave it deferred (status quo).** Rejected — the cross-workflow review flagged this as the single
  genuine A advantage worth porting; delivering it non-fatally is cheap and adds real ops observability.

## Consequences

- `/ready`'s body now reports driver liveness (`checks["driver"]`) for operators/dashboards, without
  coupling API serving to driver health. `ready`/HTTP status semantics are **unchanged** (stores only).
- The driver's own container HEALTHCHECK (ADR 0051) is unchanged and remains the **authoritative** driver
  liveness gate; `/ready` is a convenience mirror.
- Compose adds one named volume (driver rw, api ro). No migration; no schema change; not person-affecting.

## Upgrade trigger
If a **fatal, durable, cross-service** driver-liveness signal is ever needed (e.g. an external probe must
treat a stalled driver as a hard failure, or there is no shared filesystem between api and driver), move
the heartbeat to **Postgres** (A's model: a `driver_heartbeat` row read in `/ready`) — at the cost of a
migration. Until then, the file + shared RO volume + non-fatal `/ready` field is the accepted design.

## Reversibility
Reversible (readiness wiring + one compose volume). Reversal cost: low — drop the driver probe + the
volume mount. Revisit trigger: the upgrade trigger above.
