# ADR 0029 — Long-running ingest driver (ER-streaming Gate A)

> Status: **LOCKED** · June 2026 · Gate A of the ER-streaming fork map. Format: Context → Decision →
> Status → Consequences.

## Context
`run_ingest` and `resolve_pending` were call-once functions invoked only by tests; nothing ran them on a
cadence, and `ConnectorInstance` (with `last_run`/`next_run`) + the config cipher existed but were never
read. The ER-streaming scoping (fork map) established — via the user's decision on the root fork **F0** —
that **a batch cadence covers every downstream today; there is no real-time consumer.** So this gate is
**S1a only**: a long-running driver that runs batch connectors on a timer and resolves the queue on a
cadence. Incremental ER (S2), persisted/cross-run referent rewriting (S3), graph-mutation surfaces, and
the canonical-canonical guard rail (S4) are **deferred entirely** — they become a future gate *only if a
real-time consumer is named*. No STREAM connector exists, so the stream cursor (X1) and hard-kill
isolation (S1b) are out of scope here.

A precondition was found and fixed first: the per-batch resolver leaked across tenants (a live G4 issue),
closed by [ADR 0028](0028-per-batch-resolver-isolation.md). Persistent **per-tenant** resolver state
remains the hard precondition for any future S2.

## Decision
Add `runner/driver.py` (`IngestDriver`): an asyncio loop over the connector-instance registry. Per the
user's Gate A fork decisions:

- **A1 — asyncio + task table.** The driver is an asyncio loop; every run is recorded in a new `task_run`
  table (`kind` = `ingest`/`resolve`, `status` `running`→`ok`/`error`, `stats` JSONB, `error`,
  timestamps), tenant-scoped.
- **A2 — independent resolution cadence.** `resolve_pending` runs on `RESOLVE_CADENCE_SECONDS`, **not**
  fired per ingest.
- **A3 — single global cadence.** One `INGEST_CADENCE_SECONDS` for all instances now (default 3600); a
  per-connector cadence column is deferred. `DRIVER_TICK_SECONDS` (30) is the wake interval.
- **A4 — minimal status.** `ConnectorInstance.status` ∈ `disabled|enabled|running|error`; the lifecycle UI
  is Phase 2.
- **A5 — decrypt-at-use.** Config is `ConfigCipher.decrypt`-ed at each run; never cached in plaintext.
- **A6 — idempotent enqueue.** `ErQueueItem` gains an indexed `entity_id` column + `UNIQUE(tenant_id,
  source_record, entity_id)`; `run_ingest` enqueues via `INSERT … ON CONFLICT DO NOTHING`, counting only
  new rows. A crash/restart re-ingest is a no-op. **Confirmed precondition:** both shipped connectors emit
  deterministic, content-derived ids (OpenSanctions preserves the source `NK-` id; GeoNames uses
  `geonames-{id}`), so the constraint genuinely dedups. (An id-less entity has `entity_id = NULL`, which
  Postgres treats as distinct — those rare rows are not deduped.)
- **Capability.ACTIVE is refused VISIBLY** — the driver records a `task_run` `error` with the reason (and
  a WARNING), never a silent skip and never agent-auto-run, until the authorized-scope-token gate exists.
- **Concurrency (single-node).** Resolution is serialized by an in-process lock — the driver never
  overlaps its own `resolve_pending` runs. Multi-replica safety (instance lease, single-writer-per-tenant
  advisory lock — forks X2/X3) is **deferred** until a multi-replica deployment exists.
- **Startup recovery.** Rows left `running` by a crash are reset to `error` (task) / `enabled` (instance)
  so the next tick re-runs them.

## Status
**LOCKED** for Gate A (single-node, batch sources on a timer). The driver reuses the already-shipped,
tested primitives with **no new resolution-correctness surface**.

## Consequences
- ✅ A continuously-fed, continuously-resolved graph from a cadence-driven driver, with a durable
  `task_run` trail and idempotent restart (A6) — verified by failure-injection, restart (re-ingest
  no-double-enqueue), visible-active-refusal, stale-recovery, and resolution-no-overlap tests, not just
  counts.
- ✅ Inherits ADR 0026's accepted cross-batch-dedup limitation unchanged; adds none.
- ⚠️ **Follow-up (1): `task_run` retention/pruning** is not implemented — the table grows unbounded; a
  retention policy is owed before long-running production.
- ⚠️ **Follow-up (2): the startup `running`→`error` stale-reset is a single-node assumption.** Under HA
  (multiple driver replicas) it would race; it must be replaced by the deferred instance lease (fork X2)
  before running more than one driver.
- ⚠️ Active connectors cannot run at all until the scope-token gate (fork F2) is built — intentional.

## Note — 2026-06-25 (ADR 0042: single-tenant)
The system is now **single-tenant** (locked decision D1; [ADR 0042](0042-single-tenant.md) supersedes
[ADR 0017](0017-tenant-scoping.md)) and `tenant_id` has been removed from all code. This amends the
tenancy claims above without rewriting the Gate A decision:
- **Per-tenant resolution routing is gone.** The driver no longer runs the distinct-tenant `select` that
  enumerated tenants, nor the `_resolve_tenant` per-tenant loop. `run_resolution` is now a **single pass**
  over the queue — there is no per-tenant fan-out.
- The A1 `task_run` rows and the A6 idempotency constraint are no longer tenant-scoped (`tenant_id`
  dropped from both); the rest of A6's content-derived-id dedup reasoning is unchanged.
- The "persistent per-tenant resolver state" precondition for a future S2 (Context) is moot — there is
  one resolver state.
- The **X2 (HA instance lease)** and **X3 (single-writer-per-tenant advisory lock)** forks are **moot
  under single-tenancy**: X3 has no per-tenant key to guard, and the Follow-up (2) stale-reset race they
  hedged against remains a single-node assumption, not a multi-tenant one.
