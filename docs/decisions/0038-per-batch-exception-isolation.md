# 0038 — Per-stage exception isolation in `_resolve_batch` (audit B-2)

- **Status:** accepted (implemented 2026-06-23)
- **Date:** 2026-06-23
- **Addresses:** `docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md` finding **B-2**
- **Touches:** `resolution/pipeline.py` (`_resolve_batch`, new `_quarantine`/`_summarize` helpers). Builds on [0027](0027-ingest-windowed-bounded-deadletter.md) (the ingest dead-letter pattern), [0026](0026-batch-first-resolution.md) (bounded drain), [0036](0036-deterministic-canonical-id.md) (idempotent re-resolution).

## Context

Audit finding **B-2** — the **third recurrence** of one failure mode: a single bad input at some
stage of `_resolve_batch` raises, the batch never commits, the rows stay `pending`, and the
bounded drain re-loads + re-fails them **forever** (the tenant's whole queue wedges behind one
poison input). Prior instances: id-less rows looping the drain (closed by the safety sweep), the
eponymous-namesake over-merge crash (`InvalidData` on incompatible schemas), and the
all-no-name window (`score_pairs` → `SplinkException`). Each surfaced at *whatever stage was not
wrapped*. The fix must cover **every** stage so it cannot recur at a fourth call site.

`_resolve_batch` had **no isolation**: `make_entity` (`:202`), `score_pairs` (`:205`),
`cluster_and_merge`, and `write_entities` all ran unguarded, so any raise propagated out of
`resolve_pending`, the per-batch commit was skipped, and the rows were re-loaded next tick.

## Decision

Isolate **every stage** of `_resolve_batch` at the right **granularity**, and on a poison input
**quarantine** (status `invalid` + an `ingest_dead_letter` row carrying `source_record`,
mirroring the ingest land/map pattern) so the bounded drain **always terminates**. The unifying
invariant: *a poison input — at any stage, row or batch — leaves the `pending` set and is recorded
for replay; it never wedges the drain.*

| Stage | Granularity | On poison input | Dead-letter `stage` |
|---|---|---|---|
| **construct** (`make_entity`) | **row** | quarantine the one bad row; good rows proceed | `resolve-row` |
| **score + cluster** (`score_pairs`, `cluster_and_merge`) | **batch** | quarantine the whole constructed window (nothing written/audited yet — no partial state) | `resolve-batch` |
| **merge** (`_merge_entities`) | member | already skips a schema-incompatible member (ADR 0035); the batch wrap covers any other failure | (`resolve-batch`) |
| **promote/write** (`validate_or_raise` → `write_entities`) | **entity** | a merged canonical that fails FtM validation is quarantined **before** the write | `resolve-write` |
| **safety sweep** (unclustered / id-less) | row | quarantine (now also dead-lettered, not just `invalid`+log) | `resolve-noid` |

Quarantine writes go in the same session the caller commits, so a quarantined construct/score
batch commits cleanly (those paths return before any graph write).

### Two deliberate judgment calls

1. **The write stage quarantines *poison*, not *infra*.** A merged canonical that is not a valid
   FtM entity is caught by `validate_or_raise` **before** `write_entities` and quarantined
   (`resolve-write`). But a genuine `write_entities`/Neo4j failure is **left to propagate** — the
   driver's per-tenant handler logs it, the rows stay `pending`, and the next tick retries
   **idempotently** (deterministic canonical id, ADR 0036). Rationale: a transient Neo4j blip must
   **not** dead-letter a whole batch of valid data; only *poison input* is quarantined, and
   "poison input" at the write stage means an invalid entity (caught pre-write), not an outage.
   Residual: a deterministic ftmg-write failure that `validate_or_raise` (id + schema + parse)
   does not catch would still wedge — a narrow case, noted.

2. **An all-no-name batch is quarantined, not written as singletons.** Per B-2's spec
   ("quarantine the unscoreable set"), an unscoreable window is dead-lettered (`resolve-batch`,
   replayable) rather than resolved as singleton nodes. This is the conservative choice (it does
   not *assert* "no merges" for entities we could not score on country/wikidata). Trade-off: valid
   no-name entities (e.g. standalone `Sanction`/`Address`) in such a window do not reach the graph
   until replayed. All-no-name *windows* are an edge case (a mixed batch never trips the Splink
   name-blocking dtype path). **Resolving no-name entities as singletons instead of quarantining is
   a clean future refinement** (it would also fix the underlying `score_pairs` dtype handling) —
   deferred to keep B-2 scoped to isolation.

No migration: reuses `ingest_dead_letter` with new `stage` values (`resolve-row` / `resolve-batch`
/ `resolve-write` / `resolve-noid`, all ≤ 16 chars). The stage isolation uses a broad
`except Exception` (containment), but every catch **logs + dead-letters** — observable, never
silent.

## Verification (PROGRESS, not just "exception caught")

`tests/integration/test_b2_poison_batch_isolation.py` proves **termination**, the property that
distinguishes containment from "didn't propagate":
- **Row-level** — a batch with one un-parseable row (`schema: "NotARealSchema"`) + two good
  duplicates: the good rows resolve into one canonical, the poison row is dead-lettered
  (`resolve-row`, with its `source_record`), **`queue_pending == 0`**, and a re-run is a clean
  no-op (the poison row is never re-loaded).
- **Batch-level** — a window of only no-name `Sanction` entities (unscoreable): the set is
  quarantined (`resolve-batch`, each with `source_record`), nothing is written, **`queue_pending
  == 0`**, re-run is a no-op.

Pre-fix these wedge: `make_entity` raises `InvalidData` on a bad schema and `score_pairs` raises
`SplinkException` on an all-no-name window (both verified), so the unguarded `_resolve_batch`
propagated and the rows were never drained. The `queue_pending == 0` + re-run-no-op assertions are
the containment proof. (Integration-gated → CI; no Docker locally.) The existing id-less sweep
test (`test_resolution_batching.py`) and the happy-path resolution/referent tests are unaffected.

## Scope

**B-2 only.** No B-3 / H-5 (the over-merge cluster), no Gate B/C/S4, no G3. Judgement *consumption*
(`cluster_and_merge`) is unchanged beyond being wrapped, so H-1 is not affected; propagated write
failures compose with B-1's idempotent retry.
