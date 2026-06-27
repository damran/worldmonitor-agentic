# ADR 0019 — Entity resolution: periodic re-batch for streaming (incremental deferred)

> Status: **ACCEPTED (2026-06-28)** — option (a) periodic re-batch, decided with the user during Phase-2
> forward planning. Supersedes the OPEN state. (Was: June 2026, OPEN pending the first STREAM connector.)

## Context
Phase 1 resolution (`resolution/pipeline.py:37-89`) is **whole-queue batch**: `resolve_pending` loads
*all* pending `ErQueueItem`s for a tenant, runs Splink `dedupe_only` over the entire set, clusters,
applies the guard, and writes the auto-promoted canonical entities. This is correct and deterministic for
a one-shot bulk import (OpenSanctions). Phase 2 introduces `StreamConnector`/`RestApiConnector` that
deliver a continuous trickle of small candidate batches.

## Decision
**ACCEPTED — (a) periodic re-batch.** A Phase-2 `StreamConnector`/`RestApiConnector` lands its candidate
records into the existing `er_queue`; resolution stays the proven bounded-window `resolve_pending`
(ADR 0026) on its cadence — **no new ER machinery**, and the merge guard / audit trail / referent
rewriting are untouched. New records simply wait one batch cadence to resolve.

Considered and **deferred**:
- **(b) Incremental resolution** — score each new batch against the already-resolved graph, maintaining
  clusters incrementally. Far more scalable for high-volume streaming, but is net-new incremental-cluster
  machinery with non-trivial interaction with the merge audit trail + referent-rewriting (ADR 0023).
  Deferred until stream volume actually demands it (see the upgrade trigger).

**Rationale:** option (a) is the lowest-regret, fully-reversible choice — it reuses the proven resolver
and ships the StreamConnector without destabilizing ER correctness. The O(n²)-per-window cost is bounded
by `RESOLVE_BATCH_SIZE` windows and acceptable until stream volume grows.

## Reversibility / upgrade trigger
Reversible: (b) can be swapped in **behind the same `er_queue`** without changing connectors. **Build (b)
when** a stream's sustained volume makes the per-cadence re-batch latency or cost unacceptable (e.g. the
queue's resolve cadence can't keep up with ingest, or re-scoring dominates runtime) — i.e. when the H-8
"resolve falls behind ingest" signal fires. Until then, (a) is the accepted model.

## Status
**ACCEPTED (2026-06-28)** — option (a). Stream connectors may use production resolution via the queue +
periodic re-batch. Revisit per the upgrade trigger above.

## Consequences
- Choosing (a) ships fast but caps throughput and will need replacement; choosing (b) is the real answer
  but is net-new work that touches the merge guard and edge rewriting.
- Either way, `run_ingest` must also stop being a one-shot bounded run (audit gap **G8**): a stream's
  `collect()` never returns, so it needs windowed commits + a long-running driver/scheduler.
- This is the **single biggest Phase 2 contract stress** identified in the audit (Q3).
