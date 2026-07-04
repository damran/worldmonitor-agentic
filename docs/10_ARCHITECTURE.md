# 10 — Architecture

> `v0.4` · June 2026 · Tech choices verified current as of June 2026 (re-verify versions at build time).

The master architecture: the **layers**, the **data-flow contract**, the **stack**, the **local→cloud**
story, and **security/auth**. Domain method-detail lives in the Algorithms Design Doc (Sec 1–9);
the ontology, plugin, agent, and API specs are siblings (`20`/`30`/`50`/`60`).

> **Tenancy (D1 / ADR 0042, supersedes ADR 0017):** WorldMonitor is **single-tenant** — one deployment,
> one tenant, no `tenant_id` on any row/node/edge. The multi-tenant claims below are **superseded**;
> they survive as historical context with the single-tenant reconciliation noted in place. A future
> managed-cloud tier may reintroduce per-tenant isolation (RLS / Neo4j Enterprise multi-db) as its own
> gate — that **door is left open**, but it is a deferred decision, not a current property.

---

## 1. The layered model

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│ L9  AGENT LAYER (Hermes) + UI   Hermes: user assistant (Telegram/CLI), scheduled   │
│                                 reports, autonomous investigation, self-improving. │
│                                 UI: Integrations page · graph explorer · dashboards│
├──────────────────────────────────────────────────────────────────────────────────┤
│ L8  API + MCP SURFACE           FastAPI REST/GraphQL + FastMCP server. The         │
│      (query / decision boundary) query+action contract: query_graph, get_entity,   │
│                                 find_paths, enrich, run_connector, list_alerts…    │
├──────────────────────────────────────────────────────────────────────────────────┤
│ L7  Fusion & forecast (plugins) Weighted / Bayesian / Dempster–Shafer · calibration│  Sec 8
│ L6  Anomaly & signals (plugins) point / time-series / coordinated · insider signals│  Sec 3
│ L5  Domain enrichers (plugins)  news/NLP · social · crypto · CTI/infra · geo/imagery│  Sec 2,4,5,6,7
├──────────────────────────────────────────────────────────────────────────────────┤
│ L4  Graph store & analytics     Neo4j (+ GDS) = derived property-graph projection   │  Sec 2
│                                 (SoR = Postgres statement log, ADR 0095) ·          │
│                                 centrality/community/paths/fund-flow · ref layers   │
│ L3  Entity resolution           Splink (DuckDB) + nomenklatura · canonical-ID       │  Sec 1
│                                 registry · clustering · catastrophic-merge guard    │
│ L2  Normalization & ONTOLOGY    raw → FtM/STIX entities+relations, canonical IDs,   │  ← the contract
│                                 provenance  (FtM schema-validated)   (see 20)       │
├──────────────────────────────────────────────────────────────────────────────────┤
│ L1  PLUGIN FRAMEWORK            Everything is a plugin: connectors · mappers ·       │  see 30
│      (ingestion + extension)    resolvers · enrichers · rules · scorers · notifiers │
│                                 · tools. Registry · manifest+config-schema · runner │
│                                 · Integrations page (catalog UI)                    │
├──────────────────────────────────────────────────────────────────────────────────┤
│ L0  Substrate                   Docker Compose (cloud-portable) · 12-factor · vault │
│                                 · CI/CD · dev (WSL2) vs always-on host              │
└──────────────────────────────────────────────────────────────────────────────────┘
  Cross-cutting (every layer): Provenance & audit · Auth (Zitadel; single-tenant, D1/ADR 0042) · Security
  (passive/active gating, hostile-input, OPSEC, GDPR) · Observability · LLM gateways
  (Hermes for agents, LiteLLM for services) · Self-improvement loop (gated)
```

**The one rule that makes a multi-domain, plugin platform coherent:** **L2 (the ontology) is the
contract.** Everything below it *produces* FtM/STIX entities-with-provenance; everything above
*consumes* the resolved graph. A new source/method is a new L1 plugin emitting/working on L2 objects —
no layer above it changes. This is what prevents Frankenstein sprawl.

---

## 2. The data-flow contract (end to end)

```
plugin: connector (L1) → raw + source metadata → mapper → FtM/STIX candidates w/ provenance (L2)
   → entity resolution (L3): resolve to canonical IDs, dedupe, cluster (merge audit)
   → upsert into the graph (L4)
   → enrichers/anomaly/fusion plugins (L5–L7) attach derived edges/attrs + scores
   → API + MCP (L8) expose query/action
   → Hermes agent layer + UI (L9) investigate, report (Telegram), and decide
   → self-improvement loop feeds outcomes back (gated) to params/rules/models
```

Invariants: **provenance threads the whole way**; **resolution is central (L3)**, never inside a
connector (that fragments the entity model); **self-modification is gated** (§6, and `50`).

---

## 3. The spine, reconciled (FtM ontology graph — not OpenCTI)

Confirmed: **graph-native with a custom ontology on a property graph.**
- **OpenCTI is demoted** to an optional upstream CTI *source* (ingest its STIX), not the system of record.
- **STIX 2.1 stays as a domain vocabulary**, mapped into the FtM graph by canonical ID. **CTI is just
  one plugin domain** — not special, not first.
- **Why FollowTheMoney:** the de-facto OSINT/fincrime ontology your tools already speak (OpenSanctions,
  OpenAleph), actively maintained (FtM 4.x, MIT), with Python/Java/TS bindings, an RDF/OWL spec, a
  maintained Neo4j bridge (`followthemoney-graph`), and an ER framework (`nomenklatura`). Adopt an
  ecosystem; don't invent a model.

---

## 4. The stack (current, verified June 2026)

### Core spine
| Concern | Choice | Why / notes | Alternative |
|---|---|---|---|
| Graph store | **Neo4j 2026.x Community + GDS** | Derived property-graph projection (statement log = SoR, ADR 0095; Neo4j live SoR in transition until F1 projector cutover); GDS Community includes the algorithms; Cypher; `graphdatascience` client. Single-tenant (D1/ADR 0042) — one graph, no per-tenant scoping; Enterprise RBAC/multi-db stays available *if* a future cloud-tier reintroduces multi-tenancy. | Memgraph; ArangoDB |
| Ontology / model | **FollowTheMoney 4.x** + **STIX 2.1** (CTI) | Maintained, MIT, ecosystem (nomenklatura/yente/ftmq/rigour). | Custom schema (rejected) |
| FtM↔graph | **followthemoney-graph** | Purpose-built FtM→Neo4j. | hand-rolled |
| Entity resolution | **Splink** (DuckDB) + **nomenklatura** | Unsupervised Fellegi–Sunter, ~1M rec/min on a laptop, scales to Spark; FtM-native merge. | Dedupe, recordlinkage |
| Canonical IDs | Wikidata Q / GeoNames / LEI / OpenCorporates / VIAF-ISNI / ISO-3166 | The anchor keys (see `20`). | — |

### Application, ingestion & agents
| Concern | Choice | Why / notes |
|---|---|---|
| Language / API | **Python 3.12+ · FastAPI**, stateless | Richest OSINT ecosystem; clean boundary over CLI tools. |
| Plugin framework | **Custom, declarative** (manifest + JSON-Schema config) | Sources/methods are heterogeneous + map to the ontology + gate active scans; ELT tools can't. (See `30`.) |
| **Agent layer** | **Hermes Agent (Nous Research, MIT)** — adopted | Self-improving loop, any-LLM, Telegram+cron, MCP client/server — already built. Connects to L8. (See `50`.) |
| **Service LLM gateway** | **LiteLLM** | Provider-agnostic for WorldMonitor's own LLM use (NLP enrichers etc.) — Ollama/OpenRouter/Anthropic swappable. |
| Tasks / scheduling | **`asyncio` + task table** → arq/Celery (Redis) when durability bites | Don't add Temporal/Celery before a plugin runs. |
| Streaming vs batch | **Lambda** — streaming monitors (Jetstream, GDELT 15-min, BGP), batch ER/fund-flow | Sec 9.1. |
| Relational store | **PostgreSQL** | App/plugin config, task rows, provenance ledger, versioned self-improvement artifacts. |
| Full-text / vector | **Postgres FTS + pgvector** → OpenSearch / Qdrant at scale | Save RAM early; graduate when news/embeddings volume justifies. |
| Object/raw store | **MinIO** (S3-compatible) | Raw landing zone, imagery, trajectories; S3-portable. |
| Embeddings/NLP | **LaBSE / sentence-transformers**, spaCy/GLiNER, XLM-R | Multilingual (LaBSE) so cross-language stories cluster as one. |

### Platform
| Concern | Choice | Why / notes |
|---|---|---|
| Packaging | **Docker Compose** (core + optional profiles) | Cloud-portable; profiles toggle heavy services. |
| Auth / identity | **Zitadel** (self-hosted) | OIDC for the single tenant (D1/ADR 0042); its Instance→Org→Project→App model can later carry multi-tenancy *if* a cloud-tier reintroduces it, but that is unused now. Docker-deployable. *Caveats:* AGPLv3; back up DB before major upgrades. (Alt: Keycloak.) |
| Secrets | **`.env` (gitignored)** now → **SOPS+age** when config enters repo | Real store, never plaintext-committed. |
| Deps · lint · types · tests · CI | **uv · Ruff · Pyright · pytest · GitHub Actions** (`quality` + `security`: Trivy, CodeQL; OIDC) | The whole baseline; everything else deferred. |
| Observability | **Structured logs** now → OTel + Prometheus + Loki later | Don't build the full stack around nothing. |

---

## 5. Local profile (64 GB) and the cloud path

**Local (now).** 64 GB comfortably runs Neo4j (bounded heap+pagecache, ~16–24 GB), Postgres (+pgvector),
MinIO, Redis, Zitadel, the FastAPI app, and Splink/DuckDB jobs (memory-frugal, frees between runs).
**Compose profiles** keep heavy/optional services (OpenSearch, GPU CV, imagery) down until needed.
Hermes runs locally too (or on a cheap VPS / serverless backend — it supports local/Docker/SSH/Modal/Daytona).

**Runtime split (the WSL answer).** Dev on **WSL2 Ubuntu** (git/gh authenticated — autonomous Claude
Code runs here). Run the **always-on** stack (graph + streaming + scheduler) on something that stays up —
a persistent Linux host (NUC/VPS) — and code against it. For an MVP you *can* keep it all on the one
machine (WSL2 + systemd, sleep off, Docker autostart), but treat that as temporary.

**Cloud (later).** Containers + 12-factor + S3 + OIDC + LiteLLM means the move is a **deploy-target
change**: Compose → managed containers/K8s; MinIO → S3; pgvector → Qdrant; GPU fine-tuning → a GPU
service. No app rewrite. **Neo4j Community → Enterprise/Aura** stays available as a scaling option, and
is the path a future **multi-tenant cloud-tier** would take for per-tenant RBAC + multi-db — but
multi-tenancy is a deferred decision (D1/ADR 0042 keeps the system single-tenant; that door is left open,
not walked through).

---

## 6. Security, auth & self-improvement guardrails

- **Auth from commit zero** — all app/API/MCP access behind **Zitadel** (OIDC). The system is
  **single-tenant** (D1/ADR 0042, supersedes ADR 0017): one tenant, no `tenant_id` on any
  row/node/edge. Roles: read / run-passive / run-active / admin.
- **Graph isolation (deferred, not current):** under single-tenancy there is one graph and no
  per-tenant scoping to enforce. A future managed-cloud tier that reintroduces multi-tenancy would
  enforce isolation via Neo4j Enterprise/Aura (per-tenant RBAC / multi-db) as its own gate — the door
  is left open, but app-layer `tenant_id` scoping (the ADR 0017 approach) was torn out by ADR 0042.
- **Passive vs active gating** — every plugin declares `capability: passive | active`. Active modules
  require an **authorized-scope token per run**, separate logging, and are **never agent-auto-run**
  without a human in the loop.
- **Hostile input is the default** — scraped/dark-web/tool/API output parsed in isolation; never
  `eval`/shell-interpolated; heavy CLI tools in **containers with constrained egress**. Agent/MCP
  inputs validated before execution (prompt-injection mitigation). MCP stdio → **all logs to stderr**.
- **OPSEC** — route active recon through controlled proxies; isolate credentials per plugin in the vault.
- **GDPR as process (EU)** — for people/social/breach workflows document purpose limitation, data
  minimization, storage limitation, TOMs; assess the **DPIA threshold** and **third-country transfer**
  for any external API / hosted service. The provenance ledger doubles as the "who ran what against whom" audit log.
- **Self-improvement guardrails (the riskiest subsystem — see `50`):** the agent layer may improve
  three things — its own skills/memory (Hermes loop), its model (fine-tune from trajectories), and
  **WorldMonitor's params/rules**. **Nothing self-modifies silently.** Every change is
  **propose → evaluate (held-out metrics) → gate (auto-promote only if it beats baseline on
  calibration/accuracy + passes safety checks; human sign-off for sensitive ones) → promote
  (versioned, instant rollback)**, fully audited. Changes affecting a real person (ER thresholds,
  scoring that flags individuals) always require human approval.
- **Autonomous build guardrail** — Claude Code self-merges only on **green CI**; branch protection
  requires the `quality` + `security` checks. CI is the safety net for "merge its own PR."
- **Cross-model review** — periodically have a second model red-team the agents/adapters for injection,
  secret leakage, missing validation.

---

## 7. Reference architectures (study; adopt/wrap/borrow; never fork-as-foundation)

| Source | Take | Avoid |
|---|---|---|
| **Hermes Agent** (Nous, MIT) | **Adopt** as the agent layer (self-improvement, any-LLM, Telegram/cron, MCP) | Re-implementing an agent runtime |
| **FollowTheMoney stack** (ftm/nomenklatura/yente/ftmq/rigour/memorious) | **Depend on** — ontology, ER, matching API, scraping | Forking OpenAleph as the app |
| **Flowsint** (`reconurge/flowsint`) | The enricher pattern (app→api→core→enrichers); Neo4j+FastAPI shape | Building on it (early; ER/STIX only planned) |
| **OpenCTI** | Connector taxonomy (EXTERNAL_IMPORT/INTERNAL_ENRICHMENT), STIX patterns | Adopting as system of record |
| **OSINT MCP servers** (soxoj list; BurtTheCoder; zoomeye) | **Wrap** as connectors/enrichers | Re-implementing tools |
| **Published Neo4j-OSINT builds** (47M-edge case study; Sandia paper) | Graph schema & enrichment lessons | — |

## 8. Explicitly deferred (do NOT build until unlocked)
Breadth of plugins beyond the current slice · Temporal/Celery · full observability stack · IaC/K8s/Helm/GitOps ·
SBOM/signing · GPU CV & satellite ingestion · model fine-tuning infra (until the spine + agent loop exist) ·
UI beyond the current phase (graph exploration can start with Neo4j Bloom).
