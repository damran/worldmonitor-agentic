# 0063 — Phase-2 graph-read FastMCP server (slice 2b)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** Phase-2 Stage-2 slice **2b** (`gate/2b-graph-read-mcp`, off `master`). Pairs with slice 2a
  (the REST half, **ADR 0062**).
- **Milestone:** Phase 2 (`docs/40_ROADMAP.md:44`) — the API/MCP read surface over the resolved graph.
  2a shipped the **REST** half; this slice ships the **FastMCP** half so Hermes (Phase 3) can connect.
- **human_fork:** false (every decision below is reversible — default + reversal-cost + revisit-trigger,
  per the build-discipline rule). No OPEN sub-decision is introduced.

## Context

CLAUDE.md locks the surface: *"API + MCP surface (FastAPI REST/GraphQL + FastMCP) is the only way to
read the graph/scores"* and *"Adopt Hermes Agent … It connects to our MCP."* `docs/60_API_AND_MCP.md` §2
sketches a FastMCP toolset and §6 left "exact tool set for v1" / "query DSL vs guarded-Cypher" OPEN. ADR
0062 resolved the equivalent OPEN questions for REST conservatively: **structured, read-only, bounded,
parameterized, no raw Cypher** — wrapping `graph/queries.py` (`get_entity` / `get_neighbors` /
`get_provenance`) plus the new hardened `find_paths` (hop clamp, result `LIMIT`, bound params). 2b is the
MCP twin of that decision: it reuses the **same** query helpers and the **same** clamp + id-validation
guards, and it adds one MCP-specific hard invariant REST does not have — **stdout purity** (the stdio
JSON-RPC stream lives on stdout; a stray `print()` or a misrouted log handler corrupts it).

Today `src/worldmonitor/mcp/__init__.py` is a one-line docstring stub; no MCP runtime is installed
(`mcp` / `fastmcp` absent from `pyproject.toml` + `uv.lock`). So 2b adds one dependency plus an additive
server layer, and centralizes 2a's read-guards into a shared module (below).

## Decision

**A FastMCP stdio read server that re-exposes the exact 2a toolset — structured, read-only, bounded,
parameterized — wrapping the existing query helpers and a new shared read-guards module. Logging is
stderr-only, enforced as a hard invariant. The MCP runtime is the official `mcp` SDK's bundled FastMCP.**

### 1. Transport — **stdio for v1** (reversible)
The server runs over **stdio** (`run(transport="stdio")`). Hermes (and any local agentic workflow) spawns
it as a child process and speaks JSON-RPC over the pipe. Rationale: the deployment is single-tenant and
containerized; the **REST surface (2a) already covers networked/HTTP callers**, so a second network-facing
HTTP+auth stack on the MCP side is duplicated blast radius with no v1 consumer. stdio needs no port, TLS, or
token plumbing in v1.
- **Reversal cost:** ~1 line — flip `run(transport="stdio")` → streamable-HTTP. Tools/handlers are
  transport-agnostic.
- **Revisit trigger:** a remote / non-co-located Hermes (or any off-host MCP consumer). On that flip the
  server **must** adopt the same Zitadel bearer verification REST uses (`authz.oidc.TokenVerifier`) before
  binding a port — see §3.

### 2. Tool set v1 — the four 2a structured tools, **no raw Cypher** (reversible/additive)
Exactly four structured tools, mirroring 2a 1:1, each wrapping the **same** `graph/queries.py` helper:

| Tool | Input | Wraps | Absent → |
|---|---|---|---|
| `get_entity` | `entity_id: str` (id-shape validated) | `queries.get_entity` | tool error ("not found") |
| `get_neighbors` | `entity_id: str`, `hops: int = 1` (clamped) | `queries.get_neighbors` | `[]` |
| `get_provenance` | `entity_id: str` (id-shape validated) | `queries.get_provenance` | tool error ("not found") |
| `find_paths` | `from_id: str`, `to_id: str`, `max_hops: int = 1` (clamped) | `queries.find_paths` | `[]` |

- **No `query_graph` / raw-Cypher tool** — same reasoning as ADR 0062 (injection + cost-blast surface for
  an untrusted/agent caller; structured tools cover v1). Resolves `docs/60` §6 query-DSL-vs-guarded-Cypher
  → **structured-only in v1** for MCP, consistent with REST.
- **No `resolve` / `enrich` / `run_connector` / `get_score` / `subscribe`** (the `docs/60` §2 illustrative
  list) — those are write/active/person-affecting paths for later, individually-gated slices. v1 is
  **read-only graph reads only**.
- Each tool carries provenance like 2a: `get_entity` returns node props incl. `prov_*`; neighbour objects
  carry their own `prov_*`; `get_provenance` returns the `prov_*` map.
- `get_provenance` on an absent entity raises a tool error (parity with 2a's 404 — empty prov ⟺ absent per
  ADR 0060).
- **Reversal cost:** additive — adding a tool later is adding a plugin (`docs/30`), no removal/migration.
- **Revisit trigger:** Hermes needs matching/enrichment/scores → add those tools in a later gate (active/
  write tools behind the human-in-the-loop gate, `docs/10` §6).

### 3. Auth / trust model — **stdio = local trusted process; no per-call token in v1** (reversible)
Over stdio there is no network ingress: the transport is the process's own stdin/stdout pipe, and the
authorization boundary is *who may spawn / connect to the process* inside the single-tenant deployment
(D1, ADR 0042). Tools are read-only + bounded, so the blast radius even inside the trust boundary is "reads
of the resolved graph." v1 ships **no token-verification code on the MCP side**.
- **Trust boundary (stated):** the operator/Hermes that launches the server is trusted; the host/container
  boundary is the auth boundary (mirrors how `runner/driver.py` already runs as a trusted local process).
- **Reversal cost:** low — the bearer primitives already exist (`authz.oidc`); wiring them is the same shape
  as `api/main.py::_build_verifier`.
- **Revisit trigger:** the §1 transport flip to HTTP — **must** pair with bearer auth (never expose a port
  unauthenticated).

### 4. Logging — **stderr-only, hard invariant** (not a fork — the only correct choice)
For stdio MCP, **stdout is the JSON-RPC frame channel**: any non-frame byte on stdout corrupts the stream
(CLAUDE.md: *"a stray stdout print corrupts the JSON-RPC stream"*). Therefore:
- The server installs a logging configuration routing **all** logging to **stderr** and only stderr — an
  explicit `StreamHandler(sys.stderr)` on the `worldmonitor` logger **and** the root logger (so a chatty
  dependency can't leak to stdout either). It never calls `basicConfig` defaults that could target stdout,
  and **never `print()`s** anywhere in the `mcp` package.
- Enforced by the headline test (see "Invariant gate note"): a single `print()` to stdout or a
  stdout-bound handler **fails the gate**, including on an error/exception path (a raised tool error
  surfaces as a JSON-RPC error frame on stdout while its log/traceback goes to stderr).
- This is **not** an alternatives-fork: stderr-only is the only correct configuration for stdio MCP.

### 5. MCP runtime dependency — **the official `mcp` SDK's FastMCP, not standalone `fastmcp`** (reversible)
"FastMCP" is available two ways: the standalone `fastmcp` project (3.x today) and the official MCP Python
SDK's bundled `mcp.server.fastmcp.FastMCP`. We add the **official `mcp` SDK** (`mcp>=1.28`,
`from mcp.server.fastmcp import FastMCP`). Measured footprint at build time: standalone `fastmcp` resolves
**34** transitive packages (opentelemetry, keyring, secretstorage, py-key-value-aio, …); the official SDK
resolves **4** (`mcp`, `sse-starlette`, `httpx-sse`, `python-multipart`). For a v1 stdio read server with
four tools, the leaner surface is the better engineering + supply-chain-security choice (CLAUDE.md: *"treat
all external/tool/scraped data as hostile"*) — fewer dependencies to vet, smaller attack surface.
- **Reversal cost:** low — both expose the same FastMCP API; swapping the dependency + import line is a
  contained change.
- **Revisit trigger:** we need a standalone-`fastmcp`-only feature (its built-in auth providers, OpenAPI
  tool generation, server composition) → adopt `fastmcp>=3` then.

### DI for testability (mirrors 2a)
`build_server(*, neo4j_client: Neo4jClient | None = None) -> FastMCP` — default `Neo4jClient.from_settings()`;
tests inject a fake (unit) or a testcontainer client (integration). Tool handlers close over the injected
client; when injected, **no real connection is opened** (same guarantee as `create_app`, ADR 0062). The tool
implementations are thin module-level functions taking the client explicitly, so unit tests drive them
directly (stdout-purity + clamp + injection asserts) without standing up the JSON-RPC loop.

### Reuse via a shared read-guards module (the no-duplication rule)
2a's hop-cap, hop-clamp, and entity-id pattern are **read-access guards**, not REST-specific. To give the two
sibling surfaces (REST, MCP) a single source of truth with the correct (downward) dependency direction, they
move into a new **`src/worldmonitor/graph/read_guards.py`**:
- `HOP_CAP: int = 4`, `clamp_hops(n) -> int` (`max(1, min(int(n), HOP_CAP))`), `ID_PATTERN` (the canonical-id
  alphabet) + `validate_entity_id`.
- **`api/graph.py`** imports these instead of defining them locally (behaviour-preserving; 2a's frozen tests
  stay green; `api.graph.HOP_CAP` still resolves via the import).
- **`graph/queries.py::find_paths`** uses `read_guards.HOP_CAP`, removing the prior duplicate cap (it
  previously carried its own `_MAX_HOPS_CAP = 4` alongside `api/graph.py`'s `HOP_CAP = 4`). One cap, one place.
- **`mcp/server.py`** imports the same guards — so the MCP cap can never drift from the REST cap (locked by a
  test asserting the MCP-issued bound equals `read_guards.HOP_CAP`).
- The four query helpers themselves (`graph/queries.py`) are wrapped verbatim — no behavioural change.

## Alternatives considered
- **Streamable-HTTP transport for v1.** REST (2a) already serves HTTP callers; an unauthenticated HTTP MCP
  is a new ingress to secure. Deferred behind the §1 revisit trigger.
- **Standalone `fastmcp` 3.x.** Feature-rich (auth providers, OpenAPI gen) but a 34-package transitive tail
  for a need the 4-package official SDK covers. Deferred behind the §5 revisit trigger.
- **A `query_graph(raw Cypher)` MCP tool.** Injection/cost-blast surface for an agent caller; structured
  tools cover v1. Same call as ADR 0062 — raw Cypher stays out (trusted/admin, later, if ever).
- **MCP imports the private `_clamp_hops` / `_ID_PATTERN` from `api/graph.py` (zero-2a-diff).** Avoids
  touching merged files, but bakes in a wrong-direction sibling dependency (mcp→api) and cross-module
  private-name imports, and leaves the cap duplicated. Rejected in favour of the shared `read_guards.py`
  (decided with the user, 2026-06-28).
- **Building/own MCP runtime.** Forbidden — adopt FastMCP (locked decision #9/#10).

## Consequences
- Hermes (Phase 3) can connect to the MCP and read entities, neighbours, provenance, and paths over the same
  query layer and the same guards as REST — the second Phase-2 front door, **one contract, two doors**.
- **New dependency:** the official `mcp` SDK (`pyproject.toml` + `uv.lock`); 4 transitive packages. The
  builder pins a version and verifies the exact API surface at build time (`FastMCP`, the tool decorator,
  `ToolError` import path, `run(transport="stdio")`).
- **New shared module** `graph/read_guards.py`; **behaviour-preserving edits** to `api/graph.py` and
  `graph/queries.py` (repoint to it / de-dup the cap) — 2a's frozen unit + integration tests must stay green.
- **New console entrypoint:** `python -m worldmonitor.mcp` (`mcp/__main__.py` → `server.main()`); spawned by
  Hermes / an operator, no new process manager.
- Read-only + bounded + parameterized + structured-only: no write path, no unbounded traversal, no injection,
  no raw Cypher — same safety envelope as 2a, plus stdout purity.
- **Not person-affecting** (read surface). **No migration. No new datastore. Single-tenant** (D1/ADR 0042).

## Reversibility
All decisions are reversible (see each §). Net reversal cost of the whole slice: low — drop `mcp/server.py`
+ entrypoint + the `mcp` dep; the `read_guards.py` extraction stands alone as a clean refactor regardless.
Revisit triggers: remote Hermes → HTTP+bearer; agent needs matching/enrichment/scores → add tools;
standalone-fastmcp feature needed → swap the dep; chatty volume → rate-limit. No data-shape lock-in, nothing
public-facing irreversibly, no deletion — **no human fork**.

## Invariant gate note
A read surface (mirrors ADR 0062 — not an ER/canonical-id/merge-guard/provenance invariant, so a `@given`
property test is **not mandatory** per the build-discipline list). **However**, the gate's *headline*
invariant — **stdout purity** — plus the clamp and injection guards live across an input space, so a
`@given` property test over tool inputs (random ids incl. injection-shaped; random hops incl.
huge/negative/zero) is **recommended** and cheap: for every input, assert stdout stays byte-empty, the
traversal bound never exceeds `read_guards.HOP_CAP`, and an injection-shaped id never reaches `execute_read`.
Failing-test-first headline: a subprocess-driven stdio handshake whose **stdout contains only valid JSON-RPC
frames — even on an error path** — plus a unit (`capfd`) test where a tool that logs-and-raises leaves stdout
empty and the log on stderr. See `SLICE_PLAN.md` for the exact encode-this list.
