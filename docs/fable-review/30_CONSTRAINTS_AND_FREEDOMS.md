# 30 — Constraints & Freedoms

> This document draws the line the two review tracks turn on. WorldMonitor was built under a set of
> constraints, several of which are **now relaxed**. This tells you **what is newly open to
> challenge** and **what remains core to the vision**. When you propose changes (Track 1) or design
> from scratch (Track 2), treat the "now open" list as fair game and the "core" list as load-bearing
> — challengeable only with a strong, explicit argument.

---

## A. What remains CORE to the vision (challenge only with a strong argument)

These are the identity of the product. You *may* argue against them in Track 2 (clean-slate) — and
if you think one is wrong, say so plainly — but understand you are challenging the thesis itself,
not tuning a parameter.

| # | Core commitment | Why it's core |
|---|-----------------|---------------|
| C1 | **The resolved entity graph is the product.** Many sources → one canonical, deduplicated graph. | The entire thesis. Everything else is plumbing or consumption. |
| C2 | **Ontology-first: an open, standard schema is the contract** (currently FollowTheMoney 4.x + STIX 2.1). Producers emit it; consumers read it. | Prevents multi-domain sprawl; a new source/method is a plugin against the contract, not a rewrite. |
| C3 | **Provenance on every fact**, doubling as the GDPR/audit log. | Analytic (merge protection) + legal requirement; it is what makes the graph trustworthy. |
| C4 | **De-dupe before you count; calibrate before you conclude.** | The quality bar that separates this from a feed aggregator. |
| C5 | **Leads, not verdicts** — ranked hypotheses with confidence, human-reviewed; never automated accusation. | The ethical spine of an intelligence tool. |
| C6 | **Gated self-improvement, never silent** — propose → evaluate → gate → promote, versioned, rollback; **human sign-off for anything affecting a real person.** | The differentiator *and* the primary safety control. |
| C7 | **Open, plugin-extensible by construction** — connectors/mappers/resolvers/enrichers/rules/scorers/notifiers/tools are all addable/removable. | The "grows without rewrites" requirement. |

> Note: *how* each of these is realised is **not** core. "Ontology-first" is core; "FtM as the
> internal model rather than as an interchange format" is a **choice** you may revisit. "Provenance
> on everything" is core; "provenance stored as flat `prov_*` node properties" is a **choice**.
> Track 2 especially should separate the commitment from its current implementation.

---

## B. What is NOW OPEN (previously locked by constraint — challenge freely)

Each of these was a *reasonable decision under a constraint that no longer binds*. They are the
richest territory for both tracks. [`20_DECISION_REGISTER.md`](20_DECISION_REGISTER.md) tags the
specific ADRs; below is what each relaxation *opens up*.

### B1. Single-tenancy → multi-tenancy is back on the table
*Was:* locked single-tenant (ADR 0042 tore `tenant_id` out of ~110 call sites, 8 tables, the Neo4j
keys, and every predicate). *Now:* multi-org / SaaS is a permissible direction.
**Opens:** RLS on Postgres and Neo4j Enterprise multi-db (or per-tenant subgraphs) for isolation;
per-tenant identity/canonical-ID spaces (or a shared reference graph + per-tenant overlays);
tenant-scoped auth on every route; usage metering and billing; a managed cloud tier.
**Watch-out for the review:** re-introducing multi-tenancy is now a **fresh build against a deleted
reference implementation, not a revert** — a full data-shape migration. The reversal cost is real
and should be *quantified*, not assumed cheap. (This is itself a finding worth making: the teardown
maximised distance from multi-tenancy exactly as the constraint lifted.)

### B2. "Self-hosted only / no productionisation / no cloud" → managed & cloud allowed
*Was:* everything self-hosted on a single 64 GB WSL2/host box; production hardening deferred.
*Now:* cloud and managed substrates are permitted.
**Opens (removes whole categories of deferred work):** Neo4j Aura (HA + hot backup + RBAC);
managed Postgres (PITR, replicas); S3/R2 (versioning, object-lock, cross-region replication);
managed observability (Prometheus/Grafana/OTel/Loki); managed IdP (WorkOS/Auth0) with SSO + orgs;
serverless GPU for trajectory fine-tuning; a managed container sandbox replacing the sidecar;
K8s/Helm/GitOps and real DR (defined RTO/RPO) become buildable now rather than Phase-6-deferred.

### B3. License restriction → any license (there is currently no LICENSE at all)
*Was:* self-hosted, non-commercial-friendly choices only (e.g. the OS-Pairs benchmark is CC BY-NC).
*Now:* MIT, GPL/AGPL, commercial, open-core — all open; the repo ships **no** LICENSE file today, so
this is a genuinely blank choice.
**Opens:** commercial calibration corpora (OS-Pairs in a commercial path); Neo4j Enterprise/Bloom;
managed ER (Senzing, AWS Entity Resolution); proprietary enrichment/data feeds; frontier LLM APIs
as a *default* rather than a caveated mode; GPL/AGPL tooling stops being a distribution concern.
**New obligation it creates:** a source-by-source **data-licensing / redistribution audit** of the
~2,765-source inventory that "pull-only" currently lets the project defer.

### B4. "Adopt, don't build" → build where adoption is a ceiling
*Was:* mandated to adopt/depend/wrap the FtM ecosystem and Hermes; never fork-as-foundation.
*Now:* you may build the pieces the adopted stack fits poorly.
**Opens:** a custom incremental/streaming ER engine; a graph-native provenance/versioning model not
constrained by FtM's `merge_context` internals; a purpose-built agent loop tuned to the graph
contract instead of a general runtime.
**Caveat worth stating:** the *ecosystem leverage* of FtM/nomenklatura/OpenSanctions is still the
single highest-value adoption in the stack — build only where the adopted tool is a genuine ceiling
(incremental ER, statement-level provenance), not reflexively.

### B5. Batch-first resolution & the deferred-work backlog → the deferrals are now decisions to make
*Was:* a long, honestly-tagged backlog of "visible but deferred" seams — batch-only ER, cross-batch
dedup, Tier-1-only provenance, single-node driver, in-process task table, deferred GraphQL/graph
explorer, deferred fine-tuning and param/rule auto-tuning.
*Now:* with cloud/productionisation open, each of these is a live design choice, not a "later."
**Highest-leverage single item:** **incremental ER** (score new arrivals against the already-
resolved graph) closes the standing cross-batch dedup gap — the change with the largest effect on
the truth of the C1/C4 claims. See the digest and the strategic tensions.

### B6. LLM default-local / sovereignty-by-identity → sovereignty as an *option*, not the identity
*Was:* default-local Ollama, single auditable egress point; cloud LLM a caveated mode.
*Now:* frontier cloud models can be a first-class default.
**Opens:** dramatically higher ceiling on the Phase-4 fusion layer (multilingual news NLP, event
and relationship extraction, anomaly narratives — exactly where local models underperform);
LLM-assisted mapping/extraction in the *ingest* path, not just enrichment. The confidential selector
can be re-positioned from "local by identity" to "operator-chosen per-workload."
**The strategic fork to name:** keep the sovereignty brand and accept a weaker fusion layer, or make
frontier models first-class and re-position sovereignty as a supported mode. This is a genuine
product-identity decision, not just a config default.

---

## C. Still off the table (do not treat as open)

- **The safety invariants themselves** (C5, C6 above): human-in-the-loop for person-affecting
  changes, leads-not-verdicts, gated self-improvement. You may redesign *how* they are enforced;
  you may not propose removing them. (You *may* argue they are currently under-enforced — e.g. the
  merge guard sitting in "alert" mode — that is a valid and wanted finding.)
- **Turning the platform into an offensive tool.** Out of scope, by design and by request.

---

## D. How to use this in each track

- **Track 1 (improve in place):** prioritise the **Section B** relaxations that unlock the most
  value for the least cost against the *current* codebase, plus the "improvement-candidate" and
  "core-sensitive" decisions in the register that you'd tune. Respect that this is an evolution of a
  real repo maintained by one person.
- **Track 2 (clean slate):** start from **Section A** (the commitments) and design forward with *all*
  of Section B assumed free from day zero. This is where you decide substrate, data model, tenancy,
  and agent design without inheriting any historical choice. Explicitly mark where you'd re-converge
  with today's design and where you'd diverge.
