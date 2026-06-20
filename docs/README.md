# WorldMonitor — Documentation Index

> **Plan version:** `v0.4` · June 2026 · Supersedes the retired CTI-orchestrator `ARCHITECTURE.md`.
> WorldMonitor is a graph-native, ontology-first, **plugin-extensible** OSINT & geopolitical
> intelligence platform, driven by an autonomous build agent (Claude Code) and operated by a
> self-improving agent layer (Hermes).

This `docs/` tree **is the plan** — modular, single-purpose files, easy to keep current, optimized for
**Claude Code** and readable by any agent. The always-loaded agent rules live in `../CLAUDE.md`
(lean, < 200 lines); these docs are the on-demand reference.

## Changelog
- **v0.4** — Generalized the connector framework into a uniform **plugin system** (30); added the
  **agent layer** (Hermes adopted) + **self-improvement loop** (50); added the **API + MCP query
  surface** (60); rewrote `CLAUDE.md` for **autonomous operation** (PRs/merge via gh, gated on CI);
  CTI demoted from a layer to just one plugin domain.
- **v0.3** — Clean-slate graph-native rewrite (Neo4j + FollowTheMoney + Splink + Zitadel).

## Read order

| # | File | Answers | Read when |
|---|------|---------|-----------|
| 00 | [`00_VISION_AND_SCOPE.md`](00_VISION_AND_SCOPE.md) | What it is, principles, scope, non-goals | First, always |
| 10 | [`10_ARCHITECTURE.md`](10_ARCHITECTURE.md) | Layered model, data-flow, stack, local→cloud, security & tenancy | Before any design |
| 20 | [`20_ONTOLOGY.md`](20_ONTOLOGY.md) | Entity/relationship model (FtM+STIX+canonical IDs), provenance, expandability | Before normalization / ER / graph |
| 30 | [`30_PLUGIN_FRAMEWORK.md`](30_PLUGIN_FRAMEWORK.md) | The open plugin system — connectors, enrichers, rules, scorers, notifiers — + the Integrations page | Before building any source/method |
| 40 | [`40_ROADMAP.md`](40_ROADMAP.md) | Phased build order, current milestone, acceptance criteria | To know what to build next |
| 50 | [`50_AGENT_LAYER.md`](50_AGENT_LAYER.md) | Hermes adoption, the self-improvement loop + guardrails, LLM pluggability | Before agent / self-improvement work |
| 60 | [`60_API_AND_MCP.md`](60_API_AND_MCP.md) | The REST/GraphQL + MCP query/decision surface for external workflows & Hermes | Before exposing data outward |
| — | [`decisions/`](decisions/) | ADRs: what's **LOCKED**, what's **OPEN** (needs the user) | When a choice arises |

## How to use this with Claude Code (autonomous mode)

1. Plan mode first on any non-trivial task; read `CLAUDE.md` (auto-loaded) + the relevant docs.
2. Build toward the **Current Milestone** (`40_ROADMAP.md`) — one vertical slice at a time.
3. Work autonomously: branch → implement + tests → open PR via `gh` → **CI must pass** → merge.
   Chain work without stopping for approval. **Pause only** for genuine questions or **OPEN** ADRs.
4. Do web research, use any tool/connector, as needed.
5. Tag every component: `researched` → `scaffolded` → `implemented` → `tested` → `operational`.

## Keeping this plan alive
- **Decisions** → `decisions/` (add an ADR; supersede, never silently rewrite a LOCKED one).
- **Scope/architecture** → the numbered file it belongs to; bump its version line.
- **Catalog/plugins** are **data/code, not prose** — adding a source or method is a registry entry +
  plugin, not a doc edit (see `30_PLUGIN_FRAMEWORK.md`).
- Keep `CLAUDE.md` in sync with the LOCKED decisions and agent rules; keep it < 200 lines.
