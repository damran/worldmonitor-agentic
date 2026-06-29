# ADR 0084 — Online-migration safety (audit finding M-5)

- **Status:** Accepted
- **Date:** 2026-06-29
- **Gate:** Stage-4 MEDIUM/LOW sweep — audit finding **M-5** (online-migration safety).
- **Addresses:** `docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md:181` — "Migrations are
  unguarded for online execution; no tested rollback."
- **Classify (reversibility):** **reversible** — reversal = delete `db/_migration_guard.py`,
  restore `env.py::_run` to the prior one-liner, remove the two settings, delete the tests and
  runbook. No data change, no migration, nothing public-facing.  Revisit trigger: a future migration
  writer needs `CREATE INDEX CONCURRENTLY` (see Deferred §D1 below).

## Context

`0002_runway.py` calls `op.add_column` + `op.create_unique_constraint` on `er_queue_item` inside a
single transaction (`migrations/env.py`).  Both operations take an `ACCESS EXCLUSIVE` lock on the
table.  Against a multi-million-row queue:

- `add_column` on a nullable column is fast (catalog update only, no scan) on Postgres ≥ 11.
- `create_unique_constraint` (backed by a regular `CREATE UNIQUE INDEX`) holds `ACCESS EXCLUSIVE`
  for the duration of the full-table index build — this is the expensive lock.

Without a `lock_timeout`, the migration waits indefinitely for the lock, blocking every concurrent
`INSERT`/`UPDATE` on `er_queue_item` — the driver's enqueue path.  In a worst case a long-running
query holds a conflicting lock; the migration queues behind it; and every new query queues behind
the migration, stalling the entire table.

No `lock_timeout` was set, and no runbook existed for either the safe migration path or rollback.

## Decision

### D1 — Dialect-aware `SET LOCAL lock_timeout` in `env.py::_run`

A new pure helper `worldmonitor.db._migration_guard.apply_migration_timeouts(connection)` is
called **inside `context.begin_transaction()`, before `context.run_migrations()`** in `_run`.

It issues `SET LOCAL lock_timeout = '<N>ms'` on Postgres connections (and optionally
`SET LOCAL statement_timeout = '<M>ms'`).  `SET LOCAL` scopes both GUCs to the current
migration transaction — they revert to their session-level values on commit/rollback and cannot
bleed onto the app connection shared via `config.attributes["connection"]`.

**Dialect-aware (non-negotiable safety invariant):** `connection.dialect.name != "postgresql"` →
the function returns immediately without executing anything.  SQLite (used by the unit suite)
would raise a syntax error on `SET LOCAL`; silently skipping it is the correct behaviour.  Other
non-Postgres dialects (MSSQL, MySQL, DuckDB) are treated identically.

**Two-path coverage:** `env.py`'s `run()` has two paths to `_run`:

1. **Shared connection** (`config.attributes["connection"]`) — the app's `migrate_to_head` call;
   `apply_migration_timeouts` is called on the app's connection *inside* the migration transaction
   (`SET LOCAL` ensures no session bleed after the transaction ends).
2. **Fresh engine** (the alembic CLI, `create_engine(_url(), poolclass=pool.NullPool)`) — a new
   connection per migration run; `SET LOCAL` vs plain `SET` is equivalent here, but `SET LOCAL`
   is used for consistency.

### D2 — Two new settings (`Settings`)

Both default to the **safe** posture:

| Setting | Default | Meaning |
|---|---|---|
| `migration_lock_timeout_ms` | `3000` | Abort if the DDL lock cannot be acquired within 3 s. `0` = opt-out (Postgres default: no timeout). |
| `migration_statement_timeout_ms` | `0` | Off by default (some migrations, e.g. backfills, are legitimately long). |

The `migration_lock_timeout_ms=3000` default catches the "online without a maintenance window"
footgun: the migration aborts immediately with a `LockNotAvailable` error (no waiting, no queue
buildup) rather than silently stalling the enqueue path.  When running the **migrate-while-stopped**
procedure (`docs/runbooks/migrations.md`), operators may set `MIGRATION_LOCK_TIMEOUT_MS=0` to
disable the guard (no live traffic competing → no lock contention).

### D3 — Runbook `docs/runbooks/migrations.md`

Documents:

1. The safe **migrate-while-stopped** procedure for the single-node deployment (D1, ADR 0042).
2. Rollback procedure (`alembic downgrade -1` + restore from backup if needed).
3. What `lock_timeout` does and when to use `MIGRATION_LOCK_TIMEOUT_MS=0`.
4. **Online-safe patterns for FUTURE migrations** (see Deferred below).

## Deferred (explicit follow-ups; record here so the next migration author sees them)

### D1-DEFERRED — `transaction_per_migration=True` + `CREATE INDEX CONCURRENTLY`

`CREATE INDEX CONCURRENTLY` is the correct long-term fix for index builds on large tables: it
acquires only a weaker `ShareUpdateExclusiveLock` and runs the build without blocking writers.
**But `CONCURRENTLY` cannot run inside a transaction** — it requires `autocommit`.  Alembic
supports this via `transaction_per_migration=True` (one transaction per migration script, with
`context.run_migrations()` called per revision instead of once for the batch) or by using
`op.execute("CREATE INDEX CONCURRENTLY ...")` with `execute_if(dialect="postgresql")` and
explicitly disabling the outer transaction.

This restructuring of `env.py` is a larger change (affects every migration's DDL semantics) and
is deferred to the **first migration that actually writes a large-table index build**.  The
runbook (`docs/runbooks/migrations.md` §"Online-safe patterns") calls this out explicitly so the
next migration author doesn't have to rediscover it.

`ADD CONSTRAINT ... NOT VALID` + `VALIDATE CONSTRAINT` (for large-table constraint additions) has
the same `CONCURRENTLY`-like deferral path and is noted in the runbook for the same reason.

### D2-DEFERRED — Retrofitting `0002_runway.py`

`0002_runway.py` is already applied on every live database.  Applied migrations do not re-run,
so retrofitting it (changing `create_unique_constraint` to a `NOT VALID` + `VALIDATE` sequence)
would have no effect in production.  It is left as-is.

## Tests

- **Unit** (`tests/unit/test_migration_lock_timeout.py`, 15 tests):
  - SQLite and other non-Postgres dialects: `connection.execute` never called.
  - Postgres + `migration_lock_timeout_ms=3000`: `SET LOCAL lock_timeout = '3000ms'` executed.
  - Postgres + `migration_lock_timeout_ms=0`: no `SET LOCAL` executed (opt-out).
  - Postgres + `migration_statement_timeout_ms=60000`: `SET LOCAL statement_timeout = '60000ms'`.
  - Postgres + `migration_statement_timeout_ms=0`: no statement_timeout SET.
  - Both positive: two calls, in order.
  - `Settings` defaults + validation (negative rejected via pydantic `ge=0`).

- **Integration** (`tests/integration/test_migrations.py`, 4 new tests + 6 existing):
  - `test_lock_timeout_applied_on_real_postgres_connection`: `SHOW lock_timeout` returns `'2500ms'`
    inside the transaction after `apply_migration_timeouts`.
  - `test_lock_timeout_reverts_after_transaction`: `SET LOCAL` reverts after commit (no bleed).
  - `test_lock_timeout_zero_leaves_postgres_default`: `migration_lock_timeout_ms=0` leaves
    `lock_timeout` unchanged.
  - `test_migrate_to_head_succeeds_with_lock_timeout_configured`: full `migrate_to_head` succeeds
    with the default `migration_lock_timeout_ms=3000` (no lock contention in isolation).
  - All 6 existing migration tests (`fresh ≡ create_all ≡ adopted; alembic check; partial-restore
    refused`) pass unchanged.

## Consequences

- A migration that cannot acquire its DDL lock within 3 s aborts with `LockNotAvailable` (Postgres
  error code `55P03`) rather than stalling the enqueue path indefinitely.  The operator reruns the
  migration during a maintenance window (or uses `MIGRATION_LOCK_TIMEOUT_MS=0` when stopped).
- No schema change; no migration; no person-affecting ER/merge/guard/score path touched.
- The default-off `migration_statement_timeout_ms=0` is available for future use when a long
  backfill needs an explicit wall-clock cap.
- `env.py` is unchanged in shape — `run()` at module level, same two paths, same transaction
  model.  Only `_run` gains a one-line call before `context.run_migrations()`.

## Reversibility

**Reversible.** Reversal: delete `db/_migration_guard.py`; restore `env.py::_run` to the prior
two-line form; remove the two settings from `settings.py`; delete `tests/unit/test_migration_lock_timeout.py`; remove the 4 new tests from `tests/integration/test_migrations.py`; delete
`docs/runbooks/migrations.md`. No data change, no migration, nothing public-facing.

**Revisit trigger:** the first migration that writes a large-table index build → implement
`transaction_per_migration=True` and switch to `CREATE INDEX CONCURRENTLY` (see D1-DEFERRED).
