# 0099 — Statement spine, step 1: statement + decision tables and the merge-time dual-write

- **Status:** ACCEPTED (2026-07-04)
- **Date:** 2026-07-04
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** Mithat — per ADR 0095:106–107 (user-confirmed) + Gate 2 fleet approval 2026-07-04
- **Realises:** ADR 0095 §1 (the first build step of the F1 statement-spine sequence) — see
  `docs/fable-review/70_EXECUTION_HANDOFF.md` Gate 2. **Supersedes:** nothing.

## Context

ADR 0095 (ACCEPTED, user-confirmed) sets the target storage topology: **PostgreSQL = the system of
record** (an append-only *statement log* + a *decision/judgement log*), **Neo4j = a derived,
rebuildable graph projection**. Its build sequence is explicitly staged and additive; step 1 is:

> Create statement + decision tables; **dual-write** the fused `StatementEntity` at merge time (it
> already exists in memory — persist it). **No user-facing change.**

The fused `StatementEntity` already exists. `resolution/merge.py` builds it at merge time
(`_fuse_statement_entity`, called from `_merge_entities`) to derive the Tier-1 witness map (ADR 0045),
then **discards it** — only the witness map survives onto the projected node. This gate persists that
same fused evidence to a durable statement log, plus one decision row per merge, at the existing
promote point in `resolution/pipeline.py`. Nothing about clustering, thresholds, scoring, the merge
guard, the referent rewrite, or the Neo4j write changes: **Neo4j stays the live system of record**;
this is purely additive substrate-banking. The cutover (outbox → projector → retire the direct write)
is Gate 3 (ADR 0095 steps 3–5); the backfill + per-cohort fidelity spike is the deferred **step 2**
(Gate 2b), referenced below but **not built here**.

Why now, and why small: banking the statement + decision logs is the substrate that makes merged-node
provenance-collapse structurally impossible, makes merges reversible belief-revision, and gives DR /
value-level erasure / incremental ER their persistent substrate (ADR 0095 §"What this buys"). Step 1
is the low-reversal-cost, no-behaviour-change foundation for all of that.

## Decision

Create two additive tables and dual-write to them, atomically, inside the existing per-batch commit.

### 1. `statement` table — the append-only per-claim log

One row per `(subject, entity_id, schema, prop, value, dataset)` claim the fused `StatementEntity`
yields (the `id` pseudo-statement excluded). This realises **G1 provenance at the per-claim grain**
(source_id, retrieved_at, reliability, raw pointer on every claim) — strictly richer than the
node-level `prov_*`.

| column | type | null | source at merge time |
|--------|------|------|----------------------|
| `id` | `String(64)` PK | no | fresh `uuid4()` surrogate (mirrors every other table) |
| `statement_id` | `String(64)`, indexed | no | FtM `Statement.id` (deterministic content hash) — dedup/backfill key |
| `canonical_id` | `String(255)`, indexed | no | **subject** = `statement.canonical_id` = the cluster's durable id (post-rekey) |
| `entity_id` | `String(255)`, indexed | no | `statement.entity_id` = the contributing source member id |
| `schema` | `String(64)` | no | `statement.schema` |
| `prop` | `String(64)` | no | `statement.prop` (never `"id"` — excluded) |
| `value` | `String` (unbounded / TEXT) | no | `statement.value` |
| `dataset` | `String(255)` | no | `statement.dataset` = the member's `Provenance.source_id` (G1 source) |
| `reliability` | `String(16)` | **yes** | member's `Provenance.reliability`, enriched via `by_id[entity_id]`; NULL if unstamped |
| `retrieved_at` | `String(64)` | **yes** | `statement.first_seen` = `Provenance.retrieved_at` (ISO-8601 **string** — no cast on hostile data) |
| `raw_pointer` | `String` | **yes** | `statement.origin` = `Provenance.source_record` (landing-zone pointer); NULL if unstamped |
| `first_seen` | `String(64)` | **yes** | `statement.first_seen` |
| `last_seen` | `String(64)` | **yes** | `statement.last_seen` (FtM defaults it to `first_seen`) |
| `method` | `String(64)` | **yes** | **not modelled anywhere today** → always NULL in step 1 (reserved) |
| `scope` | `String(64)`, `server_default "default"` | **yes** | reserved forward-compat (see Decision A) |
| `created_at` | `DateTime(timezone=True)`, `server_default now()` | no | insert time |

Honesty notes (per ADR 0095's "where a field isn't available, make it nullable rather than invent
data"):
- **`retrieved_at`, `first_seen`, `last_seen` coincide** at step 1 (the fusion sets `first_seen =
  Provenance.retrieved_at`, and FtM defaults `last_seen = first_seen`). All three columns are kept to
  match ADR 0095's named shape; they diverge only once incremental-ER re-observation lands (a later
  gate). This is the same real value now, not invented duplication.
- **`reliability`** is the only field enriched by a back-read: it is not carried on the FtM
  `Statement`, but it is real (the member's stamped `Provenance.reliability`) and is required by G1, so
  it is recovered deterministically via `by_id[statement.entity_id]`. NULL when a member was never
  stamped.
- **`method`** has no source in the current model (`Provenance` = source_id / retrieved_at /
  reliability / source_record only). It is a reserved nullable column, always NULL until a `method`
  field exists — a **blocked-on-not-modelled** criterion, not a promissory populated value.
- Timestamps are stored as **strings**, not `timestamptz`: `Provenance.retrieved_at` is a free-form
  connector-supplied string (hostile-data rule — a cast could raise). A `timestamptz` migration is a
  Gate 2b/3 concern once the values are known-normalised.

### 2. `decision` table — the merge/split/negative belief-revision log

One row per **promoted merge** (`cluster.is_merge`, i.e. ≥2 source members). Step 1 only ever writes
`kind="merge"`. This is the ADR 0095 decision log, distinct from the legacy `merge_audit` (which also
records singletons + `pending_review`) and from `resolver_judgement`/`sign_off` (human pair sign-offs
seeded into the resolver). The three coexist in step 1; a later gate reconciles them.

| column | type | null | value at merge time |
|--------|------|------|---------------------|
| `id` | `String(64)` PK | no | fresh `uuid4()` |
| `canonical_id` | `String(255)`, indexed | no | the id the decision acts on = `cluster.canonical_id` |
| `kind` | `String(16)`, indexed | no | `"merge"` (only kind in step 1; `"split"`/`"negative"` reserved) |
| `member_ids` | `JSONB` | no | `list(cluster.member_ids)` — the collapsed source ids (evidence of what merged) |
| `score` | `Float` | no | `cluster.score` (weakest-link match probability) |
| `decided_by` | `String(255)` | no | `"auto:resolver"` — the automated decider (no human in the auto-merge path) |
| `evidence` | `JSONB` | **yes** | `{"reason": reason}` when the guard reason is non-empty, else NULL |
| `supersedes` | `String(64)` | **yes** | always NULL in step 1 (belief-revision back-pointer — reserved for Gate 3 un-merge) |
| `superseded_by` | `String(64)` | **yes** | always NULL in step 1 (reserved) |
| `scope` | `String(64)`, `server_default "default"` | **yes** | reserved forward-compat (Decision A) |
| `created_at` | `DateTime(timezone=True)`, `server_default now()` | no | insert time |

`decided_by` fills ADR 0095's "human-or-`model@version`" slot with the automated-resolver identity; a
precise `model@version` string and the human-decision path (approve/reject → `decided_by=<operator>`)
are a later gate. The `merge_audit` row for the same cluster is unchanged; `decision` is the forward
substrate, `merge_audit` stays until Gate 3 reconciles them.

### 3. Dual-write module + hook

A **new module `resolution/statements.py`** owns both writers (statement + decision), because ADR 0095
treats the statement log + decision log as one SoR spine that Gate 3's projector reads together; the
existing tiny `resolution/audit.py` stays the `merge_audit`/`merge_alert` helper it is. `statements.py`
exposes, as pure `session.add` helpers whose caller commits (the exact idiom of `audit.record_merge`):

- `fuse_statement_rows(cluster, by_id) -> list[StatementRow]` — the single canonical projection: it
  calls the fusion, iterates `fused.statements` skipping `prop == "id"`, and builds one row per claim
  (enriching `reliability`). Consumed by **both** the persist path and the property test.
- `record_statements(session, cluster, by_id)` — `session.add`s the rows from `fuse_statement_rows`.
- `record_decision(session, cluster, *, reason)` — `session.add`s one `decision` row.

To keep one authoritative fusion function feeding both the witness map and the statement log,
`merge.py`'s `_fuse_statement_entity` is **renamed to the public `fuse_statement_entity`** (its single
internal caller updated). This is a **pure identifier rename — zero logic, threshold, score, or value
change** — guarded by the non-mutation/byte fence property below and the existing merge suite.

**Call site + transaction ordering** (in the `pipeline.py` promote block, after the cluster is decided
to be written and re-keyed to its durable id, extending the existing sequence):

```
if cluster.is_merge:
    record_durable_id(...)          # existing
record_merge(session, cluster, decision="merged", reason=reason)   # existing
record_statements(session, cluster, by_id)          # NEW — every promoted cluster (singleton + merge)
if cluster.is_merge:
    record_decision(session, cluster, reason=reason) # NEW — merges only
_set_status(...)                    # existing
```

All rows are added within the same batch and committed by the one `session.commit()` at
`pipeline.py:148`, so they land **atomically** with `merge_audit`/`canonical_id_ledger` and **roll back
together** on any failure. A `pending_review` (parked) cluster takes the block-mode `continue` at
`pipeline.py:425` and therefore writes **no** statement rows and **no** merged decision row.

## Decision A (owned) — the reserved `scope` column

Both tables carry a `scope String(64)`, nullable, `server_default "default"`, **UNENFORCED**: no code
reads, writes, or filters on it. It is a forward-compatibility reservation only, sourced from the
execution handoff (`docs/fable-review/70_EXECUTION_HANDOFF.md:81`).

This runs *opposite in direction* to ADR 0042 (migration `0004_drop_tenant_id.py` dropped `tenant_id`;
single-tenant D1), so it is stated explicitly: **this is NOT re-adding tenant scoping.** Single-tenant
D1 (ADR 0042) is unchanged — there is no tenant/workspace resolution, no request scoping, no query
predicate on `scope`. It is an inert reserved slot on brand-new tables so that *if* a future
multi-scope/workspace requirement ever lands, the SoR spine already has a place to put it without a
data-shape migration on a populated statement log.

- **`human_fork: false`.** Reserving one inert nullable column is not a product/architecture fork and
  does not relitigate ADR 0042.
- **Reversal cost:** low — drop one unused nullable column (no data depends on it, nothing reads it).
- **Revisit trigger:** a genuine multi-tenant / multi-scope / multi-workspace requirement is accepted
  (at which point ADR 0042 itself is revisited on its own merits — not by this ADR).

## Decision B (owned) — person-affecting self-tag

**`person_affecting: false`.** The dual-write is a faithful *record* of a decision already made by the
existing, unchanged merge path. It changes **no** ER threshold, **no** clustering/merge outcome, **no**
individual-affecting score, **no** guard behaviour, **no** projected graph. ADR 0095:106–107
(user-confirmed) states this directly: *"Not person-affecting in itself; the ER decisions it enables
(thresholds, merges) keep their human sign-off unchanged."*

Because this gate's diff touches `resolution/**` (a person-affecting *area*) while self-tagging
non-sensitive, ADR 0097 §4/§5 requires an explicit, auditable human co-sign — carried in the header
above (`human_cosign`). The claim the checker/judge must reproduce against the diff is narrow and
verifiable: **no merge/threshold/score/guard behaviour changes** — `merge.py`'s edit is a rename only;
`pipeline.py`'s edit only *adds* `session.add` calls after the existing decision is taken; the projected
Neo4j entity and the `merge_audit` row are byte-identical with the dual-write on or off (the fence
property below proves it).

## The mandatory property invariants (`tests/property/test_prop_statement_spine.py`)

Merge/provenance surface → a `@given` property suite is mandatory (CLAUDE.md build-discipline).

- **P-STMT-1 — Lossless projection.** For a promoted cluster, the persisted statement rows equal —
  none invented, none dropped, `id` pseudo-statement excluded — the set of claims derived
  **independently** from the member entities: for each member `m ∈ cluster.member_ids`, each
  `prop ∈ m.properties` with `prop != "id"`, each `value ∈ m.get(prop)`, the tuple
  `(canonical_id = cluster.canonical_id, entity_id = m.id, schema = m.schema.name, prop, value,
  dataset = source_of(m))`; and each row's `reliability` / `retrieved_at` / `raw_pointer` match that
  member's `Provenance`. Exercised through a real Postgres round-trip (`postgres_dsn`) so column
  length / NULL fidelity is proven.
- **P-STMT-2 — Non-mutation fence.** `fuse_statement_rows` / `record_statements` / `record_decision`
  leave `cluster.entity.to_dict()` and every `by_id[m].to_dict()` byte-identical to a snapshot taken
  immediately before, and touch only the `statement` / `decision` tables (never `MergeAudit`, never the
  FtM entity). Equivalently: the entity handed to `write_entities` and the `merge_audit` row are
  identical (by content signature, excluding `uuid`/`created_at`) whether the dual-write runs or is
  monkeypatched to a no-op — proving the persist is a pure side-effect.
- **P-STMT-3 — Exactly-one-decision + parked-writes-nothing.** Every promoted merge (`is_merge`)
  appends exactly one `decision` row consistent with its `merge_audit` row (same `canonical_id`,
  `member_ids`, `score`, `kind="merge"`); a promoted singleton writes statement rows but **no**
  decision row; a `pending_review` (parked) cluster writes **no** statement rows and **no** merged
  decision row.

## Deferred (explicitly not built here)

- **Gate 2b (ADR 0095 step 2) — backfill + per-cohort fidelity spike.** Data-dependent: single-source
  nodes reconstruct exactly; merged nodes from `prov_witnesses`; pre-0045 merges may be lossy. Run the
  fidelity spike first and record the per-cohort choice — a later fleet pass.
- **Gate 3 (ADR 0095 steps 3–5) — outbox → idempotent projector → scheduled rebuild-and-diff →
  cutover → retire the direct write.** The `supersedes`/`superseded_by` write path (un-merge as a
  superseding decision), any `timestamptz` normalisation, a `method` field, folding `merge_audit` into
  `decision`, and any statement-id uniqueness/idempotency constraint all belong to that gate.

## Reversibility

**Reversible** (ADR 0095 is additive/low-reversal-cost until cutover). Reversal cost: drop the two
tables (`downgrade()`), delete `resolution/statements.py`, revert the ~4-line `pipeline.py` hook and
the `merge.py` rename. No data migration of live graph state, no behaviour to unwind (the projection
never depended on the new tables). **Revisit trigger:** Gate 2b's fidelity spike, or Gate 3's projector
needing a schema change (statement-id uniqueness, `timestamptz`, supersession pointers) — at which point
these tables are extended, not reworked.

## Consequences

- **The statement log becomes the G1 substrate at the per-claim grain** — richer than the node-level
  `prov_*` projection, and the thing Gate 3's projector rebuilds Neo4j from.
- **Additive, behaviour-preserving.** Neo4j stays the live SoR; every existing merge/guard/audit/write
  assertion holds unchanged. The new writes commit atomically with `merge_audit` and roll back together.
- **Temporary redundancy** between `merge_audit` and `decision` (both carry canonical_id / member_ids /
  score / reason) is intentional and documented; Gate 3 reconciles them.
- **New drift-guard surface:** the two models and migration `0009_statement_spine` must agree
  byte-for-byte (`tests/integration/test_migrations.py`, ADR 0030); `_migration_guard` auto-applies the
  lock timeout to the new DDL (ADR 0084) — no builder action needed.

## Alternatives rejected

- **Put the writers in `resolution/audit.py`.** Rejected: the statement projection (re-fusion +
  reliability enrichment) is substantive and imports the fusion; co-locating bloats the tiny audit
  helper. `statements.py` groups the ADR-0095 spine writes the Gate-3 projector reads together.
- **Stash the fused `StatementEntity` on `ResolvedCluster`** to avoid re-fusing at persist time.
  Rejected for step 1: `ResolvedCluster` is frozen and the fused entity is deliberately discarded;
  re-fusion is O(statements) and negligible next to Splink/Neo4j. Revisit only if profiling says so.
- **`statement_id` as PK / a UNIQUE(statement_id) constraint** for idempotency. Rejected for step 1:
  it risks aborting a whole batch on a replay-after-partial-commit, and idempotency is already provided
  by B-1 (a committed batch's items are never re-loaded — the same guarantee `record_merge` relies on).
  `statement_id` is kept as an indexed non-unique column so a later gate can add the constraint.
- **`timestamptz` columns / populating `method`.** Rejected: the source values are free-form strings
  and `method` is unmodelled — casting or inventing them violates the hostile-data and no-invented-data
  rules. Nullable string columns, upgraded when the data is known-normalised.

## ADR-index coupling

Adding this file requires the builder to re-run `python scripts/gen_adr_index.py` so
`docs/decisions/README.md` gains the `0099` row (else the `adr-index` CI check goes red). This header
uses the canonical list dialect (`Status`/`Date`/`human_fork`/`person_affecting` on lines 3–6) the
generator parses, so the regenerated row reads `ACCEPTED | 2026-07-04 | false | false`.
