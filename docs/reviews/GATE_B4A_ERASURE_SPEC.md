# Gate B-4a — GDPR cross-store source erasure (`erase_source`)

- **Status:** PROPOSED (spec) — BUILD gate.
- **Branch:** `gate/b4a-gdpr-erase-source` (off `master`).
- **ADR:** [0049](../decisions/0049-cross-store-gdpr-source-erasure.md) (PROPOSED).
- **Severity it closes:** HIGH. The cross-line audit found Workflow B has **NO right-to-erasure of any
  kind**. CLAUDE.md non-negotiable: provenance "doubles as the GDPR/audit log" — a right-to-erasure
  must be able to remove a source's personal data wherever it landed. Workflow A built this (WM-082);
  B never did. B's Gate C (`delete_source` in ADR 0045 §4) was **proposed but never implemented** — no
  `delete_source`, no `graph/ops.py`, no Tier-2 exists in B today (verified, §2).

---

## 1. The gap (verified against B's code)

`grep -rn "delete_source\|erase_source\|erasure"` over `src/` returns **nothing**. B can ingest a
person's record into three stores and has no way to remove it:

| Store | Where the PII lands in B | Erasure today |
|---|---|---|
| Landing zone (MinIO) | `storage/landing.py` `put()` → `s3://<bucket>/<connector_id>/<safe_dataset>/<key>.json` (verbatim raw record) | none — `LandingStore` has no `delete` |
| ER queue (Postgres) | `db.models.ErQueueItem.raw_entity` (JSONB; the mapped FtM entity incl. its provenance context) | none |
| Dead-letter (Postgres) | `db.models.IngestDeadLetter` (map-stage rows point `source_record` at landed raw PII; `error` may carry a raw fragment) | none |
| Graph (Neo4j) | `:Entity` nodes + relationships, each carrying `prov_*` (G1) + nodes carrying `prov_witnesses` (Tier-1 witness map, ADR 0045) | none |

This violates the non-negotiable. The gate adds **`erase_source(source_id)`** that removes one
source's contribution from all four, idempotently and source-scoped, under explicit human
authorization, with a durable audit row.

---

## 2. Workflow A studied (re-derived from `8a7bda4`, independent line) + cross-line bugs

A's `erase_source` (`src/worldmonitor/erasure.py`, ~78 LOC) coordinates three stores:
- **Graph** via Gate C's `graph/writer.py:delete_source(client, dataset)` — A **has Tier-2**: it
  `DETACH DELETE`s `(:Statement {dataset})` nodes, drops orphaned `(:Source)`, then rewrites every
  Tier-1 witness map (`prov_prop_sources`, a JSON string) to drop the dataset via a `CONTAINS`
  superset pre-filter + a Python `if pruned == witness: continue` substring-false-positive guard.
- **Landing**: iterates `ErQueueItem` rows, matches `get_provenance(make_entity(raw_entity)).source_id
  == source_id`, deletes `landing.key_for(row.source_record)`.
- **Queue**: redacts the matched row's `raw_entity` to `{"erased": True}`, keeping the row shell.
- Idempotent; an unrelated source untouched.

**Cross-line bugs / limitations in A that B must NOT inherit (standing duty):**

- **B-1 — A leaves the property VALUES in the graph.** A's `delete_source` deletes Tier-2 statements
  and prunes the Tier-1 witness map but **never `DETACH DELETE`s a sole-source node and never removes
  an X-only property**. A sanctioned person's `name` value survives on the node after "erasure" — only
  its lineage pointers are gone (the ADR-0045 §4(a) "leave the value + audit" default). For a *GDPR
  right-to-erasure* that is **incomplete**: the personal-data value persists. B fixes this (§4.3): B
  `DETACH DELETE`s sole-source nodes and removes X-only props, under runtime authorization.
- **B-2 — A's landing erase misses landed-but-unqueued bytes.** A deletes landing objects **only**
  via matched queue rows. A **map-stage dead-letter** (raw landed, mapping raised → no queue row) and
  any **orphaned** landed object (crash between `put` and enqueue) keep their raw PII bytes in MinIO
  forever. B fixes this with a **prefix delete** of all of the source's landing objects (§4.1).
- **B-3 — `key_for` silently skips foreign URIs.** A's `key_for` returns `None` (no delete) for any
  `source_record` not of the form `s3://<this-bucket>/...`. B's prefix delete derives the prefix from
  `source_id`, not from each row's URI, so it is robust to that.
- **B-susceptibility — the `CONTAINS` substring trap.** A guards it; B MUST too: a Cypher
  `prov_witnesses CONTAINS "ofac"` over-matches a node witnessed only by `"ofac-eu"` (and prop names).
  B uses `CONTAINS` (or `prov_source_id =`) ONLY as a candidate pre-filter and decides precisely by
  **parsing the JSON in Python** (§4.3). Pinned by the prefix-collision test (T5b).

A's value-provenance ADR touch (`0044` in A) just added one line noting `delete_source` is
value-level for the allow-list + prop-level otherwise, and that full cross-store erasure is
`erase_source`. B's analogue is ADR 0049 amending 0045 (§9 here).

---

## 3. B's model — the crux (B's provenance ≠ A's)

**Verified state of B's Gate C (commit `720841f`, "tier-1 witness map ... slice-1"):**

- `provenance/model.py`: every entity carries flat `wm_prov_*` context → projected by the writer as
  **`prov_*`** node/edge properties (G1, single-source = `source[0]`). A fused entity *additionally*
  carries `wm_prov_witnesses` (a JSON string `{prop: [datasets…]}`) → projected as the node property
  **`prov_witnesses`** (Tier-1 witness map). Datasets are `Provenance.source_id` values.
- `resolution/merge.py`: `_merge_entities` fuses a `StatementEntity` purely to derive the per-prop
  dataset sets → `stamp_witness_map`. The per-`(prop, value, dataset)` granularity exists transiently
  during fusion and is **collapsed to per-prop dataset sets** when stamped.
- **There is NO Tier-2.** No `:Statement` / `:Source` / `:FROM_SOURCE`, no `graph/ops.py`, no
  `config/audited_properties.yml`, no `delete_source`. (ADR 0045 *proposed* Tier-2 + `delete_source`;
  only Tier-1 shipped.) Edges carry `prov_*` **only** — the witness map is **node-only** (writer Pass
  2 stamps `edge_props = provenance_node_properties(entity)`; no `witness_node_properties`).

**Consequences that drive the design:**
1. B has nothing to `DETACH DELETE` by `{dataset}` (no `:Statement`). The graph erase works purely on
   `prov_witnesses` + `prov_*` + node/edge deletion.
2. B's finest graph granularity is **per-property** (the witness map is `prop → {datasets}`, NOT
   `value → {datasets}`). A property witnessed solely by X can be removed completely; a property
   witnessed by `{X, Y}` cannot have an X-only *value* surgically removed without Tier-2 (§7 residual).
3. B's single-source `prov_*` is `source[0]`. If `source[0] == X` on a *surviving* multi-source node,
   `prov_*` (incl. `prov_source_record`, a pointer to the now-deleted raw record) must be **rebuilt**
   from a surviving witness, or the node leaks a dangling pointer to erased data (§4.3). A's Tier-2
   model did not have this exact wrinkle; it is B-specific.

---

## 4. Store-by-store erase plan

`erase_source(*, neo4j, session, landing, source_id, authorized_by)` — `source_id` is the provenance
dataset (`"{connector_id}:{dataset}"`, e.g. `"ofac:sdn"`). Order: graph → landing → queue →
dead-letter → audit. Returns an `ErasureResult` of per-store counts.

### 4.1 Landing zone (MinIO) — prefix delete (more complete than A)
- `LandingStore.delete(key)` — single-object delete (idempotent; S3 delete of a missing key is a
  no-op).
- `LandingStore.delete_prefix(prefix) -> int` — `list_keys(prefix)` then delete each; returns count.
- `erasure._landing_prefix(source_id)` derives `"{connector_id}/{safe_segment(dataset)}/"` by mirroring
  the ingest key scheme (`runner/ingest.py:147-156`). It **reuses the existing `_safe_segment`** (single
  source of truth — no duplicated sanitizer; the T3 integration test ingests through the real runner
  then erases, pinning alignment end-to-end). Delete every object under the prefix → erases
  queue-referenced AND dead-letter-referenced AND orphaned raw bytes (closes B-2).

### 4.2 ER queue (Postgres `er_queue_item`)
- Match rows whose `get_provenance(make_entity(raw_entity)).source_id == source_id`.
- Redact `raw_entity` → a **non-PII shell** `{"erased": True, "source_id": source_id}`; KEEP the row
  (`id` / `connector_id` / `entity_id` / `source_record` / `status` / `created_at`) as the audit shell.
- Idempotent: an already-redacted row has no parseable schema → `get_provenance` is `None` → skipped.

### 4.3 Graph (Neo4j) — Tier-1-aware prune (`graph/ops.py:erase_source_graph`)
Candidate pre-filter (cheap, may over-match): `MATCH (n:Entity) WHERE n.prov_source_id = $x OR
n.prov_witnesses CONTAINS $x`. **Decide precisely in Python** by parsing `prov_witnesses`:

For each candidate node, let `datasets(n)` = (union of all witness sets in `prov_witnesses`) ∪
({`prov_source_id`} if present):
- **Sole-source node** — `datasets(n) ⊆ {X}` → **`DETACH DELETE`** the node (removes the node, its
  values, and its incident edges). This is the GDPR-complete removal A omits (B-1).
- **Multi-source survivor** — `X ∈ datasets(n)` but other datasets remain:
  - For each prop, drop X from its witness set.
    - prop's set becomes **empty** (X was its sole source) → **`REMOVE n.<prop>`** (value-retraction;
      no collateral — no other source witnessed this prop) + audit.
    - else → keep the prop and its (shared) values.
  - Re-serialize the pruned map back to `prov_witnesses` (dropping emptied props).
  - If `prov_source_id == X` → **rebuild `prov_*`**: set `prov_source_id` to the lexicographically
    smallest *surviving* dataset; set `prov_retrieved_at` / `prov_reliability` / `prov_source_record`
    to `""` (Tier-1 stores only dataset *names*, so the surviving source's full single-source
    Provenance is unrecoverable from the graph — and X's `source_record` MUST NOT remain, it points at
    the deleted raw record). **G1 preserved** (a `prov_source_id` always remains on a survivor).
  - The substring trap (B-3): if parsing shows X was never actually a witness/`prov_source_id` (pure
    `CONTAINS` false positive), the node is unchanged (no-op) — pinned by T5b.

Edges (relationships carry `prov_*` only — Tier-1 is node-only):
- **`MATCH ()-[r]-() WHERE r.prov_source_id = $x DELETE r`** — an edge is its `source[0]` provenance
  (all B ever recorded on it). Sole-source nodes' edges are already gone via `DETACH DELETE`.
- **Documented limitation:** B never recorded co-asserting sources on an edge (witness map is
  node-only). A multi-asserted edge written with `source[0] == X` is deleted whole. Edge-level witness
  is the Tier-2/edge follow-up (§7); the crux multi-source survival is asserted at the **node** level
  (where B records multi-source provenance), per T1.

### 4.4 Audit — `TaskRun(kind="erase")` (NO migration)
`erase_source` writes one `TaskRun` row: `kind="erase"` (free `String(16)`), `status` running→ok/error,
`started_at`/`finished_at`, `stats = {source_id, authorized_by, nodes_deleted, nodes_pruned,
props_retracted, edges_deleted, queue_rows_redacted, landing_objects_deleted, dead_letters_redacted}`.
The row is **non-PII by construction** (a dataset name, an operator id, aggregate counts) — the durable
GDPR "we honored the request" trail. Reuses the existing audit table; **no schema change** (§8).

### 4.5 Dead-letter (Postgres `ingest_dead_letter`)
- Match rows whose `source_record` falls under the source's `s3://<bucket>/<prefix>` (map-stage rows
  that point at landed PII). Redact `error` → `""`; keep the row shell. (Land-stage rows — nothing
  landed, no `source_record` — carry no landed PII; their possible `error` fragment is a documented
  residual, §7.) Idempotent: an already-`""` error is a no-op.

---

## 5. Preserve vs erase (exact)

**ERASE (X's contribution):** all landing objects under X's prefix; `ErQueueItem.raw_entity` of X's
rows (→ shell); sole-source `:Entity` nodes (DETACH DELETE); X-only props on survivors (REMOVE) + X
pruned from every `prov_witnesses`; `prov_*` rebuilt where `source[0] == X`; relationships with
`prov_source_id == X`; `IngestDeadLetter.error` of X's map-stage rows.

**PRESERVE (never touched):**
- **Surviving entities** — other sources' nodes/edges/values; a survivor keeps its durable id. **No
  un-merge, no re-clustering, no node split, no resurrection.**
- **`canonical_id_ledger`** — entirely. Alias rows are the no-un-merge ledger; deleting them would
  break referent resolution / un-map survivors (the DENY condition). Pseudonymous id mappings, not
  source PII.
- **`ResolverJudgement` / `SignOff` / `MergeAudit` / `MergeAlert`** — human decisions + audit trail
  (CLAUDE.md: "never silent in-place mutation"; durable sign-off). A negative judgement that *prevents*
  a forbidden merge MUST survive (deleting it could resurrect the merge). Their ids are pseudonymous
  member ids, not direct PII. The "delete a judgement that references *only* erased ids" carve-out is
  **deliberately deferred** (§7) — preserving the audit trail is the fail-safe; over-deleting a
  judgement risks un-protecting against a forbidden re-merge.
- **`TaskRun`** existing rows; the erase appends its own audit row.

---

## 6. Idempotency, source-scoping, authorization

- **Idempotent.** 2nd erase: landing prefix already empty (no-op); queue rows already shells (skipped);
  graph — sole-source nodes already gone (MATCH empty), survivors already pruned (X absent → no
  change); dead-letters already `""`. A 2nd run records a 2nd `TaskRun` with all-zero counts (the GDPR
  trail of the repeat request) — that is the only state delta, and it is append-only audit, not data.
- **Source-scoped.** Every match is keyed on X: provenance `source_id == X` (queue), `X ∈
  datasets(n)` decided by JSON parse (graph), the `<connector_id>/<safe_dataset>/` prefix (landing /
  dead-letter). Source Y (different id, prefix, witnesses) is never matched. A name that is a *prefix*
  of another (`"ofac"` vs `"ofac-eu"`) never collides (precise JSON decision + `/`-terminated prefix).
- **Runtime-authorized.** `authorized_by: str` is **required, no default** — the caller must name the
  human operator who authorized this erasure; it is recorded in the audit row. The function cannot be
  invoked anonymously or agent-auto (§10).

---

## 7. Known limitations / named follow-ups (NOT in this gate)

- **Value-level erasure inside a multi-witness property** needs **Tier-2** (per-`(prop,value,dataset)`
  reified statements — ADR 0045 §2, never built in B). With Tier-1, a prop witnessed by `{X, Y}` whose
  values include an X-only value cannot have that one value surgically removed without risking Y's data
  (the DENY). v1 erases at **property** granularity: shared props keep their (shared) values, X-only
  props are removed wholesale. **Follow-up: Gate C slice-2 (Tier-2), then value-level erase.**
- **Edge-level multi-source provenance** — the witness map is node-only; an edge is its `source[0]`.
  Follow-up: extend Tier-1 to edges.
- **Land-stage dead-letter `error` fragments** (nothing landed) — rare; redact-by-connector is the
  follow-up.
- **`ResolverJudgement` rows referencing only-erased ids** — deferred (preserve = fail-safe).

> **FLAG to the human (not a STOP):** this gate ships **property-level** GDPR erasure — the strongest
> B's current (Tier-1-only) model supports, and a HIGH-severity improvement over today's *zero*
> erasure. Full **value-level** completeness requires Tier-2 first. Acceptance criteria are crisp at
> property granularity (§8), so the gate is buildable now; confirm property-level v1 is acceptable, or
> sequence Tier-2 ahead of it.

---

## 8. Migration conclusion

**NO migration.** The audit reuses `TaskRun` (`kind` is a free `String(16)`; `"erase"` fits; `stats`
is JSONB). No new table/column/constraint → `tests/integration/test_migrations.py` (alembic-head ==
`create_all`, ADR 0030) is **not triggered** and MUST stay green. `db/models.py` is **read-only** in
this gate (imported, not edited). A dedicated `erasure_audit` table is rejected (§9 alt C) — if a
future query surface needs one, that is a separate `0007_*` migration gate.

---

## 9. Failing-first test plan (integration; Neo4j + Postgres + MinIO testcontainers)

RED before the build, GREEN after. Slice-1 tests in `tests/integration/test_erasure_graph.py`;
slice-2 in `tests/integration/test_erasure.py`.

- **T1 — multi-source survival (THE crux, mirrors A's value-provenance test).** Ingest the SAME entity
  from `src-A` + `src-B` → one fused node, `prov_witnesses` has each shared prop → `[src-A, src-B]`.
  `erase_source("src-A")`. Assert: node **SURVIVES**; `prov_witnesses` no longer references `src-A`
  (each set is `[src-B]`); shared values intact; `prov_source_id == src-B`.
- **T1b — `prov_*` rebuild.** Construct the survivor so `source[0] == src-A`. After erase:
  `prov_source_id == src-B` and `prov_source_record == ""` (no dangling pointer to the deleted raw
  record); `prov_witnesses` has no `src-A`.
- **T2 — sole-source delete (value-complete, beats A's B-1).** Entity only from `src-A`, with an edge.
  After erase: the node is **gone** (`get_entity` is `None`), its edge is gone, and **its value is gone
  from the graph** (assert the name no longer appears).
- **T3 — cross-store.** Ingest a sanctioned record from `src-A` through the **real ingest runner** (so
  landing key + queue + provenance are authentic) + a map-stage dead-letter for `src-A`. After erase:
  landing prefix empty; `ErQueueItem.raw_entity` is the shell + no PII; graph pruned; dead-letter
  `error == ""`; a `TaskRun(kind="erase")` row exists with `authorized_by` + non-zero counts.
- **T4 — idempotency.** 2nd `erase_source("src-A")`: all counts zero; every store byte-identical to
  the post-T3 state; a 2nd (zero-count) `TaskRun` row appended.
- **T5a — source isolation.** A separate `src-B` entity + its landing object + its queue row are fully
  intact after erasing `src-A`.
- **T5b — prefix-collision (B-3 guard).** Datasets `"ofac"` and `"ofac-eu"`; erase `"ofac"` leaves the
  `"ofac-eu"` node, witness map, landing prefix, and queue row untouched (proves the precise JSON
  decision, not `CONTAINS` substring).
- **T6 — no resurrection / no un-merge.** A 3-source merged canonical; erase one contributing source.
  Assert: the canonical SURVIVES with its durable id; `canonical_id_ledger` rows unchanged;
  `get_entity_by_alias` still resolves a surviving member id to the canonical; surviving inbound/
  outbound edges intact.
- **T7 — authorization (unit).** `erase_source` cannot be called without `authorized_by`; the value is
  recorded in the audit row.

---

## 10. Person-affecting / sign-off assessment

**YES — person-affecting; RUNTIME human authorization required (not merely build-gate).** `erase_source`
**deletes a real person's data**. Two-sided:
- It is the data subject's GDPR right — legitimate when the subject requests it.
- It is a **tampering/evasion vector** — an attacker who can trigger it could scrub a sanctioned PEP
  to evade monitoring (the catastrophic inverse of "never auto-merge a sensitive entity").

Per CLAUDE.md ("changes affecting a real person … always need human sign-off"; "active/privileged …
never agent-auto-run") and ADR 0045 §4 (value-retraction is sign-off-gated), the **build gate is not
sufficient** — each erasure **run** needs a human in the loop. Enforced at two levels:
1. **Function contract (this gate):** `authorized_by` is required (no default) and recorded in the
   `TaskRun` audit row. `erase_source` is **not** wired into any autonomous/Hermes path.
2. **Runtime gate (Phase 2, named not built here):** the API/MCP surface exposing erasure is behind a
   Zitadel operator role; no agent token may invoke it.

This is the **one sanctioned exception to append-only**: it performs the GDPR-mandated removal of the
erased source's own data, while **preserving the no-un-merge sub-invariant** (the ledger is never
touched; survivors are never re-clustered or split). It does not touch `DEFAULT_MERGE_THRESHOLD`,
Splink, scores, or `cluster_and_merge` — it never *promotes* or *loosens* anything; it only removes.

---

## 11. Locked invariants the gate must hold

- **G1 — provenance on every node AND edge.** Every *surviving* node/edge retains `prov_*`
  (rebuilt from a surviving witness where `source[0]` was erased). DENY if a survivor is left without
  `prov_source_id`, or if any Gate C provenance test regresses.
- **Append-only / no un-merge.** The `canonical_id_ledger` is never modified; no re-clustering, no
  node split, no resurrection. Node/value deletion is the GDPR exception, runtime-authorized + audited.
  DENY if erase un-merges or resurrects a survivor, or deletes a ledger/judgement row.
- **Canonical-canonical only via the guard.** `erase_source` performs **no merge** — it cannot create
  a canonical-canonical edge. `DEFAULT_MERGE_THRESHOLD` / Splink / `cluster_and_merge` untouched.

---

## 12. Slice plan

Two slices, each individually CI-green and mergeable (slice 2 lands after slice 1).

- **Slice 1 — graph prune (the crux + riskiest).** NEW `src/worldmonitor/graph/ops.py`
  `erase_source_graph(neo4j, source_id) -> GraphErasureCounts` (Tier-1-aware: sole-source DETACH
  DELETE; multi-source witness-prune + X-only-prop REMOVE + `prov_*` rebuild; edge delete; precise JSON
  decision). NEW `tests/integration/test_erasure_graph.py` (T1, T1b, T2, T5b, T6). Graph-only;
  mergeable alone.
- **Slice 2 — cross-store orchestrator.** NEW `src/worldmonitor/erasure.py` `erase_source(*, neo4j,
  session, landing, source_id, authorized_by)` (calls slice 1 + landing prefix delete + queue/
  dead-letter redaction + `TaskRun` audit). `storage/landing.py` gains `delete` + `delete_prefix`. NEW
  `tests/integration/test_erasure.py` (T3, T4, T5a, T7). Depends on slice 1.

## 13. Verdict

**APPROVE (build, property-level v1)** subject to §11 invariants and §9 all-green, with the §7 FLAG
acknowledged. **DENY** if: a multi-source entity loses a non-erased source's data (Y's values / edges /
witnesses); erase resurrects or un-merges a survivor (ledger touched, re-clustering, split); it is not
idempotent or not source-scoped; a sole-source node's VALUES persist in the graph (B-1 not fixed); G1
is broken on a survivor; `authorized_by` is optional or the audit row is not written; or any FROZEN
suite regresses (§11, Gate C provenance / resolution+sign-off / migrations drift).
