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
| 29 | **Long-running ingest driver (ER-streaming Gate A)** ([0029](0029-ingest-driver-gate-a.md)) | **LOCKED** · Gate A | asyncio driver over the connector-instance registry: cadence-run batch connectors (decrypt-at-use), resolve on an independent cadence (serialized, single-node), record `task_run`, refuse ACTIVE visibly, idempotent enqueue (`UNIQUE(tenant_id, source_record, entity_id)` + ON CONFLICT). Incremental ER / graph mutations (S2/S3/S4) deferred — no real-time consumer (F0). |
| 30 | **Alembic schema migrations (replace create_all)** ([0030](0030-alembic-migrations.md)) | **LOCKED** | In-package migrations: baseline (pre-runway) + runway delta. `migrate_to_head` adopts a pre-Alembic database (stamp baseline, then upgrade) so fresh + existing deployments converge — fixing the broken pre-existing-`er_queue_item` case. `create_all` kept as a proven-equivalent test/dev path. |
| 31 | **Return-to-block + human sign-off for parked merges** ([0031](0031-return-to-block-signoff.md)) | **LOCKED** · fulfils 0024 | `MERGE_GUARD_MODE` defaults to `block`. Durable, **tenant-scoped** `resolver_judgement`s seed every batch's ephemeral resolver (a Splink pair a judgement decided is skipped); an approved cluster bypasses the guard so it never re-parks. CLI `worldmonitor.review` lists / approves (promote canonical + outbound edges) / rejects (split members) parked merges, recording a `sign_off` trail. Inbound-edge restore = deferred Gate C; cross-batch canonical-id stability = deferred Gate B. |
| 32 | **Cheap hardening + audit follow-ups** ([0032](0032-cheap-hardening.md)) | **LOCKED** | Driver-loop resilience (a tick/instance/tenant failure can't crash the driver); `task_run` retention/pruning (`TASK_RUN_RETENTION_DAYS`); config→URL `pattern`s (SSRF/path-injection, review H5) + landing-key sanitization (tenant-escape, review H6); `compare_type` + X2 notes. Deferred (surfaced, not built): HA lease (X2), cross-store outbox (H1/H2), entity-link drop (H3), provenance-collapse (H4), single-writer (X3). |
| 33 | **Neo4j bounded memory + pinned GDS-compatible image** ([0033](0033-neo4j-bounded-memory.md)) | **LOCKED** | Fixes Neo4j OOM-on-WSL2 (unbounded heap → `7687` refused): bound `heap.initial/max__size` + a `mem_limit` (heap+pagecache+overhead < limit), env-overridable with laptop defaults scaling to prod. Keep GDS (used by `graph/gds.py`, ADR 0002) on the **CI-proven** `2026.05.0-community`+auto-download pair — pinned, not floated. |
| 34 | **Neo4j compose auth fix + compose-boot CI guard** ([0034](0034-compose-boot-validation.md)) | **LOCKED** | The real crash-loop cause: a bare `NEO4J_PASSWORD` env var the image maps to an invalid `password` setting that strict validation rejects. Fix: password via `NEO4J_AUTH` only (server no longer inherits it; client still reads `.env`); healthcheck interpolated at parse time. Adds a `compose-boot` CI job that `docker compose up --wait`s the real deploy stack so deploy-config defects (invisible to testcontainers) fail CI. |
