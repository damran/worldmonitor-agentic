# 0045 — Value-level (per-claim) provenance: StatementEntity fusion + two-tier witness model

- **Status:** ACCEPTED (2026-06-27) — **Tier-1 shipped; Tier-2 deferred with an upgrade trigger** (see
  the Acceptance section directly below). Decided with the user during the cross-workflow Phase C fork.
- **Date:** 2026-06-25 (accepted 2026-06-27)
- **Gate:** C — Value-Level Provenance (`docs/reviews/GATE_C_VALUE_PROVENANCE_SPEC.md`). **BUILD gate.**

## Acceptance (2026-06-27) — Tier-1 accepted as the current model; Tier-2 deferred

The cross-workflow comparison (`docs/reviews/CROSS_WORKFLOW_REVIEW.md`) surfaced the sibling line's
reified `:Statement`/`:Source` Tier-2 provenance and asked: is B's Tier-1-only live graph a real gap?
A file:line deep-read (recorded in the cross-workflow review) found the gap is **real but its payoff is
Phase-2-shaped**, and the decision was taken **with the user** to **defer Tier-2**:

- **Tier-1 is ACCEPTED and shipped** (live): `prov_*` on every node+edge (G1) + the per-property
  `prov_witnesses` JSON map (`provenance/model.py`, `graph/writer.py`), source-scoped GDPR erasure
  (`graph/ops.py::erase_source_graph`, **value-complete for sole-source entities** — stronger than the
  sibling's lineage-only `delete_source`). This is sufficient for property-level provenance, the
  catastrophic-merge audit log, and the current (write-side) phase.
- **Tier-2 is DEFERRED, not rejected**: the reified `(:Statement)-[:FROM_SOURCE]->(:Source)` write pass,
  the `config/audited_properties.yml` allow-list + loader, and value-level `delete_source` remain
  **specified-but-unbuilt**. They are additive (no Tier-1 rework needed to add them later).
- **What deferral costs:** per-value attribution, in-Cypher value-level conflict detection, and
  value-level GDPR erasure *inside a multi-witness property* stay Python-side / unavailable until built.
  (The erasure corner is tracked as `WM-ERASE-T2` in [0049](0049-cross-store-gdpr-source-erasure.md).)

### Tier-2 upgrade trigger (build it when EITHER holds)
1. The **Phase-2 read API/MCP** needs to expose **per-statement lineage** (e.g. `GET /entity/{id}/statements`
   listing per-source values, timestamps, reliability) or in-Cypher value-level conflict surfacing; OR
2. **Value-level GDPR erasure inside a multi-witness property** becomes a requirement (the `WM-ERASE-T2`
   follow-up) — i.e. removing one source's specific value from a property other sources also witness.

### Reversibility
Additive / low-regret. Building Tier-2 later does **not** rework Tier-1 (it adds a write pass + delete
branch gated by an allow-list). Estimated cost when triggered: ~2–3 days + 3–5× write amplification on
allow-listed props only. Until a trigger fires, Tier-1 + the witness map is the accepted model.
- **Deepens (does NOT overturn):** [0018](0018-provenance-as-ftm-context-properties.md) — 0018:29-30
  explicitly anticipated this debt ("provenance is currently single-source per entity; multi-source
  provenance after a merge collapses to the surviving context values. Revisit if per-claim provenance is
  needed"). Gate C is that revisit; 0018's storage decision (flat `wm_prov_*` context keys) **stays**.
- **Preserves (does NOT relitigate):** the G1 `prov_*` projection on every node AND edge
  (`graph/writer.py`); [0040](0040-er-anchor-conflict-negative-evidence.md) (anchor-conflict masking via
  `get_anchors`); [0044](0044-anchor-preferred-stable-ids.md) (durable canonical id — Gate C aggregates
  lineage on the durable id `rekey_cluster` already sets); [0041](0041-resolution-signoff-integrity.md)
  (H-2 schema-incompat re-emit); [0031](0031-return-to-block-signoff.md) (sign-off);
  [0042](0042-single-tenancy-teardown.md) (native `{id}` MERGE); `DEFAULT_MERGE_THRESHOLD=0.92` and the
  Splink weights (Gate A slice-2 owns those).
- **Touches:** `resolution/merge.py`, `provenance/model.py`, `graph/writer.py`, `graph/ops.py` (NEW),
  `provenance/audited.py` (NEW), `config/audited_properties.yml` (NEW), `graph/constraints.py` (iff a
  Statement/Source index is needed), `settings.py` (iff an allow-list-path field is needed),
  `pyproject.toml`+`uv.lock` (iff `pyyaml` added). Tests in `tests/`.

> ## Naming note — the "Gate C" collision (resolved here)
> `docs/GATE_LEDGER.md` already had a deferred row labelled **"Gate C"** meaning the OLD *cross-run
> referent-rewriting / persisted graph-mutation* surface (ADR 0023/0025) — **unrelated** to this
> value-level-provenance gate. To avoid two "Gate C"s (same class as the prior Gate-A / ADR-0029
> collision), the old row is **renamed "Gate C-rewrite / cross-run referent surface"** in the ledger,
> and "Gate C" now denotes **value-level provenance** (this ADR). The cross-run rewrite surface remains
> deferred and out of scope here.

## Context

`resolution/merge.py:281-282` seeds a merged entity from `member_ids[0]` then folds the rest in with
`ValueEntity.merge` (`ontology/ftm.py:18`). `ValueEntity.merge` **unions values but binds no lineage** —
it records nothing about *which source asserted which value*. `provenance/model.py` compounds it:
`stamp()` writes `wm_prov_*` context **lists**, but `get_provenance` / `provenance_node_properties` read
only `[0]` (`model.py:41-46,59-75`), so a node carries exactly one projected lineage — `source[0]`'s.
**A 3-source merge keeps one lineage and silently drops two.** On a sanction/PEP/beneficial-ownership
node, that defeats the GDPR/audit-log guarantee and "de-dupe before counting, calibrate before
concluding" on exactly the high-stakes claims the product exists to assert.

This is the named debt of ADR 0018 (its §Consequences ⚠️ bullet). The writer already upholds G1
(provenance on every node AND edge, `writer.py:133-138` / `:160-172`) — but the projection is *lossy*
on a merged node. Gate C makes the merged node's lineage faithful to all contributing sources, additive
to G1, without disturbing the existing `prov_*` projection.

### Verified finding driving the design (followthemoney 4.9.2)

Verified verbatim against the installed runtime (recorded in `VERIFIED_API.md`):
`StatementEntity.merge(other)` **branches on `other`'s type.** If `other` is a `StatementEntity`, it
re-canonicalizes each statement to `self.id` and `add_statement`s it — so **all sources' per-`(prop,
value, dataset)` statements aggregate under the survivor canonical id, lineage intact.** If `other` is a
plain `ValueEntity`, it falls to `unsafe_add` — the current lineage-losing path. `add_statement` stores
per-prop in a **`set`**, so the fused *value* set is the union of members' values — **identical** to
`ValueEntity.merge`'s union. (`StatementProxy` does NOT exist in 4.9.2 — do not bind it.) That set-union
equivalence is what makes a lineage-only, value-preserving change possible.

## Decision

### 1. Fuse with `StatementEntity`, not `ValueEntity` — on the durable id

`resolution/merge.py` builds a `StatementEntity` per cluster member whose statements carry that member's
`Provenance` as `dataset = source_id`, `origin = source_record`, `first_seen = retrieved_at`, then fuses
them via `StatementEntity.merge` under the **durable canonical id** (already set by Gate B-front's
`rekey_cluster` at `pipeline.py:352-355`, *before* the write). The H-2 schema-incompat `(merged,
dropped)` contract (ADR 0041) is preserved — `StatementEntity.merge` raises `InvalidData` on no-common-
schema exactly as `ValueEntity.merge` does.

**Provenance-semiring rationale (Green / Karvounarakis / Tannen, *Provenance Semirings*, PODS 2007):**
provenance under union is an additive operation — `merge ≡ "+"` in the semiring. The current
`ValueEntity.merge` discards the "+" annotations (keeps the value, drops the lineage); `StatementEntity`
is the annotation-preserving "+": values union (the data) **and** their source annotations accumulate
(the provenance). This is why values are invariant under the change while lineage becomes faithful — it
is the semiring's defining property, not a coincidence.

**Rejected alternative (A):** keep `ValueEntity` and store multi-source provenance as a richer context
structure. Rejected — FtM `merge_context` hashes context values and cannot merge nested dicts (the very
reason ADR 0018 chose flat scalar lists); a per-claim structure cannot survive `merge_context`.
`StatementEntity` is FtM's own native answer to exactly this problem.

### 2. Two-tier writer model

- **Tier-1 (ALWAYS):** a compact per-property **witness-set map** (`prop_sources`) on every node,
  derived from the fused `StatementEntity`'s per-prop dataset sets, projected via the existing flat
  node path (`writer.py:133-138`) **alongside** (never replacing) `get_anchors` + `prov_*`. Encoded as a
  JSON-string or flattened per-prop arrays (Neo4j stores scalars/arrays, not maps) — builder records the
  encoding. Cheap, unconditional, audit-everywhere.
- **Tier-2 (ALLOWLIST-ONLY):** reified `(:Statement)-[:FROM_SOURCE]->(:Source)` nodes + an
  entity→`:Statement` edge, a NEW write pass gated by `config/audited_properties.yml`. **Rejected
  alternative (B): reify everything.** Naïve full reification is `props × values × sources` extra nodes
  per entity — node-count blow-up that does not scale. Tier-2 is gated to a small audit-critical
  allow-list (sanction status, PEP, beneficial-ownership). Both tiers key on the **durable** id, so
  Tier-2 nodes hang off the durable node and `resolve_node_id` alias-on-read (`writer.py:175-203`) keeps
  working. Tier-2 MERGEs on a deterministic key for idempotency.

### 3. The allow-list — `config/audited_properties.yml` + a loader

A new `provenance/audited.py` loads the Tier-2 allow-list from `config/audited_properties.yml`. **There
is no `config/` dir, no YAML loading, and no `pyyaml` in the repo today.** The builder picks and records
one minimal resolution: (a) add `pyyaml` (declared dep + `uv.lock`); or (b) stdlib `tomllib` over a
`.toml` (no new dep) / a frozen constant. The allow-list MUST be single-source data shared by
Tier-2 + `delete_source` + tests.

### 4. `delete_source(dataset)` — source-scoped removal + the value-retention decision

A new `graph/ops.py` op removes one source's contribution: Tier-1 prunes `dataset` from each node's
witness map; Tier-2 `DETACH DELETE`s `dataset`'s `:Statement` nodes and prunes orphaned `:Source`. Other
datasets' lineage is untouched.

**Decision (the one genuine product choice — orient §6 FLAG):** when a value was witnessed ONLY by the
deleted source, `delete_source` **(a) removes only the lineage and LEAVES the now-unwitnessed value
(default), plus an audit row** recording the `(node, prop, value)` that became unwitnessed.
Value-retraction (b) — removing the value too — is **OUT OF SCOPE and SIGN-OFF-GATED**: retracting a
value about a real person is person-affecting and changes the fused value set (the very thing the §5
fence forbids the auto path from doing). If (b) is ever built it routes through
`signoff.approve/reject`, never auto-run, versioned + audited — like a sensitive un-merge. The shipped
op is the append-only-friendly (a). `delete_source` MUST NOT silently strand an unwitnessed value with
no audit trail.

This is **not** an OPEN fork: (a) is the only option consistent with the append-only invariant for the
autonomous path, and (b) is fenced to the existing sign-off mechanism — the human decision point is the
sign-off gate, not an unresolved ADR. Recorded here so the choice is auditable.

### 5. The value-set-invariance fence (the #1 person-safety property)

Gate C is **lineage-only and person-NEUTRAL iff the fused value set does not change.** A required test
asserts the `StatementEntity`-fused value set is **byte-for-byte identical** to the current
`ValueEntity` path for every cluster shape (1-/2-/3-source, schema-mixed, anchor-conflict), including
the ADR-0040 anchor-conflict masking via `get_anchors`. The gate **DENIES if the fused value set
changes** (a silent, possibly person-affecting, ER behaviour change). Gate C does **not** touch
`DEFAULT_MERGE_THRESHOLD`, Splink, or `cluster_and_merge`'s who-merges-with-whom — clustering is decided
upstream, unchanged; Gate C changes only the fusion of an already-decided cluster + the writer.

### 6. G1 preserved (additive)

The writer still stamps `prov_*` on every node AND edge. Tier-1 witness map + Tier-2 reified nodes are
*additional* lineage, never a replacement; `test_edge_provenance.py` + `test_graph_writer.py` pass
unchanged.

## Person-affecting fencing (CRITICAL)

1. **Value-set invariance** — the fused value set is unchanged (the fence, §5). The auto path changes no
   value about any real person; it only makes existing values' lineage faithful.
2. **`delete_source` value-retraction** is the only person-affecting corner — NOT shipped in the
   auto path; sign-off-gated and out of scope (§4). The shipped `delete_source` (a) removes only
   provenance pointers and audits any value it leaves unwitnessed.
3. **Tier-2 allow-list** bounds the reification surface to audit-critical props — no scope creep, no
   node-count blow-up.

## Alternatives considered

- **(A) Keep `ValueEntity`, encode multi-source provenance in context.** Rejected — `merge_context`
  cannot merge nested structures (ADR 0018's founding constraint). `StatementEntity` is FtM's native
  per-claim model.
- **(B) Reify every property in Tier-2.** Rejected — `props × values × sources` node blow-up; does not
  scale. Allow-list gating (Tier-2) + a compact Tier-1 witness map on every node is the bounded design.
- **(C) `delete_source` retracts unwitnessed values by default.** Rejected for the auto path — violates
  append-only and is person-affecting; fenced to sign-off (§4).
- **(D) A Postgres `:Statement` mirror.** Rejected — the lineage lives where the graph is (Neo4j);
  duplicating it in Postgres is a parallel model (CLAUDE.md: no parallel datastore). A `delete_source`
  *audit* row (only) may land in Postgres `0007_*` if a query surface needs it — preferred as a log first.

## Consequences

- A multi-source merge now reconstructs **all** contributing lineages: a 3-source merge yields a fused
  entity witnessed by all three datasets, and an adversarial single-source value (a passport from one
  feed) retains exactly its one witness.
- G1 is faithful on merged nodes, additively — `prov_*` unchanged, witness map + reified statements
  added.
- `delete_source` gives a source-scoped GDPR/audit removal that leaves other sources intact; the
  lineage-only default keeps append-only, with value-retraction fenced to sign-off.
- The value-set-invariance fence makes the change provably lineage-only and person-neutral.
- **Scope caveat:** Tier-1/Tier-2 are a write-side + capability delivery; no live API/MCP read path
  consumes them yet (same posture as Gate B-front's alias-on-read). Read-side cutover is a follow-up.
- Migration: Neo4j-side only; **no Postgres migration** recommended (a `delete_source` audit log over a
  table). If a table is added it is `0007_*` and must pass the `test_migrations.py` drift guard (ADR 0030).

## Out of scope (hard stops)

Changing `DEFAULT_MERGE_THRESHOLD` / any Splink weight/score/blocking rule / `cluster_and_merge`
membership; `delete_source` value-retraction as an autonomous mutation (fenced to sign-off);
Tier-2 reification of non-allow-listed props; the **cross-run referent-rewriting / persisted
graph-mutation surface** ("Gate C-rewrite", ADR 0023/0025) and inbound-edge restore on sign-off;
incremental/streaming-ER (OPEN fork of ADR 0019); a new datastore / parallel model; a live read-path
cutover. The `followthemoney` API used MUST be verified verbatim before any code (`VERIFIED_API.md`;
spec §2) — a paraphrased/unverified binding is a judge DENY.

## Builder record (resolved at acceptance, 2026-06-27)

**Tier-1 (shipped) — DONE:**
- ✅ Verbatim `StatementEntity` / `Statement` / `Dataset` signatures + the `merge` type-branch evidence
  recorded in `VERIFIED_API.md`.
- ✅ `StatementEntity`→writer handoff: the fused `StatementEntity`'s per-prop dataset sets are projected
  via `resolution/merge.py::_witness_map_from_statements` → `stamp_witness_map` → the flat node path in
  `graph/writer.py`; the value-set-invariance fence (below) proves the written value set matches the
  `ValueEntity` path.
- ✅ Tier-1 Neo4j encoding chosen: **JSON string `prov_witnesses`** (`{prop: [datasets]}`), projected
  alongside `get_anchors` + `prov_*` (`provenance/model.py::witness_node_properties`, `graph/writer.py`).
- ✅ Value-set-invariance fence passes for every cluster shape (`tests/unit/test_provenance_witnesses.py`
  + the merge/witness suites) — the change is lineage-only / person-neutral.
- ✅ `delete_source`: shipped as the Tier-1 op `graph/ops.py::erase_source_graph` (ADR 0049),
  value-complete for sole-source nodes; an audit row lands via the `TaskRun(kind="erase")` record (no
  separate `0007_*` table needed — the log sufficed).

**Tier-2 (DEFERRED, 2026-06-27 — see the Acceptance section's upgrade trigger) — NOT built:**
- ⏸ The Tier-2 `:Statement` MERGE key (FtM `Statement.id` vs a `(canonical_id, prop, value, dataset)`
  hash) — to be chosen when Tier-2 is built.
- ⏸ `pyyaml` (a) vs stdlib `tomllib`/constant (b) for the allow-list, and the final allow-list — deferred.
  (Lean: option (b), a stdlib/constant allow-list, to avoid a new dep — but not binding until built.)
- ⏸ The reified `(:Statement)-[:FROM_SOURCE]->(:Source)` write pass + value-level `delete_source` branch.
- ⏸ Value-level erasure inside a multi-witness property → tracked as `WM-ERASE-T2` (ADR 0049).
