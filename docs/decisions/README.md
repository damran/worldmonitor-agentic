# Architecture Decision Records

> `v0.4` · June 2026 · What's **LOCKED** (with why) and what's **OPEN** (needs the user before it's
> built). To change a LOCKED decision, **supersede** it with a new ADR — never silently rewrite.
> Format per ADR: Context → Decision → Status → Consequences.

## Locked decisions

| # | Decision | Status | Why |
|---|----------|--------|-----|
| 1 | **Graph-native, ontology-first** (the resolved graph is the product) | LOCKED | OSINT is a graph-traversal problem. |
| 2 | **Property graph (Neo4j + GDS)** as system of record | LOCKED | Best analytics + ecosystem; RDF/OWL reasoning not needed yet. |
| 3 | **FollowTheMoney 4.x core ontology** + STIX 2.1 (CTI) + `wm:` extensions | LOCKED | Maintained, MIT, the model your tools speak; ships ER + graph bridge. |
| 4 | **ER = Splink (DuckDB) + nomenklatura**, central (L3), never in connectors | LOCKED | Unsupervised, laptop-fast, FtM-native; per-connector dedup fragments the model. |
| 5 | **OpenCTI demoted** to optional upstream CTI source; **CTI is just one plugin domain** | LOCKED | Follows from graph-native + max-expandability. |
| 6 | **Open plugin framework** — connectors/mappers/resolvers/enrichers/rules/scorers/notifiers/tools, all addable/removable | LOCKED | "Plugins, rules, algorithms, research easily addable & removable." |
| 7 | **Custom declarative connector model** (manifest + JSON-schema forms), not Airbyte/Meltano | LOCKED | Sources are heterogeneous + map to the ontology + gate active; ELT can't. |
| 8 | **Python 3.12+ / FastAPI**, stateless | LOCKED | Richest OSINT ecosystem; clean boundary over CLI tools. |
| 9 | **API + MCP surface** (FastAPI REST/GraphQL + FastMCP) as the query/decision boundary | LOCKED | External workflows + Hermes query/act through one contract. |
| 10 | **Adopt Hermes Agent (MIT) as the agent layer** (don't build a custom runtime) | LOCKED | Self-improving loop + any-LLM + Telegram/cron + MCP already built. |
| 11 | **LLM pluggable** — Hermes (`hermes model`) agent-side; **LiteLLM** service-side | LOCKED | Ollama/OpenRouter/Anthropic swappable everywhere. |
| 12 | **Telegram** outbound: Hermes (rich reports) + a `TelegramNotifier` plugin (deterministic alerts) | LOCKED | Reports/notifications; alerts survive agent downtime. |
| 13 | **Self-improvement = all three** (Hermes loop + model fine-tune + param/rule tuning), **fully gated** | LOCKED | User chose "all"; nothing self-modifies silently (propose→evaluate→gate→promote, versioned, rollback, audit). |
| 14 | **Auth/tenancy SaaS-grade from day one via Zitadel**, single-node deploy now | LOCKED | Solo now, cloud later; org model = tenants. |
| 15 | **Containerized + 12-factor + S3-compatible** | LOCKED | Portable + reproducible; dev on WSL2, always-on stack on a persistent host. |

## Phase 1 decisions (recorded from the audit)

> Decisions surfaced by the Phase 1 audit (`docs/reviews/PHASE_1_AUDIT.md`). Unlike #1–15, each has a
> detailed file (`docs/decisions/00NN-*.md`) in Context → Decision → Status → Consequences format.

| # | Decision | Status | Why |
|---|----------|--------|-----|
| 16 | **Splink ER model: expert-set weights (v0), EM-trained later** ([0016](0016-splink-expert-set-weights.md)) | LOCKED (v0) | One source, no labels; transparent + reproducible now, EM is a gated upgrade. |
| 17 | **Tenant isolation: app-layer composite keys, not per-tenant DB** ([0017](0017-app-layer-tenant-isolation.md)) | LOCKED | Neo4j Community has no per-tenant RBAC; `tenant_id` in MERGE key + composite `(tenant_id, anchor)` constraint. |
| 18 | **Provenance as flat FtM-context keys → `prov_*` node props** ([0018](0018-provenance-as-ftm-context-properties.md)) | LOCKED | `merge_context` can't merge nested dicts; flat keys survive merge + serialization. (Edges still uncovered — gap G1.) |
| 19 | **ER: whole-queue batch now; streaming/incremental** ([0019](0019-batch-vs-streaming-resolution.md)) | **RESOLVED** · superseded by [0026](0026-batch-first-resolution.md) | Resolved **batch-first**; incremental/streaming ER deferred to the ER-streaming gate. |
| 20 | **Catastrophic-merge guard: hardcoded conservative thresholds (v0)** ([0020](0020-merge-guard-thresholds.md)) | LOCKED (v0) · superseded by [0024](0024-merge-guard-alert-mode-build-phase.md) for build phase | `>10` members or any PEP/sanctioned → human review; rule-engine config deferred. |
| 21 | **Raw lands in object storage before mapping/enqueue** ([0021](0021-raw-lands-before-mapping.md)) | LOCKED | Concrete s3:// provenance pointer + replayable re-mapping without re-fetch. |
| 22 | **Connector output: strict FtM validation (fail-loud)** ([0022](0022-strict-schema-validation.md)) | LOCKED | L2 is the contract; bad data fails at the source, never corrupts the graph silently. |
| 23 | **Resolved-graph edge materialization: accepted v0 limitations** ([0023](0023-edge-materialization-v0-limitations.md)) | **OPEN** (debt) · item 1 closed by [0025](0025-referent-rewriting.md) | Edge referent-rewriting **closed (batch) by 0025**; abstract `Thing`-range links still owed before Phase 4. |
| 24 | **Catastrophic-merge guard: alert-only mode for the build phase** ([0024](0024-merge-guard-alert-mode-build-phase.md)) | **LOCKED (build phase, TEMPORARY)** · supersedes 0020 for build phase | `MERGE_GUARD_MODE` flag: build phase **alerts + writes** flagged merges (durable `merge_alerts` trail) instead of parking them; MUST flip back to `block` with human sign-off before production. |
| 25 | **Referent rewriting: redirect merged-away ids to canonical before the write** ([0025](0025-referent-rewriting.md)) | **LOCKED** · closes 0023 item 1 (batch) | ftmg MATCH-drops an edge naming a merged-away id; rewriting to the canonical id keeps neighbour traversal correct after a merge. In-batch only; cross-run sweep owed at the ER-streaming gate. |
| 26 | **Resolution batch-first: drain the queue in bounded windows** ([0026](0026-batch-first-resolution.md)) | **LOCKED** · supersedes 0019 | `resolve_pending` drains the queue in `RESOLVE_BATCH_SIZE` windows with a commit per batch; within-batch dedup only — incremental/cross-batch ER deferred to the ER-streaming gate. |
| 27 | **run_ingest: windowed commits + bounded collection + dead-letter** ([0027](0027-ingest-windowed-bounded-deadletter.md)) | **LOCKED** · closes G8 | Commit every `INGEST_COMMIT_EVERY`; stop at `INGEST_TIMEOUT_SECONDS` (1800) / `INGEST_MAX_RECORDS` (none); failed records go to `ingest_dead_letter`, never aborting the run. Hard-kill of a blocked `next()` deferred to the streaming driver. |
| 28 | **Per-batch resolver isolation** ([0028](0028-per-batch-resolver-isolation.md)) | **LOCKED** · G4 fix | `cluster_and_merge` resolves each batch on a private in-memory nomenklatura resolver, not the shared global ledger — one tenant's merges can no longer leak into another's. Persistent per-tenant resolution remains the deferred incremental-ER (S2) precondition. |
