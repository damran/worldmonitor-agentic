# 40 — Roadmap

> `v0.4` · June 2026 · **Vertical slices, spine first, breadth/agent/UI later.** Each phase is
> end-to-end and testable. Don't start a phase before the prior one's acceptance criteria are green.
> Built by **Claude Code autonomously** (branch → PR → CI → merge), pausing only for questions / OPEN ADRs.

## Sequencing rationale
Binding order (Algorithms Sec 9.4): **ingestion+normalization → entity resolution → graph+analytics →
streaming anomaly → domain enrichers → fusion**. WorldMonitor is graph-native + ontology-first, so
Phase 1 proves the *spine* with one zero-risk source. The **API/MCP surface** comes next so the agent
layer has tools; **Hermes** connects after that; self-improvement is unlocked last.

---

## Phase 0 — Foundations
**Goal:** a clean, reproducible, secure, auth-gated skeleton.
- [ ] `uv` project + toolchain (Ruff, Pyright strict on `src/`, pytest+coverage, pre-commit incl. secret-scan).
- [ ] Repo layout (below); `.env.example`; `CLAUDE.md` (mirrored to `AGENTS.md`/`.clinerules`).
- [ ] **GitHub Actions** `quality` green + `security` (Trivy, CodeQL); **branch protection requires both** (enables safe autonomous merge).
- [ ] `deploy/compose.yaml` (core): **Neo4j+GDS, PostgreSQL(+pgvector), MinIO, Redis, Zitadel**; optional profiles for the rest.
- [ ] **Zitadel** configured: instance, org (= first tenant), admin user, OIDC apps for the API and for Hermes (service principal).
- [ ] FastAPI boots, **auth-gated (OIDC) + tenant-context middleware**, `/health` returns.
- [ ] `runner/` runs an async subprocess with **timeout + error handling** (base for `CliToolConnector`).
- [ ] No hardcoded secrets; everything on a feature branch.

**Done when:** `docker compose up` → a logged-in, empty, tenant-aware platform with green CI.

---

## Phase 1 — The spine: one source → ontology → ER → graph ★ CURRENT MILESTONE
**Goal:** prove `connector → ontology → resolution → graph → query` with **OpenSanctions** (FtM-native, free, zero-risk), with tests.
- [ ] **Ontology bootstrap:** FtM installed + schema validation; `followthemoney-graph` writes FtM → Neo4j with `tenant_id` + unique constraints on canonical IDs.
- [ ] **Plugin framework v0:** base interfaces + registry + `FtmBulkConnector` + provenance stamping + tenant-scoped instance table (`30`).
- [ ] **OpenSanctions connector:** manifest + schema + collect + (near-identity) map; raw → MinIO, candidates → ER queue.
- [ ] **Entity resolution v0:** **Splink** (DuckDB) + **nomenklatura** → canonical entities; **merge audit trail**; a size/sensitivity **review-queue threshold**.
- [ ] **Reference anchor:** load **GeoNames** + a Wikidata slice → canonical IDs on resolved entities.
- [ ] **Graph queries:** Cypher returns resolved entities + relationships + provenance; one **GDS** run (centrality/community) over a projection.
- [ ] **Tests:** unit (raw→FtM; ER merges right pairs, refuses bad — incl. a catastrophic-merge negative test); integration (queried-back graph = expected resolved, deduped, provenance-tagged).

**Done when:** "show this sanctioned entity, everyone linked to it, and where each fact came from" returns a correct, deduplicated, canonical-ID-anchored answer. **No second source until green.**

---

## Phase 2 — API/MCP surface + Integrations page + first live/stream connectors
**Goal:** expose the graph outward, and the flagship self-service surface.
- [ ] **API + MCP (`60`):** GraphQL/REST reads + a FastMCP server with `query_graph`/`get_entity`/`find_paths`/`enrich`; auth + tenancy + provenance-in-responses; guarded reads.
- [ ] **Integrations page (UI):** catalog from the registry (filterable), **schema-driven config forms**, save→vault→validate→enable, status/health. Seeded from the 67-sheet inventory.
- [ ] First **`RestApiConnector`** (e.g. OpenCorporates) + first **`StreamConnector`** (**Bluesky Jetstream**, `wantedDids` watchlist) + first **`FeedConnector`** (RSS+full-text → `wm:Article`/`wm:Event`).
- [ ] **`TelegramNotifier`** plugin (deterministic system alerts).
- [ ] Active-capability gating proven on one `CliToolConnector` (scope token + separate logging + sandbox).

**Done when:** you can add a source from the UI by filling a form and watch it collect into the graph; external workflows can query via MCP/GraphQL.

---

## Phase 3 — Agent layer (Hermes) connected
**Goal:** the self-improving assistant on top of the surface.
- [ ] **Hermes deployed** and connected to WorldMonitor's **MCP** as a service principal (read + run-passive).
- [ ] **LLM pluggability** verified — Hermes on Ollama and on OpenRouter (`hermes model`); **LiteLLM** wired for any service-side LLM use.
- [ ] **Scheduled reports** (Hermes cron → Telegram): a daily brief + "what changed about entity X" queries.
- [ ] Hermes' **learning loop (skills/memory)** on (lowest-risk improvement) — active-tool/graph-write skills still gated.

**Done when:** you can ask WorldMonitor questions from Telegram and receive scheduled briefings, driven by Hermes over the MCP tools.

---

## Phase 4 — Domain enrichers (plugins, one at a time)
Each an `INTERNAL_ENRICHMENT`/`Scorer` plugin (Algorithms sections in parens), with tests, writing provenance edges:
news/NLP & multilingual fusion (Sec 6: GDELT + dedup → NER/linking to Q-numbers → topic/narrative via LaBSE → sentiment) ·
crypto/fund-flow (Sec 2: clustering + taint; USDT-on-Tron) · CTI/infra (Sec 7: passive-DNS/cert/JARM-JA3; ingest STIX from OpenCTI/MISP feeds) ·
financial/trading (prediction-market insider signals, options flow, macro/geo indices) · geospatial/imagery & media forensics (Sec 4–5; GPU; latest).

---

## Phase 5 — Anomaly, fusion & forecasting (plugins)
Anomaly (Sec 3: IsolationForest/LOF + CUSUM/EWMA/BOCPD + coordinated-behaviour; streaming+batch) ·
fusion/scoring (Sec 8: transparent weighted first, then Bayesian; **calibration** before any score is surfaced) ·
forecasting/early-warning (GBMs on ACLED/GDELT labels; prediction-market odds as a leading feature).
**Rule:** every score ships with calibration and is a *lead*, not a verdict.

---

## Phase 6 — Self-improvement (gated) & scale
- **Param/rule auto-tuning** (`50` §4c): agents propose → evaluate → gate → promote, versioned; sensitive changes (ER thresholds, individual-affecting scores) human-gated; bounded auto-tune ranges.
- **Trajectory fine-tuning** (`50` §4b): batch on a GPU path (serverless/local — OPEN); promote a new model only if it beats the incumbent on a benchmark; rollback retained.
- **Scale/cloud:** managed containers/K8s, S3, **Neo4j Enterprise/Aura** (multi-tenant RBAC), Qdrant, durable task engine, full observability — when load demands.
- **UI beyond integrations:** graph explorer (Neo4j Bloom first; custom React later, Flowsint as reference), dashboards.

---

## Repository layout (Phase 0 scaffolds this)
```
worldmonitor/
├── CLAUDE.md  AGENTS.md  .clinerules        # agent ground truth (mirror; < 200 lines)
├── pyproject.toml  uv.lock  .python-version  .env.example  .pre-commit-config.yaml
├── docs/                                    # THIS plan
├── src/worldmonitor/
│   ├── api/               # FastAPI REST/GraphQL (auth-gated, tenant-scoped)
│   ├── mcp/               # FastMCP server (the MCP tool surface)
│   ├── authz/             # Zitadel/OIDC, RBAC, tenant context, capability gating
│   ├── ontology/          # FtM use, wm: extensions, STIX mapping, validation
│   ├── plugins/           # base interfaces + registry; connectors/ enrichers/ resolvers/ rules/ scorers/ notifiers/
│   ├── runner/            # async subprocess + timeout/sandbox; scheduler; stream consumers
│   ├── resolution/        # Splink + nomenklatura; merge audit; review queue
│   ├── graph/             # Neo4j + followthemoney-graph + GDS projections/queries
│   ├── provenance/        # the ledger (doubles as audit log)
│   ├── improvement/       # propose→evaluate→gate→promote; versioned artifacts; rollback
│   ├── llm/               # LiteLLM gateway for service-side LLM use
│   └── settings.py
├── tests/{unit,integration,contract,fixtures}/
├── deploy/{compose.yaml, compose.*.yaml, neo4j/, zitadel/}
├── scripts/{dev,seed_catalog.py,...}
└── vendor-repos/          # READ-ONLY reference clones, gitignored (ftm stack, hermes, flowsint, opencti, mcp lists)
```
*Hermes runs as its own process/container (or on a separate host), configured to reach the MCP server — it is not vendored into `src/`.*

---

## Decisions that gate the roadmap
Resolve the **OPEN** items in [`decisions/`](decisions/) *with the user* as each phase begins —
especially Phase-1 source (A), Integrations UI timing (E), where fine-tuning runs (new), and the
agents' safe auto-tune ranges (new).
