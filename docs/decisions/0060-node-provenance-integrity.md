# 0060 — Node provenance integrity: additive re-emit + fail-closed node provenance

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** Stage-0 / audit **M-1** + the node-side **G1** hole (`gate/m1-node-provenance`). Off `master`.
- **Addresses:** audit M-1 (`SET n = props` clobbers anchors/`prov_*` on re-emit) and the node half of
  G1 logged in `SESSION_HANDOFF_2026-06-27.md §5` (ADR 0055 fail-closed *edges* only; a node could still
  be written unprovenanced).

## Context — two node-provenance defects in the writer

1. **Clobber on re-emit (M-1).** Nodes are written by ftmg's `generate_node_entity`
   (`ftmg/transform.py:130-131`): `MERGE (n {id: props.id}) SET n = props` — a **full replace**. B's
   `graph/ftmg_fork/` overrides the edge / entity-link generators but **not** the node generator. So a
   *thinner* re-emit of the same `{id}` (a sparser source variant, or the B-1 re-resolve) **silently
   erases** the node's prior anchors + `prov_*` + `prov_witnesses` — a G1 (provenance-on-every-node) +
   anchor-stability regression on any re-ingest, within single-node v0.
2. **Unprovenanced node (node-side G1 hole).** ADR 0055 made *edges* fail closed when the asserting
   entity has no provenance, but **nodes** were left: an unstamped non-edge entity reaching the writer is
   written as a node with **no `prov_*`** — silently violating "provenance on every node".

## Decision

1. **Additive node write — override `generate_node_entity` in `graph/ftmg_fork/`** to emit
   `MERGE (n {id: props.id}) SET n += props` (additive) instead of `SET n = props`. `write_entities`
   Pass 1 uses the fork's generator (the same override pattern already used for edges). A re-emit now
   **accumulates** — prior anchors / `prov_*` / `prov_witnesses` are never lost. (Trade-off: a value
   present in an earlier emit but absent from a later one persists; correct for an append-only resolved
   graph, and exactly the audit's recommended fix. A genuine value *retraction* is the sign-off-gated
   `delete_source` path, ADR 0045/0049 — not a silent re-emit.)
2. **Fail-closed node provenance** — in `write_entities` Pass 1, a non-edge entity with **no**
   provenance raises `NodeProvenanceError` (mirroring ADR 0055's `EdgeProvenanceError`), rather than
   silently writing an unprovenanced node. **Ghost endpoint nodes are exempt by construction:** ghosts
   are created in Pass 2 by the entity-link `ON CREATE SET t:Ghost` (`ftmg_fork/transform.py`), never via
   `generate_node_entity`, so they are never subject to this check (a ghost is a typed traversal-only
   placeholder with no anchor/prov by design, ADR 0046).

## Alternatives considered
- **Guarantee every re-emit carries the full superset** (instead of `+=`). Fragile — depends on every
  producer always emitting the union; `+=` enforces non-loss at the write regardless. Rejected.
- **`SET n += props` only, skip the fail-closed node check.** Leaves the node-side G1 hole open (an
  unstamped node still writes without `prov_*`). Bundled here because both are the same "node provenance
  integrity" surface and the same Pass-1 path.
- **Fail-closed on ALL unstamped nodes (incl. ghosts).** Would break legitimate ghost endpoints. Rejected
  — the check is scoped to Pass-1 asserted entities; ghosts (Pass 2) are exempt.

## Consequences
- A re-emit/re-resolve can no longer clobber a node's anchors or provenance; G1 holds on every node node
  across re-ingest.
- An unstamped asserted entity halts the write loud (a contract violation surfaced, not silent) — the
  node analogue of ADR 0055.
- No migration; no schema change. No merge/score/guard/resolver change. **Not person-affecting**
  (graph-write integrity). `human_fork: false`.

## Reversibility
Reversible (writer policy). Reversal cost: low — revert the node-generator override + the Pass-1 check.
Revisit trigger: if a legitimate unprovenanced-asserted-node case emerges (none known), switch the
node fail-closed from raise to skip+dead-letter (mirroring the ADR 0055 reversal target, issue #105).

## Invariant gate (Phase-E rule)
This touches the **provenance** invariant → ships a `@given` property test: (a) a thinner re-emit of a
node preserves its prior anchors + `prov_*` (additive, never clobbered); (b) every Pass-1 node carries
`prov_*`, and an unstamped non-edge entity raises `NodeProvenanceError`; (c) ghost endpoints remain
written without prov (exempt).
