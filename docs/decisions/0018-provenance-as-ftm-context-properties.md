# ADR 0018 — Provenance stored as flat FtM-context keys, projected to node properties

> Status: **LOCKED** · June 2026 · Implements the provenance-everywhere invariant (CLAUDE.md).

## Context
Provenance (`source_id`, `retrieved_at`, `reliability`, raw-record pointer) must travel with every entity
from collection through resolution into the graph, and survive FtM merges. FtM's `merge_context` hashes
context values and cannot merge nested dicts, so a nested provenance object would be lost on merge.

## Decision
Store provenance as **flat scalar keys in the entity's FtM context**, prefixed `wm_prov_`, each value a
single-element list so it survives `merge_context` (`provenance/model.py:18-55`). The raw-record pointer
is the **real landing-zone URI** returned by `landing.put`, set at ingest before any entity is enqueued
(`runner/ingest.py:54,57-62`), so the s3:// pointer is concrete, not a promise. The graph writer flattens
these to `prov_*` node properties (`provenance/model.py:74-79`, `graph/writer.py:112-117`).

## Status
**LOCKED** for the node path. **Incomplete for edges:** edge/relationship writes are not stamped with
provenance (`graph/writer.py:142`), which violates the "provenance on every node *and edge*" invariant.
Closing that gap (audit blocker **G1**) does **not** change this storage decision — it extends the same
projection to relationship batches.

## Consequences
- ✅ Provenance is merge-safe and serialization-stable; a resolved node traces back to its raw s3://
  record (proven: `tests/integration/test_graph_queries.py:60-63`).
- ✅ Doubles as the GDPR/audit log for nodes.
- ❌ **Edges currently carry no provenance** — the GDPR/audit-log guarantee does not yet hold for
  relationships (Ownership/Sanction/Directorship). Must be fixed before the Phase 2 graph-read surface.
- ⚠️ Provenance is currently single-source per entity; multi-source provenance after a merge collapses to
  the surviving context values. Revisit if per-claim provenance is needed.
