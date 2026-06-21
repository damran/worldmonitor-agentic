# ADR 0017 — Tenant isolation: app-layer composite keys, not per-tenant DB

> Status: **LOCKED** · June 2026 · Implements the `tenant_id`-everywhere invariant (CLAUDE.md, decision #14).

## Context
Every node and edge must be tenant-scoped, and two tenants holding the same real-world entity
(same LEI / Wikidata Q-number) must get **separate** nodes. Neo4j Community has no per-database or
per-tenant RBAC isolation; multi-database isolation is a Neo4j Enterprise feature. We deploy single-node
Community now and want SaaS-grade tenancy later.

## Decision
Enforce tenant isolation **at the application layer in a single shared graph**:
- Inject `tenant_id` into **every** node and relationship parameter set, and rewrite **every** ftmg-
  generated MERGE/MATCH node key to include `tenant_id` — failing loudly if a key cannot be scoped
  (`graph/writer.py:44-59`, `_KEY_REWRITES` + `_tenantize_query` raising `WriterError`).
- Make canonical-ID uniqueness **composite** `(tenant_id, anchor)` for each anchor field
  (`graph/constraints.py:25-34`), so `(A, X)` and `(B, X)` are distinct and two tenants can each hold
  the same canonical entity without collision.
- Tenant-scope every read (`tenant_id` index + `WHERE` clause).

Neo4j Enterprise per-tenant/multi-DB isolation is **deferred** to cloud deployment.

## Status
**LOCKED** for the single-node Community deployment. Re-evaluate (supersede) when moving to a managed
cloud tier where Enterprise RBAC / multi-DB becomes available.

## Consequences
- ✅ Correct by construction: tenant_id in the MERGE key + composite constraint prevents cross-tenant
  node sharing.
- ✅ One graph, one set of GDS projections, simple ops.
- ⚠️ Isolation depends on *every* query author remembering the `tenant_id` predicate — a single missing
  `WHERE tenant_id` leaks across tenants. Mitigation: route all reads through the scoped query helpers.
- ⚠️ The headline "two tenants, same canonical ID → two nodes" case is enforced but **untested**
  (audit gap **G4**) — add the test before the Phase 2 multi-tenant read surface.
- ⚠️ A noisy-neighbour / blast-radius tenant cannot be physically isolated until the Enterprise move.
