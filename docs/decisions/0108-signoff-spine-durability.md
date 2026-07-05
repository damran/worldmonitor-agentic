# 0108 — Human-decision lane durability (Gate P3): route sign-off through the SoR spine

- **Status:** PROPOSED (2026-07-05) — **DRAFT skeleton, awaiting user cosign before build.** Planner-staged
  decision-space document for Gate P3, committed by the P1-0 docs-only slice (see §ADR-index coupling);
  byte-unchanged through the Gate-P1 code PR. P3's own planning gate fills the DECIDED lines and obtains
  the cosign.
- **Date:** 2026-07-05
- **human_fork:** false — the co-commit-vs-re-route sub-fork is a reversible engineering choice with a
  recommended default, not a product/architecture fork.
- **person_affecting:** **true** — P3 alters the mechanics of the **human sign-off path** (the
  `MERGE_GUARD_MODE=block` lane that fires on exactly the sensitive, person-affecting merges the guard
  parks). Person-affecting by construction. **human_cosign REQUIRED before build.**
- **human_cosign:** PENDING — Gate P3 user cosign REQUIRED before build (person_affecting:true).
- **Realises:** the **sign-off** blocking-3b prerequisite of the Fable log-capture consult
  (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md` §6b, §7-2 — the discovered **CRITICAL**) and pre-cutover
  gate **P3** (`docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md`). **Builds on:** ADR 0099 (the spine
  writers + the reserved human `decided_by=<operator>` decision path), ADR 0106 (the P1 context capture
  already wired at `approve()`), ADR 0044 (the `canonical_id_ledger` / `record_durable_id` the survivor
  must be resolvable through), ADR 0031/0036 (the sign-off flow + its B-1 idempotent cross-store
  recovery). **Sequenced AFTER P1, BEFORE P2** (capture-before-forget). **Supersedes:** nothing.

## Context (the CRITICAL P3 closes)

`signoff.approve()` writes the canonical entity + edges to the **live graph** (`signoff.py:290`);
`reject()` writes member nodes + edges (`:342`). The module imports **none** of `record_statements` /
`record_decision` / `record_durable_id` — it writes `SignOff` + `ResolverJudgement` audit rows, but
**nothing the fold reads**. Consequences (consult §6b):

- A full rebuild **silently drops every human-approved merge and every reject-written member node** —
  evidence *and* a human judgement, the most protected content in the graph.
- The sign-off survivor is invisible to `survivor_of` (no ledger alias row), so even the parts that *are*
  logged would not resolve to it.
- The divergence guard reports these nodes as unexplained forever, deadlocking §7-10 on any corpus where a
  park+approve occurred.
- The alias⇔co-commit invariant (§7-6 / WPI-2) does **not** catch this (sign-off writes no alias) — the
  two are independent and both required.
- P1's E2 capture hooked only at the *pipeline* promote point would structurally miss sign-off-promoted
  entities — which is exactly why P1 already wires the **context** capture at `approve()`. P3 adds the
  statement/decision/ledger writes beside it.

## Decision space (each sub-fork: recommended default + reversibility)

### SF-1 — co-commit at `approve()` **vs** re-route through the pipeline promote point (RECOMMENDED = co-commit)
- **Co-commit (DEFAULT):** `approve()` gains `record_statements` (for the fused canonical), `record_decision`
  (`kind="merge"`, `decided_by=<operator>`), and `record_durable_id` (the ledger self-row + collapsed-member
  aliases), added in the SAME transaction as the existing `SignOff`/judgement rows. `reject()` gets the
  member-write equivalent (statements for each member kept as its own entity; no merge decision; the
  member ids are already their own survivors so no alias). **Reversal cost:** low (additive `session.add`
  calls; remove them to revert). **Revisit trigger:** the two promote paths drift enough that one fusion
  is worth sharing.
- **Re-route (alternative):** funnel `approve()` through the pipeline promote block. **Rejected as
  default:** the pipeline point assumes a Splink-clustered `ResolvedCluster` with `by_id` that sign-off
  does not have (it has `member_rows` + a `_merge_members` FtM merge); re-routing would force a synthetic
  cluster and widen the blast radius into the auto-merge path. Kept as the documented alternative.

### SF-2 — the graph-write-before-commit ordering (CONSTRAINT, not a fork)
`approve()` writes the graph **before** the Postgres commit (opposite of the pipeline's write-after
ordering). The spine writes MUST land in the **same transaction** as the existing `SignOff`/judgement rows
so a crash rolls them **all** back together, and the existing B-1 idempotent re-run (graph MERGE +
ON-CONFLICT-DO-NOTHING judgements + audit mutate) must stay convergent. P3 adds the spine `session.add`s
between `write_entities` and `session.commit()` — never a second commit. `reject()`'s member-write
ordering is treated the same way.

### SF-3 — `decided_by=<operator>` (the reserved human-decision path)
The decision row records the human decider identity (the `approver` string passed to `approve()`), filling
ADR 0099's reserved `decided_by=<operator>` slot — distinct from the auto path's `"auto:resolver"`. This is
what makes a rebuild attribute the merge to the human who approved it. Not a fork.

### SF-4 — the missing `record_durable_id` (survivor resolvability)
Without a ledger write, a sign-off merge is invisible to the transitive `survivor_of` the fold uses (D2
global-fold-is-truth), so the folded survivor would not absorb the collapsed member ids. `approve()` must
`record_durable_id(session, canonical_id, member_ids=source_ids, prior_id=...)` so the ledger carries the
self-row + collapsed-member aliases — the same append-only, idempotent write the pipeline promote block
does. Not a fork; a completeness requirement.

### SF-5 — mandatory `@given` coverage (build discipline)
Person-affecting sign-off path → a `@given` property suite is mandatory: (i) an approved merge round-trips
through a fold to a byte-identical survivor (statements + anchors + decision `decided_by=<operator>` +
ledger aliases present); (ii) a rejected set round-trips to the member nodes; (iii) the co-commit is atomic
(a forced failure rolls back the graph + all spine rows together); (iv) idempotent re-run convergence
preserved. Container-backed examples wrap per-example engines in `try/finally` (the known leak trap).

## Reversibility (overall)

**Reversible** (additive `session.add`s inside the existing sign-off transaction; the graph write and the
approve/reject decision are unchanged). **Person-affecting → user cosign before build.** Reversal cost:
remove the spine writes from `approve()`/`reject()`. **Revisit trigger:** SF-1 re-route becomes worth it if
the fusion paths converge.

## Explicitly NOT in P3

- Erasure / forgetting (Gate P2 — sequenced after P3; P2 layers on P3's logged sign-off nodes).
- The alias⇔co-commit *invariant assertion* (WPI-2 — independent of P3; sign-off writes no alias today, so
  both are needed; §7-6).
- E2 anchor capture at `approve()` (already landed in Gate P1 / ADR 0106).

## ADR-index coupling

This DRAFT is **committed as PROPOSED by the P1-0 docs-only slice** (with the index regen — its row reads
`PROPOSED | 2026-07-05 | false | true`), because the index generator scans the filesystem and untracked
drafts break `--check` (adversarial-verify finding). It stays **byte-unchanged through the P1 code PR**;
P3's own planning gate fills the DECIDED lines, obtains the cosign, and owns the accept flip.
`person_affecting: true` + `human_cosign` are the machine-checkable substrate ADR 0097 §5 enforces.
