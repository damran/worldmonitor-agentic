# 0056 — Migration adoption requires a complete schema (no blind-stamp on one column)

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** Phase-B #3 (`gate/migration-adoption-schema-check`) — a focused fix off `master`.
- **Addresses:** the partial-restore hazard at `db/engine.py:74` (`migrate_to_head`), confirmed
  file:line in the cross-workflow Round-2 cross-examination.

## Context — the bug

`migrate_to_head` (ADR 0030) adopts a pre-Alembic database by inferring its schema state. The
"already at the current schema" branch decides that from a **single column's presence**:

```python
elif "entity_id" in {c["name"] for c in inspector.get_columns("er_queue_item")}:
    command.stamp(config, "head")   # assume current; no DDL
```

A **partially-restored** database — `er_queue_item` present with `entity_id`, but later-migration
tables missing (e.g. `sign_off`, `resolver_judgement`, `canonical_id_ledger`) and no
`alembic_version` — takes this branch and is **stamped at head while incomplete**. Alembic then
believes the DB is fully migrated; the missing tables are never created, and the first runtime query
against them fails. The H-1 human-decision durability (`resolver_judgement`/`sign_off`) and the
no-un-merge `canonical_id_ledger` could silently be absent on a DB the system reports as current.

## Decision

**Fail closed on an incomplete schema.** Replace the single-`entity_id` proxy with a **full-schema
completeness check** against `Base.metadata` (the ORM models, which equal head per ADR 0030's drift
guard):

- Compute the expected `{table: {columns}}` from `Base.metadata`.
- In the adoption branch (no `alembic_version`, `er_queue_item` present), the DB is "already at head"
  **iff every expected table AND every expected column is present.** If so → `stamp(head)` (unchanged
  behaviour for a genuinely-current pre-Alembic DB).
- If `er_queue_item` carries `entity_id` (so it is post-runway, not pre-runway) **but the schema is
  incomplete**, raise `SchemaIncompleteError` naming the missing tables/columns — refuse to stamp a
  partial restore as current. A partial restore is an operator error that must halt loud, not be
  silently "adopted."
- The pre-runway branch (no `entity_id`) is unchanged: stamp baseline + upgrade.

## Alternatives considered

- **`create_all` to fill the missing tables, then stamp head.** Converges instead of refusing, but
  silently auto-heals what is almost always a botched restore — masking partial data loss (the very
  human-decision tables that matter). The drift guard already proves `create_all == head`, so this is
  *possible*, but refusing is safer. Documented as the reversal target.
- **Keep the `entity_id` proxy, add only a table-count check.** Misses missing *columns* from later
  migrations (the plan's stated failing case). Rejected — check tables AND columns.

## Consequences

- A partial/inconsistent restore is caught at adoption with a clear, actionable error instead of being
  marked fully-migrated and failing opaquely later.
- A genuinely-current pre-Alembic DB still adopts via `stamp(head)` (behaviour unchanged); a fresh
  install and a pre-runway DB are unaffected (`test_migrations.py` stays green).
- **No schema change / no new migration** — read-only inspection + a guard. Not person-affecting
  (DB-bootstrap integrity, no ER/score path). `human_fork: false`.

## Reversibility

Reversible (bootstrap policy). Reversal cost: low. **Revisit trigger:** if an operational need emerges
to auto-converge a partial restore (rather than halt), switch the raise to `create_all` + `stamp(head)`
(the documented alternative), which the ADR-0030 drift guard makes safe.
