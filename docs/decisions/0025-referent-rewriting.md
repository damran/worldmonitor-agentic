# ADR 0025 — Referent rewriting: redirect merged-away ids to canonical before the graph write

> Status: **LOCKED** · June 2026 · Closes item (1) of [ADR 0023](0023-edge-materialization-v0-limitations.md)
> (edge referent-rewriting) for batch resolution. Format: Context → Decision → Status → Consequences.

## Context
"The resolved entity graph is the product" and "resolve to canonical IDs" are non-negotiable
(CLAUDE.md). When ER collapses source entities `B`, `C`, … into one canonical entity `A`, any *other*
entity that referenced `B`/`C` by id — an edge endpoint (`Ownership.owner`, `Directorship.director`)
or any entity-typed property — still names the merged-away id. ADR 0023 accepted, as v0 debt, that those
references were **not** rewritten; neighbour linking was therefore only asserted on non-merged
singletons (`tests/integration/test_phase1_acceptance.py`). That debt is owed **before Phase 2** exposes
graph traversal.

The bite is worse than "an orphaned edge": ftmg materialises an edge with
`MATCH (endpoint {id: …})` (`ftmg/transform.py`), so an edge that names a merged-away id whose node was
never written **fails the MATCH and is silently dropped** — the relationship simply does not appear in
the graph, and `get_neighbors` misses the merge entirely. This is gap **G2** in the Phase 1 audit.

## Decision
Rewrite referents in the resolution layer, **before** the graph write, so the writer stays a dumb FtM→
Neo4j adapter. New module `resolution/referents.py`, wired into `resolution/pipeline.py`:

1. **`build_referent_map(promoted_clusters)`** → `{member_id: canonical_id}`. Built **only from clusters
   that are actually promoted** (written to the graph). Singletons map to themselves (a no-op); a real
   merge maps every collapsed source id to the surviving canonical id (a minted `NK-…` from nomenklatura,
   i.e. *not* one of the member ids).
2. **`rewrite_referents(entity, referents)`** → for each entity-typed property (FtM `registry.entity`),
   replace each value present in the map with its canonical id. Ids absent from the map (singletons,
   parked members, out-of-batch references) are left unchanged. Non-entity properties and the entity's
   provenance/context are never touched.
3. In `resolve_pending`, after the per-cluster guard decisions, build the map from the promoted clusters
   and rewrite every promoted entity before `write_entities`.

**Guard interaction falls out for free** (and is exactly the desired behaviour, ADR 0024):
- **block** mode — a flagged cluster is parked, never promoted, so it never enters the map; references to
  its sensitive members are **not** rewritten and the edge to them is not materialised. A sensitive
  entity is never resurrected through a back-door rewrite.
- **alert** mode — a flagged cluster is promoted, so it **does** rewrite, like any other merge.

**Provenance (G1) is preserved:** rewriting changes only entity-typed property *values*; an edge keeps
the provenance of the assertion that created it (its `prov_*`), so an alerted/merged edge stays traceable
to its source.

## Scope — and what is deliberately deferred
This rewrites referents **within the resolution batch**, before that batch's write. It does **not** rewrite
edges **already persisted** in the graph from a *prior* run. That cross-run case only arises with
incremental / streaming re-resolution (a member written by run *N*, then merged by run *N+1*), which is
coupled to the batch-vs-streaming ER decision ([ADR 0019](0019-batch-vs-streaming-resolution.md), gap G9)
and is owed at the **ER-streaming gate**, not here. In today's whole-queue batch pipeline a member is
resolved exactly once, in the same batch as the edges that reference it, so in-batch rewriting fully
closes G2 for batch resolution. A graph-side sweep (redirect existing relationships, delete the orphan
node) is the natural home for the streaming case and is recorded as the follow-up.

## Status
**LOCKED.** Closes **item (1)** of ADR 0023 (referent-rewriting) for batch resolution. ADR 0023 remains
**OPEN** for **item (2)** — abstract `Thing`-range entity-links (`Sanction.entity`, future `wm:`
indicator/target), owed before Phase 4. This ADR does not alter ADR 0024's obligation to return the guard
to `block` with human sign-off before production; referent rewriting applies under both modes as above.

## Consequences
- ✅ After a batch merge, edges to collapsed ids land on the surviving canonical node instead of being
  dropped; neighbour traversal — the headline read the Phase 2 API/MCP surface exposes — is correct
  across merges, not just on singletons. The ADR 0023 singleton-only neighbour limitation is lifted.
- ✅ Idempotent: re-running `resolve_pending` after the queue is drained loads nothing and is a no-op;
  graph writes are `MERGE`-based, so a repeated write is harmless.
- ✅ Tenant-safe (G4): the map is built per `resolve_pending` call from one tenant's clusters and applied
  before a tenant-scoped write — no cross-tenant referent can be introduced.
- ⚠️ Cross-run rewriting of already-persisted edges is **not** done; it is owed at the ER-streaming gate
  (ADR 0019 / G9). Until then, do not re-resolve a member that was written in an earlier batch.
