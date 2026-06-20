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
| 15 | **Containerized + 12-factor + S3-compatible**
