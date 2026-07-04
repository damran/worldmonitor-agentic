# 0100 — Fold engine: log-as-outbox, idempotent projector, checkpoint, and the B-1 resolution

- **Status:** ACCEPTED (2026-07-04)
- **Date:** 2026-07-04
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** Mithat — per ADR 0095 §"one genuine risk" / build-sequence step 3 (user-confirmed)
  + Gate 3 fleet approval 2026-07-04
- **Realises:** ADR 0095 build-sequence **step 3** (the *outbox + idempotent projector* half — the
  fold-determinism engine). **Supersedes:** nothing. **Builds on:** ADR 0099 (the statement + decision
  spine), ADR 0044 (the canonical-id ledger), ADR 0042 (single-tenant native `{id}` MERGE key).

## Context

ADR 0095 (ACCEPTED, user-confirmed) makes the **Postgres statement + decision log the system of
record** and **Neo4j a derived, rebuildable graph projection**, materialised by *"a transactional
outbox → projector; a scheduled full-rebuild-and-diff job [as] the DR story and the fold-determinism
guard."* It names the one genuine risk directly: *"Fold/projection determinism is the hard part …
where this design can rot into a mutated projection."* ADR 0099 (Gate 2a) banked the substrate: the
append-only `statement` and `decision` tables, dual-written at merge time.

**Gate 3 is sub-sliced** to keep each step small and reversible:

- **3a-i (THIS ADR)** — the **fold ENGINE** only: give the log a consumption order (the outbox),
  build the **idempotent projector** that folds the log back into an **isolated, ephemeral** Neo4j
  target, add its **checkpoint**, and resolve the deferred **B-1** question. The projector is
  **dormant**: it never co-writes the live graph.
- **3a-ii (next)** — the **scheduled full-rebuild-and-diff guard**: the driver maintenance hook, the
  divergence alert, and the cross-batch **parity property P-FOLD-2** (fold == direct write). *Not here.*
- **3b (later, human-gated)** — cutover (point the projector at the live graph) + retire the direct
  write path. *Not here.*

Gate 2b (backfill of pre-2a graph nodes into the log) remains deferred; 3a-i runs only on a
fresh / post-2a corpus.

## Decision

Build a dormant, isolated fold engine to the five decisions below. It is **purely additive**: nothing
in the person-affecting write path (`resolution/statements.py`, `resolution/merge.py`,
`resolution/pipeline.py`, `graph/writer.py`, the merge guard) changes.

### D1 — The log *is* the outbox (a monotonic `seq`, no separate outbox table)

Add ONE monotonic, server-assigned ordering column **`seq BIGINT` (Postgres `IDENTITY`)** to BOTH
`statement` and `decision`, each with an index. The projector checkpoints on that total order and
reads rows since a watermark.

This is chosen over a **separate transactional outbox table**. ADR 0095 states the whole point of the
inversion is that *"the dual-write problem disappears — Postgres is the sole write domain."* A separate
outbox table would reintroduce exactly that: a second in-Postgres write that must be kept in lock-step
with the append-only log it mirrors. The append-only log **already is** the change stream; it only
lacked a gap-free consumption order. A `seq` column adds that and nothing else — and, critically,
**requires zero change to the pipeline write path**: `record_statements` / `record_decision` keep their
existing `session.add` INSERTs and the `IDENTITY` column auto-assigns `seq` server-side.

- **Single-writer assumption (stated, not hidden).** `seq` is assigned at INSERT; under *concurrent*
  writers a lower `seq` can commit *after* the projector has advanced its watermark past it (the
  classic sequence-gap-under-concurrency hazard). The ingest driver is **single-node / single-writer**
  today (ADR 0026 per-batch commit; `TaskRun`'s "single-node assumption; the deferred lease replaces it
  under HA"), so commit order == `seq` order and no gap exists. This is safe **now** and recorded as a
  **revisit trigger** below (HA / multiple writers needs a "min in-flight seq" watermark or logical
  decoding).

### D2 — Global-fold-is-truth (the semantic decision, and its expected-divergence class)

The projector reconstructs the fused FtM entity from statements and applies the **global** referent
rewrite — every superseded subject id **and** every entity-typed edge endpoint is mapped to its current
survivor via the **whole** `canonical_id_ledger` (ADR 0044, `resolve_durable`) at projection time.

Contrast the **live** writer: `resolution/referents.rewrite_referents` (wired at `pipeline.py:515-518`)
rewrites referents only **within a batch** (`build_referent_map` over that batch's promoted clusters),
and cross-batch merges are resolved lazily by **alias-on-read** (`graph/writer.resolve_node_id` /
`canonical_id_ledger`). So the live graph legitimately still contains edges pointing at merged-away
*cross-batch* source ids, corrected only when read.

The projector, holding the global ledger, rewrites **all** of them at projection time. The projected
graph is therefore **strictly more resolved** than the per-batch live graph. Per ADR 0095 the
**rebuilt-from-log graph is canonical** — so this is not a bug: **the global fold is the intended
truth.** The consequence, recorded so it is chosen not discovered:

> **Expected-divergence class E1 (cross-batch referent resolution).** On a **single-batch / fresh**
> corpus the fold graph equals the direct-write graph. On a **cross-batch** corpus the fold graph is a
> more-resolved superset (dangling-at-a-merged-away-id edges are already redirected). The future parity
> guard (3a-ii / P-FOLD-2) therefore asserts equality only on single-batch/fresh corpora and treats
> cross-batch divergence as **expected**, not a failure.

Two further, smaller documented divergence classes fall out of the reconstruction (see §"Reconstruction
gaps"):

> **E2 (anchors / enrichment).** `wm_anchor_*` anchors and any enricher-added values live in the FtM
> **entity context**, which is **not** in the statement log (the log records `(prop, value, dataset)`
> claims from the source members, and enrichment runs on the *written* entity after the statement
> dual-write, `pipeline.py:437` vs `:483`). A projector rebuilt purely from statements therefore does
> **not** reproduce enricher-added anchors. Zero effect on the non-enriched 3a-i corpora (both sides
> empty); a real limitation for the eventual cutover (3b), whose resolution — capture enrichment output
> as statements, or re-run enrichment at projection time — is a later-gate concern.

> **E3 (node-level single-source `prov_*`).** The node-level `prov_*` is a single representative source;
> the multi-source truth is the `prov_witnesses` map (reconstructed faithfully). The projector picks a
> deterministic representative (§below); the merge-time writer's representative is merge-order `[0]`.
> On the 3a-i corpora (members share a source) these coincide.

> **E4 (FtM `datasets` node property).** *Discovered during the 3a-i build, by the mandatory
> fold-vs-direct equivalence test (IT-PROJ-2) — recorded here so it is chosen, not silently excluded.*
> ftmg writes `list(proxy.datasets)` as the node `datasets` property. That FtM `Dataset` label is
> ingest metadata carried on the raw connector entity — it is **NOT a per-claim statement**. The log
> stores each claim's `dataset = Provenance.source_id` (a *different* axis: the source, not the FtM
> dataset). The fold therefore cannot reproduce the connector's FtM dataset label; the projector sets
> `datasets` to the deterministic set of **source datasets** its statements carry (self-consistent, so
> P-FOLD-1/3/4 still hold on the full signature), which differs from the direct-write node's connector
> label. Like E2, the `datasets` node property is **excluded from the single-batch fold-vs-direct
> equivalence comparison** (every *other* node/edge property stays byte-identical). Whether to capture
> the FtM `Dataset` label into the log for full 3b-cutover reconstructability is a later-gate
> (Gate 2a-extension / 3b) decision — noted as a **3b prerequisite**.

### D3 — The fold algorithm (the heart of P-FOLD)

`resolution/projector.py` exposes a **pure** `reconstruct_entities(statement_rows, survivor_of)` (no DB,
no Neo4j — unit-testable) and a `project(session, target, *, full_rebuild=False)` orchestrator.

**Reconstruction (pure), deterministic throughout:**

1. **Dedup** the rows on `statement_id` (content-addressed duplicates are byte-identical). *This is the
   B-1 resolution (D4).*
2. **Group** by the **survivor subject** `survivor_of(row.canonical_id)`, where
   `survivor_of(id) = resolution.canonical.resolve_durable(session, id) or id` — the same alias-on-read
   the live writer uses (`writer.resolve_node_id`). This folds a **re-canonicalised** subject (an old
   canonical aliased to a new survivor in the ledger) into its survivor group.
3. Per survivor group, build ONE FtM entity:
   - **schema** = the group's (uniform) statement schema.
   - **properties** = `prop -> sorted set of values`, excluding `prop == "id"`. For **entity-typed**
     props (`schema.properties[prop].type == registry.entity`, the same test `rewrite_referents` uses),
     each value is rewritten through `survivor_of` (the **global** referent rewrite, D2). Others verbatim.
   - `entity = make_entity({"id": survivor, "schema": schema, "properties": props})`.
   - **Provenance (G1, node level)** — reconstruct a representative `Provenance` from the group's
     `min(entity_id)` member: `Provenance(source_id=dataset, retrieved_at=retrieved_at or "",
     reliability=reliability or "", source_record=raw_pointer or "")` (a member's rows all share this
     quad), then `stamp()` it. `dataset` is `NOT NULL`, so `provenance_node_properties` is never empty
     and `write_entities` never fails closed — **G1 upheld by construction.**
   - **Witness map (Tier-1)** — `{prop -> {row.dataset}}` over the group (excluding `prop == "id"`),
     stamped via `stamp_witness_map`. This re-derives **exactly** like
     `resolution.merge._witness_map_from_statements`.
   - **Anchors** — not reconstructed (E2). Empty on the non-enriched 3a-i corpora.

Edge-schema entities (Ownership / Sanction / …) are themselves rows in the log and reconstruct the same
way; `write_entities` Pass 2 materialises them as relationships carrying **their own** `prov_*` (G1 on
the edge). Entity-reference links likewise carry the property-holder's `prov_*`.

**Orchestration (`project`):**

1. **Read** `statement` rows `WHERE seq > checkpoint.last_statement_seq` (or all if `full_rebuild`)
   `ORDER BY seq`; `decision` rows likewise; a **full** ledger read (the survivor map).
2. **Fold** via `reconstruct_entities`.
3. **Write** into the isolated `target` via `graph/writer.write_entities` — **unchanged**; two-pass
   ftmg; idempotent MERGE on the native `{id}` (ADR 0042). *ftmg is not reimplemented.*
4. **Checkpoint (at-least-once ordering, invariant):** write Neo4j **first** (idempotent MERGE), **then**
   upsert `projection_checkpoint` to the max `seq` consumed and commit. A crash between the two
   re-projects the delta on restart (idempotent → converges); a crash before the Neo4j write leaves the
   watermark unmoved (re-reads the same delta). No lost or half-applied delivery.

`decision.member_ids` corroborates the collapse (and is the substrate for the deferred, decision-driven
supersession path); in 3a-i the survivor fold is **ledger-driven** (the ledger already records every
collapse via `record_durable_id`), so no decision-replay is required for the value graph.

### D4 — B-1 resolution: projector-side dedup, **not** `UNIQUE(statement_id)`

ADR 0099 (`:242-245`) deferred `UNIQUE(statement_id)` to Gate 3, citing batch-abort risk. **Resolved
here: keep `statement_id` indexed and non-unique; the projector dedups on it in the fold (step 1
above).** A `UNIQUE(statement_id)` constraint is **rejected again**, on two verified grounds:

1. **It would abort a batch on a legitimate re-observation.** FtM `Statement.id` hashes
   `(entity_id, prop, schema, value, dataset)` and **excludes** `first_seen` / `last_seen` (verified:
   two observations of the same claim at different times produce the *same* id). Under incremental ER a
   later batch that re-observes the same claim would collide, and since the batch is one transaction the
   whole batch aborts — a poison batch. This is the original ADR-0099 concern, still valid.
2. **It contradicts the append-only "record every observation" semantics ADR 0095 wants.** The log is
   meant to carry each observation so `first_seen` / `last_seen` can diverge; a uniqueness constraint
   would collapse re-observations into one row and destroy that record.

The idempotency the projector needs is provided **structurally** — dedup on `statement_id` + the
idempotent MERGE — and is proven by **P-FOLD-3** (re-delivery is a no-op) and **P-FOLD-4** (duplicate
`statement_id` rows converge). A constraint would buy nothing the projector doesn't already guarantee,
at the cost of correctness. (A future *observation-grained* uniqueness — e.g. on
`(statement_id, retrieved_at)` — may be revisited when incremental re-observation lands; a bare
`UNIQUE(statement_id)` will not.)

### D5 — Module placement + dormant/isolated posture

- **`src/worldmonitor/resolution/projector.py`.** The projector's substance is the **fold** — a
  resolution-domain re-run of the merge math from the persisted log (it reuses
  `resolution.canonical.resolve_durable`, mirrors `resolution.merge`'s witness-map derivation, reads the
  statement/decision spine). Its Neo4j write is a thin **unchanged** delegation to
  `graph/writer.write_entities`. ADR 0095 and `resolution/statements.py` frame the projector as reading
  "the statement + decision logs together" — resolution's SoR spine — so it co-locates there, not in
  `graph/`.
- **Dormant / isolated.** The projector is a **library** function exercised only by tests in 3a-i — no
  driver, no settings flag, no compose profile (those are 3a-ii / 3b). Its target is an **ephemeral**
  Neo4j (a testcontainer here; an opt-in separate instance on a host later). **Neo4j Community is
  single-database** (ADR 0094 D5), so there is no free shadow DB to co-write; pointing the projector at
  the live graph **is** the cutover (3b). This keeps 3a-i additive and trivially reversible.

## The mandatory property invariants (`tests/property/test_prop_fold_engine.py`)

Fold/canonical-id/provenance surface → a `@given` property suite is mandatory (CLAUDE.md build
discipline; ADR 0095's fold-determinism suite is the primary risk mitigation). *P-FOLD-2 (cross-batch
parity) is 3a-ii, not here.*

- **P-FOLD-1 — Determinism.** Projecting the same fixed log into a fresh isolated target twice yields a
  **byte-identical** canonical graph signature (node set, node properties incl. `prov_*` / anchors /
  `prov_witnesses`, edge set with edge props).
- **P-FOLD-3 — Idempotent re-delivery.** Projecting the same log twice into the **same** target (a
  re-run / duplicate at-least-once delivery) converges — the second projection is a no-op; the signature
  is unchanged.
- **P-FOLD-4 — Dedup / supersession convergence.** A log with duplicate `statement_id` rows **and** a
  ledger re-canonicalisation (old canonical `X` aliased to survivor `Y`, statements under both) projects,
  in a full rebuild into a fresh target, to **exactly one** node under `Y` carrying the folded statements
  and **no** node under `X` — the ADR-0095 fold-under-re-canonicalisation guard.

A single-batch **fold-vs-direct-write equivalence** check (non-enriched corpus, excluding ONLY the E2
anchor and E4 `datasets` divergences — every *other* node/edge property must be byte-identical) is the
**mandatory** reconstruction-correctness anchor (IT-PROJ-2); the full cross-batch parity guard is 3a-ii.

## Reconstruction gaps (bridged / documented)

- **Reconstructable from the log:** value set, schema, per-property witness map (`prov_witnesses`), a
  representative single-source `prov_*` (dataset non-null → always present), and — via the ledger — the
  global survivor rewrite of subjects and entity-typed endpoints.
- **Not in the log (documented divergence):** `wm_anchor_*` anchors and enricher-added context/values
  (E2); the exact merge-order representative for node-level `prov_*` (E3, coincident on 3a-i corpora);
  the connector-assigned FtM `datasets` node label (E4 — the log carries per-claim `source_id`, a
  different axis; excluded from the fold-vs-direct comparison; a **3b-cutover reconstructability
  prerequisite**).

## Governance (ADR 0097 §4/§5)

- **`person_affecting: false`.** The projector **projects** decisions already taken by the existing,
  unchanged, guarded merge path. It changes no ER threshold, no clustering/merge outcome, no
  individual-affecting score, no guard behaviour, and it never writes the live graph. A parked
  (`pending_review`) cluster wrote no statements, so it produces **no** projected node — canonical
  fusion still routes only through the guard.
- Because the diff **adds a `resolution/**` module** (a person-affecting *area*) while self-tagging
  non-sensitive, ADR 0097 requires an explicit human co-sign — carried in the header. The narrow,
  checker-verifiable claim: **no file in the person-affecting write path changes** (`statements.py` /
  `merge.py` / `pipeline.py` / `graph/writer.py` / the guard are byte-unchanged); the only edits are the
  additive `seq` columns + `projection_checkpoint`, the new `projector.py`, and tests.

## Reversibility

**Reversible** (ADR 0095 is additive / low-reversal-cost until cutover at 3b).

- **D1 (log-as-outbox `seq`).** Reversal cost: low — drop two nullable-additive `seq` columns +
  indexes. **Revisit trigger:** HA / multiple concurrent writers (the single-writer watermark stops
  being gap-safe → move to a "min in-flight seq" watermark or logical decoding).
- **D2 (global-fold-is-truth).** Reversal cost: this is a *semantic* commitment inherited from ADR 0095,
  not new data-shape lock-in; nothing to unwind in 3a-i (the projector is dormant). **Revisit trigger:**
  the cross-batch divergence (E1) proves operationally confusing at parity time (3a-ii) → reconsider
  whether the parity guard should assert on cross-batch corpora at all.
- **D3/D5 (projector + placement).** Reversal cost: delete `projector.py` (dormant, no caller) — the
  direct writer is byte-unchanged.
- **D4 (B-1 dedup).** Reversal cost: none to unwind (no constraint added). **Revisit trigger:**
  incremental re-observation lands and wants an observation-grained uniqueness.
- **Checkpoint table.** Reversal cost: drop `projection_checkpoint`.

**Overall revisit trigger (ADR 0095's):** the fold/projection maintenance cost exceeds the
merged-node/DR/erasure pain it removes — the 3a-ii rebuild-and-diff job is the early-warning signal.

## Deferred (explicitly not built here)

- **3a-ii** — the scheduled full-rebuild-and-diff driver hook, the divergence alert/metric, and
  **P-FOLD-2** cross-batch parity.
- **3b** — cutover (project into the live graph) + retire the direct write path (human-gated).
- **Gate 2b** — backfill of pre-2a graph nodes into the log.
- The `supersedes` / `superseded_by` **write** path (decision-driven un-merge), `timestamptz`
  normalisation, a `method` field, capturing anchors/enrichment into the log, and folding `merge_audit`
  into `decision`.

## Alternatives rejected

- **A separate transactional outbox table.** Rejected (D1): a second in-Postgres write to keep in sync
  with the append-only log — the very dual-write ADR 0095 removes; the log already is the change stream.
- **`UNIQUE(statement_id)` for idempotency.** Rejected (D4): aborts a batch on legitimate re-observation
  (id excludes `first_seen`/`last_seen`) and destroys append-only observation semantics; projector-side
  dedup + idempotent MERGE provide idempotency structurally.
- **Co-writing a live shadow graph.** Impossible on Neo4j Community (single database, ADR 0094 D5) and,
  regardless, that would be the 3b cutover — out of scope for a dormant engine.
- **Reimplementing the ftmg transform in the projector.** Rejected: `write_entities` is the one FtM→Neo4j
  boundary (G1 fail-closed, two-pass, `ftmg_fork`); the projector reuses it verbatim.
- **`graph/projector.py` placement.** Rejected (D5): the substance is the resolution-domain fold; the
  graph write is a thin delegation.

## Consequences

- The resolved graph becomes **rebuildable from the log** by a deterministic, idempotent, G1-complete
  projector — the engine ADR 0095's DR / erasure / reversibility story stands on.
- **Additive, behaviour-preserving, dormant.** Neo4j stays the live SoR; every existing merge / guard /
  audit / write assertion holds unchanged; the projector never touches the live graph.
- **New drift-guard surface:** the two `seq` columns + `projection_checkpoint` in the models and
  migration `0010_projection_outbox` must agree byte-for-byte (`tests/integration/test_migrations.py`,
  ADR 0030); `test_no_autogenerate_drift` must stay green (verify the `Identity()` declaration matches).
- Divergence classes **E1–E3** are recorded now so 3a-ii's parity guard is designed to expect them.

## ADR-index coupling

Adding this file requires the builder to re-run `python scripts/gen_adr_index.py` so
`docs/decisions/README.md` gains the `0100` row (else the `adr-index` CI check goes red). This header
uses the canonical list dialect (`Status` / `Date` / `human_fork` / `person_affecting` on the header
lines the generator parses), so the regenerated row reads `PROPOSED | 2026-07-04 | false | false`.
