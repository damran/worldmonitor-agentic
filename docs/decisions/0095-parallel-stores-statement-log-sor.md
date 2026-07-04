# 0095 — Parallel data stores: Postgres statement-log = system of record, Neo4j = derived graph projection

- **Status:** ACCEPTED (2026-07-04) — **direction confirmed by the user**; supersedes foundational
  locked decision **#2** ("Neo4j + GDS = system of record. No parallel datastore"). This ADR records
  the *target architecture and the direction*; the build is the multi-gate F1 sequence (below) and is
  **not yet cut over** — until it is, Neo4j remains the live system of record.
- **Date:** 2026-07-04
- **human_fork:** the reversal of a *locked* decision — so recorded as a user-confirmed architectural
  decision, not an agent classification. The user wrote: *"your main decision is to use parallel data
  stores — postgres and neo4j as the graph end. I agree and confirm it. Makes much more sense."*
- **Realises:** the strategic review's finding **F1** (`docs/fable-review/50_FABLE_REVIEW.md`) and the
  Track-2 substrate position. **Supersedes:** locked decision #2; **amends** the "no parallel
  datastore" clause and `docs/20_ONTOLOGY.md` §2.3 ("Store = Neo4j").

## Context

Locked decision #2 made **Neo4j the sole system of record** with an explicit "no parallel datastore"
rule. That was the correct v0 call (one store, one truth, fastest path to a working spine via the
FtM→Neo4j tooling). But it forces the platform's provenance, reversibility, erasure, DR, and
streaming-resolution goals to fight the substrate — the review traced **every** tension in the
digest §12 to storing the *merged output* (a labelled property graph) rather than the *evidence*.
The project already half-lives with two truths: multi-source provenance is admittedly thinner in the
Neo4j projection than in the Postgres audit tables behind it, and ADR 0045 already constructs a
`StatementEntity` (per-claim statements) transiently at merge time and then discards it, persisting
only the projection.

The two flagship products of WorldMonitor's own ecosystem already resolve this the same way:
**OpenSanctions/nomenklatura** are statement-based internally (per-claim rows grouped by
`canonical_id`; merge/unmerge = repointing, sources never mutated), and **Aleph/OpenAleph** runs
**PostgreSQL as the system of record with a rebuildable index** as the query view. This ADR adopts
that shape.

## Decision

**Two parallel stores with a single truth and a rebuildable projection:**

1. **PostgreSQL = the system of record.** A durable, append-only **statement log** (per-claim rows:
   subject · schema · prop · value · dataset/source · `retrieved_at` · `first_seen`/`last_seen` ·
   reliability · raw pointer · method) plus a **decision/judgement log** (merge/split/negative,
   evidence, decided-by human-or-model@version, supersedable). This is where "one truth" lives.
2. **Neo4j Community + GDS = the derived graph projection** — "the graph end": the resolved,
   canonicalised property graph used for traversal, pattern queries, and GDS analytics. It is
   **derived, disposable, and rebuildable** from the statement + decision logs by an idempotent
   projector (transactional outbox → projector; a scheduled full-rebuild-and-diff job is the DR
   story and the fold-determinism guard).

**The "no parallel datastore" rule is superseded, not violated in spirit.** Its *intent* — never two
divergent truths — is now enforced **by construction**: statements are the one truth; Neo4j (and any
future search/ANN index) are derived views that can always be rebuilt from it. Postgres was never a
"parallel datastore" in the forbidden sense (it always held relational/audit state); the change is
which store is *canonical* for entity facts.

### What this buys (all structural, not by discipline)
- Merged-node provenance collapse **cannot exist** (a merged entity *is* its N statements).
- Merges become **belief revision**: a decision row repoints `canonical_id`; **un-merge** is a
  superseding decision + reprojection — catastrophic merges become reversible.
- The **dual-write problem disappears**: Postgres is the sole write domain; the projection is async +
  idempotent, so at-least-once + idempotent MERGE suffices (no saga).
- **DR** = `pg_dump`/PITR of statements; Neo4j Community's missing hot-backup/HA/RBAC become
  irrelevant because the graph is disposable — **decisive given the "Neo4j Community forever /
  no paid products" constraint (ADR 0094 D5)**: rebuild-from-Postgres is the only DR story CE will
  ever have.
- **Value-level GDPR erasure** = `DELETE … WHERE` + reproject (WM-ERASE-T2 dissolves).
- **Tier-2 provenance** (ADR 0045) stops needing reified `(:Statement)` nodes except as an optional
  read optimisation — statements are first-class where they live.
- **Incremental ER** (review F2) gets its persistent resolved index and durable judgement log for
  free — this *is* that substrate.

## The one genuine risk (recorded so it is chosen, not discovered)

**Fold/projection determinism is the hard part.** Deterministic materialisation under decision
supersession and re-canonicalisation is where this design can rot into a *mutated* projection (two
half-truths). Mitigations are mandatory and part of the build's primary invariant tests:
fold-determinism property suite; a scheduled **full-rebuild-and-diff** job whose failure pages the
operator; and a rule that the projection has **no write path except the projector**. If that diff
job is ever disabled, this design has failed.

## Build sequence (the F1 gates — additive, low reversal cost until cutover)

1. Create statement + decision tables; **dual-write** the fused `StatementEntity` at merge time (it
   already exists in memory — persist it). **No user-facing change.**
2. **Backfill** — fidelity varies by cohort (single-source nodes reconstruct exactly; merged nodes
   from `prov_witnesses`; full fidelity needs re-map + judgement replay; pre-0045 merges may be
   lossy). Run a fidelity spike first; record the per-cohort choice.
3. **Outbox + idempotent projector + scheduled rebuild-and-diff** (the fold-determinism guard).
4. Cut the graph writer over to the projector.
5. Retire the direct write path.

You can stop after step 2 and still have banked the audit substrate.

## Reversibility

Reversible up to step 4 (drop the projector, keep writing Neo4j directly). After cutover the SoR is
Postgres; reverting to Neo4j-as-SoR would be a fresh migration. **Revisit trigger:** the
fold/projection maintenance cost proves higher than the merged-node/DR/erasure pain it removes — the
scheduled rebuild-and-diff job is the early-warning signal.

## Consequences for the record

- **Locked decision #2 is amended** in `CLAUDE.md` (+ `AGENTS.md`/`.clinerules` mirrors) to state the
  target: Postgres statement-log = SoR; Neo4j = derived projection; "one truth (statements) + N
  rebuildable views" replaces "Neo4j sole SoR / no parallel datastore". The mirror byte-identity test
  (`tests/test_contract_consistency.py`) still holds.
- `docs/20_ONTOLOGY.md` §2.3 ("Store = Neo4j") is updated to "Store = statement log (Postgres, SoR) →
  projected to Neo4j" in the F5 truth-up sprint.
- Not person-affecting in itself; the ER decisions it enables (thresholds, merges) keep their human
  sign-off (ADR 0031/0047) unchanged.
