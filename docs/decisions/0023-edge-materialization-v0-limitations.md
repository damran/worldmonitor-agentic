# ADR 0023 — Resolved-graph edge materialization: accepted v0 limitations

> Status: **OPEN** (accepted debt) · June 2026 · Records the two known Phase 1 follow-ups.

## Context
"The resolved entity graph is the product" and "resolve to canonical IDs" are non-negotiable. Two edge-
materialization shortcuts were taken in Phase 1 and explicitly noted in the acceptance test rather than
silently — this ADR records them as decisions with owed-by dates.

## Decision
Accept, for v0, the following limitations as **known debt** (not silent shortcuts):

1. **No edge referent-rewriting for merged-away ids.** When ER merges entity B into canonical A, edges
   that referenced B are **not** rewritten to A. Neighbour linking is therefore asserted only on
   non-merged singletons (`tests/integration/test_phase1_acceptance.py:14-16`). The merge path writes
   canonical nodes only (`resolution/pipeline.py:69-85`).
2. **No materialization of abstract `Thing`-range entity-links.** ftmg materialises an entity-link only
   when the property's range is a concrete schema; links whose range is the abstract base `Thing`
   (`Sanction.entity`, `CourtCase.entity`, future `wm:` indicator/target) are dropped. Only concrete
   edges (e.g. `Ownership.owner`/`asset`) are proven (`graph/writer.py:136-138`,
   `tests/integration/test_phase1_acceptance.py:86-89`).
   > **CLOSED-by-0046 (Gate D, 2026-06-25).** The thin `graph/ftmg_fork/` override re-keys both
   > abstract-range drop sites (`generate_entity_links`, `generate_edge_entity`) onto
   > `prop.type == registry.entity` with an `ENTITY_LABEL` fallback, so `Sanction.entity` /
   > `UnknownLink.subject` now materialize; a never-ingested target is MERGEd + tagged `:Ghost`.
   > See ADR 0046. (Audit gap G3 → CLOSED.)

## Status
**OPEN.** Both are owed before they bite:
- Referent-rewriting → **before Phase 2** (the API/MCP graph-read surface exposes neighbours; orphaned
  edges become a correctness bug). Interacts with the batch-vs-streaming ER choice (ADR 0019).
- Thing-range links → **before Phase 4** (CTI/enrichers rely on `Thing`-ranged links); needs a custom
  entity-link materializer or an ftmg upgrade.

## Consequences
- ✅ Limitations are explicit and tested-around, not hidden.
- ❌ Until (1) lands, the resolved graph has orphaned edges after any merge — directly at odds with
  "resolve to canonical IDs."
- ❌ Until (2) lands, Sanction→entity and similar links are not traversable; analysis over those
  relationships is impossible.
