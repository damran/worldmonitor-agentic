# ADR 0046 — Abstract `Thing`-range edge materialization (thin ftmg override + `:Ghost`)

> Status: **PROPOSED** · 2026-06-25 · Closes audit gap **G3** · Addresses **ADR 0023 item 2**
> Gate: [Gate D — Abstract `Thing`-Range Edge Materialization](../reviews/GATE_D_ABSTRACT_EDGES_SPEC.md)
> Person-affecting: **NO** (graph structure only; the one person-relevant surface is a *forward-constraint*, §Decision 4)

## Context

"The resolved entity graph is the product" and "resolve to canonical IDs" are non-negotiable
(CLAUDE.md). `followthemoney-graph` (ftmg 0.1.0, imported only in `graph/writer.py`) projects FtM
entities into Neo4j but keys edge projection on the **range SCHEMA**. `ftmg/config.py:67-70`
registers a node-schema config only for `not schema.edge and not schema.abstract`, so the abstract
base `Thing` has no `config.nodes.schemata` entry and `config.py:73` raises if you try to add one.
Every entity-link whose property range is `Thing` is therefore dropped at the range-schema lookup —
in **two** places:

- `generate_entity_links` (transform.py:198) drops at 227-229 (`Sanction.entity`, `Note.entity`,
  `Risk.entity`, `Similar.candidate/match`, `Address.things`);
- `generate_edge_entity` (transform.py:291) drops at 317-322 (`UnknownLink.subject/object`,
  `Documentation.entity`, `CourtCaseParty.party`).

This is the headline OFAC failure: `Sanction → entity` is not traversable, and the smoke run reports
`graph_edges=0` for a Sanction dataset. It was recorded as accepted debt in **ADR 0023 item 2**
(owed before Phase 4) and pinned by `tests/integration/test_entity_link_materialization.py:117-122`
asserting `sanction_edges == 0`. Phase 4 (CTI/enrichers, which rely on `Thing`-ranged links) is now
in view, so the debt is being paid.

Concrete-range links are unaffected and contract correctly today (`Ownership`, `Directorship`,
`Membership`, `Associate`/`Family`, `Representation`, `Occupancy`, `Person.addressEntity`).

## Decision

### 1. Thin override, NOT a fork-as-foundation
CLAUDE.md: cloned repos are "adopted / depended on / wrapped — never forked as foundation." We add a
wrapper package `graph/ftmg_fork/` that **overrides exactly the two affected generators**
(`generate_entity_links`, `generate_edge_entity`) and **imports everything else unchanged** from
upstream ftmg 0.1.0 (`QueryBatch`, `QueryBatcher`, `generate_node_entity`, `generate_topic_labels`,
`get_schema_labels`, `ENTITY_LABEL`). The ftmg boundary stays in one place (`writer.py` +
`ftmg_fork/`). Rejected: forking ftmg wholesale (re-implements `QueryBatcher` etc., un-tracked from
upstream — a maintenance liability and a CLAUDE.md violation).

### 2. Re-key the target lookup on `prop.type == registry.entity`, with an `:Entity` fallback
The override drops the range-SCHEMA gate (transform.py:227-229 / 317-322 — the bug) and keeps the
type-level test (`prop.type == registry.entity`, already correct at upstream line 220). When the
range schema is abstract or absent, the endpoint is matched on the base label
`ENTITY_LABEL = "Entity"` — every node ftmg writes already carries `:Entity`, and `config.py:73`
forbids registering the abstract `Thing` in `config.nodes.schemata`, so the fallback (not a config
entry) is the correct mechanism. The relationship type is derived **deterministically** from the
property (its `config.edges.properties` label, or a stable uppercase form of the prop name), so
re-projection produces the same rel-type. Edges are projected as
`(:Sanction)-[:ENTITY {prov_*}]->(target)`.

The endpoints are the **durable canonical ids** (ADR 0044) and merged-away referents are already
rewritten (G2 / ADR 0025), because edge projection runs in `write_entities` strictly AFTER
clustering → `rekey_cluster` → the merge guard → referent-rewrite (`pipeline.py:352-447`). The
entity-link MERGE form `MERGE (s)-[r:REL]->(t) ON CREATE SET r = item.props` keyed on
(source durable id, target durable id, rel-type) is **idempotent**: no duplicate edges on
re-projection. Each new edge carries the **asserting entity's `prov_*`** (G1 — non-negotiable).

### 3. `:Ghost` for dangling endpoints
A `Sanction → target` whose `target` id was **never ingested as a concrete entity** would
MATCH-miss and silently re-drop the edge. Instead the fork **MERGEs the target node and tags it
`:Ghost`** — a typed, traversal-only endpoint that preserves the assertion while being structurally
inert to resolution. (Distinct from **G2** merged-away referents, which are rewritten to their
survivors before write — a ghost is a never-seen id, not a collapsed one.) A `:Ghost` is excluded
from THREE surfaces:

1. **Anchoring** (`canonical.py:pick_anchor`) — never a cluster member, never a durable-id survivor.
2. **The merge guard** (`review.py:needs_review`) — never a merge survivor.
3. **Corroboration** (Gate B-abjad, unbuilt) — see Decision 4.

Surfaces (1) and (2) are **structurally true** by ordering: ghosts are minted at write-time, after
`cluster_and_merge`, so they are never in `by_id`/`members`. The gate proves this by test (and may
add a defensive assertion). Rejected alternatives: (a) **drop the edge** (the status-quo bug —
loses the assertion, the OFAC link stays invisible); (b) **mint a full concrete entity** for the
target (a never-seen id would then be eligible to anchor/merge — directly violates the
catastrophic-merge guard and could fuse two identities; rejected).

### 4. Forward-constraint: a materialized edge / ghost is NOT independent corroboration
Gate B-abjad (corroboration / multi-agreement merge support) does **not exist yet**. This ADR
mandates, as a **forward-dependency constraint** on it: a `:Ghost`, and any materialized
`Sanction→target` (or other abstract-range) edge, **MUST NEVER count as independent corroboration
that could lower a merge bar for a real person** (CLAUDE.md catastrophic-merge invariant — "multiple
*independent* agreements before merging"). A materialized edge re-states one source's assertion; it
is not an independent second observation. Gate D ships only the *constraint and its test*, not a
corroboration mutation. This is the single person-relevant surface, and it is fenced (DENY D-CORROB
in the gate spec) so the rule is locked before the surface that could violate it is built.

## Consequences

- ✅ `Sanction → entity` and the other abstract-range links (incl. `UnknownLink`) materialize; the
  resolved graph is traversable over those relationships — the product invariant is restored. G3
  closes; ADR 0023 item 2 closes.
- ✅ The ftmg boundary stays a thin, auditable override; upstream improvements remain importable.
- ✅ Person-neutral: no threshold/Splink/cluster change; edge projection is post-guard. The one
  person-relevant rule is a *forward-constraint* recorded here and test-locked, not a live mutation.
- ✅ Idempotent re-projection; every edge carries provenance (G1); endpoints are durable canonical
  ids (ADR 0044) with referents already rewritten (G2 / ADR 0025).
- ⚠️ `:Ghost` nodes are a new node class. They are typed, traversal-only, and excluded from
  resolution — but downstream readers (API/MCP) must learn to treat `:Ghost` as "asserted-target,
  not-yet-resolved." Tracked in the GATE_LEDGER; the read-path cutover is out of scope here.
- ⚠️ Gate B-abjad inherits the corroboration-exclusion constraint (Decision 4). Building it without
  honoring this constraint is a regression against this ADR.

## Alternatives considered (not chosen — no OPEN fork)
- **Upstream ftmg upgrade / PR.** Slower; we do not control the release cadence; the platform needs
  the link now. The thin override is reversible if upstream later fixes it (delete `ftmg_fork/`,
  re-point the import).
- **Register `Thing` in `config.nodes.schemata`.** Forbidden by `config.py:73` (raises on an
  abstract/edge schema). The `ENTITY_LABEL` fallback is the supported mechanism.
- **Key on the range schema but expand to all concrete subtypes of `Thing`.** Combinatorial, brittle,
  and still range-schema-keyed (the original bug class). The type-level `registry.entity` test is the
  correct, minimal fix.

This is **not** a product/architecture fork requiring a human OPEN decision: the design is determined
by the verified ftmg internals + the locked invariants. No sign-off required (person-neutral).
