# 0055 — Fail-closed edge provenance (no silently-unprovenanced edges)

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** Phase-B #2 (`gate/edge-provenance-g1`) — a focused fix off `master`.
- **Addresses:** the G1 edge-provenance hole at `graph/writer.py` (`_inject_props`, the `if edge_props:`
  guard), confirmed file:line in the cross-workflow Round-2 cross-examination.

## Context — what ground truth actually shows

G1 (non-negotiable): **provenance on every node AND every edge.** `write_entities` projects an edge's
provenance from its *asserting* entity (the Ownership/Sanction/… entity, or the property-holder) via
`edge_prov = provenance_node_properties(entity)` (`writer.py:188`), merged in `_inject_props` under
`if edge_props:` (`writer.py:80`).

`provenance_node_properties` returns `{}` **iff** the entity is unstamped (`get_provenance` is None →
`source_id` is None, `model.py:85-99`). A *stamped* entity always yields a non-empty dict. So the
`if edge_props:` guard drops provenance **only for an unstamped asserting entity** — and in that case
the edge is written **silently unprovenanced**, violating G1 with no error.

This is not merely theoretical: `tests/integration/test_writer_writes_nodes_and_edges`
(`test_graph_writer.py:55-64`) writes an **unstamped** `Ownership` edge and asserts only that it lands —
so the hole is *exercised today*; that edge currently lands with no `prov_*`.

**The plan's shorthand ("stamp edge provenance unconditionally") cannot apply literally:** you cannot
synthesise a real `source_id` for an entity that has none, and fabricating one would be *false*
provenance — worse than none for a GDPR/audit log. The honest fix is therefore not "invent provenance"
but **"never silently write an unprovenanced edge."**

## Decision

**Fail closed.** `write_entities` raises a clear `EdgeProvenanceError` (a `ValueError`) when it is about
to write a **relationship** (an edge-schema entity's edge, or an entity-reference link) whose asserting
entity has **no provenance** — naming the offending entity id. An unprovenanced edge is a G1 violation
and must halt loud, consistent with this codebase's halt-loud style (backup validate-before-touch,
fail-closed sensitivity guard) rather than corrupt the audit log silently.

- Scope: only the relationship path (the nested-`props` branch of `_inject_props`). **Nodes** (flat
  params → the `node_props_by_id` branch) and **topic-label** batches (no `props` dict → untouched) are
  unaffected and never raise. A non-edge entity with no entity-typed properties produces no relationship
  batch, so it never raises.
- A stamped asserting entity (the normal pipeline case — provenance is "required on every mapped entity"
  and connectors stamp on `map()`) is unchanged: `edge_prov` is non-empty, merged as before.

`test_writer_writes_nodes_and_edges` is updated to stamp its entities (its purpose is node/edge
*materialisation*; the unstamped usage was incidental and is now contract-violating). Intended change,
flagged for the judge — every provenance/materialisation assertion is preserved.

## Alternatives considered

- **Merge `edge_props` unconditionally (the literal plan wording).** A no-op for a truly-empty dict —
  merging `{}` adds nothing, the edge stays unprovenanced. Does not close the hole. Rejected.
- **Fabricate a sentinel provenance** (e.g. `source_id="unknown"`). False provenance in the audit log;
  violates the spirit of G1. Rejected.
- **Skip + dead-letter the unprovenanced edge** (don't write it, record why). Viable and less disruptive
  than raising, but needs a DB/dead-letter dependency in the writer and silently *drops* the edge (a
  different data-loss). Deferred as the documented reversal target if raising proves too aggressive.

## Consequences

- The writer's stated invariant ("provenance on every node *and edge*") is now **enforced**, not
  assumed — an unstamped edge-asserting entity halts the write loudly instead of corrupting the graph.
- In the normal pipeline this never fires (entities are stamped upstream); it is a safety net that turns
  a silent G1 violation into a loud, attributable failure.
- No migration; no merge/score/guard/resolver change. **Not person-affecting** (graph-write integrity,
  no ER/score decision). `human_fork: false`.

## Reversibility

Reversible (writer policy). Reversal cost: low — revert `_inject_props` + restore the test. **Revisit
trigger:** if a legitimate unprovenanced-edge case ever emerges (none known — ghosts are *endpoints*,
not asserters), switch from raise to **skip + dead-letter** (the deferred alternative) so one bad entity
doesn't abort a whole `write_entities` batch.
