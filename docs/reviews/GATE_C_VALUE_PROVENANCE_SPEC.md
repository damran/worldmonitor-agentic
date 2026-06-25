# Gate C — Value-Level Provenance (gate spec)

> **BUILD gate.** Deepens [ADR 0018](../decisions/0018-provenance-as-ftm-context-properties.md)
> (provenance-as-context-keys, LOCKED) and closes audit gap **G1**'s *multi-source* corner. Owns
> [ADR 0045](../decisions/0045-value-level-provenance.md) (PROPOSED).
> Branch `gate/c-value-provenance` off `master@a28de24` (Gate B-front stable-ids slice-1 merged).
>
> **NAMING:** this is **"Gate C — Value-Level Provenance"** — *per-claim source lineage on a merged
> entity*. It is **NOT** the old `GATE_LEDGER.md:63` "Gate C" row, which meant the *cross-run referent
> rewriting / persisted graph-mutation* surface (ADR 0023/0025). That old row is renamed **"Gate
> C-rewrite / cross-run referent surface"** in the ledger + ADR 0045 so there are not two "Gate C"s
> (same class as the prior Gate-A / ADR-0029 collision). Wherever this spec says "Gate C" it means
> value-level provenance; the cross-run rewrite surface is referred to by its new name.

---

## 0. TL;DR

`resolution/merge.py:281-282` seeds a merged entity from `member_ids[0]` then folds the rest in with
`ValueEntity.merge` (`ontology/ftm.py:18`). `ValueEntity.merge` **unions values but binds no lineage** —
it does not record *which source asserted which value*. `provenance/model.py` compounds the loss:
`stamp()` writes `wm_prov_*` context **lists**, but `get_provenance` / `provenance_node_properties`
read only `[0]` (`model.py:41-46,59-75`), so a node carries exactly **one** projected lineage —
`source[0]`'s. **A 3-source merge keeps one lineage and silently drops two.** This is the named debt
ADR 0018:29-30 anticipated ("provenance is currently single-source per entity; multi-source provenance
after a merge collapses to the surviving context values. Revisit if per-claim provenance is needed").

Gate C fuses an already-decided cluster with **`StatementEntity`** (verified `followthemoney 4.9.2`)
so every member's per-`(prop, value, dataset)` statement aggregates under the **durable canonical id**,
and the writer projects lineage in **two tiers**: a compact per-property witness map on every node
(Tier-1, always) and reified `(:Statement)-[:FROM_SOURCE]->(:Source)` nodes for an audit-critical
**allowlist** (Tier-2, gated). A new `delete_source(dataset)` op removes one source's lineage,
source-scoped, leaving others intact. **G1 is PRESERVED (additive, not replaced).** The change is
**lineage-only and person-NEUTRAL** — enforced by a hard fence that the `StatementEntity`-fused value
set is byte-for-byte identical to the current `ValueEntity` path.

---

## 1. Why now (the named debt)

- **ADR 0018:29-30** explicitly flagged this as deferred debt and named the trigger ("Revisit if
  per-claim provenance is needed"). Gate C does **not** overturn 0018 — the storage decision (flat
  `wm_prov_*` context keys) stays; Gate C *deepens* the projection from single-source to per-claim.
- **G1** ("provenance on every node AND edge") is CLOSED structurally
  (`graph/writer.py:133-138` nodes, `:160-172` edges) but is **lossy on a merged node**: the projected
  `prov_*` reflects one source. Gate C makes the merged node's lineage faithful to *all* contributing
  sources without disturbing the existing `prov_*` projection.
- The resolved graph **is the product**. A sanction/PEP/beneficial-ownership claim sourced from one
  feed and corroborated by a second must be traceable to *both*, or the GDPR/audit-log guarantee and
  "de-dupe before counting, calibrate before concluding" are hollow on exactly the high-stakes nodes.

---

## 2. Verify-before-code gate (BLOCKING — mirrors Gate A / Gate B-front)

**No implementation may begin** until `VERIFIED_API.md` (repo root) carries a new **Gate C —
followthemoney StatementEntity** section recording, **verbatim** from `inspect.signature` against the
**installed `followthemoney 4.9.2`** (the authoritative runtime — exactly what executes), every bound
signature with **correct module paths**. A paraphrased, missing, or wrong-module binding is a judge
**DENY** (§10 D-VERIFY). The orient-verified facts the builder MUST reproduce verbatim:

```text
# top-level import works in 4.9.2:
from followthemoney import StatementEntity, Statement, Dataset
# module paths:
#   StatementEntity -> followthemoney.statement.entity.StatementEntity
#   Statement       -> followthemoney.statement.statement.Statement
#   StatementProxy  -> DOES NOT EXIST (ImportError) — do not bind it
Statement(self, entity_id: str, prop: str, schema: str, value: str, dataset: str,
          lang: Optional[str] = None, original_value: Optional[str] = None,
          first_seen: Optional[str] = None, external: bool = False, id: Optional[str] = None,
          canonical_id: Optional[str] = None, last_seen: Optional[str] = None,
          origin: Optional[str] = None)
StatementEntity.merge(self, other: EntityProxy) -> StatementEntity
StatementEntity.from_statements(dataset: Dataset, statements: Iterable[Statement]) -> StatementEntity
StatementEntity.add_statement(self, stmt: Statement) -> None
Dataset.__init__(self, data: Dict[str, Any]) -> None
```

**The verified behaviour that drives the DESIGN** (confirmed against `StatementEntity.merge` /
`add_statement` source — record verbatim in `VERIFIED_API.md`):
- `StatementEntity.merge(other)` **branches on `other`'s type.** If `other` is a `StatementEntity`, it
  re-canonicalizes each of `other`'s statements to `self.id` (`stmt.canonical_id = self.id`) and
  `add_statement`s them — so **all sources' per-`(prop, value, dataset)` statements aggregate under the
  survivor canonical id, lineage intact.** If `other` is a plain `ValueEntity`, it falls to
  `unsafe_add` (the current lineage-LOSING path). **Gate C must feed it `StatementEntity`, not
  `ValueEntity`.**
- `add_statement` stores per-prop in a **`set`** (`self._statements[prop].add(stmt)`), so the fused
  **value** set is the union of members' values — identical to `ValueEntity.merge`'s union. This is
  what makes the value-set-invariance fence (§9) *achievable*: lineage is added, values are unchanged.
- The semantic field mapping: `dataset` = the source (= our `Provenance.source_id`), `origin` = the
  raw-record pointer (= `Provenance.source_record`), `first_seen` = the retrieval timestamp (=
  `Provenance.retrieved_at`); `canonical_id` defaults to `entity_id`.

**Dataset name collision note (record in VERIFIED_API.md):** `from_statements` takes a `Dataset`, and
`StatementEntity` stamps a dataset on statements built without an explicit `dataset` arg. The
*statement-level* `dataset` (per-`Statement`) is what carries our `source_id`; do not conflate the
entity-construction `Dataset` with the per-statement `dataset` — the builder records both call sites.

---

## 3. Scope — exact files / areas

**Production (allow-list — mirrors `.claude/gate.scope`):**

| File | Change |
|---|---|
| `resolution/merge.py` | `_merge_entities` builds `StatementEntity` per member (statements stamped with each member's `Provenance` → `dataset`/`origin`/`first_seen`) and fuses via `StatementEntity.merge` under the canonical id. ValueEntity collapse removed from the merge path. |
| `provenance/model.py` | Add a **multi-source** read/projection: a per-property witness-set view derived from the fused `StatementEntity` (Tier-1 `prop_sources`). The existing `Provenance`/`stamp`/`get_provenance`/`provenance_node_properties` single-source surface is **kept** (G1's `prov_*` stays). |
| `graph/writer.py` | Tier-1: project the `prop_sources` witness map onto `node_props_by_id` (flat `_inject_props`). Tier-2: a NEW gated write pass reifying `(:Statement)-[:FROM_SOURCE]->(:Source)` for allow-listed props, keyed on the durable id. |
| `graph/constraints.py` | (only if needed) a `:Statement`/`:Source` label index/constraint for Tier-2 lookup + idempotent MERGE. |
| `graph/ops.py` (**NEW**) | `delete_source(dataset)` — source-scoped lineage removal (Tier-1 witness-map prune + Tier-2 `DETACH DELETE`). |
| `provenance/audited.py` (**NEW**) | the allow-list loader (`load_audited_properties()` reading `config/audited_properties.yml`). |
| `config/audited_properties.yml` (**NEW**) | the Tier-2 allow-list (small; audit-critical props only). |
| `settings.py` | (only if needed) a field for the allow-list path (default `config/audited_properties.yml`). |
| `pyproject.toml` + `uv.lock` | (only if a YAML loader is needed) add `pyyaml` as a declared dep. **FLAG:** no `config/` dir, no YAML loading, and no `pyyaml` exist today (§3.1). |

**Tests:** `tests/test_provenance_merge.py` (**NEW**, the failing-first multi-source merge proof),
`tests/unit/*` + `tests/integration/*` per §11.

**Docs:** `VERIFIED_API.md`, this spec, `docs/decisions/0045-value-level-provenance.md`,
`docs/decisions/0018-*.md` (note-extend only), `docs/decisions/0023-*.md` (only if edge-prov touched),
`docs/GATE_LEDGER.md` (rename old "Gate C" row + add the value-provenance row), `.claude/gate.scope`.

### 3.1 New-dependency flag — `pyyaml` + a `config/` dir (decide minimally)

There is **no `config/` directory, no YAML loading, and no `pyyaml`** in the repo today (verified:
`pyproject.toml` has no yaml dep). Two acceptable resolutions — the builder picks one and records it:
- **(a)** add `pyyaml` (declared dep + `uv.lock` update) and read `config/audited_properties.yml`. This
  matches the brief's intent and is the smallest *config* surface.
- **(b)** if a new third-party dep is judged not worth it for a 3-line allow-list, use a checked-in
  **`config/audited_properties.toml`** read with the stdlib `tomllib` (Python 3.12, no new dep), or a
  module-level frozen constant in `provenance/audited.py`. The allow-list MUST still be data, not
  scattered literals, so `delete_source`/Tier-2/tests share one source of truth.

Either way the loader lands in `provenance/audited.py` and the file in `config/`. **If the builder adds
`pyyaml`, the dep edit is in scope; if not, `pyproject.toml`/`uv.lock` stay untouched.** Migration
note: this is a **Neo4j-side** gate (§8) — **no Postgres migration** unless a `delete_source` audit
table is added (§7.4); prefer none.

---

## 4. The `StatementEntity` fusion (replacing the `ValueEntity` collapse)

**Current (lossy), `merge.py:281-286`:**
```python
base = by_id[member_ids[0]]
merged = make_entity({**base.to_dict(), "id": canonical_id})   # ValueEntity
for member_id in member_ids:
    merged.merge(by_id[member_id])                              # union values, drop lineage
```

**Gate C:** build a `StatementEntity` for each member whose statements carry that member's
`Provenance` as `(dataset=source_id, origin=source_record, first_seen=retrieved_at)`, then fuse them
under the **durable canonical id** (already set by Gate B-front's `rekey_cluster` at
`pipeline.py:352-355` **before** `write_entities` — so the survivor id Gate C aggregates on is the
durable id, not the `wmc-` fingerprint). The per-member `Provenance` is read with the existing
`get_provenance` (single-source per *member* — correct; a source entity has one source). The fused
entity's per-prop statement sets give the witness map.

Constraints on the fusion:
- **Schema-incompatibility (H-2, ADR 0041) is preserved.** `StatementEntity.merge` raises `InvalidData`
  on no-common-schema exactly as `ValueEntity.merge` does (verified: both call
  `common_schema`). The existing `_merge_entities` `(merged, dropped)` contract and the H-2 re-emit in
  `cluster_and_merge` stay byte-for-byte; only the entity *type* and the lineage binding change.
- **The output must still be a writable entity.** `write_entities` consumes `FtmEntity`; a
  `StatementEntity` is an `EntityProxy` and serialises through `to_dict()` the writer/`ftmg` already
  use. If `ftmg` requires a `ValueEntity` specifically, the merge produces the `StatementEntity` for
  the witness-map derivation **and** the equivalent value entity for the write — but the value entity's
  values MUST equal the `StatementEntity`'s (the fence, §9). The builder records the exact handoff.

---

## 5. The two-tier writer model (`graph/writer.py`)

### Tier-1 — per-property witness map (ALWAYS)

A compact `prop_sources` map on the node: for each property, the **set of datasets that witnessed any
value of that property**, derived from the fused `StatementEntity`'s per-prop statement datasets. It
lands where `node_props_by_id` is built (`writer.py:133-138`) and is projected via the existing flat
`_inject_props` node path — alongside (never replacing) `get_anchors(...)` + `provenance_node_properties(...)`.

**Neo4j encoding (pick one, record it):** Neo4j stores scalars + homogeneous arrays, not maps. So
`prop_sources` is encoded as either (a) a **JSON string** property `prov_witnesses` (one parse on read),
or (b) **flattened** per-prop array properties `prov_src_<prop>` (string[]). Tier-1 must not break the
existing constraints (`graph/constraints.py` uniqueness on anchor ids — untouched) and must coexist with
`prov_*`. The builder records the chosen encoding in `VERIFIED_API.md`/the ADR builder record.

### Tier-2 — reified Statement/Source nodes (ALLOWLIST-ONLY)

A NEW write pass: for each allow-listed property of an entity, MERGE a `(:Statement {…})` node per
`(prop, value, dataset)` statement, a `(:Source {dataset})` node, an entity→`:Statement` edge, and a
`(:Statement)-[:FROM_SOURCE]->(:Source)` edge. **All keyed on the durable canonical id**, so a Tier-2
`:Statement` hangs off the durable node and `resolve_node_id` alias-on-read (`writer.py:175-203`) keeps
working.

**Why gated, not reify-everything:** naïve full reification is `props × values × sources` extra nodes
per entity — node-count blow-up that does not scale. Tier-2 is **gated to a small audit-critical
allow-list** (`config/audited_properties.yml`): sanction status, PEP status, beneficial-ownership. A
property absent from the allow-list is **never** reified (INV, §9; DENY D-TIER2).

**Idempotency:** the Tier-2 pass uses `MERGE` (like the node pass) on a deterministic `:Statement` key
(e.g. the FtM `Statement.id`, or a hash of `(canonical_id, prop, value, dataset)`) so a re-write of the
same batch converges on one `:Statement`, not duplicates. Builder records the key.

---

## 6. `delete_source(dataset)` — source-scoped removal (NEW graph op)

No `delete_source` exists today. It is the GDPR/audit operational dual of value-level provenance: remove
one source's contribution without disturbing the others.

- **Tier-1:** drop `dataset` from every node's `prov_sources` witness map (prune the witness entry).
- **Tier-2:** `DETACH DELETE` the `dataset`'s `:Statement` nodes and prune any `:Source` left with no
  inbound `:FROM_SOURCE` edge. Other datasets' `:Statement`/`:Source` lineage is untouched.
- Source-scoped: a node witnessed by `{A, B, C}` after `delete_source("B")` is witnessed by `{A, C}`,
  with A's and C's statements/witness entries byte-unchanged (acceptance §10 A4).

### 6.1 DECISION (orient §6 FLAG) — value retention vs retraction

**When a property VALUE was witnessed ONLY by the deleted source, does `delete_source`:**
- **(a)** remove only the lineage and **leave the now-unwitnessed value** on the node (default,
  append-only-friendly); OR
- **(b)** also retract the value?

**DECISION: (a) lineage-only by default, plus an audit row** (or a structured log if no audit table is
added — §7.4). Rationale: append-only is the platform invariant; retracting a value about a real person
is **person-affecting** (it changes the fused value set — the exact thing the §9 fence forbids the
person-neutral path from doing). **Value-retraction (b) is OUT OF SCOPE / SIGN-OFF-GATED** — if it is
ever built it must route through `signoff.approve/reject` (human sign-off, never auto-run), versioned +
audited, exactly like a sensitive un-merge. `delete_source` as shipped in Gate C is the lineage-only
(a) path and is therefore person-neutral **except** for the carve-out below.

**The only person-affecting corner:** an orphaned-value left by (a) is now an *unwitnessed* assertion on
the node. That is append-only-safe (the value was already there; we removed only a provenance pointer)
but the audit row MUST record which `(node, prop, value)` became unwitnessed by this `delete_source`, so
a later operator decision (the sign-off-gated (b)) has the evidence. **`delete_source` MUST NOT silently
strand an unwitnessed value with no audit trail** (DENY D-DELSILENT).

---

## 7. Acceptance criteria

Each is a named, frozen test (§11). APPROVE requires **all** of A1–A10; any DENY trigger fails the gate.

### APPROVE

- **A1 — 3-source merge reconstructs 3 lineages.** A cluster of three members (sources `s1,s2,s3`),
  each asserting a distinct value (or corroborating one), fused under the durable id, yields a fused
  entity whose per-prop witness sets cover **all three** datasets — not just `source[0]`. Tier-1
  `prop_sources` contains `{s1,s2,s3}` for the corroborated prop; the adversarial single-source value
  (e.g. a `passportNumber` only `s3` asserts) carries witness `{s3}`.
- **A2 — adversarial single-source value retained.** A value only ONE source has (passport from `s3`)
  is present in the fused value set **and** its witness set is exactly `{s3}` (Tier-1), and — if the
  prop is allow-listed — a Tier-2 `:Statement`→`:Source(s3)` exists.
- **A3 — G1 PRESERVED (additive).** Every node and every edge still carries `prov_*`
  (`writer.py:133-138`, `:160-172`); `test_edge_provenance.py` + `test_graph_writer.py` pass unchanged.
  Tier-1/Tier-2 are *additional* properties/nodes, never a replacement.
- **A4 — `delete_source` source-scoped, other sources intact.** After `delete_source("s2")` on a node
  witnessed `{s1,s2,s3}`: witness set is `{s1,s3}`; `s1`/`s3` Tier-1 entries + Tier-2 Statement/Source
  nodes unchanged; `s2`'s Tier-2 Statements `DETACH DELETE`d; an orphaned `:Source(s2)` pruned.
- **A5 — `delete_source` lineage-only default.** A value witnessed only by the deleted source is
  **left on the node** (value not retracted) and an audit row/log records it as now-unwitnessed.
- **A6 — Tier-2 limited to the allow-list.** Only allow-listed props produce `:Statement`/`:Source`
  nodes; a non-allow-listed prop produces **zero** reified nodes (only its Tier-1 witness entry).
- **A7 — durable-id keying.** Tier-1 witness map + Tier-2 nodes key on the durable canonical id; an
  alias-on-read lookup (`resolve_node_id`) of a superseded member id reaches the same node + its
  Tier-2 statements.
- **A8 — schema-incompat (H-2) unchanged.** The `StatementEntity` path raises `InvalidData` and the
  `(merged, dropped)` re-emit behaves identically; `test_resolution_merge_incompat.py` +
  `test_b6_resolve_incompat.py` pass unchanged.
- **A9 — ADR-0040 anchor-conflict masking preserved.** A two-QID cluster still has its conflicting
  anchor omitted by `get_anchors` (the fused entity's anchor projection is unchanged), and the fused
  value set is identical to the ValueEntity path including this masking (part of A10).
- **A10 — VALUE-SET INVARIANCE FENCE (the load-bearing test).** For a representative set of clusters
  (1-source, 2-source corroborating, 3-source with a unique value, schema-mixed, anchor-conflict),
  the `StatementEntity`-fused **value set is byte-for-byte IDENTICAL** to the current `ValueEntity`-fused
  value set — same `name`/`registrationNumber`/anchors/every property, same multiset of values per
  prop. This proves Gate C is **lineage-only** and person-NEUTRAL (§9).

### DENY (any one fails the gate)

- **D-COLLAPSE** — the merge collapses lineage to `source[0]` (i.e. the fused node carries only one
  source's witness for a multi-source prop). This is the bug the gate exists to kill.
- **D-VALUESET** — the `StatementEntity`-fused value set **differs** from the `ValueEntity` path for any
  cluster (a silent, possibly person-affecting, ER behaviour change). A10 must pass exactly.
- **D-TIER2** — Tier-2 reifies any property **not** on the allow-list (node-count blow-up / scope creep).
- **D-G1** — `prov_*` is removed from any node or edge, or any frozen G1 test (`test_edge_provenance.py`,
  `test_graph_writer.py`) is loosened/skipped/xfailed.
- **D-VERIFY** — code binds a `followthemoney` symbol absent from `VERIFIED_API.md`'s Gate C section, or
  bound to the wrong module (e.g. `StatementProxy`, which does not exist in 4.9.2).
- **D-DELSILENT** — `delete_source` strands an unwitnessed value with no audit row/log, OR retracts a
  value (the sign-off-gated (b) path) outside `signoff.approve/reject`.
- **D-THRESHOLD** — any change to `DEFAULT_MERGE_THRESHOLD`, Splink weights/scores/blocking, or
  `cluster_and_merge`'s who-merges-with-whom. Gate C changes only the **fusion** of an already-decided
  cluster + the writer.
- **D-FROZEN** — any frozen test (§11) is edited (removed assert, added skip/xfail, loosened tolerance).
- **D-MIGRATION** — if a Postgres audit table is added, it is anything other than `0007_*` Revises the
  current head, OR `test_migrations.py` (drift guard) goes red.

### 7.4 Migration assessment (prefer none)

The two-tier model + `:Statement`/`:Source` are **Neo4j-side** — no Postgres schema change. If a
`delete_source` audit table is judged necessary (over a structured stderr log), it is migration
**`0007_*`** Revising the current head and MUST keep `test_migrations.py` (ADR 0030 drift guard) green
(model + migration agree byte-for-byte). **Recommendation: ship the lineage-only audit as a structured
log first; defer the audit table** unless a query surface needs it — keeping Gate C Postgres-free.

---

## 8. Locked invariants this gate MUST hold

- **G1 — provenance on every node AND edge — PRESERVED (additive).** The writer still stamps `prov_*`
  on every node and relationship; Tier-1/Tier-2 are *additional* lineage, never a replacement (A3,
  D-G1).
- **Append-only / no un-merge — PRESERVED.** Tier-2 uses MERGE; the ledger/value set is never silently
  mutated. `delete_source` default (a) removes only *lineage* (provenance pointers), leaving the value —
  the only append-only-adjacent op, fenced by the audit row (§6.1). Value-retraction (b) is sign-off-gated.
- **Value-set invariance (NEW hard INV).** The fused value set MUST equal the current `ValueEntity`
  path byte-for-byte (A10 / D-VALUESET). This is what makes Gate C lineage-only and person-NEUTRAL.
- **Tier-2 allow-list (NEW hard INV).** Only allow-listed props are reified (A6 / D-TIER2).
- **Canonical-canonical only via the guard — PRESERVED.** Gate C does not touch clustering; the merge
  guard (`needs_review`/approved-groups) and ADR-0040 anchor-conflict deferral are upstream and unchanged.
- **Resolve to canonical IDs — STRENGTHENED.** Lineage now keys on the durable canonical id (Gate
  B-front), so multi-source provenance survives re-ingest with the durable id.
- **ER decisions affecting a real person need human sign-off; no silent in-place mutation
  (CLAUDE.md / ADR 0031) — ENFORCED.** Gate C's auto path changes no value (the fence); the only
  person-affecting corner (value-retraction) is sign-off-gated and out of scope.
- **ADR-0036 crash-retry idempotency — PRESERVED.** Tier-2 MERGE + deterministic statement keys
  converge on retry; `test_b1_crash_recovery.py` stays green.

---

## 9. Person-affecting fence (the #1 fence — orient §9)

Gate C is **lineage-only and person-NEUTRAL iff the fused value set does not change.** It MUST NOT
touch `DEFAULT_MERGE_THRESHOLD` / Splink / who-merges-with-whom — clustering is decided upstream in
`cluster_and_merge`, unchanged. Gate C changes only (1) the *fusion* of an already-decided cluster
(`ValueEntity` → `StatementEntity`, lineage added, values unchanged) and (2) the writer (added Tier-1
witness map + Tier-2 reified nodes). The **value-set-invariance fence (A10 / D-VALUESET)** makes this
provable: the `StatementEntity`-fused value set is byte-for-byte identical to the `ValueEntity` path,
including the ADR-0040 anchor-conflict masking. **DENY if the fused value set changes** — that would be
a silent, possibly person-affecting, ER behaviour change.

**The one person-affecting corner is `delete_source` value-retraction (§6.1 (b)).** It is NOT shipped in
the person-neutral path; it is sign-off-gated (`signoff.approve/reject`, never auto-run, versioned +
audited) and out of Gate C scope. The shipped `delete_source` (a) removes only provenance pointers,
leaves the value, and writes an audit trail for any value it leaves unwitnessed (D-DELSILENT).

---

## 10. Out of scope (hard stops)

- Changing `DEFAULT_MERGE_THRESHOLD`, any Splink weight/score/blocking rule, or `cluster_and_merge`
  membership logic (D-THRESHOLD).
- `delete_source` value-retraction as an autonomous (non-sign-off) mutation (D-DELSILENT / fenced §6.1).
- Reifying non-allow-listed properties in Tier-2 (D-TIER2).
- The **cross-run referent-rewriting / persisted graph-mutation surface** (the *old* "Gate C-rewrite"
  row, ADR 0023/0025) and inbound-edge restore on sign-off — a separate deferred gate, untouched here.
- Incremental/streaming-ER (the OPEN fork of ADR 0019); a new datastore or parallel model.
- A live read-path cutover consuming Tier-1/Tier-2 in the API/MCP surface (capability-only this gate;
  same posture as Gate B-front alias-on-read).
- A Postgres migration, unless a `delete_source` audit table is added (then `0007_*` only; §7.4).

---

## 11. Tests

### Failing-test-first (must FAIL on `master@a28de24`, PASS post-fix)

`tests/test_provenance_merge.py` (NEW, test-author):
- **multi-source lineage** — a 3-source merge currently keeps only `source[0]`'s projected lineage
  (`provenance_node_properties` reads `[0]`); the test asserts all three datasets are witnessed. FAILS
  pre-fix (only one survives), PASSES once `StatementEntity` fusion + Tier-1 land. (A1.)
- **adversarial single-source value** — passport from `s3` retains witness `{s3}`. FAILS pre-fix. (A2.)
- **value-set invariance** — the fused value set equals the `ValueEntity` path byte-for-byte. (A10 —
  this one PASSES pre- and post-fix; it is the *fence* that must never go red.)

### Frozen (keep-green, UNCHANGED — a removed assert / added skip / loosened tolerance is a judge DENY)

- `tests/unit/test_resolution.py`
- `tests/unit/test_resolution_canonical_id.py`
- `tests/unit/test_resolution_negative_judgement.py`
- `tests/unit/test_resolution_anchor_conflict.py`
- `tests/unit/test_resolution_distinguishing_evidence.py`
- `tests/unit/test_resolution_multiscript.py`
- `tests/unit/test_resolution_merge_incompat.py`
- `tests/unit/test_provenance.py`
- `tests/unit/test_anchors.py`
- `tests/unit/test_canonical.py`
- `tests/integration/test_resolution_pipeline.py`
- `tests/integration/test_resolution_batching.py`
- `tests/integration/test_b6_resolve_incompat.py`
- `tests/integration/test_b6_signoff_poison.py`
- `tests/integration/test_b1_crash_recovery.py`
- `tests/integration/test_b1_signoff_idempotency.py`
- `tests/integration/test_edge_provenance.py`  (G1 — must stay green)
- `tests/integration/test_graph_writer.py`     (G1 — must stay green)
- `tests/integration/test_graph_queries.py`
- `tests/integration/test_stable_id_graph.py`
- `tests/integration/test_migrations.py`        (drift guard — only `0007` if a table is added)

### New (per slice — §12)

- `tests/unit/test_provenance_witnesses.py` — Tier-1 witness-map derivation from a fused
  `StatementEntity` (per-prop dataset sets; single-source value → singleton set).
- `tests/integration/test_value_provenance_graph.py` — Tier-1 props land on the node; G1 `prov_*`
  coexists; durable-id keying (A7).
- `tests/integration/test_statement_reification.py` — Tier-2 allow-listed props reified; non-allow-listed
  props NOT reified (A6); idempotent MERGE.
- `tests/integration/test_delete_source.py` — source-scoped delete (A4); lineage-only default + audit
  (A5); other sources intact.

---

## 12. Slice breakdown (each individually mergeable + CI-green)

**slice-1 — multi-source fusion + Tier-1 witness map + the fence (person-NEUTRAL, the 3-lineage core).
LAND FIRST.**
- `resolution/merge.py` `StatementEntity` fusion (replacing the `ValueEntity` collapse, under the
  durable id); `provenance/model.py` multi-source witness view (Tier-1 derivation, keeping the
  single-source `Provenance` surface); `graph/writer.py` Tier-1 `prop_sources` projection onto
  `node_props_by_id`.
- Tests: `tests/test_provenance_merge.py` (failing-first), `tests/unit/test_provenance_witnesses.py`,
  `tests/integration/test_value_provenance_graph.py`.
- Proves A1, A2 (Tier-1 half), A3, A7 (Tier-1), A8, A9, **A10 (the fence)**. Changes **no value**.
- Mergeable: quality + security + integration green; `VERIFIED_API.md` Gate C section present; frozen
  tests unchanged; G1 preserved; the fence green. **No Postgres migration.** `human_fork = false`.

**slice-2 — Tier-2 reified Statement/Source nodes (allow-list + config) + `delete_source`. LAND SECOND.**
- `provenance/audited.py` loader + `config/audited_properties.yml` (+ `pyyaml` iff chosen, §3.1);
  `graph/writer.py` Tier-2 gated write pass; `graph/ops.py` `delete_source`; `graph/constraints.py`
  Statement/Source index iff needed; `settings.py` allow-list path field iff needed.
- Tests: `tests/integration/test_statement_reification.py`, `tests/integration/test_delete_source.py`.
- Proves A2 (Tier-2 half), A4, A5, A6, A7 (Tier-2). Tier-2 is allow-list-gated (D-TIER2);
  `delete_source` is lineage-only default (D-DELSILENT); value-retraction is fenced/out of scope.
- Mergeable: quality + security + integration green; allow-list-only reification proven; `delete_source`
  source-scoped + audited. **No Postgres migration** (recommended; if an audit table is added it is
  `0007_*` only). `human_fork = false` (value-retraction path is NOT shipped; if ever built, that
  sub-path is `human_fork = true`, sign-off-gated).

**Land order:** slice-1 FIRST (the lineage-faithful fusion + Tier-1 + the fence — the audit-critical
fix), slice-2 SECOND (scale-out reification + GDPR delete op).

---

## 13. Adversarial target (judge weights heavily)

A property **value only ONE source has** — e.g. a `passportNumber` asserted solely by `source 3` in a
3-source cluster. The gate must: (1) **retain that value** in the fused entity (A2, value-set
invariance A10); (2) Tier-1 witness it as exactly `{s3}` (not `{s1}` from `source[0]`, not `{s1,s2,s3}`);
(3) if the prop is allow-listed, Tier-2 reify a single `:Statement`→`:Source(s3)`; and (4)
`delete_source("s3")` removes **only** that lineage — Tier-1 drops `s3` from that prop's witness set
(leaving the value, with an audit row recording it as now-unwitnessed), Tier-2 `DETACH DELETE`s that one
`:Statement` + prunes the orphaned `:Source(s3)`, while `s1`/`s2`'s statements/witnesses are byte-for-byte
untouched. The pre-fix code fails (1)-(3) (the value's lineage collapses to `source[0]`); (4) has no
implementation today.
