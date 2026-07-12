# Gate WPI-3 — single-writer ingest assert (ADR 0110)

> Write-path-integrity slice 1 of 3 (F1 pre-cutover). Non-person-affecting, reversible, additive.
> Consult item §7-7 (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md`). Owner-mapped in
> `docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md:128-132`.

## Why

ADR 0100 D1 added a `seq BIGINT IDENTITY` outbox column and a projector watermark that reads
`seq > watermark`, and **assumed** a single writer so `seq` commit-order == assignment-order. Under two
concurrent writers a lower `seq` can commit *after* the watermark has advanced past it → the incremental
fold silently drops it → post-3b silent live-loss. This gate turns that assumption into an **enforced,
fail-loud invariant**.

## Invariant

**`INV-SINGLE-WRITER`** — at most one writer holds the SoR-spine promote transaction at a time. A second
concurrent writer that tries to enter a promote transaction while another holds it is **refused
(fail-closed)** with `ConcurrentSpineWriterError`, never allowed to interleave its `seq`-assign/commit
window. Consequence asserted downstream: under this discipline the committed `seq` values the projector
reads form a **contiguous, gap-free** consumption order (no committed row is skipped by the watermark).

## Mechanism (ADR 0110 option (a))

- New module `src/worldmonitor/resolution/spine_lock.py`:
  - `class ConcurrentSpineWriterError(RuntimeError)` — raised when the lock is refused.
  - `acquire_spine_writer_lock(session, *, key: int | None = None) -> None` — Postgres-only. Executes
    `SELECT pg_try_advisory_xact_lock(:key)`; on `False` raises `ConcurrentSpineWriterError`. On any
    non-Postgres dialect it is a **no-op** (return immediately) — SQLite tests are single-connection and
    have no concurrency hazard. `key` defaults to `settings.spine_writer_lock_key`.
- `resolution/pipeline.py::resolve_pending` calls `acquire_spine_writer_lock(session)` **at the top of
  each batch iteration, before any spine INSERT**, inside the same transaction that the per-batch
  `session.commit()` closes. Because `pg_try_advisory_xact_lock` is transaction-scoped, the lock is
  auto-released at each commit/rollback — no explicit unlock, no leak, no cross-batch holding.
- `settings.py`: `spine_writer_lock_key: int = Field(default=<fixed constant>)`. Not otherwise
  operator-toggleable (data-integrity property, cf. ADR 0109 provenance-not-toggleable).

## Acceptance criteria

1. **PRIMARY integration test** (`tests/integration/test_single_writer_lock.py`, Postgres via the
   `postgres_dsn` fixture): two connections race the spine. Connection A holds
   `pg_try_advisory_xact_lock(key)` in an open transaction; a second writer calling
   `acquire_spine_writer_lock` (and/or `resolve_pending`) is **refused** with
   `ConcurrentSpineWriterError`. After A commits (releasing the lock), the second writer succeeds and the
   resulting `statement.seq` values are contiguous (no gap). RED before the builder (the module/exception
   does not exist yet).
2. **Mandatory `@given` property test** (`tests/property/test_prop_single_writer_seq_gap.py`): over
   synthetic single-writer batch schedules, the projector's incremental watermark folds **every**
   committed `seq` exactly once with **no gap** (positive), and a metamorphic negative shows that an
   out-of-order commit — the exact interleaving the lock forbids — *would* leave a gap (so the property
   depends on the single-writer discipline, not on luck). Exercises the real projector watermark read
   where possible. RED before the builder.
3. Full `pytest -m "not integration"` + local `-m integration` green; `ruff format --check .` repo-wide
   clean; `ruff check` clean; `pyright` clean.
4. The **checker independently reproduces** `INV-SINGLE-WRITER` against the diff.
5. `projector.py` diff is **empty**. No change to any merge/park/ER/erasure/sign-off outcome. No schema
   change, no migration.

## FROZEN (byte-unchanged this gate)

`resolution/projector.py`, `resolution/divergence.py`, `resolution/statements.py`,
`resolution/signoff.py`, `resolution/canonical.py`, `resolution/merge.py`, `resolution/guard.py`,
`resolution/erasure_scrub.py`, `erasure.py`, `db/models.py` (no schema change — the `seq` columns already
exist), all `db/migrations/**` (no new migration), `graph/**`, `mcp/**`, `authz/**`, `api/**`, `llm/**`,
`runner/driver.py` (the in-process `_resolve_lock` is untouched; the advisory lock composes with it).

## Editable this gate

`docs/decisions/0110-single-writer-ingest-assert.md`, `docs/decisions/README.md` (index regen),
`docs/reviews/GATE_WPI3_SINGLE_WRITER_SPEC.md`, `.claude/gate.scope`,
`src/worldmonitor/resolution/spine_lock.py` (NEW), `src/worldmonitor/resolution/pipeline.py` (the one
lock call at the top of the drain loop), `src/worldmonitor/settings.py` (one key),
`tests/integration/test_single_writer_lock.py` (NEW), `tests/property/test_prop_single_writer_seq_gap.py`
(NEW).
