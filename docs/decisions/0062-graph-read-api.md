# 0062 — Phase-2 graph-read REST API (slice 2a)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** Phase-2 Stage-2 slice **2a** (`gate/2a-graph-read-api`). Off `master`.
- **Milestone:** Phase 2 (`docs/40_ROADMAP.md:44`) — the API/MCP read surface over the resolved graph.
  This slice ships the **REST** half; the FastMCP server is slice 2b (ADR 0063, next).

## Context

The resolved graph is the product, but it has **no read surface**: `api/main.py` exposes only
`/health`, `/ready`, `/me`. Internal read helpers already exist and are tested
(`graph/queries.py`: `get_entity`, `get_neighbors`, `get_provenance`; `tests/integration/test_graph_queries.py`)
— they just aren't exposed. `docs/60_API_AND_MCP.md` §6 left three sub-decisions OPEN (GraphQL-vs-REST,
the v1 tool set, query-DSL-vs-guarded-Cypher); the Phase-2 forward plan resolved them conservatively.

## Decision

**REST-first, structured, read-only, auth-gated, bounded — wrapping the existing query helpers + a new
`find_paths`.** (GraphQL + a graph-explorer UI are deferred to a later slice; raw-Cypher `query_graph`
is deferred to trusted/admin only — v1 ships neither raw path to callers.)

- **New `graph/queries.py::find_paths(client, *, from_id, to_id, max_hops)`** — bounded relationship
  paths between two entities (the "who connects to whom" core), parameterized Cypher with a hard
  `max_hops` cap, returning each path's node ids + relationship types. Mirrors the existing helpers'
  style (read-only, `execute_read`).
- **New auth-gated REST routes** (a router added in `api/`, every route behind the `get_principal`
  dependency — the same gate as `/me`; 401 without a valid token):
  - `GET /entities/{id}` → `get_entity` (404 if absent).
  - `GET /entities/{id}/neighbors?hops=N` → `get_neighbors` (N clamped to a hard cap).
  - `GET /entities/{id}/provenance` → `get_provenance` (the `prov_*` of the node). Returns **404 when
    the entity is absent** (empty provenance ⟺ absent, per ADR 0060 fail-closed writes), consistent
    with `GET /entities/{id}`.
  - `GET /paths?from=&to=&max_hops=N` → `find_paths` (max_hops clamped).
- **Provenance in responses** (`docs/60` §2): entity/neighbor payloads carry their `prov_*`.
- **Safety** (`docs/60` §5): read-only; every query parameterized (no injection — ids are bound params,
  never string-interpolated); `hops`/`max_hops` clamped to a hard ceiling (default cap, e.g. 4) and a
  result `LIMIT`; entity-id shape validated before the query. No raw Cypher from callers in v1.
- **DI for testability:** the Neo4j read client is **injectable into `create_app`** (mirroring the
  existing `readiness=` injection) — default `Neo4jClient.from_settings(settings)`; tests inject a fake
  or a testcontainer client. No new datastore; reads go only through this layer (`docs/60` §1).

## Alternatives considered
- **GraphQL first.** A natural fit for a graph product + explorer UI, but heavier for v1 and the UI isn't
  built yet. REST ships the "external workflows query the graph" acceptance now; GraphQL follows with the
  UI. (Resolves `docs/60` §6 GraphQL-vs-REST → REST-first.)
- **`query_graph(raw Cypher)` for callers.** Powerful but an injection/cost-blast surface for untrusted
  callers; the structured routes cover the v1 need. Raw Cypher stays trusted/admin-only, later.
  (Resolves §6 query-DSL-vs-guarded-Cypher → structured-only in v1.)
- **A per-request new Neo4j driver.** Wasteful; reuse an injected client.

## Consequences
- External workflows (and, via slice 2b, MCP/Hermes) can read entities, neighbours, provenance, and paths
  over auth — the Phase-2 read half. Unblocks Phase 3 (Hermes) once 2b lands.
- Read-only + bounded + parameterized: no write path, no unbounded traversal, no injection. Single-tenant
  (D1/ADR 0042) — auth-gated, no tenant scoping.
- No migration; not person-affecting (read surface, no ER/merge/score decision). `human_fork: false`.

## Reversibility
Reversible (additive routes + one query helper). Reversal cost: low — drop the router + `find_paths`.
Revisit triggers: GraphQL/explorer UI need → add a GraphQL surface; trusted raw-Cypher need →
add a gated `query_graph` (admin role); high read volume → add caching/rate-limit middleware.

## Invariant gate note
Read surface — not an ER/provenance invariant, so no `@given` required. Failing-test-first: auth-gating
(401 without token), `get_entity` 200/404, neighbors hop-cap clamp, provenance-in-response, `find_paths`
bounded, and input validation — over an injected fake / testcontainer Neo4j.
