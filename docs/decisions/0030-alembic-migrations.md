# ADR 0030 — Alembic schema migrations (replace create_all)

> Status: **LOCKED** · June 2026 · Format: Context → Decision → Status → Consequences.

## Context
Schema was managed by `Base.metadata.create_all`, which **creates missing tables but never ALTERs
existing ones**. A deployment whose `er_queue_item` predates the runway therefore never gained the A6
dedup column/constraint when the code upgraded — the idempotent-enqueue `ON CONFLICT` would target a
constraint that doesn't exist. A code review flagged this "pre-existing table" case as genuinely broken.
The platform needs versioned migrations so fresh installs and existing deployments converge on one schema.

## Decision
Adopt **Alembic**. Migrations live **inside the package** (`worldmonitor/db/migrations/`) so they ship
with the wheel. The chain is split so an existing pre-runway database can be brought forward:

- **0001 baseline** — the pre-runway Phase-1 schema (`connector_instance`, `er_queue_item` *without*
  `entity_id`/`uq_er_queue_dedup`, `merge_audit`, `merge_alerts`).
- **0002 runway delta** — `ALTER er_queue_item` add `entity_id` + its index + `uq_er_queue_dedup`; create
  `ingest_dead_letter` (G8) and `task_run` (Gate A).

**`migrate_to_head(engine)`** (`db/engine.py`) is the production schema path, with **adoption** for a
database created before Alembic existed: `alembic_version` present → upgrade; no `er_queue_item` → fresh
upgrade from base; `er_queue_item` already has `entity_id` → stamp head; a **pre-runway** `er_queue_item`
→ stamp the baseline then upgrade (the previously-broken case). It is URL-driven (Alembic manages its own
connection/transaction, so DDL commits unambiguously). A CLI entrypoint — `python -m worldmonitor.db.migrate`
— runs it in a deploy step. `create_all` is **retained as a test/dev convenience**, proven to produce the
same schema as `alembic upgrade head`.

**Deliberately not migrations** (no app-schema change, despite being "runway deltas"): `ConnectorInstance.status`
changed only its set of valid *values* (enabled/running/error), not its column DDL; and the D1 fix (ADR
0028) isolated nomenklatura's own external store, not an app table.

## Status
**LOCKED.** Append-only resolution and canonical-canonical-via-guard positions are untouched; no Gate B/C
work is reopened. Future schema changes are expressed as new revisions, not `create_all`.

## Consequences
- ✅ Fresh installs and **existing pre-runway deployments converge on an identical schema** — proven by
  `tests/integration/test_migrations.py` comparing introspected schema snapshots across all paths
  (`migrate_to_head` == `create_all` == pre-runway-adopted-then-upgraded), including the previously-broken
  pre-existing-`er_queue_item` case.
- ✅ `migrate_to_head` is idempotent; the in-package migrations ship with the wheel; downgrades are
  provided for both revisions.
- ✅ Tests keep using the fast `create_all`, which is proven equivalent, so the suite is unchanged.
- ✅ **Drift guard:** `test_no_autogenerate_drift` runs `alembic check` against a head database and fails
  the build if the models and migrations diverge (a model change without a matching revision), so drift
  cannot silently return. The post-runway pre-Alembic **adoption** path (a `create_all` database with no
  `alembic_version` → stamp head, upgrade is a no-op) is covered directly by
  `test_post_runway_create_all_database_is_stamped_at_head`.
- Running migrations at app startup vs. as a deploy step is left to ops (the CLI supports either);
  at-startup would need the deferred lease under multi-replica.
