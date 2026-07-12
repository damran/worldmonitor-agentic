# 0107 — Right-to-forget reaches the SoR (Gate P2): scrub all three log lanes, keep a defined live-removal mechanism, reconcile granularities

- **Status:** ACCEPTED (2026-07-12; proposed 2026-07-05) — DECIDED lines filled by the P2 planning gate
  (2026-07-11); cosigned + built via the P2 code PR (2026-07-12). This is a planner-staged decision-space
  document for Gate P2, committed by the P1-0 docs-only slice (see §ADR-index coupling); byte-unchanged
  through Gate-P1 (#174) and Gate-P3 (#176).
- **Date:** 2026-07-05
- **human_fork:** false — the sub-forks below are each a reversible default with a revisit trigger, not a
  genuine product/architecture fork. P2 planning (2026-07-11) **found no irreversible data-shape lock-in**;
  the load-bearing SF-3 mechanism choice is a reversible engineering default (a→b has a documented revisit
  trigger). The four cosign-disclosure items in §Decided are behaviour/governance disclosures, **not**
  forks.
- **person_affecting:** true — P2 changes *what an erasure actually erases* (a person's claims across
  the live graph AND the SoR log). Person-affecting by construction (CLAUDE.md: erasure). **human_cosign
  REQUIRED before build** (not a `false`-waiver — a genuine `true` needing sign-off).
- **human_cosign:** Mithat 2026-07-12 — cosigned before the code build (person_affecting:true), the four
  D-i..D-iv disclosures (§Decided) presented; the code build then surfaced, across three rounds of
  adversarial verification (each round found by an independent, runtime-reproducing checker, not
  self-reported by the builder), a CRITICAL data-loss defect + a HIGH GDPR-completeness gap + two MEDIUMs
  in the original implementation, all fixed; the round-1/round-2 fixes each introduced a narrower
  recurrence of the same class (a caption-recompute gating bug, a prop-vs-value-granularity gap, a
  co-witnessed-identical-value gap), each found and fixed in turn. One residual survives, disclosed rather
  than fixed: an over-removal-only (never a leak), narrow (requires an exact literal-string coincidence
  between an erased value and an unlogged legacy value on the same prop), self-healing-at-Gate-2b gap —
  see `resolution/erasure_scrub.py::prune_live_to_fold`'s KNOWN RESIDUAL docstring for the full mechanism
  and revisit trigger. All findings disclosed to the user before the final cosign stamp.
- **Realises:** the **forgetting** blocking-3b prerequisites of the Fable log-capture consult
  (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md` §6, §7-4, §7-5) and pre-cutover gate **P2**
  (`docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md`). **Builds on:** ADR 0095 (:63 — the target
  "value-level GDPR erasure = `DELETE … WHERE` + reproject", which is **not sufficient as stated** for an
  already-written live graph), ADR 0099 (the statement/decision lanes to scrub), ADR 0106 (the P1
  `context_claim` lane — the third scrub surface), ADR 0100/0101 (the fold + P-FOLD-2 no-deletion bound),
  ADR 0102 (the divergence guard whose §7-10 usability depends on granularity reconciliation), ADR 0108
  (the P3 sign-off spine — P2 scrubs the statement/decision/context rows P3 now writes), ADR 0049 (the
  existing cross-store `erase_source` + `graph/ops.py` value-level prune P2 extends). **Sequenced AFTER
  P3** (capture-before-forget: you cannot scrub-from-the-log what sign-off never logged). **Supersedes:**
  nothing.

## Context (the problem P2 closes)

Cutover makes rebuild the routine DR/verification path. Today erasure (`erasure.py` + `graph/ops.py`)
deletes landing objects, redacts `er_queue`/dead-letter rows, and value-level-prunes Neo4j — but the
statement writers "only INSERT; no UPDATE or DELETE" and **no log-scrub code exists**. Two symmetric
failures block cutover:

- **The log cannot forget.** A `full_rebuild` fold **resurrects every erased claim** promoted since the
  Gate-2a dual-write began — holding dangling `raw_pointer`s to deleted landing objects. A GDPR liability.
- **Reprojection cannot enforce removal on the live graph.** The projector only MERGEs + additively
  `SET`s; a log `DELETE` emits no `seq` row, so the incremental fold never revisits the scrubbed survivor;
  even a full fold into the live graph never removes a value/prop/node. **Reprojection enforces
  non-resurrection; it cannot enforce removal.**

## Decision space (each sub-fork: recommended reversible default + reversal cost + revisit trigger)

### SF-1 — Scrub scope = ALL THREE lanes
`statement` (`DELETE … WHERE dataset = <erased source_id>`), `context_claim` (same — P1's per-member
`dataset` attribution is what makes anchor claims reachable by dataset), and `decision` rows referencing
erased members (SF-2). **A scrub scoped to statements alone resurrects erased-source anchor claims on
rebuild.** Not a fork — a completeness requirement.

### SF-2 — Decision-row treatment: **tombstone/redact**, not delete (RECOMMENDED DEFAULT)
Decision rows are *judgements* (the merge/split history), not raw evidence; deleting them loses the
belief-revision trail. **Default:** redact the erased member ids from `decision.member_ids` (and/or write
a superseding tombstone decision via the reserved `supersedes`/`superseded_by` columns) rather than
`DELETE` the row. **Reversal cost:** low (the columns already exist, ADR 0099). **Revisit trigger:** a
legal requirement to delete the judgement record itself (not just redact the member reference) ⇒ switch to
row delete for that class.

### SF-3 — Live-removal mechanism (consult §6 a/b/c): **keep `graph/ops.py`'s direct prune** (RECOMMENDED DEFAULT = a)
Reprojection cannot remove; a mechanism must be picked.
- **(a) DEFAULT — keep the direct prune** as a permanent, explicitly-carved-out second live-writer that
  survives "retire the direct write path" (§7-14). **Reversal cost:** low (the prune already exists; the
  carve-out is a documented exception, not new machinery). **Revisit trigger:** the prune becomes hard to
  keep correct against a MERGE-only projector ⇒ switch to (b).
- **(b) Seq-bearing erasure-event rows** the projector consumes with delete capability — heavier (adds a
  delete path to the fold) but would also give **superseded-node deletion** (§7-8) a home. Kept as the
  documented alternative + revisit target.
- **(c) Wipe-and-full-reproject on erasure** — rejected as the default (a swap-rebuild is the DR mechanic,
  too heavy for a routine erasure; and it presumes the log is already fully faithful, which is exactly
  what P1/P3 are still establishing).

### SF-4 — Granularity reconciliation
The live prune is **prop-granular** (a co-witnessed prop keeps all its values, including erased-source-only
values); the log scrub is **row-granular**. Unreconciled, every real erasure leaves live value-sets
exceeding the fold on compared props → permanent unexplained divergence → the §7-10 guard deadlocks. P2
must align them (or teach the guard to explain erasure deltas). **Reversal cost / revisit trigger:** filled
at P2 planning once the reconciliation shape (prune-to-match vs guard-explains) is chosen — recommend
**prune the live node to the fold's row-granular result** so the two instruments agree, with the guard
change as the fallback.

### SF-5 — Stock, not just flow
The log has accumulated claims since 2026-07-04, including claims from **sources erased after logging**. A
one-off retroactive scrub of every erasure executed during the dual-write window (driven from the
erase-audit records), verified by a rebuild-contains-no-erased-source check. Not a fork — a completeness
requirement.

### SF-6 — The round-trip property asserts BOTH surfaces
`erase → (i) full_rebuild into a fresh target contains nothing of the erased source AND (ii) the live
graph no longer holds the erased values.` A fresh-target-only oracle goes green while the live graph still
holds everything (the adversarial pass caught exactly this in the consult's first draft). Mandatory
`@given` per the build discipline.

### SF-7 — P-FOLD-2 deletion bound
P-FOLD-2 (incremental == full) is proven under a **no-deletion bound**. P2 must **bound or extend** it: a
scrub between batches legitimately breaks naive incremental-vs-full equivalence unless the erased survivor
is re-folded (event-driven). **Recommended default:** make the scrub emit a re-fold trigger for the
touched survivor (event-driven), extending P-FOLD-2 to `incremental == full under interleaved deletions`.

## Decided (2026-07-11, P2 planning gate)

The DECIDED lines below fill the recommended defaults, refined against a **first-hand read of the current
code on master** (Gate P1 #174 + Gate P3 #176 merged, master `b1b061c`): `erasure.py`, `graph/ops.py`
(`erase_source_graph`, `graph/constraints.py`), `resolution/statements.py`, `resolution/projector.py`,
`resolution/divergence.py`, `db/models.py`, `llm/egress_audit.py`, and the P1 DB-level append-only detector
(`tests/integration/test_context_claim_lane.py::test_context_claim_writes_are_append_only_at_db_level`),
then a **3-lens plan-verify (all FIX_FIRST)** whose findings are folded in (the two HIGHs — provenance-clobber
in the live write + the anchor UNIQUE-constraint crash; the anchor-oracle vacuity MEDIUM; the fallback-keyed
residual MEDIUM; the append-only-carve-out-is-a-tautology governance MEDIUM; the cross-store non-atomicity
LOW; the buildability LOWs). **Status stays PROPOSED and `human_cosign` stays PENDING** — this section
commits the filled decision-space (the 0107/0108 committed-draft precedent); the person-affecting **user
cosign is requested by the main loop before the P2 CODE build**, with the plan-verify findings below
disclosed, and the accept-flip (stamp the dated `human_cosign` line, PROPOSED → ACCEPTED, regen the index)
happens at **gate end** per the house pattern (ADR 0106/0108 §human_cosign). **The accept flip must not
occur on a PENDING cosign line** (ADR 0097 §5). The buildable mechanics live in the gate spec
`docs/reviews/GATE_P2_ERASURE_SPEC.md`; this section records the *decisions*.

**No genuine human fork.** P2 planning found no irreversible data-shape lock-in. Four items are disclosed
for the cosign conversation (not forks, but person-affecting/governance disclosures): (D-i) the SF-3
load-bearing mechanism recommendation; (D-ii) P2 extends the append-only **erasure carve-out** to a lane
the current erasure does not touch (`decision` redaction + `statement`/`context_claim` DELETE — see SF-2),
proven confined by a POSITIVE test (SF-2); (D-iii) SF-4 makes erasure **more complete** than today (it
removes erased values the current prune leaves on co-witnessed props and erased anchor values the current
prune never touches — REMOVE-only) — a person-affecting behaviour change in the *correct* direction; (D-iv)
SF-1 requires a **schema migration** (a `dataset` index).

- **SF-1 — DECIDED: three-lane scrub; `statement` + `context_claim` reached by `(dataset = source_id) OR
  (entity_id ∈ erased_member_ids)`; `decision` redacted by member-id (SF-2); ADD a `dataset` index
  (migration 0013).** First-hand: `statement.dataset` and `context_claim.dataset` are both `String(255)`
  **without `index=True`** (`db/models.py:336,557`; ADR-0106 §Verification minor finding (ii)). The scrub's
  primary match is `WHERE dataset = ?`; an unindexed full-table scan per erased source — repeated once per
  source by the SF-5 stock scrub and again by the SF-6 verification queries — is a production-scale finding,
  so P2 adds a b-tree index on `statement.dataset` and `context_claim.dataset` in **migration `0013`** (add
  `index=True` to those two columns; model + migration byte-agree, ADR 0030 drift guard). **Erasure
  correctness does not depend on the index; erasure latency at scale does.** **This is a schema change —
  surfaced, not hidden** (D-iv). **Reversal cost:** low (drop the index; `downgrade()`). **Revisit
  trigger:** if the migration coordination is deemed not worth it for a rare operator-triggered op, accept
  the scan (but the stock scrub's per-source repeated scans make the index the right default).
  - **Fallback-keyed residual closure (plan-verify MEDIUM).** The P1 writer stamps
    `dataset = prov.source_id or member.id or ""` (`statements.py:196`), so an **empty-`source_id` member**
    wrote rows keyed by `member.id` — unreachable by a bare `WHERE dataset = source_id`, AND its `member.id`
    would be absent from an `erased_member_ids` derived from `dataset`-matched rows only, so its `decision`
    reference would also survive. **P2 closes the reachable part:** `scrub_log_lanes` reaches rows by
    **`(dataset = source_id) OR (entity_id ∈ erased_member_ids)`**, and redacts `decision.member_ids` by
    **`(erased_member_ids ∪ the erased rows' `entity_id`s)`** — so any member with *at least one*
    `dataset = source_id` row has *all* its rows (including `member.id`-keyed ones) and its decision refs
    scrubbed. **The irreducible residual** — a member whose rows are *all* `member.id`-keyed (empty
    `source_id` everywhere) — is **fundamentally unreachable-by-source from the log** (nothing links the
    `member.id` back to the erased source), and is a **P1-writer defect**: P2 **NAMES the P1-writer
    non-empty-`source_id` enforcement (ADR 0106 §Verification minor finding (i)) as a P2 dependency** — a
    small skip-and-log / dead-letter guard requiring a non-empty `source_id`, routed to a WPI slice (kept
    OUT of P2's erasure code so `statements.py` stays FROZEN) — so no new pure-fallback rows are created.
    **This closes the D-iii completeness direction for all source-linkable rows** (the residual is
    forward-eliminated by the named dependency, not left silently open). Disclosed.

- **SF-2 — DECIDED: redact `decision.member_ids` IN PLACE (remove the members in
  `erased_member_ids ∪ erased-rows'-`entity_id`s); preserve the row; NO delete, NO superseding tombstone.**
  Confirmed mechanism-safe first-hand:
  - `decision.member_ids` is a **mutable JSONB column** (`db/models.py:384`) with **no `MutableList` /
    `as_mutable`** — so the redaction MUST **REASSIGN a new list** (`row.member_ids = [id for id in
    row.member_ids if id not in erased]`) or call `flag_modified(row, "member_ids")`; **an in-place
    `.remove()` silently will NOT persist** (SQLAlchemy change-detection does not see it). Explicit.
  - **The projector reads `decision` rows ONLY to advance `last_decision_seq`** (`projector.py:346-349,417`
    — `member_ids` is never consumed into node reconstruction). **So redacting `member_ids` cannot corrupt
    node reconstruction** — the exact confirmation the sub-fork required.
  - **The `decision` lane has NO `dataset` column** (`db/models.py:352-396`), so it is **not**
    dataset-scrubbable. It is reached via `member_ids ∩ (erased_member_ids ∪ …)`, where `erased_member_ids`
    is the DISTINCT `entity_id` over the pre-scrub `statement ∪ context_claim` rows reached by the SF-1
    predicate. **This forces a compute-erased-member-set-BEFORE-DELETE ordering** (surprise #2 — the
    skeleton's "`DELETE FROM statement/context WHERE dataset`" line implied the decision lane was
    dataset-scrubbable too; it is not).
  - The row is preserved with `kind`/`score`/`decided_by`/`evidence`/surviving members intact — the
    belief-revision trail survives minus the erased reference. A redaction that empties or drops the row
    below two members leaves a *degenerate* record that the projector ignores (member_ids unconsumed) — no
    correction needed. **`evidence` (`{"reason": …}`) is a guard LABEL, not source evidence — left
    untouched** (revisit if a reason ever carries source PII).
  - **Bounded residual:** a **zero-prop-zero-anchor** erased member contributes no `statement`/`context`
    rows, so it is **not** in the log-derived erased set and its id can persist in `decision.member_ids`.
    That id is an opaque connector-minted identifier whose evidence is already absent; routed to **WPI-1**
    (the §7-3 zero-prop evidence class), not solved in P2. Disclosed.
  - **Governance (D-ii):** P2 **extends the append-only erasure carve-out** to the SoR spine. Today
    `erasure.py` explicitly *preserves* `ResolverJudgement`/`SignOff`/`MergeAudit` and never touched
    `DecisionRecord` (it post-dates Gate 2a). P2 now redacts `decision` and deletes
    `statement`/`context_claim` — the sanctioned erasure exception widened. **This is PROVEN confined, not
    assumed** (plan-verify governance MEDIUM): a **POSITIVE confinement test** asserts (a) the FULL normal
    pipeline (seed → resolve → approve → project) issues **ZERO** DELETE/UPDATE against
    `statement`/`context_claim`/`decision`, AND (b) `scrub_log_lanes` DOES emit exactly those
    DELETE/UPDATEs — "append-only EXCEPT the sanctioned erasure scrub" is demonstrated in both directions
    (spec §4, INV-ERASE-APPENDONLY-CARVEOUT). **Reversal cost:** low. **Revisit trigger:** the SF-2
    legal-delete requirement.

- **SF-3 — DECIDED: (a) keep `graph/ops.py`'s direct prune** as the permanent, explicitly-carved-out
  second live-writer, **extended** to close the value-level and anchor-level live-removal gaps (SF-4).
  **REJECT (b) seq-bearing erasure-event rows for P2.** This is the load-bearing P2 decision (D-i). The
  cross-gate analysis the skeleton flagged but left open, resolved first-hand:
  - **What (a) + SF-4 closes for P2's own erasure mandate:** (A) **non-resurrection on rebuild** — the
    LOG SCRUB (SF-1) removes the erased rows, so a `full_rebuild` reads a smaller log and produces an
    erased-free fresh target **without any projector delete path** (the projector just reads less); (B)
    **live removal of property values, including co-witnessed** — via the SF-4 provenance-preserving
    value-level prune-to-fold; (C) **live removal of anchor values** — via a **targeted `REMOVE`-only step**
    in the direct prune (the ADR-0106 §Verification "targeted REMOVE" option, applied to the LIVE graph;
    REMOVE-only, never SET — SF-4).
  - **What (a) does NOT close** (and correctly leaves to Gate 3b, with a safety net): (i) the
    **P1-DEFERRED incremental-projector anchor-retraction bound** (ADR 0106 §Verification record) — that
    bound is the DORMANT/ISOLATED fold engine failing to retract a bare anchor key under a **cross-batch
    anchor CONFLICT** on `SET n += props`; it is a property of the projector writing its *isolated target*,
    **NOT** an erasure concern, and (a)'s direct prune (which writes the LIVE graph) gives the projector
    **no** delete path — so (a) does **not** close it. `full_rebuild` (the DR/guard path) is correct
    unconditionally; the safety net is Gate 3b's guard-green-over-N-cycles precondition. (ii)
    **Superseded-node deletion** (§7-8) — same story: needs a projector/guard delete step (a) does not
    provide; stays 3b.
  - **Why NOT (b):** (b) would close (A)(B)(C)(i)(ii) at once, but at the cost of **un-freezing the
    projector** (a delete path into the fold — the person-affecting DR path this gate must not destabilise),
    a **new erasure-event table + migration**, and a much larger blast radius — none of which P2 *needs*
    for its own erasure mandate, which (a)+SF-4 fully discharges. **Reversal/blast-radius tradeoff:** (a)
    is additive to an existing, tested prune with zero new tables and a FROZEN projector; (b) is a
    structural fold-engine change. **Recommendation: (a).**
  - **Recorded revisit trigger (a → b):** a real **cross-batch anchor conflict** OR a **superseded-node
    staleness** incident appears on a pre-cutover corpus, OR keeping the direct prune correct against the
    MERGE-only projector becomes hard — then switch to (b) at **Gate 3b** (where the projector is being
    wired anyway, so the delete path lands where it belongs). **The anchor-retraction bound + superseded-node
    deletion travel together to 3b under this default** (the two §7-8 / ADR-0106 owners the skeleton
    linked to §6-b).
  - **This keeps `resolution/projector.py` and `resolution/divergence.py` FROZEN** (SF-4 is
    prune-to-match, not guard-explains); the value/anchor prune lives in `graph/ops.py` + the new scrub
    module and imports `reconstruct_entities` **read-only** as the parity oracle. No projector edit, no
    erasure-event table, **no migration beyond SF-1's `dataset` index.**
  - **Cross-store non-atomicity (plan-verify LOW).** The live prune writes Neo4j immediately while the log
    scrub stages Postgres for the caller's commit (the SAME split `erase_source` already documents:
    landing/graph immediate, DB staged). A commit failure after the Neo4j prune leaves the live graph
    pruned but the log un-scrubbed → a `full_rebuild` would resurrect onto the pruned live graph **until
    the idempotent retry**. **Contract:** erasure is cross-store-non-atomic + **idempotent-retry-recovers**
    (a re-run re-scrubs the log and re-prunes to convergence); stated explicitly in the spec and pinned by
    the IT-ERASE-idempotent post-Neo4j/pre-commit-failure case (resurrection-then-recovery PROVEN).

- **SF-4 — DECIDED: prune the live node to the fold's row-granular result, PROVENANCE-PRESERVING, with
  REMOVE-only anchor handling (REJECT guard-explains).** First-hand, `erase_source_graph`
  (`graph/ops.py:79-183`) is **value-INCOMPLETE** in two ways the skeleton half-anticipated as a
  *divergence* artefact but which are actually **GDPR-completeness gaps** (surprise #1, load-bearing):
  - **Co-witnessed property values survive.** `prov_witnesses` is `{prop: [datasets]}` — **prop-granular,
    no per-value attribution** (`graph/ops.py:106-136`). For a multi-source survivor, the prune keeps a
    prop whenever *any* surviving source co-witnesses it, retaining **erased-source-only VALUES** on that
    prop (e.g. `name = {"Alice" (erased-only), "Alicia" (kept)}` keeps `"Alice"`). The fold over the
    scrubbed log is row-granular and correctly omits it.
  - **Anchor values survive.** Bare anchor keys (`wikidata_id`/`geonames_id`/`lei`/`opencorporates_id`)
    are **not in the witness map**, so `erase_source_graph` **never removes an erased source's anchor value**.
  - **Decision (compared props, 1):** after the log scrub, for each touched survivor, reconstruct the
    row-granular result via the FROZEN pure `reconstruct_entities` over the survivor's *scrubbed*
    `statement ∪ context_claim` history (with `survivor_of`), and prune the live node to match **without
    dropping G1 provenance** (plan-verify HIGH-1): the live write MUST **read the node's CURRENT full prop
    dict, merge the row-granular value-sets into it, and `SET n = $full_props`** (mirroring
    `graph/ops.py:124-156` exactly), OR `SET n += <non-empty sets>` + literal-key `REMOVE` of now-empty
    props. **A bare `SET n = <partial map>` is FORBIDDEN** — it would wipe `prov_source_id`/`prov_witnesses`/
    `datasets`/`id`/`caption`. The value-prune touches only the non-excluded compared props (removes
    erased-only values; makes `measure_divergence` green AND is GDPR-complete); every `prov_*`/`id`/`caption`/
    `datasets` field the co-existing `erase_source_graph` already rebuilt is preserved.
  - **Decision (anchor keys, 2): REMOVE-only (plan-verify HIGH-2 — definitive).** The erasure **only
    `REMOVE`s the erased-source anchor value(s); it NEVER `SET`s a new or changed anchor value.** Reason:
    `graph/constraints.py:24-30` puts `REQUIRE n.<anchor> IS UNIQUE` on every `CANONICAL_ID_FIELDS` prop —
    so a prune that *surfaced* a previously omit-on-conflict surviving value (erase src-A `Q1` on a
    human-approved conflicting-anchor merge → fold yields `Q2` → `SET n.wikidata_id = Q2` while another
    node holds `Q2`) would raise `ConstraintValidationFailed` and **abort the erasure mid-transaction**.
    REMOVE-only is **GDPR-complete** (the erased value is gone) and **guard-neutral** (anchors are
    `_excluded` from divergence). **The earlier "align-to-fold could ADD a surviving value" build-time
    ambiguity is DELETED — REMOVE-only is the decision** (this also resolves the under-pinned anchor
    assertion). Because anchors are guard-excluded, the anchor removal is verified by a **DIRECT live-node
    property read** (SF-6), never by `measure_divergence`.
  - **Guard-explains is REJECTED** for P2: it would leave the erased value/anchor **live** — a GDPR
    failure masked as an explained delta. It is kept only as a divergence-instrument fallback if a
    value-level live prune ever proves infeasible (it is not — the fold gives the exact target).
    **`reconstruct_entities` is the single source of truth — do NOT hand-roll a second value-diff.**
  - **Behaviour disclosure (D-iii):** P2 makes erasure **strictly more complete** than today (removes
    co-witnessed erased values + erased anchor values). This is a person-affecting behaviour change in the
    *correct* direction, and it discharges the ADR-0095 :63 "value-level GDPR erasure" promise the build
    had not reached. **Reversal cost:** low (revert the value/anchor prune, back to prop-granular).
    **Revisit trigger:** the SF-3 (a → b) switch (then the fold's delete path subsumes this).

- **SF-5 — DECIDED: one-off stock scrub driven from `TaskRun(kind="erase").stats["source_id"]`, read
  Python-side.** First-hand: `erase_source` audits every run as one `TaskRun(kind="erase", status="ok")`
  whose `stats` JSONB is `ErasureResult.as_dict()` carrying `source_id` + `authorized_by` + the seven
  counts (`erasure.py:168-190`). **That is the source of truth for "every erasure since the dual-write
  window":** load the `TaskRun(kind="erase", status="ok")` rows via the ORM and read `.stats["source_id"]`
  **in Python** (plan-verify LOW — NOT a Postgres-only `stats->>'source_id'` query, which would break the
  Docker-free / SQLite unit lane; `scrub_stock` is otherwise integration-exercised). The stock driver runs
  the SF-1 three-lane scrub + the SF-4 live value/anchor prune **once per distinct `source_id`**,
  idempotently. Erasures **before** 2026-07-04 never entered the spine (nothing to scrub); erasures
  **after** ran `erase_source` (live/queue/landing) but not the then-nonexistent log-scrub, so the log
  still holds them — the stock scrub fixes exactly that window, and its SF-4 live prune also
  **retroactively closes the co-witnessed-value + anchor gaps** on the already-pruned live graph.
  **Verification:** a `full_rebuild` after the stock scrub contains **no** row/value with
  `dataset ∈` the erased set (SF-6). **Note:** `erase_source` has **no** live API/runner/MCP caller today
  (operator/test-invoked only, `authorized_by` required, ADR 0049); the scrub is exercised via the erasure
  entry points directly, like the existing erasure suite.

- **SF-6 — DECIDED: `P-ERASE-1` asserts BOTH surfaces, RED-first, with TWO vacuity fences (fresh-target +
  anchor-oracle).** New `tests/property/test_prop_erasure_scrub.py`. **Statement:** for an erased
  `source_id`, after the scrub + a `project(full_rebuild=True)` into a FRESH isolated target: **(i)** the
  fresh rebuild contains **nothing** of the erased source (no `statement`/`context` row reached by the SF-1
  predicate; no reconstructed node/edge value equal to an erased-source-**only** value; no
  `decision.member_ids` referencing an erased member) **AND (ii)** the LIVE graph no longer holds the erased
  values (sole-source nodes DETACH-DELETEd; co-witnessed erased-only values removed; erased anchor values
  removed).
  - **Fresh-target-only-oracle fence (mandatory):** the property MUST assert BOTH (i) and (ii) — a
    fresh-target-only oracle is REJECTED as vacuous (goes green while the live graph still holds everything).
  - **Anchor-oracle vacuity fence (plan-verify MEDIUM, mandatory):** clause (ii)'s **erased-anchor removal
    MUST be asserted by a DIRECT live-node property read** (query `n.wikidata_id` / `n.geonames_id` / … on
    the live node) — **`measure_divergence` is REJECTED as the anchor oracle** because it `_excludes`
    `CANONICAL_ID_FIELDS` (`divergence.py:96-102`) and would go **green with a residual erased anchor still
    on the node**. (Compared-prop removal MAY use `measure_divergence.total`; anchors MUST NOT.)
  - **Generator MUST include:** a sole-source node, a multi-source survivor with a **co-witnessed prop
    holding an erased-source-only value** (the SF-4 hard case), an **erased-source anchor** on a surviving
    node, and a `decision` row referencing an erased member. **RED-first shape:** against master the log is
    not scrubbed → (i) resurrects on rebuild → RED; the co-witnessed erased value + erased anchor survive
    live → (ii) → RED. Container-backed examples wrap per-example engines in `try/finally` (the known leak
    trap). Integration: `IT-ERASE-*` (`tests/integration/test_erasure_scrub.py`, real Postgres + Neo4j) +
    extend `tests/integration/test_erasure.py` (the scrub wired into `erase_source`).

- **SF-7 — DECIDED: re-bound P-FOLD-2 (keep it FROZEN in its no-deletion regime) + prove `full_rebuild`
  correctness (`P-ERASE-2`) + specify the deferred incremental re-fold trigger.** First-hand: the
  projector is **DORMANT/ISOLATED** (never wired live) and the production divergence guard uses
  `project(full_rebuild=True)` (`runner/driver.py`, `metrics/collector.py`) — so under SF-3(a) the ONLY
  fold that matters for erasure is `full_rebuild`, which is **correct over the scrubbed log** (it reads
  less). The mechanics:
  - **`P-FOLD-2` stays byte-FROZEN** (`tests/property/test_prop_fold_engine.py`) — proven in its
    no-deletion regime; P2 **adds a sibling** rather than weakening it. **`P-ERASE-2`** (in the erasure
    property file) asserts: `full_rebuild` over the scrubbed log is erased-free unconditionally (the
    DR/guard path).
  - **The incremental bound, and the SF-3(a) trigger mechanism (specified even though deferred):** a
    `DELETE` emits **no `seq` row**, so an incremental `project(full_rebuild=False)` reading
    `WHERE seq > watermark` **never revisits the scrubbed survivor** — its isolated-target values go stale,
    breaking naive incremental==full. Under SF-3(a) the projector stays FROZEN and its target is ephemeral,
    so this staleness is **bounded to the dormant fold engine and resolved by a `full_rebuild`** (the
    DR/guard path). The re-fold TRIGGER a *wired* incremental projector would need is a **non-`seq`
    touched-survivor marker** the scrub records (a `refold_pending` obligation the projector's existing
    touched-survivor full-history re-read — ADR 0101 A1 — would then consume). **P2 records this trigger
    contract but builds NO consumer** (the incremental projector is unwired); its natural producer is
    exactly SF-3 option (b)'s seq-bearing erasure event — **the documented (a → b) revisit path.** So P2
    does NOT "extend P-FOLD-2 with machinery" (the skeleton's default); it re-bounds it honestly and hands
    the event-driven consumer to the projector-wiring gate (3b). **Reversal cost:** none (documentation + a
    sibling property). **Revisit trigger:** the projector is wired incrementally against the live graph
    (3b) → build the `refold_pending` consumer (or take SF-3(b)).

- **F2 rider — DECIDED: `llm_egress` is NOT a fourth P2 scrub lane; the `entity_manifest × erasure`
  design is a NAMED PRECONDITION on the F2 durable-audit enablement gate (deferred-with-owner).**
  First-hand: `LlmEgressRecord.entity_manifest` is a caller-declared JSONB list of canonical-ids
  (`db/models.py:491`, `llm/egress_audit.py:84-99`) that **can carry person canonical-ids into a
  never-deleted append-only table**; but (1) the durable audit is **DORMANT**
  (`llm_egress_durable_enabled=False`) so the table holds **no production data**; (2) the scrub AXIS
  differs — the three log lanes scrub by `dataset` (source), while `llm_egress` has **no `dataset`
  column** and would be erased by *canonical-id membership* in `entity_manifest`, a different resolution
  (source → canonical-id) the three-lane scrub does not perform; (3) ADR 0105 (F2) already books this as a
  pre-enablement blocker (its B-1). **Decision:** P2 stays scoped to the three SoR log lanes it owns;
  `llm_egress` erasure is a **hard precondition on the gate that flips `llm_egress_durable_enabled=True`**
  — that gate MUST NOT ship until the manifest-erasure design lands. Recorded here so the F2 gate cannot
  enable durable egress audit without solving manifest erasure. **Reversal cost:** none (defer).
  **Revisit trigger:** any move to enable the durable LLM-egress audit ⇒ `llm_egress` becomes a fourth
  lane on the same erasure spine (it is a lane *when* it holds data — it does not today).

### Surprises (code facts the skeleton did not anticipate — disclose at cosign)

1. **`erase_source_graph` is value-INCOMPLETE on the live graph** (co-witnessed erased-only property
   values survive because the witness map is prop-granular, no per-value attribution, `graph/ops.py:106-136`;
   erased anchor values are never touched because anchors are not in the witness map). The skeleton framed
   the granularity gap as a *divergence* artefact; it is also a **GDPR-completeness gap**. SF-4 closes both.
   Load-bearing.
2. **The `decision` lane has NO `dataset` column** → reached only via `member_ids ∩ (erased_member_ids ∪ …)`,
   forcing **compute-erased-member-set-BEFORE-DELETE** ordering. The skeleton's
   "`DELETE FROM statement/context WHERE dataset`" implied the decision lane was dataset-scrubbable.
3. **The projector reads `decision` rows ONLY for the watermark** (never `member_ids`) → SF-2 redact-in-place
   cannot corrupt reconstruction (the required confirmation).
4. **`statement.dataset` + `context_claim.dataset` are UNINDEXED** → SF-1 requires migration `0013` (D-iv).
5. **The projector is dormant/isolated and the guard uses `full_rebuild`** → SF-7's incremental-after-delete
   staleness only affects the dormant fold engine; `full_rebuild` over the scrubbed log is correct; the
   event-driven trigger has no live consumer yet (deferred, contract recorded).
6. **`erase_source` has no live API/runner/MCP caller** → SF-5's stock source of truth is
   `TaskRun(kind="erase").stats["source_id"]` (read Python-side); the scrub is exercised via the erasure
   entry points.
7. **P2 extends the append-only erasure carve-out to the SoR spine** — the current `erasure.py` preserves
   `ResolverJudgement`/`SignOff`/`MergeAudit` and never touched `DecisionRecord`. Confinement is **PROVEN,
   not assumed** (a POSITIVE test: the normal pipeline emits ZERO DELETE/UPDATE against the three lanes AND
   `scrub_log_lanes` DOES emit exactly those — SF-2). The **P1 DB-level append-only detector stays green**.
8. **The bare anchor keys carry a Neo4j UNIQUE constraint** (`graph/constraints.py:24-30`) → the SF-4 anchor
   prune is **REMOVE-only** (never SET) so it can never crash the erasure on a surfaced conflict-resolved
   value; the "align-to-fold ADD" option is deleted. REMOVE-only is GDPR-complete + guard-neutral.
9. **The value-prune live write MUST read-current-props-then-merge** (`SET n = $full_props`, never a bare
   `SET n = <partial map>`) so it never drops `prov_*`/`id`/`caption`/`datasets` (plan-verify HIGH-1).

## Reversibility (overall)

**Reversible in mechanism, irreversible in effect** — an erasure is meant to be permanent, but every
*design* sub-fork above is a reversible default (the direct-prune carve-out, the tombstone-vs-delete
choice, the reconciliation shape). Erasure is **cross-store-non-atomic + idempotent-retry-recovers** (SF-3),
the same contract `erase_source` already documents. **Person-affecting → user cosign before build.** No
sub-fork is escalated as a human fork unless P2 planning discovers a data-shape lock-in — **and P2 planning
(2026-07-11) found none** (SF-1's index + SF-3(a)'s no-new-table are both additive/reversible).

## Explicitly NOT in P2

- Sign-off spine routing (Gate P3 — a prerequisite: P2 layers on P3's logged sign-off nodes).
- E2 capture (Gate P1 — a prerequisite: P2's `context_claim` scrub needs the lane to exist).
- **The incremental-projector anchor-retraction bound + superseded-node deletion** (ADR 0106 §Verification /
  §7-8) — need a projector delete path; travel together to **Gate 3b** under the SF-3(a) default (the
  full_rebuild-correct + guard-green-over-N-cycles safety net covers the interim).
- **`llm_egress` erasure** — a NAMED precondition on the F2 durable-audit **enablement** gate (F2 rider).
- **The zero-prop-member decision residual** — WPI-1 (§7-3 zero-prop evidence class).
- **The P1-writer non-empty-`source_id` enforcement** — a NAMED P2 dependency (SF-1) routed to a WPI slice,
  kept OUT of P2's erasure code so `statements.py` stays FROZEN.
- Cutover mechanics (Gate 3b-planning-proper).

## ADR-index coupling

This DRAFT is **committed as PROPOSED by the P1-0 docs-only slice** (with the index regen — its row reads
`PROPOSED | 2026-07-05 | false | true`), because the index generator scans the filesystem and untracked
drafts break `--check` (adversarial-verify finding). It stays **byte-unchanged through the P1 (#174) and
P3 (#176) code PRs**; this P2 **planning** gate fills the DECIDED lines above and ships them as a
**docs-only PR** (autonomously mergeable, P1-0 / 0108-fill precedent — committing the filled PROPOSED
decision-space; the 0107 index row is unchanged, still `PROPOSED`, so **no README regen in the planning
PR**). The P2 **code** PR (after the user cosign) owns the accept flip: stamp the `human_cosign` dated
line, PROPOSED → ACCEPTED, regen (the 0107 row's status changes). The `person_affecting: true` +
`human_cosign` are the machine-checkable substrate ADR 0097 §5 enforces. **The accept flip must not occur
on a PENDING cosign line.**
