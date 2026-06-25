# Gate D — Abstract `Thing`-Range Edge Materialization (gate spec)

> Status: **PROPOSED** (BUILD gate; failing-test-first) · 2026-06-25
> Branch: `gate/d-abstract-edges` off `master@720841f` (Gate C slice-1 merged; cut clean)
> ADR: [`docs/decisions/0046-abstract-thing-range-edges.md`](../decisions/0046-abstract-thing-range-edges.md) (PROPOSED)
> Addresses: **ADR 0023 item 2** ("No materialization of abstract `Thing`-range entity-links") · Closes audit gap **G3**
> Person-affecting: **NO** — graph structure only (see §9). No sign-off required.

---

## 1. Why (the gap, re-confirmed against the installed code)

"The resolved entity graph is the product" and "resolve to canonical IDs" are non-negotiable
(CLAUDE.md). `followthemoney-graph` (ftmg 0.1.0, pinned `pyproject.toml:14` / `uv.lock`, imported
only in `src/worldmonitor/graph/writer.py`) projects FtM entities into Neo4j but keys edge
projection on the **range SCHEMA**, and `ftmg/config.py:67-70` registers a node schema config only
for `not schema.edge and not schema.abstract`. The abstract base `Thing` therefore has **no**
`config.nodes.schemata` entry, so every entity-link whose property range is `Thing` is dropped at
the range-schema lookup. There are **TWO drop sites** — this gate handles BOTH:

- **Drop site 1 — `ftmg/transform.py:generate_entity_links` (line 198).** Correctly filters
  `prop.type == registry.entity` at line **220**, but then drops at **227-229**
  (`config.nodes.schemata.get(prop.range.name)` is `None` for abstract `Thing`). Verified affected
  (non-edge-schema source): `Sanction.entity` (the headline OFAC failure), `Note.entity`,
  `Risk.entity`, `Similar.candidate`, `Similar.match`, `Address.things`.
- **Drop site 2 — `ftmg/transform.py:generate_edge_entity` (line 291).** Drops at **317-322** when
  an edge schema's `source_prop.range` / `target_prop.range` is abstract. Verified affected:
  `UnknownLink.subject/object → Thing`, `Documentation.entity → Thing`,
  `CourtCaseParty.party → Thing`.

**NOT affected (concrete range — the regression set that must STAY contracting):**
`Ownership.owner/asset` (→LegalEntity/Asset), `Directorship.director` (→LegalEntity),
`Membership.member` (→LegalEntity), `Associate.associate`/`Family.relative` (→Person),
`Representation.agent` (→LegalEntity), `Occupancy.holder` (→Person), and the H3 frozen line
`Person.addressEntity` (→Address). Verified via `model.get(...).get(prop).range.abstract` against
installed FtM (recorded verbatim in `VERIFIED_API.md`, §2).

**Live evidence:** the smoke run reports `graph_edges=0` for a Sanction→entity dataset (the
OpenSanctions OFAC slice), and the standing regression
`tests/integration/test_entity_link_materialization.py:117-122` asserts `sanction_edges == 0`
TODAY — i.e. the bug is pinned as accepted debt (ADR 0023 item 2 / GATE_LEDGER.md G3 row, status
OPEN). Until this lands, `Sanction → entity` and similar links are not traversable; analysis over
those relationships is impossible.

---

## 2. Verify-before-code (BLOCKING — mirrors every prior gate)

**NO implementation may begin** until `VERIFIED_API.md` carries a
**"Gate D — followthemoney-graph abstract-range edge projection"** section recording — **VERBATIM,
against the installed ftmg 0.1.0** (not this spec, not the brief, not upstream docs) — every item
below with the **correct module path**. A missing, paraphrased, or wrong-path entry is a judge
**DENY (D-VERIFY)**.

Required (all at `.venv/lib/python3.12/site-packages/ftmg/transform.py` unless noted):

- `generate_entity_links(config: Configuration, proxy: ValueEntity) -> Generator[QueryBatch, None, None]` (line **198**) — drop site 1.
- `generate_edge_entity(config: Configuration, proxy: ValueEntity) -> Generator[QueryBatch, None, None]` (line **291**) — drop site 2.
- `class QueryBatch(NamedTuple)` with fields `query: str`, `params: QueryParams` (line **26**).
- `class QueryBatcher` (line **374**); methods `add(batch)`, `consume(batches)`, `flush_query(query)`, `flush()`.
- `ENTITY_LABEL = "Entity"` (line **21**) — the base label, the abstract-range fallback target.
- `registry.entity` and `registry.entity.node_id(value)` (`from followthemoney.types import registry`; also re-exported `from followthemoney import registry` — `writer.py:23`). Record that `registry.entity.node_id('org-1') == 'entity:org-1'` (the prefix `_align_entity_link_ids` strips, `writer.py:91`).
- The **upstream behaviour being overridden**, quoted verbatim:
  - drop site 1: `srconfig = config.nodes.schemata.get(prop.range.name)` then `if srconfig is None or sconfig.ignore: return/continue` (lines **227-229**);
  - drop site 2: `ssconfig/tsconfig = config.nodes.schemata.get(<prop>.range.name)` then the `is None` guards (lines **317-322**);
  - the abstract-exclusion at `ftmg/config.py:67-70` (`if not schema.edge and not schema.abstract: schemata[schema.name] = {}`) AND `config.py:73` (`raise ValueError` if a schema config references an abstract/edge schema — so the fork MUST NOT register `Thing` in `config.nodes.schemata`; it must use the `ENTITY_LABEL` fallback instead).
- The imported-from-upstream symbols the fork re-exports unchanged: `QueryBatch`, `QueryBatcher`, `generate_node_entity`, `generate_topic_labels`, `get_schema_labels`.
- A pinned-version line: ftmg `0.1.0` (`pyproject.toml`, `uv.lock`), imported only in `writer.py` (+ the new `ftmg_fork/`).

The FtM model facts (record verbatim, from `model.get(...).get(prop).range.abstract`): the
abstract-range affected set and the concrete-range NOT-affected set listed in §1.

---

## 3. Scope (exact files / areas)

The new ftmg boundary lives in **one package**. The writer touches only its import block and Pass-2
loop. Ghost exclusion is structural (write runs after the guard, §6) plus one defensive assertion.

| Area | File(s) | Change |
|------|---------|--------|
| **New thin override** | `src/worldmonitor/graph/ftmg_fork/__init__.py`, `src/worldmonitor/graph/ftmg_fork/transform.py` | Override ONLY `generate_entity_links` + `generate_edge_entity`. Re-export `QueryBatch`, `QueryBatcher`, `generate_node_entity`, `generate_topic_labels`, `get_schema_labels`, `ENTITY_LABEL` from upstream ftmg. |
| **Writer boundary** | `src/worldmonitor/graph/writer.py` | Import block (25-32): import the 2 generators from `worldmonitor.graph.ftmg_fork` instead of `ftmg.transform`; keep the rest from upstream. Pass-2 loop (177-178): the new edges flow through the same `_align_entity_link_ids` + `_with_props(edge_props=edge_prov)` path. Update the H3/G3 docstrings (lines 100-102, 11-12) that currently say "G3 stays deferred". |
| **Constraints** | `src/worldmonitor/graph/constraints.py` | ADD an index/constraint for `:Ghost` IFF the ghost-tagging needs one (`CREATE INDEX ghost IF NOT EXISTS FOR (n:Ghost) ON (n.id)`). Do NOT touch the canonical-anchor uniqueness constraints. |
| **Ghost exclusion guard** | `src/worldmonitor/resolution/review.py` and/or `resolution/canonical.py` | ONLY IF a defensive guard lands (a `:Ghost` is never a cluster member / merge survivor / anchor). See §6 — this is structurally true by ordering; the guard is a belt-and-braces assertion + test. |
| **Failing test (invert)** | `tests/integration/test_entity_link_materialization.py` | INVERT lines 116-122 (`sanction_edges == 0` → assert a materialized `(:Sanction)-[:ENTITY]->(:Organization)`). KEEP the H3 line 109-114 FROZEN. |
| **New unit/integration test** | `tests/test_abstract_edge.py` | The ghost / idempotency / second-site / corroboration-exclusion cases (§7). |
| **Verify gate** | `VERIFIED_API.md` | The Gate D section (§2). |
| **Docs** | `docs/reviews/GATE_D_ABSTRACT_EDGES_SPEC.md` (this), `docs/decisions/0046-abstract-thing-range-edges.md`, `docs/decisions/0023-edge-materialization-v0-limitations.md` (note-extend item 2 → CLOSED-by-0046), `docs/GATE_LEDGER.md` (G3 row → CLOSED). | |
| **Blast contract** | `.claude/gate.scope` | Overwrite Gate-C's. |

**Out of bounds (DENY if touched):** `DEFAULT_MERGE_THRESHOLD`, any Splink weight/score/blocking,
`cluster_and_merge` membership, `pick_anchor`'s tier precedence, who-merges-with-whom, the
referent-rewriting sweep (G2), any Postgres migration (§8), the live API/MCP read path.

---

## 4. The fix — a THIN OVERRIDE, not a fork-as-foundation

CLAUDE.md: cloned repos are "adopted / depended on / wrapped — never forked as foundation." The fix
is a **wrapper module** that overrides exactly the **2** generators and imports everything else from
upstream ftmg 0.1.0. The override re-keys the target lookup off the **range schema** and onto
**`prop.type == registry.entity`** (the type-level test that is already correct at upstream line
220), with a **fallback to the base `Entity` label** (`ENTITY_LABEL = "Entity"`) when the range
schema is abstract/absent — because every node ftmg writes already carries the `:Entity` label
(`generate_node_entity`), and `config.py:73` forbids registering the abstract `Thing` in
`config.nodes.schemata`.

### 4.1 Drop site 1 — `generate_entity_links` override
Replicate upstream lines 198-250, but replace the **227-229** range-schema lookup with:
- keep `if prop.type != registry.entity or prop.range is None: continue` (line 220, unchanged);
- resolve the **target label**: `srconfig = config.nodes.schemata.get(prop.range.name)`; if it is
  `None` or `srconfig.ignore` **AND** the range is abstract → use `target_label = ENTITY_LABEL`
  (the `:Entity` base) instead of returning; else use `srconfig.label`.
- the `MERGE (s)-[r:{rel}]->(t)` query is unchanged in shape; `s` matched on `{sconfig.label}`, `t`
  on `{target_label}`.

### 4.2 Drop site 2 — `generate_edge_entity` override
Replicate upstream lines 291-371, but replace the **317-322** source/target range-schema lookups
with the same fallback: when `source_prop.range` / `target_prop.range` is abstract and has no
`config.nodes.schemata` entry, match the endpoint on `ENTITY_LABEL`. The endpoint `:Entity` label
makes `UnknownLink.subject/object → Thing` materialize. The idempotency-by-`id` `OPTIONAL MATCH …
WHERE existing.id = item.props.id … CREATE` form (upstream 347-356) is preserved unchanged (edge
schemas carry the FtM edge id; idempotency is by that id).

### 4.3 Relationship type (deterministic)
The rel-type is derived deterministically from the property — for the entity-link path use the
property's configured `pconfig.label` (upstream `config.edges.properties.get(prop.qname)`) exactly
as upstream does; if no edge config exists for an affected prop, derive a stable uppercase rel-type
from the prop name (e.g. `Sanction.entity → :ENTITY`). The mapping MUST be deterministic and
re-derivable so re-projection produces the SAME rel-type (idempotency, §5). Project
`(:Sanction)-[:ENTITY {prov_*}]->(target)`.

---

## 5. Idempotent edge key + durable-id endpoints + edge provenance

- **Endpoints are the DURABLE canonical ids.** By `resolve_pending` ordering (verified
  `pipeline.py:352-447`): `cluster_and_merge` → `rekey_cluster(durable_id)` → `needs_review` (the
  guard) → `build_referent_map`/`rewrite_referents` (G2, rewrites entity-typed values to surviving
  canonical ids) → `write_entities`. So by the time the fork runs in `write_entities`, every
  endpoint value is already a durable canonical id and every merged-away referent is already
  rewritten to its survivor (Gate B-front / ADR 0044; G2 / ADR 0025).
- **Idempotent by MERGE.** The entity-link path uses
  `MERGE (s)-[r:REL]->(t) ON CREATE SET r = item.props`, keyed on **(source durable id, target
  durable id, rel-type)**. Re-projection of the same assertion creates **no duplicate edge** (MERGE
  is the dedup). The endpoint ids realign via `_align_entity_link_ids` (writer.py:94-109), which
  strips the `entity:` prefix `registry.entity.node_id` adds — the new edges flow through that same
  realignment (verified `registry.entity.node_id('org-1') == 'entity:org-1'`).
- **Edge provenance (G1 — non-negotiable).** The new edge MUST carry the **asserting `Sanction`
  entity's `prov_*`** via `_with_props(batch, edge_props=edge_prov)` (writer.py:169/178). The
  asserting entity is the property-holder (`Sanction`/`Note`/…), exactly as for every other
  entity-link today. NO edge without `prov_*`.

---

## 6. `:Ghost` — the dangling-endpoint decision (recorded in ADR 0046)

A `Sanction → target` whose `target` id was **never ingested as a concrete entity** would MATCH-miss
and silently drop the edge again. The fork instead **MERGEs the target node and tags it `:Ghost`**:
a typed traversal-only endpoint that preserves the assertion's edge while being structurally inert
to resolution. (Distinct from **G2** merged-away referents, which are already rewritten to their
survivors BEFORE write — a ghost is a never-seen id, not a collapsed one.)

A `:Ghost` MUST be excluded from THREE surfaces:

1. **Anchoring / canonical-id derivation** (`resolution/canonical.py:pick_anchor`). A ghost is never
   a cluster member, so it can never be an anchor source or a durable-id survivor. **Structurally
   true:** ghosts are minted at write-time, after `cluster_and_merge`; they are never in `by_id` /
   `members`. The gate adds a test that proves it (and, if a defensive guard lands, asserts
   `pick_anchor` ignores any `:Ghost`-tagged input).
2. **The merge guard** (`resolution/review.py:needs_review`). A ghost is never a merge survivor.
   Same structural argument — never a `cluster.member_id`. Test-proven; optional defensive guard.
3. **Corroboration** (Gate B-abjad — **NOT built yet**). This is a **FORWARD-DEPENDENCY constraint**
   ADR 0046 mandates on the future corroboration gate: a `:Ghost`, and any materialized
   `Sanction→target` edge, MUST NEVER count as independent corroboration that could lower a merge
   bar for a real person. See §6.1.

### 6.1 The corroboration-exclusion fence (HARD INV — DENY-able)
The one place Gate D touches person-relevant logic is this fence: the new `Sanction→target` edge and
its `:Ghost` endpoints must be **excluded from corroboration / auto-merge**. A materialized edge must
never silently lower a merge bar (CLAUDE.md catastrophic-merge invariant). This gate ships **no**
threshold/Splink change and adds **no** corroboration code (Gate B-abjad does not exist yet) — it
ships the **constraint and the test** so the fence is locked before the surface that could violate it
is built. DENY (**D-CORROB**) if any code in this gate makes a materialized edge or a ghost count
toward a merge decision, OR if the fence is not recorded as a forward-constraint in ADR 0046.

---

## 7. Tests

### 7.1 Failing-test-first (the invert)
`tests/integration/test_entity_link_materialization.py:116-122` — INVERT the G3 boundary. Today it
asserts `sanction_edges == 0`. The gate rewrites those lines to assert a materialized
`(:Sanction {id:'san-1'})-[:ENTITY]->(:Organization {id:'org-1'})` edge (≥1), using the existing
`san-1` (`entity:["org-1"]`) + `org-1` Organization fixtures (lines 79-96). This test **FAILS on
`master@720841f`** (the edge is dropped) and **PASSES post-fix**. The H3 concrete-range assertion
(lines 109-114, `Person.addressEntity → Address`) is **FROZEN** — must stay green byte-for-byte.

### 7.2 New cases — `tests/test_abstract_edge.py`
Each named, each must fail pre-fix where applicable:
- **`test_sanction_entity_edge_materializes`** — a real `Sanction.entity → Organization`
  materializes (drop site 1). Fails pre-fix.
- **`test_every_entity_prop_gets_an_edge`** — every Sanction with an entity-prop (and a present
  target) gets ≥1 edge.
- **`test_unknownlink_second_site_materializes`** — `UnknownLink.subject/object → Thing`
  materializes (drop site 2). Fails pre-fix.
- **`test_reprojection_idempotent`** — running the write twice produces NO duplicate edge (MERGE).
- **`test_ghost_target_tagged_and_excluded`** — a `Sanction → target` whose target was never
  ingested: the target node is created and tagged `:Ghost`; the ghost never appears as a cluster
  member, never anchors (`pick_anchor` over the batch returns no ghost id), never a merge survivor
  (`needs_review` never flags it). THE ADVERSARIAL TARGET (judge weights heaviest).
- **`test_ghost_not_independent_corroboration`** — asserts the fence (§6.1): the materialized
  edge / ghost is structurally excluded from any corroboration count (a forward-constraint assertion;
  may be a structural/contract test since Gate B-abjad is unbuilt).
- **`test_edge_carries_asserting_prov`** — the new edge carries the Sanction's `prov_*` (G1).
- **`test_concrete_range_still_contracts`** — Ownership/Directorship still materialize (the
  regression set is unbroken).

### 7.3 FROZEN (keep-green, byte-for-byte — a removed assert / added skip / loosened tolerance is DENY D-FROZEN)
- `tests/integration/test_edge_provenance.py` (G1 — prov_* on every edge)
- `tests/integration/test_graph_writer.py` (G1 — prov_* on every node)
- `tests/integration/test_entity_link_materialization.py` **line 109-114 only** (H3 concrete range)
- `tests/integration/test_referent_rewriting.py`, `tests/unit/test_referents.py` (G2 — ghost ≠ referent)
- `tests/integration/test_phase1_acceptance.py` (Ownership concrete-edge contraction)
- the resolution suites (`test_resolution*.py`, `test_anchors.py`, `test_canonical.py`,
  `test_resolution_pipeline.py`, `test_b6_*`, `test_b1_*`) — no clustering/merge change.

### 7.4 The adversarial target
A **ghost** (target never ingested) tagged `:Ghost` + never anchors / merges / corroborates; AND a
schema that SHOULD contract (**Ownership**) — unbroken. The fork must distinguish the two: a concrete
range still uses its schema label; only an abstract range falls back to `:Entity`, and only a
never-seen id is `:Ghost`.

---

## 8. Migration

**NONE.** This is a Neo4j-side gate (edge projection + a `:Ghost` label, optionally a `:Ghost`
index). No Postgres schema change. If a `:Ghost` index is added it goes in
`graph/constraints.py:ensure_constraints` (idempotent `CREATE INDEX … IF NOT EXISTS`), NOT an Alembic
migration. If the builder concludes otherwise, **STOP and flag the human** — a migration here is a
scope signal, not a routine step. `tests/integration/test_migrations.py` (ADR 0030 drift guard) must
stay green untouched.

---

## 9. Person-affecting assessment

**Gate D is person-NEUTRAL.** It is graph structure only. It does NOT touch
`DEFAULT_MERGE_THRESHOLD`, Splink, `pick_anchor`, `cluster_and_merge`, or who-merges-with-whom — edge
projection runs in `write_entities` strictly AFTER clustering/merge/guard (verified
`pipeline.py:352-447`). The single person-relevant fence is the corroboration-exclusion (§6.1): a
materialized edge / ghost must never lower a merge bar — and this gate ships only the *constraint and
test*, not a corroboration mutation. **No sign-off required for this gate.** (If a future change in
this package ever made a materialized edge feed a merge decision, that change would be
person-affecting and would require sign-off — out of scope here, fenced by D-CORROB.)

---

## 10. Locked invariants (must hold across the gate)

- **G1 — provenance on every node AND edge.** Every new `Sanction→target` edge carries the asserting
  entity's `prov_*` via `_with_props(edge_props=edge_prov)`; every node (incl. a `:Ghost`) carries
  `prov_*`/`id`. PRESERVED, additive. DENY **D-G1** if any node/edge loses `prov_*` or a G1 test is
  loosened.
- **append-only / no un-merge.** Edges MERGE (idempotent); nodes MERGE. No silent mutation, no
  un-merge. A ghost is MERGEd, never deleted.
- **canonical-canonical only via the guard.** Gate D does not touch clustering or the merge guard.
  The guard (`needs_review`) and ADR-0040 anchor-conflict deferral are upstream and unchanged.
- **resolve to canonical IDs.** Endpoints are the DURABLE canonical ids (ADR 0044); G2 rewrite has
  already run. Ghosts are typed traversal-only endpoints, never canonical survivors.
- **`:Ghost` exclusion (NEW HARD INV).** A `:Ghost` is excluded from anchoring (`pick_anchor`), the
  merge guard (`needs_review`), and corroboration (Gate B-abjad forward-constraint). DENY
  **D-GHOST** if a ghost anchors, merges, or corroborates.
- **corroboration-exclusion fence (NEW HARD INV).** A materialized edge / ghost never lowers a merge
  bar; recorded as a forward-constraint in ADR 0046. DENY **D-CORROB**.
- **NO change to `DEFAULT_MERGE_THRESHOLD` / Splink / `pick_anchor` precedence / cluster membership.**
  DENY **D-THRESHOLD**.
- **The rule keys on `prop.type == registry.entity`, NOT the range schema.** DENY **D-RANGEKEY** if
  the override re-introduces a range-schema gate (the original bug).
- **ftmg boundary stays in one module.** All ftmg imports remain in `writer.py` + `ftmg_fork/`.

---

## 11. APPROVE / DENY (judge gate)

**APPROVE** requires ALL of:
- `VERIFIED_API.md` carries the Gate D ftmg section, verbatim, correct paths (§2).
- A real `Sanction → target` edge is present in the graph (the inverted test passes).
- The `UnknownLink` second site (drop site 2) materializes.
- Ownership/Directorship concrete-range contraction stays green (regression unbroken).
- Ghosts are tagged `:Ghost` AND excluded from anchor / merge / corroboration.
- Re-projection is idempotent (no duplicate edges).
- Every new edge carries the asserting entity's `prov_*` (G1).
- All FROZEN suites green byte-for-byte (§7.3).

**DENY** if any of:
- **D-VERIFY** — the ftmg section is missing / paraphrased / wrong-path.
- **D-RANGEKEY** — the rule keys on the range SCHEMA rather than `prop.type == registry.entity`.
- **D-FROZEN** — contraction (Ownership/Directorship) or any frozen suite breaks / is loosened.
- **D-GHOST** — a ghost anchors, merges, or corroborates.
- **D-CORROB** — a materialized edge / ghost can lower a merge bar, or the forward-constraint is not
  in ADR 0046.
- **D-G1** — any node/edge loses `prov_*`.
- **D-THRESHOLD** — any threshold/Splink/cluster-membership change.
- **D-FORK** — ftmg is forked wholesale instead of a thin 2-generator override (re-implements
  `QueryBatcher`/`generate_node_entity`/etc. instead of importing them).
- **D-MIGRATION** — an unflagged Postgres migration appears.

---

## 12. Slice plan

**ONE slice** (recommended). The fork (both drop sites), the `:Ghost` tagging, and the tests are one
cohesive, person-neutral graph-writer change with no migration and no clustering touch. The second
drop site (`generate_edge_entity`) shares the identical fallback mechanism as the first and is
exercised by one extra fixture — splitting it would create a half-fixed boundary (one generator
keyed on type, one on range) that is harder to reason about, not easier. The ghost-exclusion is
structurally true by `resolve_pending` ordering; the optional defensive guard is a few lines + a
test, not a separable feature.

**IF** the builder finds the optional ghost-exclusion *defensive guard* in `review.py`/`canonical.py`
genuinely entangles with `needs_review`'s signature or requires touching cluster membership, THEN
split:
- **slice-1** — the `ftmg_fork/` thin override (both drop sites) + `:Ghost` tagging + idempotency +
  G1 edge-prov + the inverted test + `test_abstract_edge.py` (minus the defensive-guard assertions).
  Person-neutral. Proves the headline OFAC fix.
- **slice-2** — the `:Ghost` defensive exclusion guard + its assertions. Person-neutral.

Each slice individually CI-green and mergeable. Default: ship as one slice.

---

## 13. Frozen-test list (authoritative — see §7.3)
`test_edge_provenance.py`, `test_graph_writer.py`, `test_entity_link_materialization.py` (H3 line
only), `test_referent_rewriting.py`, `test_referents.py`, `test_phase1_acceptance.py`, and the full
resolution suite. A removed assert / added skip|xfail / loosened tolerance on any is **D-FROZEN**.
