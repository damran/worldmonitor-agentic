# ADR 0019 — Entity resolution: whole-queue batch now, streaming/incremental OPEN

> Status: **OPEN** · June 2026 · Must be resolved with the user before the first Phase 2 STREAM connector.

## Context
Phase 1 resolution (`resolution/pipeline.py:37-89`) is **whole-queue batch**: `resolve_pending` loads
*all* pending `ErQueueItem`s for a tenant, runs Splink `dedupe_only` over the entire set, clusters,
applies the guard, and writes the auto-promoted canonical entities. This is correct and deterministic for
a one-shot bulk import (OpenSanctions). Phase 2 introduces `StreamConnector`/`RestApiConnector` that
deliver a continuous trickle of small candidate batches.

## Decision
**OPEN.** Two viable directions, not yet chosen:
- **(a) Periodic re-batch** — keep `resolve_pending` and run it on a schedule over the accumulated queue.
  Simple, reuses all existing code, but is **O(n²)** as the queue grows and re-scores already-resolved
  records every tick.
- **(b) Incremental resolution** — score each new candidate batch against the **already-resolved graph**
  (blocking against existing canonical entities), maintaining clusters incrementally. Far more scalable,
  but requires new incremental-clustering machinery and careful interaction with the merge audit trail
  and referent-rewriting (ADR 0023).

## Status
**OPEN** — resolve with the user before building the first STREAM connector. Record the outcome by
superseding this ADR. Until then, stream connectors are blocked from production resolution.

## Consequences
- Choosing (a) ships fast but caps throughput and will need replacement; choosing (b) is the real answer
  but is net-new work that touches the merge guard and edge rewriting.
- Either way, `run_ingest` must also stop being a one-shot bounded run (audit gap **G8**): a stream's
  `collect()` never returns, so it needs windowed commits + a long-running driver/scheduler.
- This is the **single biggest Phase 2 contract stress** identified in the audit (Q3).
