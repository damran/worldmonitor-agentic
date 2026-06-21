# ADR 0026 — Resolution: batch-first, drain the queue in bounded windows

> Status: **LOCKED** · June 2026 · **Supersedes [ADR 0019](0019-batch-vs-streaming-resolution.md)**
> (which was OPEN). Format: Context → Decision → Status → Consequences.

## Context
ADR 0019 left the resolution model OPEN between (a) periodic re-batch and (b) incremental resolution
against the already-resolved graph. Phase 1 `resolve_pending` loaded **all** pending `ErQueueItem`s for a
tenant in one shot, ran Splink `dedupe_only` over the whole set, and committed once at the end. For a
one-shot bulk import that is correct, but it is unbounded: a large backlog loads entirely into memory and
pays an all-pairs cost in a single call (audit gap **G9**).

Per an explicit user decision, ADR 0019 is resolved **batch-first**: keep the deterministic batch model
but make it operate in bounded windows. **Incremental resolution (0019 direction b) is deferred to the
ER-streaming gate** — that is the locked decision and is not to be relitigated here.

## Decision
`resolve_pending` drains the pending queue in **bounded batches**:

- A new setting `RESOLVE_BATCH_SIZE` (`settings.py`, env `RESOLVE_BATCH_SIZE`, `gt=0`, **default 1000**)
  caps the window. A per-call `batch_size` argument overrides it.
- The driver loop loads at most `batch_size` pending rows (ordered by `created_at, id`), resolves that
  window via `_resolve_batch` (score → cluster → guard → referent-rewrite → write), **commits**, and
  repeats until no `pending` rows remain. Memory and per-pass cost are bounded by `batch_size`.
- `_resolve_batch` holds the existing per-cluster logic **unchanged** (guard action by `MERGE_GUARD_MODE`,
  audit/alert trail, referent rewriting from ADR 0025). `ResolveStats` gains a `batches` count.
- **Drain safety:** every queue row in a batch is transitioned out of `pending` (`resolved` or
  `pending_review`), and **all rows sharing one FtM id move together**, so a drained batch can never be
  re-loaded — the loop always terminates.

## Scope — what is deliberately deferred
Dedup is **within a batch**. Two candidates that should merge but fall into different batches are **not**
merged, and referent rewriting (ADR 0025) is likewise per-batch. Closing that gap — scoring each new
batch against the **already-resolved** canonical entities (blocking against existing clusters) and
rewriting already-persisted edges — is **incremental resolution** (ADR 0019 direction b) and is owed at
the **ER-streaming gate**. Two adjacent, separately-gated items remain open and are *not* addressed here:
`run_ingest` windowing for a never-returning `collect()` (gap **G8**) and a long-running scheduler/driver.

## Status
**LOCKED (batch-first).** Supersedes ADR 0019. The default `batch_size=1000` bounds resources while
keeping curated bulk imports (e.g. OpenSanctions, which is already canonical within a dataset) effectively
whole-queue. Incremental ER is the production answer and is deferred — not cancelled — to the ER-streaming
gate. This ADR does not alter the catastrophic-merge guard (ADR 0024) or in-batch referent rewriting
(ADR 0025).

## Consequences
- ✅ Bounded memory + per-pass cost; a huge backlog no longer loads at once. Per-batch commit gives
  progress, resumability, and partial-failure containment — the "windowed commits" the Phase 1 audit
  called for.
- ✅ Backward-compatible: small inputs fit one batch, so existing behaviour and tests are unchanged; the
  `batches` field is additive.
- ⚠️ **Accepted limitation:** cross-batch duplicates are not merged in v0 (proven in
  `tests/integration/test_resolution_batching.py::test_cross_batch_duplicates_are_not_merged_v0_limitation`).
  For data with >`batch_size` pending candidates, intra-dataset / cross-source duplicates that span batch
  boundaries survive as distinct nodes until incremental ER lands. De-dupe-before-counting still holds
  within a batch; full-coverage dedup is the deferred incremental work.
- ⚠️ Referent rewriting is per-batch: an edge whose endpoint was merged in a *different* batch is not
  rewritten (same deferral as above).
