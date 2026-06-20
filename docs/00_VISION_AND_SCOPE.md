# 00 — Vision & Scope

> `v0.4` · June 2026

## What WorldMonitor is

A self-hosted, **graph-native, ontology-first, plugin-extensible** OSINT and geopolitical-intelligence
platform. It fuses many heterogeneous sources — news, social, corporate/financial, crypto, sanctions,
infrastructure/CTI, geospatial — into **one resolved entity graph** with provenance on every fact,
runs analysis on top (graph, anomaly, geospatial, fusion/forecasting), **exposes that graph through an
API + MCP surface**, and is operated by a **self-improving agent layer** (Hermes) that investigates,
reports, and gets better over time.

It serves investigative, journalistic, and analytical intelligence work. It is **not** a single-purpose
blue-team tool; CTI is just one plugin domain among many.

## The thesis (why graph-native)

> **Relationships are the product.** OSINT is a graph-traversal problem: value lives in how entities
> connect across sources, not in any one datapoint. A monitoring *dashboard* (the typical clone) has
> no entity resolution, no graph, no provenance — it shows feeds but can't answer "who connects to
> whom, and how." WorldMonitor inverts that: the resolved graph is the core; everything feeds or queries it.

## The second thesis (why plugin-extensible)

> **The system must grow without rewrites.** New sources, new algorithms, new rules, new research must
> drop in as **plugins** and be removable just as cleanly. This is achievable because one rule holds:
> **the ontology (L2) is the contract** — everything below it *produces* ontology objects with
> provenance; everything above *consumes* the resolved graph. A new capability plugs into that contract
> and never ripples through the rest of the system.

## Governing principles (these constrain every design decision)

1. **Resolve everything to stable canonical IDs** (Wikidata Q, GeoNames, LEI, OpenCorporates,
   VIAF/ISNI, ISO-3166). The same entity from a news article and a reference source lands on one node.
2. **The hard 80% is data plumbing**, not algorithms — ingestion, normalization, dedup, entity
   resolution, provenance. Tooling is the easy 20%.
3. **Provenance on every datapoint** — where it came from, how reliable. Analytic (merge protection) +
   legal (GDPR audit) requirement.
4. **De-duplicate before you count.** **Calibrate before you conclude.**
5. **Leads, not verdicts** — attribution / insider-signal / geolocation outputs are ranked hypotheses
   with confidence, human-reviewed. Never asserted as truth.
6. **Open by construction** — connectors, enrichers, resolvers, rules, scorers, notifiers, and tools
   are all plugins behind typed interfaces; addable, removable, status-tagged.
7. **Self-improvement is gated, never silent** — every agent-driven change to the live system (params,
   rules, models) goes propose → evaluate → gate → promote, versioned, with rollback and audit.

## In scope (the architecture covers all of this; the roadmap sequences it)

- **Reference / encyclopedic base layers** — Wikidata, GeoNames, GLEIF/LEI, OpenCorporates,
  OpenSanctions, World Bank/IMF, ACLED, DBpedia (the entity-resolution backbone).
- **Entity resolution & the canonical graph** — the spine.
- **Domains as plugins** — news & multilingual monitoring (GDELT anchor + RSS + full-text);
  social (Bluesky/Telegram/YouTube/Reddit); CTI/infrastructure (STIX); crypto/fund-flow;
  financial/trading (prediction-market insider signals, options flow, macro/geo-risk); geospatial &
  imagery; media forensics.
- **Anomaly detection, fusion, scoring & forecasting** — the convergence layer.
- **A self-service Integrations page** — the plugin catalog that makes sources addable by the user.
- **An API + MCP surface** — so external workflows can query the graph and make decisions.
- **A self-improving agent layer (Hermes)** — user-facing assistant, scheduled Telegram reports,
  autonomous investigation, learning loop + trajectory fine-tuning.

> Per-method detail is the **Algorithms Design Doc** (Sec 1–9) and the **OSINT Tool Inventory**
> workbook. This plan references them rather than restating them.

## How it is built and run

- **Built** by **Claude Code operating autonomously** (branch → PR → CI → merge via the authenticated
  git/gh clients), doing web research and using any tool, pausing only for genuine questions.
- **Run** solo now on a 64 GB Ubuntu-on-WSL2 machine, **designed for multi-tenant SaaS from day one**
  (Zitadel auth/tenancy), containerized and cloud-portable so the move to cloud is a deploy change.

## Out of scope / non-goals (for now)

- **Not** an offensive platform — active/target-touching plugins exist but are gated and logged; default is passive.
- **Not** "merge N cloned repos." Existing OSS (FollowTheMoney stack, Hermes, OpenCTI, OSINT MCP
  servers) is **adopted, depended on, or wrapped — never forked as foundation**.
- **Not** breadth before the spine works. One resolved end-to-end slice beats ten half-wired sources.
- **No** unevaluated self-modification — see principle 7.
