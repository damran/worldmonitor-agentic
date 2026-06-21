# ADR 0027 — run_ingest: windowed commits, bounded collection, dead-letter path

> Status: **LOCKED** · June 2026 · Closes audit gap **G8**. Same windowed-commit discipline as
> [ADR 0026](0026-batch-first-resolution.md). Format: Context → Decision → Status → Consequences.

## Context
Phase 1 `run_ingest` (`runner/ingest.py`) drained a connector's `collect()` to exhaustion and committed
once at the end (audit gap **G8**). Three problems block the first non-bulk connector:

1. A `STREAM`/`RestApi` connector whose `collect()` never returns never commits and grows memory without
   bound.
2. A single raising `map()` (or landing failure) aborts the entire run — there is no dead-letter path, so
   one hostile/malformed record loses a whole batch with no audit.
3. There are no progress commits mid-ingest, so a failure late in a long import loses everything.

## Decision
`run_ingest` drives `collect()` **bounded and windowed**, and dead-letters per-record failures. Per an
explicit user decision, with the defaults below.

- **Windowed commits** — land/map/enqueue in windows of `INGEST_COMMIT_EVERY` (default **1000**) and
  commit each window. Progress persists; a mid-run failure keeps the completed windows.
- **Bounded collection** — stop after `INGEST_TIMEOUT_SECONDS` wall-clock seconds (default **1800**;
  `<= 0` disables) or `INGEST_MAX_RECORDS` records (default **None** = no cap; windowed commits already
  bound the blast radius). Per-run overrides via `run_ingest(..., commit_every=, timeout=, max_records=)`.
- **Dead-letter** — a record that fails to land or map is written to the new `ingest_dead_letter` table
  (`tenant_id`, `connector_id`, `source_key`, `source_record`, `stage`, `error`, `created_at`) and
  skipped. `stage="map"` carries the `s3://` landing pointer (the raw landed first, so it is replayable);
  `stage="land"` has a null `source_record` (nothing landed). A `WARNING` is logged per dead-letter.
  `IngestStats` gains `dead_lettered`, `windows`, and `stopped_reason`
  (`"exhausted" | "max_records" | "timeout"`).

The **connector interface is unchanged** — `collect()` is only *consumed* with early-stop; no signature or
`Connector` ABC change.

## Scope — what is deliberately deferred
- The wall-clock deadline is **cooperative**: it is checked *between* records, so it bounds a `collect()`
  that yields continuously (the "never returns" stream case). A connector that blocks forever **inside a
  single `next()`** (e.g. a socket read with no timeout) is **not** interruptible in-process; a hard kill
  needs subprocess/thread isolation (`runner/subprocess.py` already provides this for `CliToolConnector`).
  That isolation + a long-running scheduler/driver is the **streaming driver's** job — **deferred to the
  ER-streaming gate**, not built here.
- This gate does not add a scheduler or change resolution; `resolve_pending` batching is ADR 0026.

## Status
**LOCKED.** Closes G8 for the bounded-import path. Defaults (1800s deadline, no record cap, 1000-record
windows) are tunable per deployment (`INGEST_*` env) and per run (args). Does not alter ADR 0021 (raw
lands before mapping — still true, and what makes a `map`-stage dead-letter replayable), ADR 0026
(resolution batching), or any guard ADR.

## Consequences
- ✅ A non-returning `collect()` can no longer hang the run (deadline + cap); a long import persists
  progress in windows; one bad record is dead-lettered, not fatal — the audit trail of failures is
  durable and tenant-scoped.
- ✅ Backward-compatible: a finite bulk import (OpenSanctions) collects well under the deadline with no
  cap, so behaviour is unchanged; the new `IngestStats` fields are additive.
- ⚠️ A `collect()` that blocks inside one `next()` is still unbounded in-process (see Scope) — covered
  only once the streaming driver runs connectors under hard (subprocess/thread) isolation.
- ⚠️ A systemic outage (e.g. the landing zone down for every record) is dead-lettered per-record rather
  than aborting; the `ingest_dead_letter` rows + WARNING logs surface the pattern for an operator. A
  DB-commit failure still aborts the run (loudly), preserving already-committed windows.
