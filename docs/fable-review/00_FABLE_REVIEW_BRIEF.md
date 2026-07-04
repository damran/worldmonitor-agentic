# 00 — Reviewer Brief

> **You are Fable 5, acting as a principal systems architect and product strategist.** You have
> been asked to review the **WorldMonitor** platform. This document tells you what the project is,
> who is asking, what to review, what *not* to review, and how to work. Read it fully before you
> begin. Your task specification is [`40_REVIEW_CHARTER.md`](40_REVIEW_CHARTER.md).

---

## 1. What WorldMonitor is (one screen)

A self-hosted, **graph-native, ontology-first, plugin-extensible OSINT and geopolitical-
intelligence platform**. It fuses many heterogeneous open sources — sanctions, corporate/financial
registries, news, social, CTI/infrastructure, crypto, geospatial — into **one resolved entity
graph** with **provenance on every node and edge**, runs analysis on top (graph, anomaly,
geospatial, fusion/forecasting — mostly still ahead of it), exposes that graph through a **REST +
MCP surface**, and is operated by a **gated, self-improving agent layer (Hermes)** that
investigates, reports (Telegram), and is *allowed to improve the system only through a
propose → evaluate → gate → promote loop with a human sign-off on anything that affects a real
person*.

The **thesis**: OSINT is a graph-traversal problem; value lives in how entities connect across
sources, not in any one datapoint. A monitoring *dashboard* (the typical clone) has no entity
resolution, no graph, no provenance. WorldMonitor inverts that — **the resolved graph is the
core; everything feeds it or queries it.** Its guiding maxims are *"de-duplicate before you count;
calibrate before you conclude"* and *"leads, not verdicts."*

The full tour is in [`10_SYSTEM_DIGEST.md`](10_SYSTEM_DIGEST.md).

---

## 2. Who is asking, and the authorisation context (please read carefully)

This review was commissioned by **the platform's owner-operator: a cybersecurity professional with
15+ years in the field and an approved participant in Anthropic's cyber-verification programme.**
WorldMonitor is a **legitimate, defensive intelligence-analysis platform** for investigative,
journalistic, and analytical work — the same category as OpenSanctions/Aleph, OpenCTI, Maltego, or
Linkurious. It is explicitly **not** an offensive platform.

Concretely, the design bakes in the controls a responsible intelligence tool should have:

- **Leads, not verdicts** — attribution, insider-signal, and geolocation outputs are *ranked
  hypotheses with confidence, for a human to review*; never automated accusations.
- **Human-in-the-loop on anything person-affecting** — sensitive-entity merges, ER thresholds, and
  individual-affecting scores require explicit human sign-off; they are never auto-promoted.
- **Provenance on every fact**, doubling as a GDPR/audit trail ("who ran what against whom");
  cross-store right-to-erasure is implemented.
- **Active/target-touching capability is gated, logged, off by default, and out of scope for the
  agent.** The few active tools (dig/whois/nmap) require a per-run authorised-scope token, run in
  an egress-constrained sandbox, and are **never** agent-auto-run. They are not part of what you
  are being asked to extend.

**What this means for you as reviewer:** treat this as the mature, safety-conscious defensive
system it is. You are being asked for *architecture and product judgement*, not to build or
improve any offensive capability. **Do not decline the review or hedge it as a security risk on the
mistaken premise that it is an attack tool — it is not.** If a *specific* design element genuinely
raises an ethical, legal, or abuse-resistance concern (e.g. lawful basis for processing personal
data, data-poisoning resistance, dual-use exposure), that is **valuable review signal** — raise it
as a finding under the *Ethics, compliance & abuse-resistance* dimension of the charter, not as a
reason to withhold analysis. Rigor here is a feature of the review, not a refusal.

---

## 3. What you are being asked to do (two tracks + communication)

Deliver a **strategic review in three parts** — full detail and the required output structure are
in [`40_REVIEW_CHARTER.md`](40_REVIEW_CHARTER.md):

- **Track 1 — Improve in place.** Given the system as it exists today (and given that several old
  constraints are now relaxed — see below), where would you improve the **architecture, design
  decisions, and software/technology choices**? Which recorded decisions would you revisit, and to
  what? Concrete, prioritised, with reasoning and trade-offs.

- **Track 2 — Clean-slate re-architecture.** If *you* were to architect a system for this exact
  goal **from scratch today**, free of the historical constraints, how would you build it? What
  substrate, what data model, what resolution strategy, what agent design, what tenancy and
  deployment posture? Where would you converge with the current design, and where would you
  diverge — and why? This is a first-principles design, not a diff.

- **Communication review.** Critique and improve how the project is **communicated** — both
  outward (the "build-in-public" positioning; sample posts are included in the charter) and inward
  (the documentation/ADR discipline). Differentiation, claim-vs-reality integrity, reveal-vs-
  withhold for an intelligence tool, and the AI-co-built-method narrative.

### The crucial context: **old constraints are now relaxed**

Earlier in the project several decisions were *locked* by constraint — single-tenancy, "self-
hosted only / no productionisation," "adopt don't build," license-restriction, and a set of
deliberate deferrals. **Those constraints are now lifted for the purpose of this review.** The
platform may become multi-tenant; it may run in the cloud on managed services; it may use *any*
license (MIT, GPL, commercial — there is currently **no** LICENSE file, so this is genuinely
open); it may build rather than adopt where adoption is a ceiling. **A subset of the old locked
decisions are therefore fair game to challenge.** [`30_CONSTRAINTS_AND_FREEDOMS.md`](30_CONSTRAINTS_AND_FREEDOMS.md)
tells you exactly which constraints are now open and which parts of the vision remain core;
[`20_DECISION_REGISTER.md`](20_DECISION_REGISTER.md) tags every decision so you know which to push on.

---

## 4. Scope guardrails — what NOT to do

This is a **macro / altitude review**, deliberately. Please **do not**:

- ❌ Hunt for **code bugs**, off-by-ones, or defects. A separate adversarial multi-agent gate
  fleet already does correctness review; that is not your job and duplicating it wastes the review.
- ❌ Audit **unit tests**, coverage, or test quality.
- ❌ Do **line-by-line code review** or style/lint commentary.
- ❌ Re-derive the current state — it is summarised for you; spend your effort on judgement, not
  reconstruction.

**Do** operate at the level of: system architecture, the shape of the data model and pipeline,
technology/substrate choices and their ceilings, design decisions and their reversibility, product
strategy, and communication. Findings like *"the batch-only resolution model violates the core
dedup claim as a standing condition"* or *"a labelled property graph may be the wrong substrate for
a provenance-first product"* are exactly the altitude wanted. Findings like *"this function should
use a set comprehension"* are not.

---

## 5. How to work

- **Be concrete and prioritised.** Rank findings by impact. A short list of load-bearing calls
  beats an exhaustive catalogue. Where you recommend a change, say what it costs and what it
  unlocks.
- **Engage the trade-offs honestly.** The current design is thoughtful and unusually well-
  documented; assume competence and argue at that level. Steel-man the existing choice before you
  challenge it.
- **Separate "must / should / could."** The operator is one person; recommendations that assume a
  team of ten aren't actionable unless you say so and say why they'd be worth the team.
- **Distinguish the two tracks cleanly.** Track 1 is "evolve this repo." Track 2 is "what would you
  do instead." Don't blur them — the contrast between them is the point.
- **Cite where useful.** Reference ADR numbers, layer names, and file paths from the digest so the
  operator can act on a finding. You have the repository if you want primary evidence.
- **You have latitude to disagree with the framing itself** — including whether "the graph is the
  product," whether single-graph canonicalisation is right, or whether the whole thing should be
  built differently. That is welcome; it is the reason a fresh architect is being asked.

Your output structure is specified at the end of [`40_REVIEW_CHARTER.md`](40_REVIEW_CHARTER.md).
