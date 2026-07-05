# 0107 — Right-to-forget reaches the SoR (Gate P2): scrub all three log lanes, keep a defined live-removal mechanism, reconcile granularities

- **Status:** PROPOSED (2026-07-05) — **DRAFT skeleton, awaiting user cosign before build.** This is a
  planner-staged decision-space document for Gate P2, committed by the P1-0 docs-only slice (see
  §ADR-index coupling); byte-unchanged through the Gate-P1 code PR. P2's own planning gate fills the
  DECIDED lines and obtains the cosign.
- **Date:** 2026-07-05
- **human_fork:** false — the sub-forks below are each a reversible default with a revisit trigger, not a
  genuine product/architecture fork. (If P2 planning finds an irreversible data-shape lock-in, that
  specific sub-fork is escalated to the user before build.)
- **person_affecting:** **true** — P2 changes *what an erasure actually erases* (a person's claims across
  the live graph AND the SoR log). Person-affecting by construction (CLAUDE.md: erasure). **human_cosign
  REQUIRED before build** (not a `false`-waiver — a genuine `true` needing sign-off).
- **human_cosign:** PENDING — Gate P2 user cosign REQUIRED before build (person_affecting:true).
- **Realises:** the **forgetting** blocking-3b prerequisites of the Fable log-capture consult
  (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md` §6, §7-4, §7-5) and pre-cutover gate **P2**
  (`docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md`). **Builds on:** ADR 0095 (:63 — the target
  "value-level GDPR erasure = `DELETE … WHERE` + reproject", which is **not sufficient as stated** for an
  already-written live graph), ADR 0099 (the statement/decision lanes to scrub), ADR 0106 (the P1
  `context_claim` lane — the third scrub surface), ADR 0100/0101 (the fold + P-FOLD-2 no-deletion bound),
  ADR 0102 (the divergence guard whose §7-10 usability depends on granularity reconciliation), the
  existing erasure path (`graph/ops.py` value-level prune). **Sequenced AFTER P3** (capture-before-forget:
  you cannot scrub-from-the-log what sign-off never logged). **Supersedes:** nothing.

## Context (the problem P2 closes)

Cutover makes rebuild the routine DR/verification path. Today erasure (`graph/ops.py` + the erasure flow)
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

## Reversibility (overall)

**Reversible in mechanism, irreversible in effect** — an erasure is meant to be permanent, but every
*design* sub-fork above is a reversible default (the direct-prune carve-out, the tombstone-vs-delete
choice, the reconciliation shape). **Person-affecting → user cosign before build.** No sub-fork is
escalated as a human fork unless P2 planning discovers a data-shape lock-in.

## Explicitly NOT in P2

- Sign-off spine routing (Gate P3 — a prerequisite: P2 layers on P3's logged sign-off nodes).
- E2 capture (Gate P1 — a prerequisite: P2's `context_claim` scrub needs the lane to exist).
- Cutover mechanics (Gate 3b-planning-proper).

## ADR-index coupling

This DRAFT is **committed as PROPOSED by the P1-0 docs-only slice** (with the index regen — its row reads
`PROPOSED | 2026-07-05 | false | true`), because the index generator scans the filesystem and untracked
drafts break `--check` (adversarial-verify finding). It stays **byte-unchanged through the P1 code PR**;
P2's own planning gate fills the DECIDED lines, obtains the cosign, and owns the accept flip. The
`person_affecting: true` + `human_cosign` are the machine-checkable substrate ADR 0097 §5 enforces.
