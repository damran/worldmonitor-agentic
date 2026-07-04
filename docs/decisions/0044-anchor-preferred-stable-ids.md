# 0044 — Anchor-preferred stable canonical ids + canonical-alias ledger + adopt/merge/split

- **Status:** ACCEPTED
- **Date:** 2026-06-25
- **Gate:** B-front (`docs/reviews/GATE_B_FRONT_STABLE_IDS_SPEC.md`) — the **front half** of Gate B
  ([0019](0019-batch-vs-streaming-resolution.md), still OPEN for the incremental-ER fork).
- **Touches:** `resolution/canonical.py` (NEW), `resolution/merge.py`, `resolution/referents.py`,
  `graph/writer.py`, `db/models.py`, `db/migrations/versions/0006_canonical_ledger.py` (NEW),
  `ontology/anchors.py` (precedence list). Tests in `tests/`.
- **Extends:** [0036](0036-deterministic-canonical-id.md) (deterministic `wmc-` id),
  [0039](0039-er-distinguishing-evidence.md) (FtM `registrationNumber`/`taxNumber` projection).
- **Depends on:** [0037](0037-transitive-negative-judgement.md) (the negative-judgement-aware resolve
  step that this gate's adopt/merge/split runs *after*).
- **Preserves (does NOT relitigate):** [0040](0040-er-anchor-conflict-negative-evidence.md) (anchor-conflict
  guard), [0031](0031-return-to-block-signoff.md) (return-to-block sign-off), [0025](0025-referent-rewriting.md)
  (referent rewriting), [0042](0042-single-tenancy-teardown.md) (single-tenant native `{id}` MERGE),
  the `DEFAULT_MERGE_THRESHOLD=0.92` and the Splink weights (Gate A slice-2 owns those).

## Context

ADR 0036 made a **merged** cluster's canonical id a content hash of its sorted member ids:
`wmc-<sha256(sorted(member_ids))[:40]>` (`merge.py:_canonical_id`). The audit lesson it closed was
the **crash-retry** corruption: re-resolving *the same `pending` rows* re-derives the same id, so the
graph `MERGE` converges on one node. ADR 0036 itself flagged the residual (§Consequences): *"cross-batch
stability across re-ingests (which mint fresh member ids) remains Gate B."*

That residual is the problem this gate closes. Member ids are **fresh per re-ingest**: connectors mint
per-collect ids and the ER queue dedups only on `(source_record, entity_id)` (`models.py:46`). So
re-ingesting the **same real entity** yields a **different** `wmc-` id every run, a different graph node,
and id churn that breaks every downstream reference, alias, and yente-referent. `wmc-<hash>` is therefore
a **crash-retry idempotency fingerprint** (ADR 0036's real and only guarantee) and was never durable
identity — but the system has been leaning on it as if it were.

**The decision splits the two concepts that ADR 0036 conflated:**
- `wmc-<hash>` stays **strictly** a within-run idempotency fingerprint (the fallback id of an
  *unanchored* merge, used only so a crash+retry converges).
- **Durable identity** becomes **anchor-preferred**: a real entity that carries a canonical anchor
  (QID / LEI / registration number / tax number) is keyed by that anchor and is therefore **stable
  across re-ingests** by construction — the anchor does not change when member ids do. An entity with
  no anchor gets a **minted `wm-mint-<uuid>` durable id recorded in a ledger**, so its identity also
  survives re-ingest (the ledger, not the ephemeral member set, carries it).

### Why nomenklatura does not already do this (the verified finding)

nomenklatura 4.10.0 (`resolver/identifier.py`, `resolver/resolver.py`) **does** do anchor-preferred
canonical selection — but **QID-only**. `Identifier.__init__` sets `weight=3` if `is_qid(id)`, `weight=2`
if `NK-`-prefixed, else `weight=1`; `get_canonical` returns `max(connected)`, so a QID wins a cluster.
But **LEI, registration number, and tax number are weight-1 raw ids** — nomenklatura would never prefer
them over each other or over a minted id deterministically. Our precedence is **richer than QID-only**,
and the resolver also **discards** its id→canonical mapping after the ephemeral per-batch resolver is
torn down (ADR 0028). So the durable id must be derived **outside** the resolver. (This mirrors how
`wmc-` already lives outside it: the resolver decides *membership*; our code decides the *durable id*.)

## Decision

### 1. Derive the durable id OUTSIDE the resolver — new `resolution/canonical.py`

A new pure module owns durable-id derivation. **Rejected alternative:** monkey-patching/extending
nomenklatura's `Identifier` weighting to teach it LEI/regNo/taxNo precedence — it would couple us to
library internals, still lose the mapping on teardown, and fight the QID-only `max(connected)` contract.
Deriving outside keeps `cluster_and_merge` the membership decider and `canonical.py` the id decider,
matching the existing `_canonical_id` seam.

`canonical.py` provides:
- `pick_anchor(members) -> str | None` — the anchor-preferred durable id for a cluster's source members,
  honoring the precedence and the anchor-conflict guard (below); `None` if no usable anchor exists.
- ledger read/write helpers — `canonical_id -> canonical_alias` companion store (table below).
- `wm-mint-<uuid>` minting for an unanchored cluster with no prior ledger entry.

### 2. Precedence: **QID > LEI > regNo > taxNo > minted `wm-mint-<uuid>`**

Ordered, first-hit-wins. Reconciliation with the existing surface (orient flagged the mismatch):

- `ontology/anchors.py` `CANONICAL_ID_FIELDS = ("wikidata_id","geonames_id","lei","opencorporates_id")`
  uses **storage keys** that do **not** match the FtM property names, and has **no
  `registrationNumber`/`taxNumber` anchor** (those are Splink-only, `splink_model.py`, ADR 0039), and
  `lei`/`opencorporates_id` have **no producer**. The durable precedence therefore defines its **own**
  ordered list and maps from FtM properties directly:
  - **QID** ← FtM `wikidataId` (also surfaced as `wm_anchor_wikidata_id` context) — `rigour.is_qid`-valid.
  - **LEI** ← FtM `leiCode` (also `wm_anchor_lei`) — 20-char LEI shape.
  - **regNo** ← FtM `registrationNumber` (the ADR-0039 `identifier`-normalized value).
  - **taxNo** ← FtM `taxNumber` (the ADR-0039 `identifier`-normalized value).
  - else **mint** `wm-mint-<uuid>`.
- `geonames_id`/`opencorporates_id` are **not** in the v0 durable precedence (no producer; GeoNames is a
  place anchor, not a legal-identity anchor). They stay in `CANONICAL_ID_FIELDS` for the Neo4j uniqueness
  constraints (`constraints.py`) unchanged. The durable precedence is a **superset-by-intent, subset-in-v0**
  list — documented as such so a later anchor producer extends one list, not the constraint set.
- The durable id is prefixed by anchor kind for traceability and to avoid cross-namespace collision
  (e.g. `qid:Q42`, `lei:5493…`, `regno:…`, `taxno:…`, `wm-mint-<uuid>`). The exact serialization is a
  builder record below; the **invariant** is that it never derives from `wmc-<hash>`.

### 3. The anchor-conflict guard (ADR 0040) is **load-bearing and preserved**

`pick_anchor` consumes `anchor_conflicts_across(members)` semantics: if the cluster's members carry **two
distinct values for the anchor at the chosen precedence tier** (two QIDs, two LEIs, …) the durable id is
**NOT** derived from that conflicting tier. The cluster is exactly the catastrophic-merge case ADR 0040
parks for review; `pick_anchor` MUST NOT silently pick `[0]`. It falls through to the next non-conflicting
tier or to a mint — it never fuses two real-world identities into one stable id. **This is the single most
important person-safety property of the gate** (below).

### 4. The ledger: `canonical_alias` ↔ `canonical_id` (NEW table, migration `0006`)

The resolver discards its `member -> canonical` mapping (ephemeral, ADR 0028). To keep durable identity
across re-ingests we add a **durable companion**: a new `canonical_id_ledger` table (migration
`0006_canonical_ledger`, **Revises `0005_er_gold_pair`**) recording, per durable canonical id:
- the durable `canonical_id` (anchor-preferred or minted),
- `canonical_alias` — every prior/superseded id that now resolves to it (the yente-referent trail and the
  superseded-id traceability),
- the anchor kind/value it was derived from (or `mint`),
- append-only audit columns (`created_at`).

`canonical_alias` is populated:
- on **merge** — the survivor durable id plus every collapsed member/prior id as alias;
- on **split** — the anchor side keeps its durable id; the ejected id is recorded as a traceable alias
  pointer (no orphan), so a reference to it still resolves.

The ledger is **append-only** (the platform invariant): a split adds an alias row, it does not delete the
prior mapping. Drift between `db/models.py` and the `0006` migration is caught by the existing
`tests/integration/test_migrations.py` guard (ADR 0030).

### 5. Adopt / merge / split — after the 0037 resolve step

The operations run **after** `cluster_and_merge` returns (after the ADR-0037 negative-judgement-aware
resolve), before `pipeline._resolve_batch` writes:
- **adopt** — a re-ingested member carrying an anchor adopts the existing durable id for that anchor
  (read from the ledger); no new node, no id churn. **Person-neutral.**
- **merge** — survivor = anchor-preferred durable id, else the min minted id; `canonical_alias` populated
  for every collapsed id. Routed through the existing guard; a **non-sensitive** merge auto-promotes,
  a sensitive/oversized one parks (ADR 0024/0031) exactly as today.
- **split** — the anchor side keeps its durable id; the ejected id is recorded as a traceable alias.
  **A split of an already-promoted canonical is an un-merge under the append-only invariant** — see fencing.

### 6. `graph/writer.py` keys on the durable id; alias-on-read

The writer keys the MERGE on the **durable canonical id** (still native `{id}`, single-tenant, ADR 0042)
and **honors `canonical_alias` on read** so a lookup by a superseded id resolves to the surviving node.
`build_referent_map` (`referents.py`) maps members to the **durable id**, not the `wmc-` hash.

## Person-affecting fencing (CRITICAL)

This gate is **pure id-derivation/stability and is person-NEUTRAL when scoped right**. It does **not**
touch `DEFAULT_MERGE_THRESHOLD=0.92`, Splink weights/scores, or who-merges-with-whom (that is Gate A
slice-2). Three fences make this hold:

1. **Anchor precedence defers to the anchor-conflict guard (ADR 0040).** `pick_anchor` never fuses two
   entities with different QIDs/LEIs/regNos/taxNos — it falls through or mints. Deriving a durable id is
   never a back-door merge.
2. **A split of an already-promoted canonical is an un-merge.** Under append-only (no delete), splitting a
   promoted cluster re-decides membership. If the canonical is **sensitive/sanctioned/PEP**, that crosses
   into human sign-off (CLAUDE.md; ADR 0031). **Split-of-a-sensitive-canonical is FENCED to the existing
   sign-off path — never auto-run.** Whether the sensitive-split operation ships at all in this gate is a
   **gated sub-slice** (slice-2), and it ships *only* wired through `signoff.approve/reject`, never as an
   autonomous mutation. The person-neutral auto path (anchor id-derivation + alias/ledger + adopt +
   non-sensitive merge) is slice-1.
3. **`canonical_alias` is person-neutral** — it is superseded-id traceability / yente-referent data, not a
   merge decision.

The spec separates the **person-neutral auto path** (slice-1) from any **sensitive-split that requires
sign-off** (slice-2, gated).

## Alternatives considered

- **(A) Extend nomenklatura's `Identifier` weighting to rank LEI/regNo/taxNo.** Rejected: couples us to
  library internals, the resolver still discards its mapping on teardown (ADR 0028), and it fights the
  QID-only `max(connected)` contract. Deriving outside in `canonical.py` is the smaller, testable change.
- **(B) Make `wmc-<hash>` the durable id and rewrite member ids to be stable.** Rejected: connectors mint
  per-collect ids by design (the ER queue dedups on `(source_record, entity_id)`); making member ids
  globally stable is a connector-contract change far outside this gate, and `wmc-` would still churn for
  any record whose source key rotates. The fingerprint/identity split is the correct seam.
- **(C) Reuse the resolver's persistent ledger (`Resolver.make_default` on a durable engine).** Rejected:
  that is the deferred incremental-ER surface (ADR 0019b) and would break batch purity (ADR 0026/0028).
  Our ledger is a *durable companion* read before clustering and written after, not a shared resolver.

## Consequences

- A re-ingest of an **anchored** entity now yields an **identical** durable canonical **id** — the
  derivation (`pick_anchor`) + ledger are re-ingest-stable, and a within-batch anchored **merge** is
  re-keyed under that durable id, so downstream references, aliases, and yente-referents on the merge
  path are stable.
- **Scope caveat (slice-1):** the durable-id re-key + ledger write are gated on `cluster.is_merge`. An
  anchored **singleton** (an anchored record with no in-batch merge mate) still keeps its raw per-collect
  member id as its **graph node key**, so its *graph node* churns across re-ingest even though its derived
  durable id is identical. Re-keying an already-persisted singleton node to its durable id across runs is
  **cross-batch / cross-run dedup**, which is the **ADR-0019-deferred** incremental-ER surface (and the
  ADR-0025/Gate-C persisted-rewrite surface) — explicitly out of this gate. slice-1 delivers the durable-id
  *instrument* + ledger + the merge-path re-key; the singleton graph-node carve-out is deferred, not solved.
- An **unanchored** entity's identity survives re-ingest via the ledger (`wm-mint-<uuid>` + alias) on the
  merge path, instead of churning a fresh `wmc-` per run.
- **Alias-on-read scope caveat (slice-1):** `graph/writer.resolve_node_id` / `get_entity_by_alias` provide
  the alias-on-read capability against the ledger and are integration-tested, but **no live read path
  consumes them yet** — the Phase-1 read surface (`graph/queries.get_entity`/`get_neighbors`/
  `get_provenance`) and the API/MCP layer still read by bare node id and are out of this gate's scope.
  Wiring alias resolution into the live read surface is a follow-up gate (backlog H-1); slice-1 delivers the
  capability + the durable-id write key + ledger, not the read-side cutover.
- `wmc-<hash>` is **provably idempotency-only** — it appears only on the unanchored-merge fallback path and
  is never read as durable identity (enforced by a grep gate + a test; see spec §10).
- Append-only is preserved: split adds an alias, never deletes. Sensitive-split is fenced to sign-off.
- ADR 0036's crash-retry guarantee is preserved end-to-end (the fingerprint still converges on retry).

## Out of scope (hard stops)

The incremental/streaming-ER fork of ADR 0019 (still OPEN); changing `DEFAULT_MERGE_THRESHOLD` or any
Splink weight/score/blocking rule (Gate A slice-2); a new datastore or parallel model; editing migrations
`0001`–`0005`; cross-run graph-side sweep of already-persisted edges (Gate C, ADR 0025); GeoNames /
OpenCorporates anchor producers. The nomenklatura API used must be **verified verbatim** before any code
is written (`VERIFIED_API.md`; spec §2) — a paraphrased/unverified binding is a judge DENY.

## Builder record (to be completed before this ADR is ACCEPTED)

- Final durable-id serialization (the `qid:`/`lei:`/`regno:`/`taxno:`/`wm-mint-` scheme).
- Final ledger column set and the `0006` migration head (must match `models.py` byte-for-byte).
- Confirmation that `wmc-` appears only on the unanchored-merge fallback (grep + test evidence).
- Whether the sensitive-split sub-slice (slice-2) shipped or was deferred, and if shipped, the sign-off
  wiring proof.
