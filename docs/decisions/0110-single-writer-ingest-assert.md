# 0110 — Single-writer ingest assert (WPI-3): fail-closed advisory lock on the SoR spine

- **Status:** ACCEPTED (2026-07-12)
- **Date:** 2026-07-12
- **human_fork:** false — a reversible, additive write-path-integrity guard (one advisory-lock call +
  one settings key); revert = drop the call. No data-shape lock-in, no schema/migration change, the
  projector is byte-frozen.
- **person_affecting:** false — the lock changes nothing about *what* merges, parks, resolves, or is
  erased; it only serializes *when* concurrent writers may commit to the spine. No individual-affecting
  outcome changes. Reversible + non-person-affecting → proceed-and-report (no cosign).

## Context

ADR 0100 D1 made the append-only log its own outbox by adding a monotonic, server-assigned
`seq BIGINT IDENTITY` to `statement` / `decision` / `context_claim`; the projector checkpoints on that
total order and reads rows `seq > watermark`. That design **stated, but did not enforce**, a
single-writer assumption (`0100-fold-engine-outbox-projector.md:56-61`):

> Under *concurrent* writers a lower `seq` can commit *after* the projector has advanced its watermark
> past it (the classic sequence-gap-under-concurrency hazard) … safe **now** … recorded as a **revisit
> trigger**.

The hazard is real and silent: writer A assigns `seq=5` (uncommitted), writer B assigns `seq=6` and
commits, the projector reads up to 6 and advances its watermark to 6, then A commits 5 — which the
incremental fold never reads again. Post-cutover (Gate 3b) the rebuild-from-log is the routine path, so a
dropped `seq` is **silent live-loss** exactly as ADR 0100 D1 warns.

Today two facts keep it safe but only by assumption, not assertion:
- The ingest **driver** serializes its own resolve passes with an in-process `threading.Lock`
  (`runner/driver.py:183` `_resolve_lock`) — but that lock is process-local; it does not serialize a
  resolve pass against any other writer.
- Single-node deploy (ADR 0042 D1) means there is one driver process today.

Before Gate 3b can retire the direct Neo4j write and trust rebuild-from-log, the single-writer
assumption must **fail loud** if it is ever violated, rather than corrupt the watermark silently.

## Decision

Take a **Postgres transaction-level advisory lock** on the SoR spine at the start of each promote
transaction; a second concurrent writer is **refused (fail-closed)**, never allowed to interleave its
`seq`-assign/commit window with the holder's.

**Fork — options considered, `(a)` chosen:**

- **(a) `pg_try_advisory_xact_lock` per promote transaction. ← CHOSEN.** A new
  `resolution/spine_lock.py` exposes `acquire_spine_writer_lock(session)`; `resolve_pending` calls it at
  the top of each batch transaction, before any spine INSERT. If the lock is already held by another
  session the call returns false → raise `ConcurrentSpineWriterError` (fail-closed). The lock is
  **transaction-scoped**: Postgres auto-releases it at each per-batch `COMMIT`/`ROLLBACK`, so there is no
  explicit unlock, no leak on exception, and no cross-batch holding. It is **Postgres-only**: a no-op on
  any other dialect (SQLite tests are single-connection — no concurrency hazard to guard). Keeps
  `projector.py` byte-frozen.
- **(b) min-in-flight-`seq` watermark in the projector** (ADR 0100 D1's named HA path) — the projector
  only advances its watermark past `seq` values that are provably committed. More machinery, real HA
  correctness. **Deferred as the revisit path** when multi-writer / HA is actually built.
- **(c) a bare runtime assert on the single-node `TaskRun` lease** — weakest; documents the assumption
  but does not enforce it against a second process.

`(a)` is the minimum that turns the ADR-0100 assumption into an enforced, loud invariant with the least
machinery and zero schema change; `(b)` is strictly more capable and is the recorded upgrade path.

**Placement note.** The lock lives inside `resolve_pending` (not the driver) because `pg_try_advisory_
xact_lock` is transaction-scoped and `resolve_pending` commits **per batch** (ADR 0026); wrapping each
batch transaction is the only placement where a transaction-scoped lock both covers the assign→commit
window and auto-releases cleanly. It composes with — does not replace — the driver's in-process
`_resolve_lock` (which still prevents overlapping passes within the one process).

**One setting** (`settings.py`): `spine_writer_lock_key: int` (a fixed default constant) so an operator
can move the advisory-lock key if it ever collides with another advisory-lock user. The lock is **not**
otherwise operator-toggleable — seq-gap-safety is a data-integrity property (like provenance stamping,
ADR 0109), not a person-affecting review guard, so it is always enforced on Postgres.

**Scope this slice does NOT cover (named, not hidden).** The invariant is asserted at the **resolve/
ingest** promote point (`resolve_pending`). The **sign-off** promote point (`resolution/signoff.py`
`approve()`) is a second spine writer running in the API process; adopting the same
`acquire_spine_writer_lock` there is a small follow-up carried by the WPI-1 slice (which already edits
`signoff.py`). Until then, sign-off and resolve are serialized in practice by the single-node deploy;
the residual is recorded here, not silently assumed away.

## Consequences

- A concurrency violation now **fails loud** (`ConcurrentSpineWriterError`) instead of silently dropping
  a `seq` from the incremental fold. The single-node production path is unchanged: with one writer the
  `pg_try_advisory_xact_lock` always succeeds.
- No schema change, no migration, `projector.py` byte-frozen. The fold's watermark logic is untouched;
  the lock only guarantees the precondition the watermark already assumes.
- SQLite / test dialects are unaffected (no-op) — existing single-connection tests keep passing.

## Reversibility

Fully reversible: delete `acquire_spine_writer_lock`, its one call site, and the settings key, and the
behaviour returns to the (assumed-safe) single-node path.
**Revisit trigger:** the first time the platform runs **more than one spine writer** (HA driver, a second
ingest node, or a sign-off writer concurrent with a resolve pass under load) — at that point either adopt
the lock at every promote point (cheap) or graduate to option **(b)**, the min-in-flight-`seq` watermark,
for true multi-writer correctness.
