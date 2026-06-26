# 0049 — Cross-store GDPR source erasure (`erase_source`)

- **Status:** ACCEPTED
- **Date:** 2026-06-26
- **Gate:** B-4a — GDPR cross-store source erasure (`docs/reviews/GATE_B4A_ERASURE_SPEC.md`). **BUILD gate.**
- **Accepted scope (v1, operator sign-off 2026-06-26):** ships **property-level / node-level** erasure
  (all Tier-1 supports). The known residual — a value the erased source UNIQUELY supplied that sits
  inside a property a SURVIVING source also witnesses persists (sole-source subjects + non-erased
  sources are fully correct) — is **consciously accepted** for v1. **Value-level erasure inside a
  multi-witness property is a tracked follow-up (`WM-ERASE-T2`)** requiring Gate C **Tier-2** (reified
  `:Statement`/`:Source`, unbuilt in B). v1 MUST NOT be described as complete erasure for a
  multi-source subject; full erasure of such a subject awaits `WM-ERASE-T2`.
- **Closes:** the cross-line audit's HIGH-severity finding — Workflow B has **no right-to-erasure of any
  kind**, violating CLAUDE.md's non-negotiable that provenance "doubles as the GDPR/audit log".
- **Amends (does NOT overturn):** [0045](0045-value-level-provenance.md) — §4 proposed `delete_source`
  but only Tier-1 shipped (commit `720841f`). This ADR records the erasure op that 0045 §4 anticipated,
  adapted to B's **Tier-1-only** reality and elevated to a full **cross-store** GDPR erasure.
- **Preserves (does NOT relitigate):** G1 `prov_*` on every node AND edge (`graph/writer.py`);
  [0044](0044-anchor-preferred-stable-ids.md) durable id + the `canonical_id_ledger` (never touched —
  no un-merge); [0031](0031-return-to-block-signoff.md) sign-off durability; [0030](0030-alembic-migrations.md)
  migration drift guard; `DEFAULT_MERGE_THRESHOLD=0.92` + Splink + `cluster_and_merge` (untouched).
- **Touches:** `graph/ops.py` (NEW), `erasure.py` (NEW), `storage/landing.py` (+`delete`/`delete_prefix`).
  Tests in `tests/integration/`. **No migration** (audit reuses `TaskRun`). `db/models.py` read-only.
- **Independently built on the other line:** Workflow A shipped the same capability as **WM-082**
  (`erase_source`, commit `8a7bda4`). The two lines are independent; B re-derives the design against
  B's own (different) provenance model and fixes two limitations A's implementation carries (below).

## Context

B's Gate C (ADR 0045) shipped **Tier-1 only**: every node carries `prov_*` (G1, single-source =
`source[0]`) and a fused node additionally carries `prov_witnesses` (a JSON `{prop: [datasets…]}` map).
There is **no Tier-2** (`:Statement`/`:Source`/`:FROM_SOURCE`), no `delete_source`, no `graph/ops.py`.
A person's record lands in four stores (landing MinIO, `er_queue_item.raw_entity`, `ingest_dead_letter`,
the Neo4j graph) and there is **no way to remove it**. CLAUDE.md makes erasure non-negotiable.

A built WM-082 on the other line. Studying it surfaced two limitations B must not inherit: A's graph
erase is **lineage-only** (it deletes Tier-2 statements + prunes the witness map but leaves the node
and its property *values* — the §4(a) "leave the value" default), so the personal-data value persists;
and A's landing erase deletes objects **only via matched queue rows**, missing map-stage dead-letter
and orphaned landed bytes.

## Decision

`erase_source(*, neo4j, session, landing, source_id, authorized_by)` removes one source's contribution
from all four stores, idempotently, source-scoped, runtime-authorized, with a durable audit row.

1. **Graph (Tier-1-aware, value-complete).** `graph/ops.py:erase_source_graph` decides per node by
   **parsing `prov_witnesses` in Python** (a Cypher `CONTAINS`/`prov_source_id =` is only a candidate
   pre-filter — never the decision, to avoid the substring trap):
   - **sole-source** (all datasets ⊆ `{source_id}`) → `DETACH DELETE` the node (value + edges gone);
   - **multi-source survivor** → drop `source_id` from each prop's witness set; `REMOVE` any prop it
     was the *sole* witness of; rebuild `prov_*` from a surviving witness when `prov_source_id ==
     source_id` (clearing the unrecoverable `prov_retrieved_at/reliability/source_record` to `""` so no
     dangling pointer to the deleted raw record remains; **G1 preserved** — a `prov_source_id` always
     stays). Relationships with `prov_source_id == source_id` are deleted (edges carry `prov_*` only).
2. **Landing (prefix delete).** `LandingStore.delete` + `delete_prefix`; erase **all** objects under
   the source's `"{connector_id}/{safe_dataset}/"` prefix (derived from `source_id` via the existing
   ingest sanitizer) — catches queue-referenced, dead-letter-referenced, and orphaned bytes.
3. **ER queue.** Redact `raw_entity` of the source's rows to a non-PII shell; keep the row shell.
4. **Dead-letter.** Redact `error` of the source's map-stage rows; keep the row shell.
5. **Audit (no migration).** Append one `TaskRun(kind="erase")` row: `authorized_by`, `source_id`,
   per-store counts, status. Non-PII by construction — the durable GDPR "request honored" trail.
6. **Runtime-authorized, append-only exception.** `authorized_by` is required (no default) and is never
   agent-auto-invoked; erasure is the *one sanctioned exception to append-only*, performing only the
   GDPR-mandated removal of the erased source's own data while **never** touching the
   `canonical_id_ledger`, re-clustering, splitting, or resurrecting a survivor (no-un-merge preserved).

## Person-affecting fencing (CRITICAL)

`erase_source` deletes a real person's data — legitimate as the subject's GDPR right, catastrophic as
an evasion vector (scrubbing a sanctioned entity). Per CLAUDE.md (person-affecting changes need human
sign-off; privileged ops never agent-auto-run) and ADR 0045 §4 (value-retraction is sign-off-gated),
**each run requires runtime human authorization**, not merely the build gate: `authorized_by` is
required + audited (function contract); the Phase-2 API/MCP erasure surface sits behind a Zitadel
operator role (named, not built here). It never *promotes* or *loosens* anything (no threshold/score/
Splink change) — it only removes.

## Alternatives considered

- **(A) Reuse A's lineage-only `delete_source`.** Rejected — B has no Tier-2 to delete, and A's
  approach **leaves the property values** in the graph (GDPR-incomplete). B `DETACH DELETE`s
  sole-source nodes + removes X-only props (value-complete, under authorization).
- **(B) Per-queue-row landing delete (A's approach).** Rejected — misses map-stage dead-letter and
  orphaned landed bytes. A **prefix delete** keyed on `source_id` is complete.
- **(C) A dedicated `erasure_audit` table.** Rejected — `TaskRun(kind="erase")` is sufficient and needs
  no migration; a dedicated table is a separate `0007_*` gate if a query surface ever needs it.
- **(D) Value-level erasure now (build Tier-2 first).** Rejected for *this* gate — out of scope.
  Tier-1's per-prop granularity gives property-level erasure; value-level inside a multi-witness prop
  needs Tier-2 (Gate C slice-2), a named follow-up. Property-level is a HIGH-severity improvement over
  today's *zero* erasure and ships now.
- **(E) Build-gate only / agent-auto erasure.** Rejected — an evasion vector; runtime authorization is
  mandatory.

## Consequences

- B gains a real, source-scoped, idempotent right-to-erasure across landing + queue + dead-letter +
  graph; a multi-source entity survives erasure of one source with only the surviving sources'
  provenance; a sole-source entity is removed completely (value included).
- More complete than A on two axes (value-complete graph erase; prefix-complete landing erase).
- **No migration.** Audit via `TaskRun`; migration drift guard untouched.
- **Scope caveat:** value-level erasure inside a multi-witness property and edge-level multi-source
  provenance both require **Tier-2** (ADR 0045 §2, unbuilt in B) — named follow-ups; this ADR ships the
  property-level / node-level capability Tier-1 supports.

## Out of scope (hard stops)

Tier-2 reification; value-level erasure inside a multi-witness prop; edge-level witness maps; deleting
`canonical_id_ledger` / `ResolverJudgement` / `SignOff` rows; any re-clustering / un-merge / node split;
changing `DEFAULT_MERGE_THRESHOLD` / Splink / scores / `cluster_and_merge`; any new table/migration; a
live API/MCP read or auth surface (Phase 2).
