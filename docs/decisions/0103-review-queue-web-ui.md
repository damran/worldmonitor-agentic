# 0103 — Review-queue web UI (Gate 1): promote the sign-off CLI to a server-rendered HTMX surface

- **Status:** ACCEPTED (2026-07-05)
- **Date:** 2026-07-05
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** n/a for the merged **1a** slice — the Gate-1a diff is **person-NEUTRAL** (strictly
  read-only: it renders the existing `pending_review` queue and never writes the graph, a gold row, a
  judgement, or a merge decision; it touches `api/**` + one CLI help string, **not** the frozen
  person-affecting write path; verified by the read-only `@given` suite + adversarial review). The
  **1b** slice (`POST /review/verdict` — the verdict-execution + gold-label semantics that record and
  **execute** human merge decisions on real people) IS **person_affecting: true** and REQUIRES the
  ADR-0097 human cosign at **its own** merge (a separate future PR): this header flips to
  `person_affecting: true` + a completed, dated `human_cosign` when the 1b code lands. Cosigning 1b's
  person-affecting behaviour *before its code exists* would be a promissory cosign — the exact
  anti-pattern the 3a-ii-A judge DENY established; the cosign attaches to the diff that introduces the
  behaviour, not to a design document ahead of it.
- **Realises:** the review-queue surface named in `docs/70_UI_AND_EXPERIENCE.md` §4D/§9/§10 and the
  Gate 1 brief in `docs/fable-review/70_EXECUTION_HANDOFF.md` §1 (review F4.1). **Builds on:** ADR 0031
  (return-to-block sign-off — the frozen `resolution/signoff.py` domain the UI drives), ADR 0047
  (fail-closed sensitivity guard — the `is_sensitive` oracle the badge reads), ADR 0069 (the HTMX/Jinja
  Integrations seam this copies), ADR 0068 (dual-path bearer/session auth + CSRF), ADR 0043/0079/0085
  (the gold-label store the verdict hook feeds), ADR 0098 (the vendor-as-data + PROVENANCE pattern for
  the vendored front-end library). **Supersedes:** nothing. ADR 0031 and ADR 0047 semantics are
  **unchanged** by this ADR.

## Context

The catastrophic-merge guard parks flagged clusters as `merge_audit.decision == 'pending_review'`
under the block-default posture (`settings.py:83`, ADR 0031/0024). The only interface to clear that
queue today is the CLI `python -m worldmonitor.review` (`src/worldmonitor/review.py` → the frozen
`resolution/signoff.py` domain: `list_parked` / `approve` / `reject`). `docs/70` §4D and the F4.1 brief
call for promoting that CLI to a **server-rendered HTMX web UI**: side-by-side candidate cards, a
statement-level evidence diff, the guard reason + a confidence **band** (never a verdict), a prominent
"blocked pending human sign-off" badge on sensitive merges, one-keystroke verdicts, and **every verdict
written as a gold label** so the review budget (≤5 h/week, ADR 0094) doubles as the calibration-label
factory that later unblocks Gate 4.

Three facts from a fresh orient shape the decisions below (verified against the tree at `e227334`):

1. **The auth/UI substrate already exists** and is copyable verbatim: `create_app` mounts
   `Jinja2Templates` + `/static`, registers routers, and enforces dual-path auth with a redirect for
   unauthenticated browsers (`api/middleware.py`); `api/integrations.py` is the reference router with a
   session CSRF synchronizer (`_csrf_token`/`_check_csrf`), POST→303, and Jinja autoescape.
2. **The person-affecting domain is frozen and complete** (`resolution/signoff.py`): `list_parked`,
   `approve`, `reject`, idempotency/orphan guards, `_record_judgements` (the O(n²) resolver-feedback
   surface). The UI is a thin driver over it, not a reimplementation.
3. **A parked merge has no per-claim statement rows.** The Gate 2a dual-write (`StatementRecord`)
   fires only on the *promoted* `"merged"` path (`pipeline.py:476-478`); the park path
   (`pipeline.py:426`) writes none. So the review evidence diff is **always** derived from the queue
   items' `ErQueueItem.raw_entity` FtM props — exactly what the CLI has to work from — and never from
   the statement spine. This is a data-shape fact, not a choice; it bounds the diff design.

Three genuinely new pieces of domain work do not exist yet and are decided here: (a) an **abstain**
verdict, (b) a **verdict→gold** hook, and (c) how a **true split** is (not) offered. None is an
architectural fork; all take reversible defaults with recorded reversal costs (per the gate-fleet
reversibility mandate).

## Decision A — Two slices: 1a read-only queue (person-neutral) then 1b verdict + gold (person-affecting)

Ship the gate as **two independently-mergeable slices**, isolating the person-affecting write surface:

- **1a — read-only review surface (person_affecting: false).** `GET /review` (queue list, counts,
  blocked/sensitive badges, confidence band, guard reason) + a `GET /review/card` HTMX fragment
  (side-by-side member cards, statement-level `raw_entity` evidence diff, source chips). No POST, no
  write of any kind. Vendored htmx v2. This is the safety-control *view* and it stands alone.
- **1b — verdict actions + gold hook (person_affecting: true).** `POST /review/verdict` (CSRF-gated,
  303) dispatching to the frozen `signoff.approve`/`signoff.reject` plus the new additive
  `signoff.abstain`, then the additive `gold.record_verdict_gold` hook; one-keystroke `a`/`r`/`x`
  bindings. This is where a human merge decision on a real person is recorded and executed.

**This matches the repo grain.** Every recent multi-part gate split the person-affecting or
higher-risk half from the neutral half and merged them separately: 2a (spine tables) → 2b (backfill),
3a-i (dormant fold) → 3a-ii (rebuild-diff guard). The read-only view carries no invariant risk and can
land, be exercised by the operator, and de-risk the layout while 1b's gold/verdict semantics get the
full property-test + cosign treatment. One combined PR would force the read-only shell through the
person-affecting review bar for no benefit and enlarge the diff the checker must reason about. The
cost of splitting is one extra PR and a small amount of forward-preparation (1a mints the CSRF token
its detail fragment will need); the benefit is a clean person-affecting boundary. Split it.

`.claude/gate.scope` is written for **1a only**. The 1b file list, tests, and invariants are specified
in a self-contained §1B roadmap in the gate spec so the 1b pass is cheap.

## Decision B — True `split` deferred to 1c; no fake affordance; CLI help truthed-up

The four-verb handoff (`approve` / `reject` / `split` / `abstain`) is satisfied **with a documented
deferral**: 1b ships approve, reject, and abstain; a **true split** (ejecting a subset of a cluster
into their own entities while merging the rest) is **deferred to Gate 1c**. There is no split function
in the domain today; the CLI's `reject` subparser help even mislabels itself as *"split a parked
merge"* (`review.py:37`) — terminology drift, because `reject` *is* the current conservative fallback
(write every member standalone).

- The UI **must not** render a fake "split" affordance. Offering a control that silently maps to
  `reject` would teach the operator a false mental model and, worse, emit misleading gold (see
  Decision C).
- 1a **truths-up the CLI help**: `reject`'s help string changes to *"reject a parked merge (write its
  members as separate entities)"*. One-line, no behaviour change; keeps the deferral honest on both
  surfaces.

**Reversal cost:** low and person-affecting-safe. Deferring split means an operator faced with a
cluster that deserves a *partial* merge (some members are one entity, others are not) must currently
choose the whole-cluster `reject` — conservative, produces **no false merge**, only a missed true
sub-merge that re-surfaces on the next resolve pass. **Revisit trigger:** the first real partial-merge
case an operator hits in the ≤5 h/week cadence (they will report "I wanted to keep A+B but eject C").
At that point 1c adds a real split verdict whose gold semantics are precise (see Decision C revisit).

## Decision C — Gold-label semantics (the calibration-truth surface; designed conservatively)

Every 1b verdict writes into `er_gold_pair` via an **additive** `gold.record_verdict_gold` hook the
POST route calls after the frozen `signoff.*` write returns. The gold surface is **separate from and
stricter than** the existing resolver-judgement surface (`_record_judgements`, ADR 0031) and the two
**intentionally diverge** — see the k>2 note. Rules (`k` = cluster member count):

| Verdict | Gold written | `source` | `label` |
|---|---|---|---|
| **approve** | all `C(k,2)` member pairs | `signoff:approve` | `match` |
| **reject**, `k == 2` | the single pair | `signoff:reject` | `non_match` |
| **reject**, `k > 2` | **nothing** | — | — |
| **abstain** | **nothing** | — | — |

- **approve ⇒ `match` on every pair.** The operator asserts *one entity*; every pairwise sub-claim is
  therefore a true match. Exactly `C(k,2)` canonically-ordered pairs, no more, no fewer.
- **abstain ⇒ zero gold rows.** An abstention is the *absence* of ground truth. Forcing an unsure
  operator to emit a label would corrupt calibration with a guess — the entire reason abstain must
  exist. This is the honest reading of the handoff's "every verdict lands in the gold-label store": a
  verdict is faithfully reflected, *including* faithfully contributing nothing where it carries no
  clean pairwise truth.
- **reject, `k == 2` ⇒ one `non_match`.** Clean: the operator says these two records are not the same
  entity. A true, unambiguous pairwise negative.
- **reject, `k > 2` ⇒ zero gold rows — THE HAZARD, and why.** A whole-cluster reject means *"this
  cluster is not one entity."* It does **not** assert that every one of its `C(k,2)` pairs is a
  non-match: a rejected 3-member cluster may well contain a valid sub-pair (A≡B, C distinct). Writing
  all-pairs `non_match` would inject **false negatives** into the calibration set and, because gold is
  the regression instrument every later ER gate measures over (ADR 0043), a wrong gold label silently
  degrades every future threshold decision. `ErGoldPair.label` is binary (`match` | `non_match`) with
  no "unknown/ambiguous" value, and no schema field can honestly represent *"unknown pairwise truth"*
  (see Alternatives). The only faithful option is therefore to **write nothing** and defer pair-truth
  to a future **split** verdict (1c), which *can* emit precise per-sub-cluster gold. **Revisit
  trigger:** when 1c's true split lands, a split emits `match` within each retained sub-cluster and
  `non_match` across the split boundary — the pairwise truth a k>2 reject cannot express.

**The gold surface and the judgement surface diverge on k>2 reject, on purpose — do not "align" them.**
The frozen `_record_judgements` writes an all-pairs `negative` judgement on any reject (including k>2):
that is the *operational* resolver-feedback surface whose job is to stop the cluster re-parking, and
it is locked (ADR 0031). Gold is the *calibration-truth* surface whose job is to be correct ground
truth. A k>2 reject legitimately produces an all-pairs negative *judgement* (don't re-merge this exact
cluster) **and** zero *gold* (we don't know which pairs are truly distinct). 1b must keep these
distinct and must not touch `_record_judgements`.

**Marker + idempotency.** `source ∈ {signoff:approve, signoff:reject}` distinguishes operator-verdict
gold from the seeded harness gold (`uncertainty`/`os_pairs`) so calibration can filter it; both fit
`ErGoldPair.source` `String(32)` → **no migration**. Writes reuse the existing `persist_gold_pairs`
idiom (canonical `(left_id, right_id)` ordering; `ON CONFLICT DO NOTHING` on `uq_er_gold_pair`), so
re-running a verdict writes nothing new (idempotent). `clerical_score` is `None` (a whole-cluster
verdict has no per-pair Splink probability; storing one would be a fiction). **Known accepted
property:** if the same pair receives verdict-gold from two different clusters (a pair re-forms under a
new canonical id), first-writer-wins (`ON CONFLICT DO NOTHING`) — the same precedence idiom
`gold.build_gold_pairs` already uses. The frozen sign-off state machine prevents a *single* audit from
producing both a match and a non_match for one pair (an audit is terminal once merged/rejected).
**Revisit trigger:** operators reporting contradictory cross-cluster verdicts on one pair → revisit
with an explicit supersession/last-writer policy.

## Decision D — Abstain: record-only, stays in the queue, revisitable, no migration

Abstain is an **additive** `signoff.abstain(session, *, canonical_id, approver, reason)` (a sibling of
`approve`/`reject`; it does **not** modify their frozen write paths). It:

- writes **no** graph node, **no** gold row, **no** resolver judgement;
- records exactly one `SignOff(decision="abstained")` audit row (`"abstained"` fits `String(16)` →
  **no migration**) — the durable "an operator looked and deferred at time T" trail;
- **leaves `merge_audit.decision == 'pending_review'` unchanged**, so the item stays in `list_parked`
  and remains fully **approve/reject-able later**. Abstain is *reversible by construction*: it never
  moves the audit toward a terminal state.

`MergeAudit.decision` is a free `String(32)` (no DB enum/check), so this needs **no new state value and
no migration**. **Deprioritization** is a pure read-side concern: the 1a queue view sorts items with a
recent `SignOff('abstained')` row to the bottom (derived at render time — no new column) so the
operator does not immediately re-encounter what they just deferred.

**Idempotency is defined at the state level.** The observable person-affecting state after N identical
abstains is identical to after one: `merge_audit.decision` still `pending_review`, zero gold, zero
judgements, zero graph nodes. The only growth is append-only `SignOff('abstained')` audit rows — which
is the *intended* provenance trail, not state divergence, and honours the append-only invariant. The
verdict-idempotency property (§1B) asserts convergence of that state, explicitly excluding the
append-only audit-row count from the oracle. Abstain refuses on an already-terminal audit
(merged/rejected), mirroring approve/reject's guards.

## Decision E — UI, auth, and front-end vendoring

- **Server-rendered HTMX-first, copying the ADR-0069 seam.** New `api/review.py` router registered in
  `create_app`; templates extend `base.html`; every route `get_principal`-gated (unauthenticated
  browser → 302 `/login` via the existing middleware). No SPA.
- **Vendored htmx v2 (0BSD), self-hosted, zero-egress.** htmx is downloaded **at build time** and
  committed under `api/static/vendor/htmx.min.js` with a `PROVENANCE.md` recording pinned version,
  license (0BSD), upstream URL, retrieved-date, and sha256 — mirroring the FtM vendor-as-data pattern
  (ADR 0098). Build-time vendoring is not runtime egress (same posture as the vendored schema YAMLs).
  Templates reference **only** `/static/...` — **no CDN** (`unpkg`/`jsdelivr`/`https://`), which a test
  asserts. This satisfies `docs/70` §1.7 / §9 (self-hosted, no external calls).
- **No Alpine.js in Gate 1.** `docs/70` §9 lists Alpine as optional micro-reactivity; the one
  interactive need here (one-keystroke verdicts) is a few lines of vanilla JS keydown→form-submit.
  Adding Alpine would be a second vendored dependency for no gate-1 need. **Reversal cost:** trivial —
  a later rich surface (Gate 6+) can add Alpine as a vendored island then. **Revisit trigger:** the
  first surface that genuinely needs reactive client state (overlay toggles, tabs).
- **CSRF.** 1b's `POST /review/verdict` uses a session synchronizer token identical to
  `integrations._check_csrf` (absent/wrong → 403). Because `api/integrations.py` is frozen, `review.py`
  carries its **own** small `_csrf_token`/`_check_csrf` helpers (deliberate ~6-line duplication rather
  than editing the frozen module; a future refactor may extract a shared helper). 1a mints the token in
  the detail-fragment context so 1b only adds the form.
- **Routes / shape** (build to `docs/70` §4D/§10):
  - `GET /review` — queue list: per parked merge, member count, guard **reason** (verbatim,
    autoescaped — never parsed), a **confidence band** styled from `merge_audit.score` as a
    gradient/bar with the numeric value (explicitly **not** a pass/fail verdict, C5), a base
    **"blocked pending human sign-off"** state (intrinsic to `pending_review`), a prominent
    **sensitive** badge when `any(is_sensitive(member))`, and the `graph_written` recovery flag
    (from `list_parked(session, neo4j)`); a total count header.
  - `GET /review/card?canonical_id=…` — HTMX fragment: side-by-side member cards from `raw_entity`
    (schema + FtM props + a per-member source chip: `source_id`/reliability/`retrieved_at`/raw
    pointer), a **statement-level evidence diff** (per FtM property, each member's value(s), marking
    agreements vs contradictions), the guard reason, the confidence band, the sensitive badge. 404 if
    `canonical_id` is not a current `pending_review` merge. A query param (not a path segment)
    sidesteps canonical-id escaping (ids like `qid:Q42`, `lei:…`).
  - **1b:** `POST /review/verdict` (form: `canonical_id`, `verdict ∈ {approve,reject,abstain}`,
    `csrf_token`; 303 back to `/review`; `a`/`r`/`x` keybindings).
- **Sensitive badge reads the guard, not the free-text reason.** The prominent badge is driven by
  `guard.sensitivity.is_sensitive(make_entity(raw_entity))` per member (pure, deny-by-default, ADR
  0047) — **not** by substring-matching `merge_audit.reason`, which the guard's own code warns embeds
  hostile data-bearing fields (member ids, anchor values — Finding F). The reason is still *shown*
  verbatim (autoescaped) as the "why parked" text.

## Decision F — Person-affecting analysis + governance

- **1a is person-neutral** (read-only; renders the existing queue; writes nothing). Its property
  obligation is a read-only invariant (GET routes issue no write) + XSS-escaping of hostile
  reason/property strings.
- **1b is person-affecting** (it records and executes a human merge decision on a real person — the
  ADR-0097 mandate's intended mechanism). Its property obligations (mandatory `@given`, this touches
  the merge/sign-off + gold invariant): **verdict idempotency**, **gold faithfulness** (the Decision-C
  table exactly), and **guard non-bypass** (no UI route writes the graph except through frozen
  `signoff.*`; every state-changing route CSRF-gated).
- The header's `person_affecting`/`human_cosign` track the **slice actually being merged**, not the
  whole design doc: the **1a** PR is `person_affecting: false` (its diff is read-only and touches no
  person-affecting write path) and needs **no** cosign; the header **flips to `person_affecting: true`
  + a completed, dated `human_cosign`** when the **1b** PR (which introduces the verdict-execution +
  gold write path on real people) is merged. Attaching the cosign to the diff that carries the
  behaviour — rather than to this document ahead of the code — is the 3a-ii-A DENY lesson applied:
  a cosign attests to a person-affecting *change*, and 1a is not one. `human_fork: false` — every open
  choice here has a reversible default with a recorded reversal cost + revisit trigger; none is a
  product/architecture fork requiring a human pick.

## Reversibility

| Decision | Class | Reversal cost | Revisit trigger |
|---|---|---|---|
| A — 1a/1b split | reversible | merge as one PR later; trivial | never (matches repo grain) |
| B — split → 1c | reversible | operator uses conservative reject meanwhile (no false merge) | first real partial-merge case |
| C — k>2 reject ⇒ no gold | reversible (conservative default) | omits some true negatives from calibration; never injects a false one | 1c split lands, or gold is measurably too sparse at the boundary |
| D — abstain record-only | reversible | if a hard "abstained" state is wanted later, add an additive column/migration | operator wants abstained items hidden, not just sorted last |
| E — HTMX, no Alpine | reversible | add a vendored Alpine island later | first surface needing reactive client state |

## Deferred (out of scope for Gate 1)

- **True split verdict (Gate 1c)** — reversal cost + revisit in Decision B.
- **Alpine.js / rich islands** — Decision E; Gate 6+.
- **Queue pagination / bounded rendering** — 1a reuses `list_parked` as-is (small queue under the ≤5 h
  budget; the CLI lists all). A follow-on if the queue grows.
- **A dedicated CSP middleware / strict-CSP hardening** — `docs/70` §9 flags CSP as strict-but-nontrivial
  for the WebGL surfaces (Gate 6+); Gate 1 ships no inline JS from untrusted data and no third-party JS,
  so a CSP middleware is not required here. Note it; do not build it unless trivial.
- **Gate 4 threshold-tightening / abstention-band sizing** and **R4 review cadence** — consume this UI;
  not built here.
- **Inbound cross-reference restoration on approve** (the deferred `signoff` Gate C) — unchanged.

## Alternatives rejected

- **One combined 1a+1b PR.** Rejected (Decision A): forces the read-only shell through the
  person-affecting bar and enlarges the checker's diff for no benefit.
- **A "split" control that maps to reject.** Rejected (Decision B): a fake affordance teaches a false
  model and would tempt all-pairs `non_match` gold (Decision C hazard).
- **k>2 reject ⇒ all-pairs `non_match` gold.** Rejected (Decision C): injects false negatives into the
  regression instrument that governs every future threshold decision.
- **k>2 reject ⇒ gold rows marked "ambiguous" via a new field.** Rejected: `ErGoldPair.label` is binary
  and no existing field can honestly encode "unknown pairwise"; a migration to add one is not justified
  for a case a future split verdict represents precisely.
- **Abstain as a new terminal `merge_audit` state.** Rejected (Decision D): would require teaching the
  frozen approve/reject state machine to re-open an abstained item, versus record-only which keeps it
  trivially revisitable with zero churn and no migration.
- **Alpine.js for keybindings.** Rejected (Decision E): a second vendored dependency for what a few
  lines of vanilla JS cover.
- **CDN-loaded htmx.** Rejected (Decision E): violates the self-hosted / no-external-call posture
  (`docs/70` §1.7/§9); vendored + PROVENANCE + sha256 instead.

## Governance

Per ADR 0097 §4/§5 the `person_affecting` / `human_cosign` header tracks the **slice actually being
merged**, checked against the **actual diff**, not this design doc's overall scope:

- **1a (this PR) is `person_affecting: false`** — its diff is strictly read-only (renders the existing
  `pending_review` queue and an already-computed `is_sensitive` flag; writes no graph node, gold row,
  judgement, or merge decision) and touches no person-affecting write path (`api/**` + one CLI help
  string; `signoff.py`/`gold.py`/`merge.py`/`guard/**`/`db/models.py`/`pipeline.py` are byte-unchanged).
  A cosign attests to a person-affecting *change*; 1a is not one, so it merges **without** a cosign.
- **1b (a future PR) is `person_affecting: true`** — `POST /review/verdict` records and **executes**
  human merge decisions on real people. Its PR flips this header to `person_affecting: true` + a
  **completed, dated** `human_cosign`. Cosigning 1b's behaviour *before its code exists* would be the
  promissory-cosign anti-pattern the 3a-ii-A judge DENY established — the cosign attaches to the diff
  that carries the behaviour, not to a design document ahead of it. The person-affecting gate stays
  fully intact: 1b cannot merge un-cosigned.

`human_fork: false`. ADR 0031 and ADR 0047 are cited and **unchanged**.
