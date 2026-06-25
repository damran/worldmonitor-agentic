# ADR 0028 — Per-batch resolver isolation (G4 fix)

> Status: **LOCKED** · June 2026 · Fixes a live **G4** (tenant-isolation) regression in resolution.
> Format: Context → Decision → Status → Consequences.

> **Note (2026-06-25, ADR 0042 — single-tenancy):** Under locked decision **D1: WorldMonitor is
> single-tenant** (ADR 0042 supersedes ADR 0017, removing `tenant_id` everywhere), the **G4
> cross-tenant-leak motivation below is moot** — with one tenant there is no "another tenant" whose
> judgements could leak through the shared ledger. The cross-tenant framing throughout this ADR (and
> the deferred per-tenant ledger in *Scope*) should be read as historical. **The decision itself does
> NOT change: the per-batch ephemeral in-memory resolver is KEPT verbatim.** Its independent,
> still-live justification is the **B-1 crash-recovery / ADR-0026 batch-purity guarantee** — the
> resolver must remain a pure function of the current batch's pairs (a throwaway `sqlite://` +
> `StaticPool` per `cluster_and_merge` call), reading/writing no cross-batch or cross-run state, so a
> mid-run crash leaves no partial ledger and within-batch dedup stays deterministic. ADR 0042 added
> this note; it did not supersede this ADR.

## Context
`cluster_and_merge` (`resolution/merge.py`) built its nomenklatura clustering resolver with
`nk.Resolver.make_default()`. That binds to nomenklatura's **shared, persistent, non-tenant-scoped**
SQLite ledger (`NOMENKLATURA_DB_URL`, defaulting to `sqlite:///…/nomenklatura.db`). Judgements are
committed there and **accumulate across every batch, every tenant, and every run** — the file persisted on
disk between runs.

This is a **live G4 isolation violation**, proven directly: two batches that share a source id (e.g. two
tenants ingesting the same OpenSanctions record) read each other's judgements through the global ledger.
Batch A merging `a1+a2` and batch B merging `a1+a3` leaves `a1`, `a2`, `a3` in **one** connected component
— so tenant A's merge canonicalizes tenant B's entities, and B's resolution outcome depends on A's prior
judgement. It also entangled canonical ids across unrelated runs (non-deterministic resolution) and
cross-contaminated tests sharing entity ids.

The discovery was triggered by the ER-streaming scoping (fork **D1**): the user required proof that the
per-batch resolver is tenant-scoped *before* any further work. It is not — so it is fixed now.

## Decision
Resolve each batch on a **private, in-memory** nomenklatura resolver, created fresh per
`cluster_and_merge` call (`_ephemeral_resolver()`): a throwaway `sqlite://` engine with a single shared
connection (`StaticPool`). The resolver becomes a **pure function of this batch's pairs** — it neither
reads nor writes any cross-batch / cross-tenant / cross-run state. This is exactly right for the
**batch-first** model (ADR 0026: dedup is within a batch); the resolver was only ever a transient
union-find over the batch's POSITIVE pairs, never an intended cross-run ledger.

## Scope — what is deliberately deferred
**Persistent, per-tenant** resolver state is required only for **incremental ER** (ADR 0019b, sub-gate S2
of the ER-streaming fork map), which is deferred (no real-time consumer today — fork F0). When that gate
is built, the resolver ledger **must** be per-tenant-isolated (a tenant-scoped store), never the global
default. This ADR removes the accidental shared ledger; it does not build the intentional per-tenant one.

## Status
**LOCKED.** Restores the G4 tenant-isolation invariant in resolution. Regression guard:
`tests/unit/test_resolution.py::test_resolver_is_isolated_per_batch_no_cross_tenant_leak` (two batches
sharing a source id must mint independent canonicals and contain only their own members).

## Consequences
- ✅ Tenant isolation holds in resolution: one tenant's merges can no longer influence another's. Each
  batch's canonical ids are a deterministic function of that batch alone.
- ✅ Removes a latent cross-test contamination source (tests previously shared the on-disk ledger within a
  run) and stops writing a `nomenklatura.db` artifact.
- ✅ No behaviour change to within-batch clustering: the same pairs produce the same merges; only the
  cross-batch leakage is removed. All existing within-batch tests pass unchanged.
- ⚠️ Incremental ER (S2) still needs a **per-tenant persistent** ledger — that remains a hard precondition
  for that future gate, not satisfied by this in-memory resolver.
- Negligible per-batch cost: constructing an in-memory SQLite engine per `cluster_and_merge` call.
