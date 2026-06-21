# 0031 — Return-to-block + human sign-off for parked merges

- **Status:** accepted
- **Date:** 2026-06-21
- **Supersedes the temporary mode of:** [0024](0024-merge-guard-alert-mode.md)
- **Builds on:** [0026](0026-batch-first-resolution.md) (batch-first), [0028](0028-ephemeral-per-batch-resolver.md) (per-batch resolver / G4), [0025](0025-referent-rewriting.md) (referent rewriting), [0030](0030-alembic-migrations.md) (migrations)

## Context

ADR 0024 set the catastrophic-merge guard to a **temporary** build-phase `alert` mode
(write the flagged merge anyway, record a `merge_alerts` row) with an explicit
obligation to **return to `block` before production, with a human sign-off path**.
Block mode parks a flagged (oversized / PEP / sanctioned) cluster as `pending_review`
and never writes it — but until now nothing let an operator *act* on a parked merge,
so block mode was a dead end.

The hard part is durability under [0028](0028-ephemeral-per-batch-resolver.md): the
resolver is **ephemeral per batch** (the G4 fix — a shared nomenklatura ledger leaked
merges across tenants). A judgement made in one batch evaporates, so a human decision
would be forgotten and the cluster would re-park on every later batch.

## Decision

1. **Default `MERGE_GUARD_MODE` → `block`** (production posture). `alert` remains
   available but is no longer the default; the guard *evaluation* is unchanged — only
   the action on a flagged cluster differs. This fulfils the ADR 0024 obligation.

2. **Durable, tenant-scoped resolver judgements** (`resolver_judgement` table). A
   sign-off writes a judgement (`positive` = approved, `negative` = rejected) keyed by
   `(tenant_id, left_id, right_id)`. `resolve_pending` loads **this tenant's**
   judgements once and seeds **every** batch's fresh ephemeral resolver with them
   *before* clustering; a Splink pair that a judgement already decided is **skipped**
   (the human decision wins). Tenant-scoping keeps the G4 invariant: one tenant's
   judgement can never bind another's resolution. A flagged cluster backed by a
   positive judgement **bypasses the guard** — so an approved merge never re-parks.

3. **Sign-off mechanism** (`resolution/signoff.py`, CLI `python -m worldmonitor.review`):
   - `list` — the parked (`pending_review`) merges awaiting review.
   - `approve` — promote: reconstruct the canonical entity from the parked members,
     rewrite + write its **outbound edges** (G2), persist a positive judgement, flip the
     audit to `merged`, record a `sign_off` row.
   - `reject` — write each member as its **own** entity (+ its outbound edges), persist a
     negative judgement, flip the audit to `rejected`, record a `sign_off` row.
   `--approver` is the operator identity (a string in v0; Zitadel-backed in Phase 2).
   The `sign_off` table is the durable human-sign-off trail CLAUDE.md requires for a
   change affecting a real person.

## Consequences

- ✅ Block is the default; a parked sensitive merge is actionable, audited, and reviewed
  by a named operator — never silently fused.
- ✅ The decision is durable and tenant-scoped: an approved cluster never re-parks, a
  rejected pair never re-merges, and neither leaks across tenants (proven by
  `tests/integration/test_signoff.py`, including judgement consumption + isolation).
- ✅ First migration after the Alembic gate (`0003_signoff_judgements`) — exercises the
  migration + drift-guard flow end to end.
- ⚠️ **Inbound** cross-references (edges pointing *at* an approved entity, dropped at
  park-time) are not restored on approval — that is **deferred Gate C**. They are
  reconstructable from the retained landing zone + queue rows (nothing is ever deleted),
  so it is deferral, not loss.
- ⚠️ A re-ingest of approved members re-merges them under a **fresh** canonical id
  (nomenklatura mints non-deterministically; cross-batch dedup is **deferred Gate B**).
  Sign-off guarantees *no re-park*, not cross-batch canonical-id stability.
- ⚠️ `signoff._outbound_edges` is a v0 full scan of the tenant's queue — acceptable for a
  manual, infrequent operation; a JSONB index / edge projection is a later optimisation.
- Judgements are recorded for all member pairs (`O(n²)` per cluster) — fine for the
  small sensitive clusters this targets.
