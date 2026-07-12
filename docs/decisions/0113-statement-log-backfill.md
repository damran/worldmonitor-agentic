# 0113 — Statement/context-claim log backfill (Gate 2b): complete the SoR spine from the retained ingest substrate

- **Status:** ACCEPTED (2026-07-12; proposed 2026-07-12)
- **Date:** 2026-07-12
- **human_fork:** false — every sub-fork below is a reversible default with a revisit trigger; the
  backfill writes are append-only INSERTs into `statement`/`context_claim`(/`decision`) undone by
  deleting the batch and re-running (content-addressed → convergent), with the live graph and direct
  writer untouched. No data-shape lock-in; the one irreversible consumer is the later, human-gated
  Gate 3b cutover — a separate gate.
- **person_affecting:** true — backfill materialises historical, possibly person-referencing
  contributions into the permanent append-only SoR and interacts with erasure two ways: it self-heals
  the P2 over-removal residual (restoring surviving values erasure wrongly dropped — correct direction),
  and a wrong backfill SOURCE could resurrect an already-erased subject's claims into the
  never-forgetting log. Erasure/GDPR surface. human_cosign REQUIRED before build.
- **human_cosign:** Mithat 2026-07-12 — cosigned before the code build (a genuine person-affecting
  classification, not a waiver), with the four disclosures (SF-1 er_queue substrate; person-data into
  the permanent SoR guarded by forget-safety; erasure self-heals over-removal; fidelity spike
  blocked-on-real-seed) presented. The build then surfaced, via an adversarial multi-lens verify + an
  Opus judge (fresh-context runtime reproduction), two engineering gaps — a missing WPI-3 single-writer
  lock and vacuous edge coverage — both fixed + tested; the judge APPROVEd with one MEDIUM
  (evolving-snapshot union under-capture, deterministic-order-mitigated, the SF-1 landing-re-map revisit
  trigger) + two LOWs, none forget-safety-affecting (see §Verification record). The person-affecting
  forget-safety invariant held and was reproduced non-vacuously throughout.
- **Realises:** ADR 0095 step 2 — the statement-log becomes **complete**, so a `full_rebuild`
  reconstructs the whole graph and the Gate 3b cutover is safe; and the Fable log-capture consult's
  Gate-2b line (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md` §5, §9). **Builds on:** ADR 0099 (the
  spine + Gate-2a dual-write this backfills behind), ADR 0106 (the `context_claim` lane), ADR 0100/0101
  (the fold whose `full_rebuild` this feeds), ADR 0111 / WPI-2 (the alias⇔co-commit completeness check
  this backfill *discharges* — see §Context), ADR 0112 / WPI-1 (the existence-claim path + rider-1
  source-reachability guard the backfill inherits), ADR 0107 (the erasure scrub whose KNOWN RESIDUAL
  this backfill self-heals), ADR 0083 (reference-GC → landing objects for live contributions are
  retained, never orphan-swept), ADR 0060 (fail-closed-on-no-provenance). **Prerequisite for:** Gate 3b
  cutover (the first sanctioned live `full_rebuild`). **Supersedes:** nothing.

## Context (the problem Gate 2b closes)

Gate 2a began **dual-writing** `statement`/`decision`/`context_claim` rows at the pipeline promote point
(`pipeline.py:497-508`) and, since P3, the sign-off promote point. Every contribution resolved **before**
that dual-write began wrote the live graph + `merge_audit` + `canonical_id_ledger` but **no spine rows**.
Two consequences make this a hard prerequisite, not a fidelity nicety:

- **A full_rebuild RAISES today.** `canonical_id_ledger` is append-only and read in full by the fold
  (`projector.py:255-276`); every pre-2a supersession alias exists with **no** co-committed statement
  row, so `find_incomplete_aliased_survivors` returns a non-empty set and `project(full_rebuild=True)`
  **raises `IncompleteAliasedSurvivorError`** (`projector.py:385-395`, `spine_integrity.py:95-99`). WPI-2
  made this fail loud precisely so it could not corrupt a rebuild silently. **Gate 2b is what discharges
  the WPI-2 obligation** — backfilling an aliased survivor's statement rows moves it from `targets −
  covered` into `covered`, and completion is *self-verifying*: when the backfill is complete,
  `find_incomplete_aliased_survivors(...) == ∅`.

- **The erasure over-removal residual self-heals here.** ADR 0107's `prune_live_to_fold` KNOWN RESIDUAL
  (`erasure_scrub.py:301-331`) over-removes (never leaks) a live value that an erased source logged AND a
  **surviving but never-logged legacy source** also holds, because the post-scrub fold has zero log
  evidence for that value. Once every *surviving* contribution has a foldable row, the fold reconstructs
  and keeps the value. **Verified conditional (see SF-1):** the self-heal is *retroactive* only if the
  backfill reads from a store that still holds the surviving source's value (`er_queue`/landing) — **not**
  the already-pruned live graph, which `set_node_values` (`graph/ops.py:247-252`) already stripped the
  over-removed value from.

## Verified facts the decision space rests on (first-hand, master `dd3002a`)

1. **The spine has no `UNIQUE(statement_id)`** — dedup is projector-side on `statement_id`
   (`projector.py:148-154`, `models.py:326`). A `statement_id` is a deterministic content hash:
   `sha1(f"{dataset}.{entity_id}.{prop}.{value}")` for a normal claim (FtM `Statement.generate_key()`;
   **does not include `canonical_id`**), `sha256(canonical_id ␀ entity_id ␀ "wm:exists" ␀ dataset)` for a
   WPI-1 existence claim. Reproducing a row byte-faithfully requires the per-member `(dataset, entity_id,
   prop, value)` preimage.
2. **`statement.dataset` and `context_claim.dataset` are both already `NOT NULL` and indexed**
   (`models.py:337,559`; `migration 0013_erasure_scrub_dataset_index` created *both* `ix_statement_dataset`
   and `ix_context_claim_dataset`). **No index work is left for Gate 2b.**
3. **Rider-1 (ADR 0112) already closed the source-unreachable write path** for new writes: `statements.py`
   skips-and-logs any member whose `Provenance` is `None`/empty-`source_id` before writing, so every
   written `dataset` equals a real `source_id`. The `member.id`/`""` fallback in `merge._member_source`
   (`merge.py:357-367`) is dead for written rows.
4. **Both value-bearing substrates are already forget-safe.** `erase_source` (`erasure.py:157-235`) deletes
   the erased source's landing prefix and redacts its `er_queue.raw_entity` to a `{"erased": True}` shell —
   so a backfill from landing/`er_queue` that skips redacted shells cannot resurrect an erased source. The
   stores erase does *not* touch (`merge_audit`, `canonical_id_ledger`) carry **no claim values**, so they
   are not value-resurrection vectors.
5. **The live graph cannot reconstruct byte-faithful rows** — `prov_witnesses` is prop-granular
   (`{prop:[datasets]}`, no per-value attribution, `graph/ops.py:106-136`) and per-member `entity_id` is
   lost at merge (`projector.py:229-240`). So the `sha1` preimage is unrecoverable from Neo4j.
6. **The core backfill is a DATA operation, not DDL** — it INSERTs rows through the existing frozen
   writers; it needs **no schema migration** unless SF-5/SF-6 riders are taken (both recommended declined).

## Decision space (each sub-fork: recommended reversible default + reversal cost + revisit trigger)

### SF-1 — Backfill SOURCE (the load-bearing fork; a genuine architecture choice for the user)

From what store does Gate 2b reconstruct pre-2a contributions? Fidelity ranking (first-hand):

| Rank | Source | Byte-faithful `statement_id`? | Forget-safe? | Note |
|---|---|---|---|---|
| **1** | **`er_queue_item.raw_entity`** | **YES** — resolution builds its members from `raw_entity` via `make_entity`, so re-parsing reproduces the identical member ⇒ identical hash (`pipeline.py:331,354,497`) | **YES** (redacted to a shell on erase) | never hard-deleted (ER-QUEUE-NEVER-HARD-DELETED invariant, `gc.py:185-193`); needs the ledger for `canonical_id` via `build_survivor_of` |
| 2 | **landing re-map** (re-run `connector.map()` over retained raw objects) | YES *iff* `map()` deterministic + connector still present | YES (prefix deleted on erase) | the consult's named "2b machinery"; reference-GC never sweeps referenced objects (ADR 0083); risk = connector drift/removal |
| 3 | live Neo4j read-back | **NO** (fact 5) | yes (pruned) | unusable for faithful rows |
| 4 | `merge_audit` | NO (no values/provenance/entity_id) | n/a | not a substrate |
| 5 | `canonical_id_ledger` | NO (aliases only) | n/a | the `survivor_of` *input*, not a source |

- **(a) DEFAULT — `er_queue.raw_entity` primary, landing re-map as fallback/corroboration.** For each
  non-redacted row: `make_entity(raw_entity)` → member FtM entity; `canonical_id =
  build_survivor_of(session)(member.id)`; then project through the **existing frozen** `fuse_statement_rows`
  / `_existence_claim_rows` / `fuse_context_claim_rows`, inheriting rider-1's source-reachability skip and
  WPI-1's existence-claim path for free, and reproducing the exact `statement_id` because it replays the
  same inputs resolution itself dual-wrote from. Landing re-map fills any gap where `raw_entity` is a
  redacted shell (skip — that source is erased) or as a raw-bytes cross-check. **Reversal cost:** low
  (append-only; `DELETE` the batch, re-run). **Revisit trigger:** a post-backfill `full_rebuild` still
  raising `IncompleteAliasedSurvivorError` or reporting unexplained live-⊋-fold survivors ⇒ an
  `er_queue` gap → fall to landing re-map; **or** enrichers get wired (INTERNAL_ENRICHMENT), whose claims
  are unreconstructable-forever and must be captured at ingest, never backfilled (consult §5).
- **(b) landing re-map only** ("raw is canonical" purity, the consult's original framing). Rejected as the
  default because it adds connector-drift / `map()`-non-determinism / connector-removal risk that (a)
  avoids by reading the map() output `er_queue` already holds. Kept as the fallback + the raw cross-check.
- **(c) live Neo4j read-back.** Rejected — fact 5: cannot reproduce faithful rows, and it cannot
  retroactively self-heal the erasure residual (the over-removed value is already gone from the node).

**This is the one item where the code-evidence deviates from the Fable consult** (which evaluated landing
only and never considered `er_queue` as a substrate). It is surfaced to the user as a real architecture
choice, though it remains a *reversible* default (append-only), not a `human_fork`.

### SF-2 — Backfill SHAPE: one-time, idempotent, projector-dedup-tolerant

A **one-time reconciliation** of the pre-2a window, but **built safely re-runnable**: both substrates also
hold post-2a contributions already in the log. With no `UNIQUE(statement_id)`, a naive backfill
double-writes every post-2a row — harmless to the *projection* (content-addressed dedup) but it bloats the
append-only table. **Default:** pre-filter on the existing `statement_id` set
(`SELECT statement_id FROM statement`) and skip rows already present, making the backfill idempotent and a
safe incremental catch-up. **Reversal cost:** low. **Revisit trigger:** table growth or run time makes the
Python-side set-filter impractical at scale → push the anti-join into SQL.

### SF-3 — Forget-safety (person-affecting; a completeness requirement, not a fork)

The mandatory GDPR guard on the resurrection hazard (fact 4). Gate 2b MUST: (i) read only from the
forget-safe substrates (landing / `er_queue`) and **skip any redacted/erased shell**; (ii) consult the
erase-audit — the same `TaskRun(kind="erase", status="ok").stats["source_id"]` enumeration `scrub_stock`
already uses (`erasure_scrub.py:453-463`) — and **exclude those `source_id`s** from the backfill; and
(iii) **re-run `scrub_stock` after the backfill** as belt-and-suspenders, re-closing any row a race
re-introduced (backfilled rows are keyed `dataset == source_id`, so the scrub reaches them exactly like
dual-write rows). Verified by a `rebuild-contains-no-erased-source` check (SF-4 / the SF-6-style property).
**Reversal cost:** n/a (a safety requirement). **Revisit trigger:** any new value-bearing store outside
`erase_source`'s purge scope (a DB/graph snapshot, an offsite mirror) becomes a backfill input → it must be
proven forget-safe or excluded first.

### SF-4 — Completion criterion + fidelity measurement

Gate 2b's completion is a **fold-side coverage assertion**, not just a divergence number:
`find_incomplete_aliased_survivors(...) == ∅` **AND** a whole-graph divergence spike over exactly the
`divergence._excluded` axes (`id`, `caption`, bare anchor keys, `datasets`/E4, `prov_*`) — **never** the
IT-PROJ equivalence signature, which is valid only in the single-batch null-divergence regime and would
false-alarm on legitimate cross-batch drift. The **per-cohort fidelity spike** the roadmap wants (fidelity
by source/vintage, incl. pre-0045 lossy merges) needs a **real-seed corpus, which is operator-blocked**.
**Default:** ship the mechanism with synthetic + `@given` property coverage now and mark the per-cohort
spike **blocked-on-real-seed / blocked-on-measurement-harness** (do not claim a fidelity validation we
cannot run — CLAUDE.md build-discipline). **Reversal cost:** n/a. **Revisit trigger:** operator provides a
real-seed host → run the per-cohort spike, then re-open the "how lossy is a pre-0045 merge" question.

### SF-5 — `statement.dataset` stamped-ness rider (Gate-2a MEDIUM backlog)

Rider-1 (ADR 0112) already made every *written* `dataset` a real `source_id` (fact 3), so the backlog item
reduces to a **backfill-side obligation** + an optional proof:
- **(c) DEFAULT — route the backfill through the same rider-1-guarded projections** (or reproduce the
  guard) so no backfilled row carries an empty/`member.id`-keyed dataset; pin it with a test asserting
  every backfilled `dataset` equals a contributing member's `source_id`. **Reversal cost:** zero (no
  schema).
- **(a) OPTIONAL — a belt-and-suspenders `CHECK (dataset <> '')`** on both tables (a DB-level proof the
  writer invariant holds). Recommended **declined** by default: redundant given rider-1, needs a migration
  with `NOT VALID` + `VALIDATE` on the now-populated `statement` table (ADR 0084), and the stronger
  "`dataset` is not a `member.id`" half is **not expressible as a CHECK** (member ids have no distinguishing
  syntax — the invariant is semantic, writer-only). **Reversal cost of taking it:** a migration to add + one
  to drop. **Revisit trigger:** a defence-in-depth audit wants a DB-enforced non-empty proof.
- **(b) make `dataset` nullable — REJECT.** A NULL `dataset` defeats P2's `WHERE dataset = source_id`
  erasure reach and breaks the fold's G1 non-empty-provenance guarantee (`NodeProvenanceError`).

### SF-6 — E4 `origin_datasets` rider (the consult's "optional cheap rider")

The FtM `datasets` label and `Provenance.source_id` are **genuinely different axes** — they coincide for 7
of 8 connectors but **never for opensanctions** (label = upstream slug e.g. `ie_unlawful_organizations`;
`source_id` = `opensanctions:<slug>`). The log only ever stores `source_id`; the fold reconstructs node
`datasets` from `source_id`s (`projector.py:221`), and `divergence._excluded` drops `datasets` as "E4 —
reconstructed but batch-dependent" (`divergence.py:84,100`).
- **DEFAULT — DECLINE the `origin_datasets` column; adopt the E4 *semantic redefinition* (node `datasets`
  := the source-id set of the folded claims) as an ADR statement only.** Rationale: node `datasets` has
  **zero in-repo consumers**; the one genuine loss (the opensanctions upstream slug) is re-map-recoverable;
  and the guard-strength payoff (deleting the `datasets` exclusion) is **undercut** — the consult's
  live⊆fold monotonicity holds only in a no-erasure/no-zero-prop regime, both now violated in-tree (P2
  scrub + WPI-1), so capturing the column would *not* make it safe to drop the exclusion. The consult
  itself calls the rider **optional/reversible and explicitly NOT load-bearing for cutover** ("decide at 2b
  planning; do not build a standalone gate"). **Reversal cost of the default:** none. **Revisit trigger:**
  an actual consumer of node `datasets` appears, OR a decision to compare `datasets` in the divergence
  guard after the erasure-granularity reconciliation lands → then add the additive nullable column (a
  migration at that point).

### SF-7 — the dangling `P-ERASE-5` citation (doc rider)

`erasure_scrub.py:323` cites "`(SF-5, P-ERASE-5)`" but `P-ERASE-5` is defined nowhere (only `P-ERASE-1..4`
exist; SF-5 is 0107's "Stock, not just flow"). **Default:** Gate 2b either formally defines the
log-completeness-boundary property as `P-ERASE-5` (the natural owner, since 2b closes it) or fixes the
citation. A one-line doc/comment rider inside the gate. **Reversal cost:** none.

### SF-8 — Migration

The core backfill needs **no migration** (SF-2 is a data op through frozen writers). A migration is added
**only if** SF-5(a) (the CHECK) or SF-6 (the column) is taken — both recommended declined. If one is taken:
model↔migration byte-agreement (ADR 0030 drift guard), dialect-aware `lock_timeout` (ADR 0084; already
applied in `env.py`), chain from the current head **`0013_erasure_scrub_dataset_index`**. **Default:** no
migration.

## Open decisions for the user (surface at planning — THIS IS THE PAUSE)

1. **person_affecting classification + ceremony.** Recommended **TRUE** (moves person-referencing data into
   the permanent SoR + interacts with erasure) → **cosign-before-build + the cosign fleet** (Opus checker +
   judge + mandatory `@given`), as P2/0107 did — *not* the lean all-Sonnet WPI fleet. Note: **either way a
   `human_cosign` line is mandatory**, because the diff touches the enumerated erasure surface (checker +
   judge FAIL/DENY an un-cosigned false-tag there, ADR 0097 §5).
2. **SF-1 backfill source.** Recommended **`er_queue.raw_entity` primary + landing re-map fallback**
   (byte-faithful, no map()-drift, forget-safe) over the consult's landing-only framing. A genuine,
   reversible architecture choice — confirm or redirect.
3. **Fidelity-spike scope (SF-4).** Recommended **ship the mechanism now with synthetic + property
   coverage; mark the per-cohort fidelity spike blocked-on-real-seed** (operator host/keys) rather than
   wait on the operator or claim an unrunnable validation.
4. **Riders scope (SF-5a / SF-6 / SF-7).** Recommended **decline** the belt-and-suspenders `CHECK` and the
   `origin_datasets` column (→ **no migration**), **adopt** the E4 semantic redefinition as an ADR line and
   the `P-ERASE-5` doc fix. Confirm, or opt into a rider (which adds a migration).

## Decided (2026-07-12, user scoping)

The user resolved all four open decisions at planning (2026-07-12), choosing every recommended default:

- **D1 — person_affecting: true + the cosign fleet.** Gate 2b is person-affecting; the user cosigns BEFORE
  the code build and the build runs the full cosign fleet (test-author → builder → adversarial multi-lens
  verify → judge), NOT the lean WPI fleet. The dated `human_cosign` line + accept-flip land at gate end with
  cumulative disclosure (house pattern). The erasure-self-heal claim (SF-1/SF-3) is verified with the P2
  fresh-skeptic-per-fix-round pattern.
- **D2 — SF-1(a): `er_queue.raw_entity` primary + landing re-map fallback.** The byte-faithful substrate; the
  fallback wires behind an off-by-default flag. (Deviates from the consult's landing-only framing — disclosed.)
- **D3 — SF-4: ship the mechanism now with synthetic + `@given` coverage; the per-cohort fidelity spike is
  blocked-on-real-seed** (operator host/keys). No fidelity validation is claimed on a promissory note.
- **D4 — SF-5/SF-6/SF-8: no schema change / no migration.** Decline the belt-and-suspenders `CHECK` (SF-5a)
  and the `origin_datasets` column (SF-6); the backfill inherits rider-1 stamped-ness. Adopt the E4 semantic
  redefinition (node `datasets` := the source-id set of the folded claims) as this ADR line only, and fix the
  dangling `P-ERASE-5` citation (SF-7). Gate 2b stays a pure data operation.

**Cosign disclosures (surfaced before the build — ADR 0097 §5):** (i) the SF-1 substrate deviates from the
consult (er_queue over landing); (ii) person-referencing historical data is materialised into the permanent
append-only SoR — forget-safety (SF-3) is the mandatory guard (forget-safe substrate + erase-audit exclusion
+ post-backfill re-scrub + a rebuild-contains-no-erased-source property); (iii) erasure reaches
effectively-*more* (the over-removal residual self-heals — correct direction, never a leak); (iv) the
per-cohort fidelity spike is not runnable now (blocked-on-real-seed). No genuine human fork — no data-shape
lock-in; the irreversible consumer is the later, separately human-gated Gate 3b cutover.

## Verification record (2026-07-12)

Built via the person-affecting cosign fleet: test-author (RED-first P-BACKFILL-1..4 + integration +
unit) → builder → a multi-lens adversarial verify → an Opus judge (fresh-context runtime
reproduction). The verify surfaced two real gaps, **both fixed + tested before the judge**:

- **INV-SINGLE-WRITER** — `backfill_spine` is a NEW SoR-spine writer that skipped the WPI-3
  `acquire_spine_writer_lock` (ADR 0110). Without it a concurrent writer defeats the `statement_id`
  dedup pre-filter (no `UNIQUE`) and interleaves `seq`. Now taken fail-closed, like
  `pipeline.py`/`signoff.py` (`IT-BACKFILL-3`).
- **Edge completeness** — the corpus had no edge, so the divergence spike was vacuous for
  `unexplained_edges`. `IT-BACKFILL-4` backfills an `Ownership` edge and proves `full_rebuild`
  re-materialises the relationship non-vacuously.

**Judge verdict: APPROVE** (all invariants reproduced at runtime; forget-safety and byte-faithfulness
non-vacuous; the corrected test precondition honest, not hiding a leak; scope clean; governance
correct). Backlog (non-blocking, recorded here + carried to Gate 3b):

- **MEDIUM — evolving-snapshot under-capture.** When >1 `ErQueueItem` shares one `member.id` (an
  entity re-crawled under a new landing record; `uq_er_queue_dedup` is `(source_record, entity_id)`),
  the backfill captures only the **latest** snapshot, whereas the dual-write logged the union across
  observations. It drops data, **never resurrects** (not a forget-safety issue), does not make
  `full_rebuild` raise, and is caught by the 3b divergence gate — this is the concrete instance of
  **SF-1's own revisit trigger** ("unexplained live-⊋-fold survivors ⇒ `er_queue` gap → landing
  re-map"). Mitigated: a **deterministic `ORDER BY (created_at, id)`** on the `er_queue` reads makes
  the winner the latest observation stably across runs (closing a latent idempotence fragility;
  `test_backfill_multi_snapshot_is_idempotent`), and the latest-wins winner matches the live graph's
  own additive last-write-wins state. The union-capture itself is the landing-re-map revisit target
  before 3b.
- **LOW — `assert_backfill_complete` checks only aliased survivors** (not singletons); the whole-graph
  divergence spike (SF-4 second half) catches a dropped singleton and is the operator's 3b check.
- **LOW — `backfill_spine` does not self-assert completeness** (returns counts); completeness is the
  3b gate. A future optional `assert_complete` guard would harden operator use.

## Reversibility (overall)

Reversible at the log level: the spine is append-only and Neo4j is a rebuildable projection, so a wrong
backfill row is deleted (identifiable by the backfill `created_at` window / `seq` range) and re-derived —
never a live-graph deletion. The mechanical build is therefore low-cost to revert **before** the Gate 3b
cutover. The genuinely irreversible-in-effect item is re-introducing an erased subject's claims into the
never-forgetting SoR — which SF-3 forecloses by design (forget-safe substrate + erase-audit exclusion +
post-backfill re-scrub + a rebuild-contains-no-erased-source verification). **Person-affecting → user
cosign before build.** No sub-fork is escalated to a `human_fork` — planning found no data-shape lock-in
(the backfill is additive/reconstructable; the one irreversible consumer is the later, separately
human-gated Gate 3b cutover).

## Explicitly NOT in Gate 2b

- **The Gate 3b cutover** (retire the direct write, make rebuild the routine path) — irreversible,
  human-gated, LAST; the first sanctioned live `full_rebuild` consumes 2b's output.
- **The per-cohort fidelity spike over a real-seed corpus** — operator-blocked; the mechanism ships with
  synthetic/property coverage and the spike is marked blocked-on-real-seed (SF-4).
- **Enricher (E2/INTERNAL_ENRICHMENT) capture** — enricher claims are unreconstructable-forever and must be
  captured at ingest, not backfilled; not wired yet (consult §5, revisit trigger in SF-1).
- **The incremental-projector anchor-retraction bound + superseded-node deletion** — projector-delete-path
  work owned by Gate 3b (ADR 0107 §Explicitly-NOT).
- **`llm_egress` erasure / any new value-bearing store** — out of the backfill substrate (SF-3 revisit
  trigger).

## ADR-index coupling

Filed as `docs/decisions/0113-statement-log-backfill.md`, H1 `# 0113 — …`, with all machine-checkable
fields (`Status`, `Date`, `human_fork`, `person_affecting`) in the first ≤15 lines as **plain, un-bolded**
value tokens (a bolded `**true**` renders `—`; the `human_fork` line carries no opposite literal token, so
it does not flip to `mixed`). `human_cosign` is not index-parsed (enforced by checker/judge). This DRAFT is
authored **PROPOSED** and, per the house pattern, the **code PR after the user cosign** owns the accept flip
(stamp the dated `human_cosign` line, PROPOSED → ACCEPTED, `python scripts/gen_adr_index.py` regen — the
0113 row's status changes) in the SAME PR. **The accept flip must not occur on a PENDING cosign line.**
