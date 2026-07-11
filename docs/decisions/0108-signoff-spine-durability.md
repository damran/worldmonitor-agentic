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

`signoff.approve()` writes the canonical entity + edges to the **live graph** (`signoff.py:291`);
`reject()` writes member nodes + edges (`:347`). The module imports **none** of `record_statements` /
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

> **Scope of the fix (see §Decided → Guard-green scope):** P3 logs **future** `approve()`/`reject()` calls,
> so the §7-10 unblock above holds for **POST-P3 (statement-bearing)** sign-off survivors and their
> already-promoted outbound edges. Sign-off nodes written **before** P3 ships still carry zero statement rows
> and remain unexplained until **Gate 2b** backfill (`81_PRECUTOVER_GATE_SEQUENCE.md` §"owner map" item 9) —
> a cosign-visible honesty item, disclosed below.

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

## Decided (2026-07-11, P3 planning gate)

The DECIDED lines below fill the recommended defaults, refined against a first-hand read of the current
code on master (Gate P1 merged, #174) and a **3-lens plan-verify (all FIX_FIRST)** whose findings are folded
in (the `.total`/edge HIGH, the pre-P3 governance HIGH, the fail-closed MEDIUM, the P-SIGN-3 RED-first
MEDIUM, and the LOWs). **Status stays PROPOSED and `human_cosign` stays PENDING** — this section commits the
filled decision-space (the 0107/0108 committed-draft precedent); the person-affecting **user cosign is
requested by the main loop before the P3 CODE build**, with the plan-verify findings disclosed, and the
accept-flip (stamp the dated `human_cosign` line, PROPOSED → ACCEPTED, regen the index) happens at **gate
end** per the house pattern (ADR 0106 §human_cosign / §ADR-index coupling). The buildable mechanics live in
the gate spec `docs/reviews/GATE_P3_SIGNOFF_SPINE_SPEC.md`; this section records the *decisions*.

- **SF-1 — DECIDED: co-commit at `approve()` / `reject()`** (reject the re-route). The re-route's concrete
  blast radius: the pipeline promote block operates on a Splink-clustered `ResolvedCluster` with a `by_id`
  map, runs `resolve_durable_id` + `rekey_cluster` + `needs_review` + the approved-group-exemption fence +
  `build_referent_map` (`pipeline.py:388-530`); sign-off holds only `member_rows` + a `_merge_members` FtM
  merge and has **already** passed human review, so re-routing would force a synthetic cluster **and re-run
  the catastrophic-merge guard on an already-human-approved merge** — widening the blast radius into the
  auto-merge/guard path this gate must not touch. Co-commit adds the spine `session.add`-family calls beside
  the P1 context capture instead (see **SF-EDGE** for why the block does NOT capture the edges). **Reversible**
  (remove the calls). **Revisit trigger:** the two fusion paths converge enough that one shared promote is
  worth it.
- **SF-2 — CONSTRAINT, PROVEN.** All P3 spine writes land in the **single existing sign-off transaction**,
  in the graph-write-before-commit window: in `approve()` **between `record_context_claims` (`signoff.py:301`)
  and the sole `session.commit()` (`:302`)**, after `write_entities` (`:291`); in `reject()` **between the
  `sign_off` row add (`:353`) and the sole `session.commit()` (`:354`)**, after `write_entities` (`:347`).
  Because each of `approve()`/`reject()` has exactly **one** `session.commit()`, a crash anywhere in that
  window leaves the SQLAlchemy session uncommitted → **every** new Postgres row (judgements, audit flip,
  `sign_off`, P1 context claims, and the P3 statement/decision/ledger rows) rolls back **together**; the
  Neo4j `write_entities` already happened but is an idempotent `MERGE` on the deterministic canonical id
  (ADR 0036 Part 1; `signoff.py:12-22,289-291`), so the B-1 re-run re-executes the spine writes **from a
  clean rolled-back state** and converges. This **subsumes the Gate-P1 judge's informational note** that
  both capture calls sit in the same graph-write-before-commit window: P1's `record_context_claims` and all
  P3 spine writes share that one window and that one commit. **No second commit is ever added** (pinned by a
  `session.commit` call-count spy == 1, INV-SIGN-ATOMIC).
- **SF-2 — Behavioral delta for the cosigner (plan-verify MEDIUM, deliberate fail-closed).** P3 adds spine
  writes **inside** the single sign-off transaction before the commit — including `record_durable_id`, which
  `session.flush()`es its ledger rows (`canonical.py:254,272`), plus `record_statements` / `record_decision`.
  So **any** spine-write failure (a ledger flush error, a `decided_by` `String(255)` overflow) now rolls back
  the **whole** `approve()`/`reject()`: a human decision that pre-P3 would have committed to the graph +
  audit can now **fail closed and require a retry** (idempotent under B-1 — a re-run converges). This is a
  **deliberate safety-direction shift** (the human decision is now atomic with its SoR record — no
  graph-written-but-unlogged half-state), NOT a silent change. Disclosed for the cosign.
- **SF-3 — DECIDED: `decided_by = f"operator:{approver}"`** — the namespace-prefixed shape that mirrors the
  auto path's `"auto:resolver"` (`statements.py`), filling ADR 0099's reserved `decided_by=<operator>` slot
  so a rebuild can distinguish an auto-merge from a human-approved merge **by namespace** and carry the
  operator identity. Mechanism: `record_decision` gains an **additive** `decided_by: str = "auto:resolver"`
  keyword (default keeps the pipeline call byte-behaviour-identical); `approve()` passes
  `decided_by=f"operator:{approver}"`. Written **only** on `is_merge` (the existing `record_decision`
  singleton guard); `reject()` writes **no** decision row. **Reversible** (a string format + one defaulted
  param). **Revisit trigger:** a structured decider identity (Zitadel subject) is wanted →
  `operator:<subject>`, same shape. *(Bound to disclose at cosign: `decision.decided_by` is `String(255)`;
  `"operator:"` (9 chars) + `approver` must fit ⇒ a 246-char approver is the exact fit and a **247-char
  approver overflows** — realistic operator identities are far shorter. Pinned by a boundary test at 246
  (persists) with the 247 overflow documented; not a blocker.)*
- **SF-4 — DECIDED: `record_durable_id(session, canonical_id, member_ids=<merged member ids>, prior_id=None)`
  in `approve()`, `is_merge` only.** Writes the ledger **self-row** for the survivor + one **alias row per
  collapsed member id** (`canonical.py:327-360`) so `survivor_of` resolves the collapsed members onto the
  survivor (D2 global-fold-is-truth) — this is what makes both the survivor **node** resolvable AND the
  **outbound-edge endpoints** rewrite member-id → canonical at fold time (SF-EDGE). The parked
  `audit.canonical_id` **IS** the durable id already: an **anchor id** (`wm-anchor-…`) when the members
  anchor, **else the `wmc-` fingerprint** — which is the durable *fallback* itself, because
  `resolve_durable_id` returns `fallback_id` when there is no usable anchor and `rekey_cluster` is a no-op in
  that case (`canonical.py:320-324`, `merge.py:269`). Either way `record_durable_id` writes a valid self-row
  (a **`wmc-` self-row is VALID under INV-SIGN-LEDGER** — checker note) and `prior_id=None` (the prior `wmc-`
  fingerprint, when the durable id is an anchor id, was never materialised in the graph — a parked cluster is
  never written — so no alias to it is needed). Idempotent/append-only (`record_canonical`/`record_alias`
  skip-if-exists, `canonical.py:243,263`). **`reject()` needs NO ledger write** — each member is written
  under its **own** id and is already its own survivor. **Edge entities need NO ledger write** either (each
  keeps its own id; its endpoints resolve via the *canonical's* aliases at approve, or the members'
  self-resolution at reject).
- **SF-EDGE — DECIDED (plan-verify HIGH-1): the guard fires on `.total` (nodes + edges); outbound edges
  reconstruct via the ledger alias, NOT via sign-off-block edge capture.** `approve()` writes
  `[canonical, *edges]` and `reject()` writes `[*members, *edges]` (`signoff.py:291,347`), where `edges` come
  from `_outbound_edges` (`:185-210`) — separate ER-queue entities with their own ids, provenance, and
  independent pipeline-promotion lifecycle. The production divergence guard fires on
  `ProjectionDivergence.total = unexplained_nodes + unexplained_edges` over the FULL snapshot
  (`divergence.py:67-69`, `metrics/collector.py:184`, `runner/driver.py:426`) — so a dropped edge keeps the
  guard red. **Decision: P3 captures NO edge statement rows in the sign-off block.** An outbound edge
  reconstructs from **its OWN pipeline-promotion statement rows** (an edge is a queue entity promoted like
  any other); P3's `record_durable_id` ledger alias (SF-4, `approve()`) is what makes the fold's
  `survivor_of` rewrite the edge's entity-typed endpoint (owner member-id → canonical) to match the live
  `rewrite_referents` (`signoff.py:287-288`). At `reject()` no alias is needed (members are their own
  survivors; edges are unrewritten, endpoints = member ids). **Honest scope (HIGH-1(3)):** guard-green holds
  for a sign-off survivor whose outbound edges are **themselves statement-bearing** (already promoted); an
  edge still `pending` at approve()/reject() time is **transiently unexplained until its OWN pipeline
  promotion** — a pre-existing ingestion-ordering property shared with the pipeline promote path, **NOT
  introduced by P3**. Pinned by ≥1 edge-bearing P-SIGN example (SF-5). **Reversible.**
- **SF-5 — DECIDED: the mandatory `@given` suite** measures the guard's real signal — **`measure_divergence
  (...).total == 0`** (unexplained_nodes AND unexplained_edges), never `unexplained_nodes` alone:
  - **P-SIGN-1** (approved-merge fold round-trip) — after `approve()` + `full_rebuild`, `.total == 0` for the
    sign-off survivor **and** its (statement-bearing) outbound edges; the fold node's bare anchors equal the
    direct node's (subject to clause (ii) below); exactly one `decision` row with `kind="merge"`,
    `decided_by="operator:<approver>"`, `set(member_ids) == set(source_ids)`; the ledger self-row +
    member aliases resolve members→survivor. Generator MUST include an **anchored**, an **unanchored (`wmc-`
    self-row)**, and an **edge-bearing** (an `Ownership` whose `owner` is a reviewed member and which is itself
    independently promoted/statement-bearing — reuse `test_signoff.py`'s `_ownership`) example. **(Build-time
    correction, 2026-07-11): there is NO parked-singleton case.** `guard/sensitivity.py::needs_review`
    short-circuits `if not cluster.is_merge: return False` ("Singletons are never flagged — nothing is being
    merged"), and every other park axis (size, anchor-conflict, Chow band on a cluster score) also requires a
    merge — so **every parked cluster `approve()`/`reject()` ever sees is a 2+-member merge.** Planner
    Surprise #4 mis-read this. `approve()`'s `is_merge=False` branch is therefore **defensive dead code**
    (harmless, kept), and the parked-singleton P-SIGN-1 arm + the standalone parked-singleton IT test are
    **removed** as asserting an unreachable state (not weakened — the state does not exist).
  - **P-SIGN-2** (reject round-trip) — each member AND each (statement-bearing) outbound edge folds to its
    own node/edge (`.total == 0`), no merge decision, no alias; edge-bearing example required.
  - **P-SIGN-3** (co-commit atomicity, RED-first with the positive control folded in) — the STATEMENT asserts
    the spine rows are **PRESENT on a successful commit AND absent after a forced commit-failure** (so it is
    not trivially green against today's zero-spine-row master), with a `session.commit` call-count spy == 1
    on the success path as the "no second commit" guardrail (a two-commit impl's first commit would itself
    raise, so the spy — not the row count — is the discriminator).
  - **P-SIGN-4** (B-1 idempotent re-run) — a second `approve()`/`reject()` (after a rolled-back attempt OR a
    committed one via `already_applied`) converges the fold to the identical survivor with no duplicate
    divergence; the survivor's decision-row count is exactly 1.

  Container-backed examples wrap per-example engines in `try/finally` (the known leak trap). **RED-first.**
  *(Clause (ii) — anchor durability inherits P1's INV-CTX-PROV: `fuse_context_claim_rows` skips an
  unstamped member (`statements.py:183-190`), so fold anchor parity holds ONLY for provenance-stamped
  members; an anchored-but-provenance-less member's anchor is intentionally NOT reconstructed — asserted, not
  a bug. Node-parity oracle note: `signoff._merge_members` stamps no Tier-1 witness map — unlike
  `_merge_entities`, `merge.py:415-417` — so the direct sign-off node has no `prov_witnesses` while the fold
  adds one (fold strictly richer); `measure_divergence` already excludes `prov_*`/`datasets`/`caption`, so it
  is the right oracle, never a raw `graph_signature`. Full analysis in the spec §Surprises.)*
  *(Spec flag — P-SIGN-1 clause (ii) is the FIRST live validation of context-claim anchor reconstruction on
  a sign-off node, dormant in P1. A clause-(ii) failure is a **pre-existing P1 reconstruction bug** whose fix
  would land in FROZEN `projector.py`/`anchors.py` — a scope change to SURFACE, not a P3 builder task on
  FROZEN code. Risk low: the same `reconstruct_entities` path is already exercised on pipeline survivors by
  P1's IT-PROJ / P-CTX.)*

### Guard-green scope (plan-verify GOVERNANCE HIGH-2 — claim bounded honestly)

P3 **closes the sign-off un-logged-write-path CRITICAL** (consult §6b) **for POST-P3 (statement-bearing)
sign-off survivors**: after P3, a full rebuild reconstructs every human-approved merge, every reject-written
member node, AND their already-promoted outbound edges — they are no longer dropped, and no longer a
*permanent* unexplained divergence source. Two explicit bounds, both cosign-visible:

- **PRE-P3 already-written sign-off nodes** (merges approved/written before P3 ships) carry **zero** statement
  rows → a rebuild still drops them → the guard stays RED on them until **Gate 2b** backfill loads pre-log
  nodes into the log (`81_PRECUTOVER_GATE_SEQUENCE.md` §"owner map" item 9). P3 fixes the **flow**, not the
  **stock**.
- A **zero-FtM-property** sign-off canonical writes no statement rows ⇒ no fold node ⇒ still unexplained
  (WPI-1 / edge (d), out of P3).

P3 does **NOT** by itself deliver "§7-10 guard-green over N cycles on the real corpus" — that operational
cutover condition also depends on P1 (anchors), P2 (erasure), and Gate 2b (backfill). The precise P3 claim:
*future sign-off-promoted nodes and their statement-bearing outbound edges cease to be an unexplained
divergence source* — not "the guard goes green."

### Edge dispositions (decided; plan-verify attacked these — full detail in the spec)

- **(a) B-1 re-run duplication.** `statement_id` is a deterministic content hash over `(dataset,
  entity_id, prop, value)` — **verified first-hand** (canonical_id is NOT in the preimage) — so the fold's
  D3 dedup (`projector.py:143-149`) folds away any duplicate statement rows for the same member claim. Under
  the single-commit design a crashed attempt commits **nothing**, so B-1 re-run writes each spine row
  **exactly once** from a clean state; `record_durable_id` is independently idempotent; `record_decision`
  has **no** dedup constraint and relies on the `approve()` `already_applied` guard (`signoff.py:265-266`) +
  single-commit atomicity for exactly-once — **and a duplicate decision row is harmless regardless, because
  the projector reads decision rows only to advance the watermark, never to reconstruct a node**
  (`projector.py:345-349`). No ON-CONFLICT / uniqueness constraint is added (consistent with ADR 0099's
  rejection of a `statement_id` uniqueness constraint). **Decided: acceptable-with-reason.**
- **(b) reject() member statements.** One singleton `ResolvedCluster` per member (`canonical_id=member.id`,
  schema from the member, `dataset` from member `Provenance.source_id`) → `record_statements` — mirroring
  `fuse_statement_rows`'s per-member shape (`merge.py:281-310`). A rejected pair later re-observed by the
  pipeline gets **fresh connector-minted ids** (ADR 0044/0036) → different `statement_id` → a new legitimate
  observation, not a double-write; an exact-id recurrence (blocked by the idempotent enqueue,
  `uq_er_queue_dedup`) would fold-dedup. **Decided: acceptable/deduped.**
- **(c) context-only-survivor no-op interplay.** P1's fold yields **no** node for a context-claim-only
  survivor (`reconstruct_entities` groups by statement rows — `projector.py:159,126`). **P3's statement rows
  are exactly what un-dormant a sign-off survivor**; the P1 integration test that pinned the no-op via
  `signoff.approve()` (`test_context_claim_lane.py::test_context_only_survivor_after_signoff_approve_is_
  projector_no_op`) **flips** — P3 repoints it to a *synthetic* statement-less survivor (preserving the
  genuine projector no-op) and adds the un-dormanting assertion. **Decided + pinned in P-SIGN-1 / the
  integration flip.**
- **(d) zero-prop sign-off canonical.** A sign-off canonical with **zero FtM properties** yields zero
  statement rows (`_member_statements` iterates `member.properties`) → the fold produces **no node** →
  divergence — identical to the pipeline's zero-prop gap. **Decided: routed to WPI-1** (the §7-3 zero-prop
  evidence class), NOT solved in P3; P3 inherits the same open, explicitly-scoped-out gap.
- **(e) scope freeze / no new migration.** **Verified:** `statement` + `decision` (migration
  `0009_statement_spine`) and `canonical_id_ledger` (`0006_canonical_ledger`) already exist; `context_claim`
  is `0012`. **P3 needs NO new migration** — `decision.decided_by` already accepts any `String(255)`. If a
  migration turns out to be needed that is a **finding to surface, not a silent add**.
- **(f) member-set derivation (plan-verify LOW).** `uq_er_queue_dedup` is on `(source_record, entity_id)`
  (`db/models.py:64`), so two queue rows can share an `entity_id` (different `source_record`). Derive the P3
  member id set from `set(m.id for m in members if m.id)` (or `audit.source_ids`, already unique), and assert
  `set(member_ids) == set(source_ids)`; a degraded queue with a missing member row is the pre-existing
  `member_rows` subset case (approve merges whatever it finds) — noted, not solved here.

### Owner-map rows that stay OUT of P3 (81 §"owner map")

WPI-2 (alias⇔co-commit invariant assertion — sign-off writes an alias in P3 but the *invariant assertion*
is independent, 3a-ii-A backlog), WPI-1 (zero-prop disposition, edge (d) above), **Gate 2b** (backfill of
PRE-P3 sign-off nodes — Guard-green scope above), and **P2 erasure** (the next gate). P3 writes the sign-off
ledger alias; it does **not** add the fold's completeness check. Inbound cross-references dropped at park
time are **not** restored on approve (deferred Gate C, ADR 0031) — no such edge is written, so it is not a
divergence source.

## Reversibility (overall)

**Reversible** (additive `session.add`s inside the existing sign-off transaction; the graph write and the
approve/reject decision are unchanged). **Person-affecting → user cosign before build.** Reversal cost:
remove the spine writes from `approve()`/`reject()`. **Revisit trigger:** SF-1 re-route becomes worth it if
the fusion paths converge. **Scope reminder:** P3 fixes the sign-off write **flow** for future decisions;
the **stock** of PRE-P3 sign-off nodes is Gate 2b's backfill (see Guard-green scope).

## Explicitly NOT in P3

- Erasure / forgetting (Gate P2 — sequenced after P3; P2 layers on P3's logged sign-off nodes).
- **Backfill of PRE-P3 already-written sign-off nodes** — **Gate 2b** (`81_PRECUTOVER_GATE_SEQUENCE.md`
  §"owner map" item 9). P3 logs only future approve()/reject(); pre-P3 sign-off nodes stay unexplained on a
  rebuild until 2b loads them into the log. **Cosign-visible.**
- The alias⇔co-commit *invariant assertion* (WPI-2 — independent of P3; sign-off writes no alias today, so
  both are needed; §7-6).
- E2 anchor capture at `approve()` (already landed in Gate P1 / ADR 0106).
- Zero-FtM-property sign-off canonical disposition (WPI-1, edge (d) above — inherited gap, not solved here).
- Sign-off-block **edge** statement capture (SF-EDGE — edges reconstruct from their own pipeline promotion +
  the P3 ledger alias; a still-pending edge is transiently unexplained, a pre-existing ingestion-ordering
  property).
- Restoring inbound cross-references on approve (deferred Gate C, ADR 0031).

## ADR-index coupling

This DRAFT is **committed as PROPOSED by the P1-0 docs-only slice** (with the index regen — its row reads
`PROPOSED | 2026-07-05 | false | true`), because the index generator scans the filesystem and untracked
drafts break `--check` (adversarial-verify finding). The P3 **planning** gate (this section) fills the
DECIDED lines and ships them as a **docs-only PR** (autonomously mergeable, P1-0 precedent — committing the
filled PROPOSED decision-space is the 0107/0108 committed-draft precedent); it does **not** stamp the cosign
or flip Status. The P3 **code** PR (after the user cosign) owns the accept flip: stamp the `human_cosign`
dated line, PROPOSED → ACCEPTED, regen the index. `person_affecting: true` + `human_cosign` are the
machine-checkable substrate ADR 0097 §5 enforces; **the accept flip must not occur on a PENDING cosign
line.**
