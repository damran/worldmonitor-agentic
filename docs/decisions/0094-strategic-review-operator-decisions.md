# 0094 — Operator decisions on the strategic review's open questions

- **Status:** ACCEPTED (2026-07-04) — all seven questions decided **by the user** in session.
  This ADR is the durable record of a user-decision round, not an agent-classified fork.
- **Date:** 2026-07-04
- **Context:** the Fable 5 strategic review (`docs/fable-review/50_FABLE_REVIEW.md`, merged #154)
  closed with seven open questions (§5) that only the operator could answer. This ADR records the
  answers and the plan re-prioritisations they trigger. Each decision carries reversibility +
  revisit trigger per build discipline.

## D1. Commercial intent (12-month horizon): **none or open-core** — never appliance+services/SaaS

No paid product, no managed instances, no SaaS. The undecided residue ("none" vs "open-core") does
not block anything: both are covered by the same license default (D1a) and neither creates
vendor-side GDPR controller/processor roles or AI-Act provider duties on a sold system.

- **Consequences:** review finding F10 (compliance pack) downgrades from *Must-before-commercial*
  to **good-practice cadence** — GDPR still applies to the *operator as controller* of the personal
  data the platform processes (lawful-basis discipline, erasure, retention remain relevant), but
  the productised artifacts (customer DPIA templates, Art-28 DPAs, buyer vetting, conformity
  assessment) drop out. Track 3c (market frame) becomes academic; build-in-public is
  community/method-sharing, not marketing. OS-Pairs (CC BY-NC) is permanently unproblematic under
  "none"; under open-core the *code* is unaffected and no commercial artifact exists to
  contaminate.
- **D1a. License default: AGPLv3**, committed **at the public flip** (repo is private today, so
  nothing is licensed to anyone yet). AGPLv3 covers both residual options; a CLA is added only
  before accepting the first external contribution (it is what keeps a future dual-license /
  open-core door open).
- **Reversal cost:** low until the public flip; after external adoption, license changes get
  socially expensive (Elastic precedent). **Revisit trigger:** a concrete open-core opportunity, or
  the public flip itself (which forces the LICENSE commit).

## D2. Sovereignty: **drop the zero-egress claim; sovereignty is an operator-discretion mode**

The operator states plainly: *"zero egress" is a false claim today* — claude-headless is in use and
OpenRouter is planned. The claim existed to support a sales pitch that (per D1) no longer exists.
Egress is therefore the operator's per-mode choice, made visible by the ADR-0091 confidentiality
badges, which stay.

- **What changes:** all "zero egress by default / data never leaves the perimeter" *identity*
  language is purged from docs (folded into the F5 truth-up sprint). The three-mode selector and
  its per-mode confidentiality labels are unchanged — they are exactly the right mechanism for
  "operator's discretion". The **Local/Ollama default stays** (a safe default is not a claim).
- **What does NOT change:** the pull-only ingestion principle (our data never routes through
  external brokers/proxies/queues) is a separate, still-true invariant and stays hard. The only
  outbound paths remain: opt-in Telegram, and LLM egress through the single audited LiteLLM
  choke point.
- **Reversal cost:** trivial (language + one settings default). **Revisit trigger:** a deployment
  context that genuinely requires an air-gapped/zero-egress profile — at which point it is a
  *deployment profile*, not an identity claim.

## D3. Tenancy: **none** — no multi-tenancy, no workspaces-as-case-files

The review's F11-T hedge (a `scope` column on the future statement tables) is **declined**; new
tables are designed single-tenant, full stop.

- **Accepted cost (recorded so it is chosen, not discovered):** if this ever reverses, the review's
  quantification stands — re-introducing tenancy is a fresh build of roughly **4–8 gate-weeks plus
  a person-affecting data migration** against a populated graph (ADR 0042's teardown surface, plus
  RLS/predicate design 0042 never needed). **Revisit trigger:** a real second user of one
  deployment — treat as a phase, not a gate.

## D4. First external user persona: **cyber-threat investigators / L3 SOC analysts**

- **Consequences:** the consumption surface (review F4) calibrates to CTI nouns — dossiers are
  threat-actor / campaign / infrastructure entities; watchlists cover actors, domains, certs,
  infrastructure ranges; diff alerts mean "what changed in this actor's infrastructure since
  Friday". **Phase-4 enricher order changes:** the CTI/infra slice (passive-DNS, cert transparency,
  JARM/JA3, STIX from OpenCTI/MISP) moves **ahead of** news-NLP in `docs/40_ROADMAP.md` Phase 4.
  Side benefit, deliberate: CTI-first is mostly non-person entities → materially lower GDPR surface
  while the person-affecting machinery matures (the review's "non-person verticals first" logic,
  applied non-commercially). STIX already being first-class in the L2 contract makes this the
  cheapest persona to serve.
- **Reversal cost:** none (ordering choice). **Revisit trigger:** a concrete design-partner user of
  a different archetype showing up.

## D5. ER-scale fork, pre-decided: **build deeper, never buy** — no paid licenses/products

If volume ever makes hybrid micro-batch ER untenable, the answer is **deeper engineering on the
Splink/nomenklatura stack plus hardware extension or a lift to cloud compute** — never a paid ER
product. Senzing is **permanently out**. The general constraint is recorded project-wide: **no paid
software products** (Neo4j Enterprise/Aura, Temporal Cloud, managed ER, managed IdP all excluded);
the tolerated paid categories are commodity compute/hosting and, selectively, **threat-intel data
sources** (fits D4). Small usage-based LLM API spend is at the operator's discretion per D2;
subscription (claude-headless) and local models are preferred.

- **Consequences:** the review's Track-1 stack choices are all already free-tier consistent
  (Postgres-as-record, DBOS Transact MIT, advisory-lock lease, Neo4j **Community-forever** as
  projection) — and Community-forever makes the statement-store inversion (F1) *more* load-bearing:
  rebuild-from-Postgres is the only DR story CE will ever have. Multi-node durability, if ever
  needed, is self-hosted OSS (DBOS/Hatchet), not Temporal Cloud.
- **Reversal cost:** n/a (a standing constraint, revisable by the user only). **Revisit trigger:**
  user says otherwise.

## D6. Human-review budget: **≤ 5 hours/week**

This is a safety parameter and now a design input:

- The ER **abstention band width and any Tier-0 sampling rate must be sized so steady-state queue
  inflow fits ≤5 h/week** of one-keystroke review; when queue debt exceeds that budget, thresholds
  **tighten automatically** (degrade-conservative) rather than debt accruing into a silent
  de-facto auto-approve.
- Every reviewed pair must land as a gold judgement (review-as-labelling): at 1–2 min/verdict, the
  budget yields ~150–300 boundary labels/week — the calibration substrate accrues meaningfully
  within a quarter *only if* the review UI (F4.1) exists. This raises the review-queue UI's
  urgency, already reflected in the review's sequencing (§2.13 move 3).

## D7. claude-headless mode: **retained** until Anthropic drops support or prohibits usage

The operator explicitly declines the review's retire-early recommendation. Rationale accepted and
recorded: with the zero-egress *claim* dropped (D2) and no commercial pitch (D1), the
claim-vs-practice landmine the review worried about is resolved **from the claim side** — the mode
is an off-by-default, honestly-labelled option whose residual risks (ToS gray zone on
subscription-vs-API usage, brittleness, account exposure) are personal risks the operator accepts.

- **Standing mitigations (unchanged):** the mode stays off-by-default, keeps its ToS/brittleness
  caveat label (ADR 0091), and stays behind the audited gateway. Build-in-public copy simply does
  not headline the shim.
- **Revisit triggers (either fires → drop the mode):** Anthropic ships a change that breaks or
  explicitly prohibits the usage; or ADR 0091's original trigger (a supported first-party path
  makes the shim pointless).

## Consequences (plan-level rollup)

1. **F5 truth-up sprint grows one item:** purge zero-egress identity language (D2) alongside the
   already-listed doc-estate fixes. Priority unchanged (Must, cheap).
2. **F10 re-labelled** good-practice (D1); LIA-per-source manifest field and query-audit remain
   worthwhile but lose the commercial deadline.
3. **F11-T dropped** (D3). **F11 Senzing row hardens** from revisit-trigger to permanently-out (D5).
4. **F4/Phase-4 re-anchored on the CTI persona** (D4): CTI/infra enricher slice first; consumption
   nouns calibrated to threat investigation.
5. **F3.4 band + §3.7 tiering get a hard sizing constraint** (D6): ≤5 h/week inflow,
   degrade-conservative coupling mandatory.
6. **F6.1 is resolved** as claim-drop rather than mode-removal (D2+D7); F6.2 (extraction in the
   ingest path) tilts **local-first with selective frontier escalation** per D5's spend posture.
7. LICENSE lands at the public flip as AGPLv3 (D1a).

None of these decisions is person-affecting in itself; D6 parameterises a person-affecting control
and its future band-width changes remain human-signed per ADR-0047/0031 discipline.
