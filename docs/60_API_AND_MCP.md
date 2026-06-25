# 60 — API & MCP Surface

> `v0.4` · June 2026 · L8 — the **query/decision boundary**. WorldMonitor exposes its resolved graph and
> actions through (a) a **REST/GraphQL API** (FastAPI) and (b) an **MCP server** (FastMCP). External
> workflows query the data and make decisions through these; the **Hermes agent layer** (`50`) consumes
> the *same* tools. One contract, two front doors.

## 1. Principles
- **The graph and scores are read through this layer, never by reaching into Neo4j/Postgres directly.**
- **Everything is auth-gated but single-tenant** (Zitadel OIDC; every call carries a role) — the
  deployment is single-tenant, so there is no tenant scoping (locked decision **D1**; **ADR 0042**
  supersedes **ADR 0017**).
- **Capability gating applies** — read/run-passive are open to scoped callers; **run-active** requires
  the human-in-the-loop gate (`10` §6).
- **MCP tools are themselves plugins** (kind `Tool`, see `30`) — adding a tool is adding a plugin.

## 2. The MCP server (for agents & agentic workflows)
A FastMCP server exposing WorldMonitor as a toolset. Core tools (illustrative):

| Tool | Purpose |
|---|---|
| `query_graph(query)` | Cypher or a safe query DSL over the resolved graph (read). |
| `get_entity(canonical_id)` | Fetch a canonical entity + its attributes + provenance. |
| `find_paths(a, b, max_hops)` | Relationship paths between two entities (the core "who connects to whom"). |
| `neighbors(id, filters)` | 1–n-hop neighbourhood with type/edge filters. |
| `resolve(name, context)` | Run matching (yente-style) → candidate canonical entities + confidence. |
| `enrich(entity, enricher)` | Trigger an `INTERNAL_ENRICHMENT` plugin on an entity (passive). |
| `run_connector(id, cfg)` | Start a collection run (passive; active → gated). |
| `list_alerts(filters)` / `get_score(entity)` | Read anomaly/fusion outputs + their calibration. |
| `subscribe(topic)` | Stream events/alerts (for live agent/workflow reactions). |

Tool results always carry **provenance and confidence** — so a consuming workflow/agent can reason about
trust, not just values. Heavy compute (graph algorithms, CV) stays in WorldMonitor services that the
tool invokes — **not** in the caller's context.

## 3. The REST/GraphQL API (for apps, dashboards, non-agent workflows)
- **GraphQL** for flexible graph/entity reads (entities, relationships, paths, scores) — natural fit for
  a graph product and a graph explorer UI.
- **REST** for actions and admin (connector instances, rules, alerts, exports, health).
- **Webhooks** for push (rule fired, run complete) — so external systems react without polling.
- Same auth (single-tenant; no tenant scoping — D1, ADR 0042), gating, and provenance-in-responses as
  the MCP surface.

## 4. Who uses it
- **Hermes** (the agent layer) — connects to the MCP server; its briefings/investigations run on these tools.
- **External workflows** (n8n, scripts, other agents) — query the graph and **make decisions** (the
  stated requirement): "is this wallet linked to a sanctioned entity?", "give me everything new about X".
- **The UI** — Integrations page, graph explorer, dashboards read via GraphQL/REST.

## 5. Safety
- **Read queries are sandboxed** — a query DSL or guarded Cypher (no writes, bounded cost/timeout) for
  untrusted callers; raw Cypher only for trusted/admin roles.
- **Inputs validated before execution** (injection mitigation) — especially for agent-supplied args.
- **Rate-limited & audited** — every call logged with caller, args (single-tenant; no tenant on the
  log line — D1, ADR 0042) (ties into the audit ledger).

## 6. Open decisions (need the user — see `decisions/`)
GraphQL vs REST-first emphasis · exact tool set for v1 of the MCP server · query DSL vs guarded-Cypher
for untrusted reads.
