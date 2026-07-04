# 40 — Review Charter (your task specification)

> This is what to produce. Read [`00_FABLE_REVIEW_BRIEF.md`](00_FABLE_REVIEW_BRIEF.md) first (context,
> authorisation, scope guardrails), then [`10_SYSTEM_DIGEST.md`](10_SYSTEM_DIGEST.md) (the system),
> [`20_DECISION_REGISTER.md`](20_DECISION_REGISTER.md) (the decisions, tagged), and
> [`30_CONSTRAINTS_AND_FREEDOMS.md`](30_CONSTRAINTS_AND_FREEDOMS.md) (what's now open). Then deliver the
> three-part review below in the output structure at the end.
>
> **Reminder of scope:** architecture, design decisions, software/technology choices, product
> strategy, and communication — **not** bug-hunting, unit-test audit, or line-level code review.

---

## Track 1 — Improve in place

*Given WorldMonitor as it exists today, and given that the old constraints are now relaxed
(Section B of the Freedoms doc), where and how would you improve it?* Evolution of a real, one-person
codebase — so weigh cost and sequencing, not just the ideal end-state.

Address at least these dimensions. (The digest §12 lists tensions the operator already sees — push
*past* them: are they the right calls, and what's the move?)

1. **Architecture & data model.** Is the L0–L9 layering and the "L2 is the contract" rule the right
   backbone? Is a **labelled property graph (Neo4j)** the right substrate for an *ontology-first,
   provenance-first* product, or is the flat-`prov_*`/single-source-collapse/edge-drop friction a
   signal that the substrate is fighting the goal? What would you change first?
2. **The resolution pipeline.** Batch-only ER leaves a standing cross-batch dedup gap (violating the
   core "de-dupe before you count" claim for the highest-volume sources). What's the right path to
   **incremental ER**, and what does it cost against the current design? How should the **0.92
   threshold get calibrated** given the cold-start/circular-label problem, and should the merge guard
   flip from "alert" to "block" before the graph is queried/acted upon?
3. **Software / technology choices.** Work the stack table (digest §11) and the now-open decisions.
   Where would you **keep, swap, or add** — Neo4j Community vs Enterprise/Aura vs a triple store;
   FtM-as-internal-model vs FtM-as-interchange; Splink vs embedding/managed/LLM-assisted ER; the
   asyncio task table vs a durable workflow engine; in-process LiteLLM vs a proxy; adopt-Hermes vs a
   thin custom loop? Give the trade-off, not just the swap.
4. **Scale, HA & operability.** The single-node design (one loop, in-process lock, dual-write without
   a saga, hours-scale RTO, no HA) is a hard ceiling the "always-on → cloud" trajectory hits
   immediately. Now that cloud/managed substrates are permitted, what's the right **HA/durability
   backbone**, and what's the migration order?
5. **Provenance & the audit/GDPR story.** Tier-1-only provenance is thinnest exactly on merged nodes.
   Is **statement-level (Tier-2) provenance** worth the migration, and how would you sequence it so
   value-level erasure and multi-source lineage become first-class?
6. **The consumption surface.** For a product whose thesis is *relationships*, the read side is four
   bounded tools + a config page. What is the **minimum analyst experience** (explorer, query surface,
   triage/alerting) that would make the value demonstrable, and should it drive the API/schema rather
   than trail it?
7. **The agent layer & self-improvement.** §4b/§4c are unbuilt and have no evaluation substrate for
   non-ER parameters. What is a **credible first version** of gated param/rule tuning — the reward
   signal, the safe-range definition, how a person-affecting proposal is scored — and is adopting a
   general self-improving runtime (Hermes) the right call vs a thin, controllable loop?
8. **Sequencing.** Given one operator and the current state (Phases 4–6 unstarted, agent live on a
   sparse/uncalibrated graph), what are the **next 3–5 moves**, in order, and why?
9. **Ethics, compliance & abuse-resistance.** Lawful basis for processing personal data (erasure is a
   remedy, not a basis), a data-poisoning / adversarial-entity-injection threat model, and the
   dual-use posture. What must exist before this scales or goes commercial?

**For each Track-1 finding**, give: *what* (the change), *why* (the reasoning/trade-off), *cost*
(rough effort + reversal cost), *unlocks* (what it enables), and a **priority** (Must / Should /
Could).

---

## Track 2 — Clean-slate re-architecture

*If you were to architect a system for this exact goal from scratch today — free of every historical
constraint — how would you build it?* This is a first-principles design, **not** a diff of the
current one. Start from the **core commitments** (Freedoms doc Section A) and assume **all** of
Section B free from day zero (multi-tenant OK, cloud/managed OK, any license, build-or-adopt, no
deferrals inherited).

Design and defend your answers to at least these forks (these are seeds — reach past them):

- **Substrate.** Labelled property graph, RDF-star / bitemporal triple store, or something else — for
  a product where provenance, statement-level lineage, and *belief over time* are the point? Is "one
  canonical graph" even right, or is a **versioned/bitemporal graph** (valid-time + assertion-time,
  non-destructive/ reversible merges, catastrophic-merge-as-belief-revision) the better model?
- **Resolution.** Incremental/streaming ER from day one vs batch. Where does **calibration ground
  truth** come from at t=0 (the cold-start / circular-label problem)? Managed ER, embeddings, active
  learning, or LLM-adjudicated boundary pairs?
- **The LLM's place.** Should an extraction model be *in the ingestion/mapping path* (LLM → FtM), not
  just enrichment — to reach the unstructured OSINT long tail? What's the real **sovereignty threat
  model**, and does default-local justify its capability tax, or is "confidential-by-workload" with
  frontier models better?
- **Tenancy & identity.** Single-tenant appliance vs multi-tenant platform as a *founding* choice —
  which is the product, and how does that shape the identity/canonical-ID space from the start?
- **Durability/HA backbone.** A log (Kafka/Redpanda) + durable workflows (Temporal) as the spine from
  day zero vs an in-process loop you plan to replace — which makes HA, streaming, exactly-once
  cursors, and dual-write correctness *free* rather than retrofitted?
- **Agent runtime.** Build a thin purpose-built loop over the MCP contract, or adopt a general
  self-improving runtime whose self-modification is the riskiest subsystem? Now that MCP is a stable
  contract, where's the line?
- **Human-in-the-loop at scale.** Is a solo reviewer a viable safety model, or does the design need
  **tiered/sampled/risk-budgeted** review (auto-approve below risk R, sample above, hard-stop only on
  person-affecting) so review isn't the graph's growth rate-limiter?
- **The consumption/analyst experience, designed first.** If value lives in cross-source
  relationships, how does the query/explore/triage/alert surface drive the schema and API from the
  start?
- **Legal basis & abuse-resistance as architecture**, not an erasure endpoint bolted on later.

**Close Track 2 with a convergence/divergence map:** for each fork, state whether your clean-slate
answer **re-converges** with the current design (and why the current call was right) or **diverges**
(and what that's worth). The contrast between Track 1 and Track 2 is the deliverable's core value.

---

## Track 3 — Communication review

Critique and improve how the project is communicated, **outward and inward**.

### 3a. Outward positioning ("build in public")
The operator is considering public "build-in-public" posts. **Sample drafts are reproduced in the
appendix below** — critique them and propose stronger versions. Weigh:

- **Differentiation clarity.** Separate table-stakes (an FtM entity graph — OpenSanctions/Aleph
  already do this) from the genuine novelty (calibrated ER + catastrophic-merge guard, provenance-as-
  audit-log, *gated* self-improvement with human sign-off, and the multi-agent adversarial build
  method). Do the drafts lead with the differentiator or the commodity? Should they name the real
  comparables (Aleph/OpenAleph, OpenCTI, Maltego, Linkurious, Palantir Gotham, Babel Street) so
  "why this is different" is legible?
- **Claim-vs-reality integrity.** Several headline claims out-run shipped state — "provenance on
  everything / GDPR audit log" (partial on merged nodes), "never auto-merge a sensitive entity"
  (guard in alert mode), "the resolved graph is the product" (batch-only dedup, uncalibrated, thin
  consumption surface). Build-in-public earns credibility only if claims match reality. Recommend
  honest framings ("designed, enforcement pending") that keep the ambition without the overclaim.
- **Reveal-vs-withhold for an intelligence tool.** What should *not* be public — the source inventory,
  active-scanning specifics, the sensitivity denylist/taxonomy, target-allowlist mechanics? Foreground
  the ethical posture (leads-not-verdicts, human sign-off, erasure); abstract the offensive-capable
  surface.
- **Method-vs-product balance.** "AI co-built this via an adversarial gate fleet" is a compelling hook
  for a build-in-public audience but must not eclipse "what it does for a user" for a customer/
  investor audience. The two narratives need deliberate separation.
- **Get ahead of the landmines.** The claude-headless mode, active scanning, and personal-data
  processing are reputational risks; strong ethical design is an asset only if communicated *before*
  the risks are discovered.

### 3b. Inward / design communication
- **ADR & governance discipline as a communicable strength — and its integrity.** 93 reversibility-
  classified decisions + a Gate Ledger + property-tests-as-gate is genuinely differentiating. Is the
  corpus navigable (index, intact supersession chains, no orphaned "proposed" ADRs)? Can a newcomer
  reconstruct "why" from the ADRs alone? Is there a self-classification risk (reversibility judged by
  the same agents that implement)?
- **Ground-truth drift.** `CLAUDE.md` is mirrored verbatim into `AGENTS.md`/`.clinerules` and is a
  per-turn token tax; `MEMORY.md` carries live state. Can these silently diverge? Does informal
  MEMORY vs formal `docs/` create a two-tier truth?
- **Roadmap honesty.** With Phases 4–6 unstarted, how should current maturity be communicated so a
  Phase-3 demo isn't read as the platform's ceiling?

### 3c. Product & market framing (now that cloud/commercial are open)
Briefly: single-tenant appliance vs SaaS vs open-core; where WorldMonitor sits versus Aleph/OpenCTI/
Maltego/Palantir/Linkurious/Babel Street; and what a defensible wedge/monetisation thesis would be.
This frames the Track-1-vs-Track-2 decision.

---

## Required output structure

Produce a single review document with these sections:

1. **Executive summary** (≤ 1 page) — your 5–8 most important calls across all tracks, ranked. If you
   could change three things, what and why.
2. **Track 1 — Improve in place** — findings per the dimensions above, each with *what / why / cost /
   unlocks / priority (Must-Should-Could)*, ordered by priority.
3. **Track 2 — Clean-slate re-architecture** — your first-principles design, the fork decisions, and
   the **convergence/divergence map** vs the current system.
4. **Track 3 — Communication** — outward positioning (with rewritten sample posts), inward/doc
   discipline, and the product/market frame.
5. **Open questions for the operator** — the decisions you can't make for them (strategy, tenancy
   model, sovereignty-vs-capability, commercial intent) framed as concrete choices.
6. **Appendix (optional)** — anything longer-form (a proposed target architecture diagram in text, a
   sequenced migration plan, a scorecard of the stack).

Be direct, prioritised, and willing to disagree with the framing. Assume a competent, safety-
conscious operator who wants the sharpest outside view, not reassurance.

---

## Appendix — communication artifacts to critique (operator's sample drafts)

> These are **examples**, not finished copy. Critique them and propose stronger versions in §4.

**Option A — "build in public," technical-professional**

> Building an OSINT intelligence platform where the graph is the product.
>
> For the past few months I've been building WorldMonitor — a self-hosted, graph-native geopolitical
> & cyber-threat intelligence platform — in close collaboration with Claude as an engineering partner.
>
> The core idea: most monitoring tools dump you a feed. We do the opposite. Many noisy sources
> collapse into one canonical entity graph — people, organizations, places, events — each node and
> edge carrying full provenance (where it came from, when, how reliable). De-duplicate before you
> count. Calibrate before you conclude.
>
> A few principles I've held the line on:
> • Ontology-first — every entity validates against an open standard (FollowTheMoney + STIX), not a
> model I made up.
> • Leads, not verdicts — attribution and geolocation come out as ranked hypotheses with confidence,
> for a human to review. Never an automated accusation.
> • Provenance on everything — the same trail that powers analysis is the audit/GDPR log.
> • Gated self-improvement — when the system proposes a change to itself, it goes propose → evaluate
> → gate → promote, versioned, with rollback. Anything that affects a real person needs a human
> signature.
>
> What's been genuinely new for me is the development model: a disciplined multi-agent workflow where
> changes go through an adversarial gate — independent test authoring, implementation, and a skeptical
> reviewer that tries to break the work before it merges. Property-based tests, not just happy-path
> checks. It's changed how I think about shipping safety-critical code.
>
> More to share soon. #OSINT #ThreatIntelligence #KnowledgeGraphs #AI #CyberSecurity

**Option B — shorter, reflective hook**

> Most threat-intel tooling gives you a firehose. I'm building the opposite: a self-hosted platform
> that turns many messy OSINT sources into one canonical, fully-provenanced entity graph — and treats
> attribution as ranked hypotheses for a human to review, never an automated verdict.
>
> I'm building it with Claude as a co-engineer, using a multi-agent workflow where every change faces
> an adversarial review gate before it merges — independent tests, a skeptical reviewer, property-based
> checks. De-dupe before you count. Calibrate before you conclude. Provenance on every node.
>
> Still early, but the architecture is holding up. #OSINT #ThreatIntel #AI #KnowledgeGraphs

**Option C — one-liner for a comment/teaser**

> Building WorldMonitor: a self-hosted OSINT platform where the resolved entity graph is the product —
> every fact carries its provenance, every attribution is a reviewable hypothesis, and the whole thing
> is co-engineered with Claude under an adversarial test-and-review gate.

**Operator's own notes on these drafts** (context for your critique): lead with the contrarian line
("the graph is the product" / "leads, not verdicts"); decide how much "AI co-built" to foreground
(given the operator's background and Anthropic vetting, the agentic-development angle is a credible
differentiator — but Option B keeps the AI part to one sentence if the post should land as a security/
intel statement); don't name sources, controls, or the repo — keep it architectural; add one simple
"many sources → resolved graph → analysis → API/agent" visual.
