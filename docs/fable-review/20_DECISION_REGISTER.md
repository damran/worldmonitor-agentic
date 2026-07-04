# 20 — Decision Register (classified for review)

> WorldMonitor records **93 numbered decisions**: **15 foundational locked decisions** (#1–15,
> captured in the ADR index `docs/decisions/README.md`) plus **78 detailed ADRs** (0016–0093, one
> file each, in Context → Decision → Status → Consequences form). This register distils all of them
> and **tags each** so you can see at a glance which are open to challenge, which are core, and which
> touch real people. It is the raw material for the charter's *"which decisions would you change?"*
>
> The project already classifies decisions by **reversibility** and pauses a human only for
> irreversible / person-affecting ones — so the tags below are its own discipline, surfaced for you.

## How to read the tags

- **Sens ●** — *person-affecting*: the decision touches a real person (ER thresholds, merge/dedupe of
  people, individual-affecting scores, erasure). By the project's own rule these are **never
  auto-changed** and always need human sign-off. Challenge them only with a strong, explicit argument,
  and treat the safety intent as fixed even if you'd change the mechanism.
- **Reversibility** — *reversible* (pick a default, cheap to change), *costly* (data-shape or
  broad-surface lock-in), *IRREVERSIBLE* (deletion / one-way).
- **🔓 Now-open** — this decision was made under a constraint that is **now relaxed** (single-tenancy,
  self-hosted-only, no-cloud, license-restriction, adopt-don't-build, or a deliberate deferral). These
  are the **richest territory** for both review tracks. See [`30_CONSTRAINTS_AND_FREEDOMS.md`](30_CONSTRAINTS_AND_FREEDOMS.md).
- **Suggested review lens** — a hint, not a constraint: *Now-open — challenge freely* /
  *Core-sensitive — strong argument only* / *Improvement candidate*.

---

## The 15 foundational locked decisions (#1–15)

These are the identity-level commitments (full text in `docs/decisions/README.md`). Several map
directly to the "core vision" in the Constraints & Freedoms doc; a few are **now-open** because the
constraint that locked them has lifted (flagged).

| # | Foundational decision | Note for review |
|---|-----------------------|-----------------|
| 1 | Graph-native, ontology-first — the resolved graph is the product | **Core** (C1). Track 2 may challenge the *substrate*, not the thesis. |
| 2 | Property graph (Neo4j + GDS) as system of record | Choice, not thesis — **now-open** substrate (RDF-star? Enterprise?). |
| 3 | FollowTheMoney 4.x + STIX 2.1 + `wm:` as the ontology | **Core** contract (C2); *how* it's the internal model is open. |
| 4 | ER = Splink + nomenklatura, central at L3, never in connectors | **Core** placement (C4); the *engine* is open. |
| 5 | OpenCTI demoted to a source; CTI is one plugin domain | **Core** (follows from graph-native). |
| 6 | Open plugin framework (all kinds addable/removable) | **Core** (C7). |
| 7 | Custom declarative connector model (manifest + JSON-Schema), not Airbyte/Meltano | Improvement candidate. |
| 8 | Python 3.12+ / FastAPI, stateless | **Now-open** (greenfield language/runtime choice). |
| 9 | API + MCP surface as the query/decision boundary | **Core** (C1/consumption). |
| 10 | Adopt Hermes (MIT) as the agent layer | **Now-open** (adopt-don't-build relaxed; ADR 0089). |
| 11 | LLM pluggable — Hermes agent-side, LiteLLM service-side | **Now-open** (sovereignty posture; ADR 0091). |
| 12 | Telegram outbound (Hermes reports + a deterministic notifier) | Improvement candidate. |
| 13 | Self-improvement = all three mechanisms, fully gated | **Core** safety (C6); mechanisms unbuilt. |
| 14 | Auth/tenancy SaaS-grade via Zitadel, single-node now | **Now-open** (multi-tenancy back on the table; ADR 0042). |
| 15 | Containerized + 12-factor + S3-compatible | **Now-open** (cloud/managed substrates allowed). |

---

## The 78 detailed ADRs (0016–0093)

Legend: **Sens ●** = person-affecting · **🔓** = now-open (constraint relaxed).

| ADR | Decision | Sens | Reversibility | Now-open | Suggested review lens |
|---|---|:---:|---|:---:|---|
| 0016 | Splink ER model: expert-set weights (v0), EM-trained later | ● | reversible |  | Core-sensitive — strong argument only |
| 0017 | Tenant isolation: app-layer composite keys, not per-tenant DB |  | costly | 🔓 | Now-open — challenge freely |
| 0018 | Provenance stored as flat FtM-context keys, projected to no… |  | costly |  | Improvement candidate |
| 0019 | Entity resolution: periodic re-batch for streaming (increme… | ● | reversible | 🔓 | Now-open — challenge freely |
| 0020 | Catastrophic-merge guard: hardcoded conservative thresholds… | ● | reversible |  | Core-sensitive — strong argument only |
| 0021 | Raw record lands in object storage before mapping/enqueue |  | reversible |  | Improvement candidate |
| 0022 | Connector output: strict FtM validation (fail-loud), not dr… |  | reversible |  | Improvement candidate |
| 0023 | Resolved-graph edge materialization: accepted v0 limitations |  | costly |  | Improvement candidate |
| 0024 | Catastrophic-merge guard: alert-only mode for the build phase | ● | reversible |  | Core-sensitive — strong argument only |
| 0025 | Referent rewriting: redirect merged-away IDs to canonical b… |  | reversible |  | Improvement candidate |
| 0026 | Resolution: batch-first, drain the queue in bounded windows | ● | reversible | 🔓 | Now-open — challenge freely |
| 0027 | run_ingest: windowed commits, bounded collection, dead-lett… |  | reversible |  | Improvement candidate |
| 0028 | Per-batch resolver isolation (G4 fix) |  | reversible |  | Improvement candidate |
| 0029 | Long-running ingest driver (ER-streaming Gate A) |  | reversible | 🔓 | Now-open — challenge freely |
| 0030 | Alembic schema migrations (replace create_all) |  | reversible |  | Improvement candidate |
| 0031 | Return-to-block + human sign-off for parked merges | ● | reversible |  | Core-sensitive — strong argument only |
| 0032 | Cheap hardening + audit follow-ups |  | reversible |  | Improvement candidate |
| 0033 | Neo4j bounded memory + pinned GDS-compatible image |  | reversible | 🔓 | Now-open — challenge freely |
| 0034 | Neo4j compose auth fix + a compose-boot CI guard |  | reversible |  | Improvement candidate |
| 0035 | Multi-script name canonicalization (fingerprint name projec… | ● | reversible |  | Core-sensitive — strong argument only |
| 0036 | Deterministic canonical id + cross-store commit ordering (a… |  | reversible |  | Improvement candidate |
| 0037 | Transitive enforcement of negative (reject) judgements (aud… | ● | reversible |  | Core-sensitive — strong argument only |
| 0038 | Per-stage exception isolation in _resolve_batch (audit B-2) |  | reversible |  | Improvement candidate |
| 0039 | ER distinguishing evidence: registration-number discriminat… | ● | reversible |  | Core-sensitive — strong argument only |
| 0040 | ER anchor-conflict + identifier-override negative evidence | ● | reversible |  | Core-sensitive — strong argument only |
| 0041 | Resolution / sign-off integrity |  | reversible |  | Improvement candidate |
| 0042 | Single-tenancy teardown: remove tenant_id (supersedes ADR 0… |  | costly | 🔓 | Now-open — challenge freely |
| 0043 | ER measurement harness + EM weights (extends ADR 0016) | ● | reversible |  | Core-sensitive — strong argument only |
| 0044 | Anchor-preferred stable canonical IDs + canonical-alias led… | ● | costly |  | Core-sensitive — strong argument only |
| 0045 | Value-level (per-claim) provenance: StatementEntity fusion … | ● | costly |  | Core-sensitive — strong argument only |
| 0046 | Abstract Thing-range edge materialization (thin ftmg overri… |  | reversible |  | Improvement candidate |
| 0047 | Fail-closed sensitivity guard (deny-by-default; topics → gr… | ● | reversible |  | Core-sensitive — strong argument only |
| 0048 | FtM-valid, injective durable canonical ID (Gate CID-fix) | ● | costly |  | Core-sensitive — strong argument only |
| 0049 | Cross-store GDPR source erasure (erase_source) | ● | IRREVERSIBLE |  | Core-sensitive — strong argument only |
| 0050 | Backup / restore disaster recovery (Gate B-4b) |  | reversible |  | Improvement candidate |
| 0051 | Driver supervision, containerization & a real readiness sur… |  | reversible |  | Improvement candidate |
| 0052 | GeoNames connector: bounded streaming + fail-closed local-p… |  | reversible |  | Improvement candidate |
| 0053 | Dead-letter (ingest_dead_letter) retention / pruning |  | reversible |  | Improvement candidate |
| 0054 | Driver connector retry with exponential backoff |  | reversible |  | Improvement candidate |
| 0055 | Fail-closed edge provenance (no silently-unprovenanced edges) |  | reversible |  | Improvement candidate |
| 0056 | Migration adoption requires a complete schema check (no bli… |  | reversible |  | Improvement candidate |
| 0057 | SSRF-guarded outbound HTTP for connectors |  | reversible |  | Improvement candidate |
| 0058 | ConfigCipher key rotation via MultiFernet |  | reversible |  | Improvement candidate |
| 0059 | Surface driver-heartbeat freshness on /ready (non-fatal) |  | reversible |  | Improvement candidate |
| 0060 | Node provenance integrity: additive re-emit + fail-closed n… | ● | reversible |  | Core-sensitive — strong argument only |
| 0061 | Production secret hygiene: loopback-bound stores + fail-clo… |  | reversible |  | Improvement candidate |
| 0062 | Phase-2 graph-read REST API (slice 2a) |  | reversible | 🔓 | Now-open — challenge freely |
| 0063 | Phase-2 graph-read FastMCP server (slice 2b) |  | reversible | 🔓 | Now-open — challenge freely |
| 0064 | Result-count bound on get_neighbors (read-surface hardening) |  | reversible |  | Improvement candidate |
| 0065 | RestApiConnector base + OpenCorporates connector (Phase-2 S… |  | reversible |  | Improvement candidate |
| 0066 | FeedConnector (RSS/Atom → FtM Article) (Phase-2 Stage-3 sli… |  | reversible |  | Improvement candidate |
| 0067 | Notifier plugin type + TelegramNotifier (Phase-2 Stage-3 sl… |  | reversible |  | Improvement candidate |
| 0068 | Browser session auth: Zitadel OIDC login + dual-path AuthMi… |  | costly | 🔓 | Now-open — challenge freely |
| 0069 | Integrations UI: HTMX + Jinja2 catalog + schema-driven conf… |  | reversible | 🔓 | Now-open — challenge freely |
| 0070 | StreamConnector (Bluesky Jetstream) + G8 cursor / long-runn… |  | costly |  | Improvement candidate |
| 0071 | ACTIVE-capability gating: scope token + operator-run path +… |  | reversible |  | Improvement candidate |
| 0072 | CliTool dig + nmap (sandbox-gated) + Run-UI + enforced allo… |  | reversible |  | Improvement candidate |
| 0073 | Abjad (Arabic/Persian) name normalization in the ER fingerp… | ● | reversible |  | Core-sensitive — strong argument only |
| 0074 | Auto-hard-disable a connector instance after N consecutive … |  | reversible |  | Improvement candidate |
| 0075 | Periodic maintenance cadence + resolve wall-clock timeout +… |  | reversible |  | Improvement candidate |
| 0076 | Prometheus /metrics exporter on the driver process |  | reversible |  | Improvement candidate |
| 0077 | Sandbox-runner sidecar for heavy ACTIVE CLI tools |  | reversible | 🔓 | Now-open — challenge freely |
| 0078 | Prometheus scrape job + alert rules for the driver /metrics… |  | reversible | 🔓 | Now-open — challenge freely |
| 0079 | Canonical-anchor silver labels for the ER measurement harness | ● | reversible |  | Core-sensitive — strong argument only |
| 0080 | External-benchmark floor for the ER measurement harness | ● | reversible | 🔓 | Now-open — challenge freely |
| 0081 | Optional headers extension to guarded_stream + Wikidata thr… |  | reversible |  | Improvement candidate |
| 0082 | Wildcard-subdomain entries in allowed_targets allowlist |  | reversible |  | Improvement candidate |
| 0083 | Landing-zone orphan GC (audit finding M-6) |  | reversible |  | Improvement candidate |
| 0084 | Online-migration safety (audit finding M-5) |  | reversible |  | Improvement candidate |
| 0085 | Silver-anchor tiering and contradiction fix for ADR 0079 | ● | reversible |  | Core-sensitive — strong argument only |
| 0086 | Landing GC safety (Gate B: grace-window guard, reference-se… |  | reversible |  | Improvement candidate |
| 0087 | guarded_stream cross-host header strip (Gate C: G-NET-1) |  | reversible |  | Improvement candidate |
| 0088 | promtool in CI (Gate D) |  | reversible |  | Improvement candidate |
| 0089 | Hermes agent layer adoption (Phase 3 plan) |  | reversible | 🔓 | Now-open — challenge freely |
| 0090 | Authenticated streamable-HTTP transport for remote Hermes MCP |  | reversible | 🔓 | Now-open — challenge freely |
| 0091 | LiteLLM gateway + three-mode confidential selector |  | reversible | 🔓 | Now-open — challenge freely |
| 0092 | OpenAI-compatible /v1/chat/completions HTTP endpoint over t… |  | reversible | 🔓 | Now-open — challenge freely |
| 0093 | Hermes agent + MCP HTTP server compose services (Phase 3 S3… |  | reversible | 🔓 | Now-open — challenge freely |

---

## The 18 "now-open" decisions, called out

These are where the relaxed constraints bite hardest — prioritise them:

- **Tenancy & identity space:** `0017` (app-layer tenant isolation), `0042` (single-tenancy teardown
  — the flagship; re-introducing multi-tenancy is now a *fresh build against a deleted reference
  implementation*, and its reversal cost should be quantified, not assumed cheap).
- **Resolution model & the driver:** `0019`/`0026` (batch-first resolution), `0029` (single-node
  ingest driver), `0033` (Neo4j bounded memory / single-box sizing). Incremental ER is the highest-
  leverage change now that cloud/HA is permitted.
- **Read surface:** `0062`/`0063` (bounded REST + 4-tool MCP), `0069` (HTMX config UI) — a bounded
  fixed surface vs the open-ended relationship queries a "graph is the product" analyst needs.
- **Sandbox / ops / observability:** `0068` (browser auth), `0077` (sandbox sidecar), `0078`
  (Prometheus) — managed substrates change these materially.
- **ER labels:** `0080` (external benchmark floor — the OS-Pairs corpus is CC BY-NC; commercial use
  now permitted changes the licensing calculus).
- **The whole agent/LLM stack:** `0089` (adopt Hermes), `0090` (MCP HTTP transport), `0091` (LiteLLM +
  confidential selector), `0092` (`/v1` shim), `0093` (Hermes deploy) — all reversible at low cost
  (compose config), all worth re-examining now that frontier cloud models and build-vs-adopt are open.

## The person-affecting decisions (● — the safety core)

21 decisions touch real people — the ER engine and its guards: `0016`, `0019`, `0020`, `0024`, `0026`,
`0031`, `0035`, `0037`, `0039`, `0040`, `0043`, `0044`, `0045`, `0047`, `0048`, `0049`, `0060`, `0073`,
`0079`, `0080`, `0085`. **`0049` (cross-store GDPR erasure) is the single IRREVERSIBLE decision** —
it deletes data. When your review touches any of these, keep the safety intent fixed (human sign-off,
leads-not-verdicts) even where you'd redesign the mechanism, and say so explicitly.

## How to use this register

- **Track 1:** work the **🔓 now-open** rows first (biggest value from the relaxed constraints), then
  the *improvement-candidate* rows you'd tune. For each, say: keep / change-to-what / cost / what it
  unlocks.
- **Track 2:** ignore the rows as *decisions* and treat them as a **menu of forks** — for each place
  the current design made a call (batch vs incremental ER, LPG vs triple store, adopt vs build the
  agent, single- vs multi-tenant, local- vs frontier-LLM), decide what *you'd* do from first
  principles, and note where you re-converge with the current answer.
