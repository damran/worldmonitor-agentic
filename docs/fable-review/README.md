# Fable 5 Architecture & Strategy Review — Reviewer Bundle

This folder is a **self-contained briefing pack** prepared for a strategic architecture and
design review of the **WorldMonitor** platform by the **Fable 5** model. It is not part of the
product; it exists to orient the reviewer and define the task.

> **You (the reviewer) do not need to read the whole repository to begin.** These documents
> summarise the system faithfully. Use the repo to go deeper where you want evidence; use this
> pack to know what you are looking at and what is being asked.

## Read in this order

| # | File | What it gives you |
|---|------|-------------------|
| 1 | [`00_FABLE_REVIEW_BRIEF.md`](00_FABLE_REVIEW_BRIEF.md) | Why this review exists, who commissioned it, the legitimacy context, the exact ask, and the scope guardrails. **Start here.** |
| 2 | [`10_SYSTEM_DIGEST.md`](10_SYSTEM_DIGEST.md) | A self-contained tour of what WorldMonitor is and how it is built — layers, the ontology contract, entity resolution, provenance, plugins, API/MCP, the agent layer, the stack, and current maturity. |
| 3 | [`20_DECISION_REGISTER.md`](20_DECISION_REGISTER.md) | All 93 recorded decisions distilled and **tagged** — which are now open to challenge, which are core, which touch real people. Raw material for "which decisions would you change?" |
| 4 | [`30_CONSTRAINTS_AND_FREEDOMS.md`](30_CONSTRAINTS_AND_FREEDOMS.md) | The explicit map of **what is now free to change** (multi-tenancy, cloud/productionisation, managed services, licensing, adopt-don't-build, deferrals) vs **what remains core to the vision**. |
| 5 | [`40_REVIEW_CHARTER.md`](40_REVIEW_CHARTER.md) | The two review tracks (improve-in-place / clean-slate re-architecture) plus the communication-plan review, each with dimensions and a required output structure. **This is your task spec.** |
| — | [`KICKOFF_PROMPT.md`](KICKOFF_PROMPT.md) | The prompt used to launch the review. (For the operator, not the reviewer.) |

## One-paragraph orientation

WorldMonitor is a self-hosted, **graph-native, ontology-first, plugin-extensible OSINT /
geopolitical-intelligence platform**. Its thesis is that *the resolved entity graph is the
product*: many heterogeneous open sources are fused into **one canonical entity graph with
provenance on every fact**, analysis runs on top, the graph is exposed through an **API + MCP
surface**, and a **gated self-improving agent layer (Hermes)** investigates and reports over it.
It is a **defensive intelligence-analysis tool** built by a vetted security professional, run
single-tenant, with strong human-in-the-loop and audit controls. See the Brief for the full
context and the ethical/authorisation framing.
