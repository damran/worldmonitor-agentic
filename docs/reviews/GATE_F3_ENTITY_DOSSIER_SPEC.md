# Gate F-3 (slice 1) — `get_entity_dossier`: deterministic assembly

> Backlog: `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-3** — "`get_entity_dossier` — their
> brief-tool concept, ours: **slice 1 = deterministic assembly** (entity + neighbors + provenance +
> merge history from existing query helpers, **zero LLM**); slice 2 = gateway-only LLM narrative with
> mandatory sources array + CTI `framework` param recorded in the egress audit. First slice: MCP tool +
> `GET /entities/{id}/dossier`. **P1 / M (2 gates)**."
> **THIS GATE = slice 1 ONLY** (deterministic, zero LLM). Slice 2 (LLM narrative) is a **separate future
> gate** and a hard NON-goal here (§7).
> ADR: `docs/decisions/0122-entity-dossier-deterministic.md` (PROPOSED).
> Predecessors relied on: ADR 0062 (REST read routes), ADR 0063 (stdio MCP), ADR 0064 (read caps),
> ADR 0090 (authenticated HTTP MCP), ADR 0121 / Gate F-2 (MCP contract polish — annotations, typed
> output schemas, `{error, hint}` envelope), ADR 0042 (single-tenant), ADR 0095 (statement/decision log
> = SoR; Neo4j = derived projection).

## 0. What this gate is (and is NOT)

**Is:** ONE new **read-only aggregation** endpoint — a deterministic dossier — exposed on **both** the
REST surface (`GET /entities/{entity_id}/dossier`) and the MCP surface (a **fifth** tool
`get_entity_dossier`), assembled by **one shared helper** that composes the **existing** graph read
helpers (`get_entity` + `get_neighbors` + `get_provenance`) into a fixed-shape object. Zero new Cypher,
zero LLM, zero egress, zero new inference/scoring.

**Is NOT** (explicit non-goals — see §7): the **slice-2 LLM narrative** (gateway-only, `sources[]`, CTI
`framework` param, egress audit) — a *separate future gate*; the **F-5 `summary` context-budget flag**;
**new merge-audit plumbing** (the Postgres merge-audit / canonical-ledger trail is NOT wired into this
graph-only lockstep helper — merge history is a **recorded absence** this slice, §3.3); a typed
Pydantic response model (F-7 OpenAPI artifact); an `assembled_at` wall-clock stamp (§3.4); any change to
the existing four tools' or four routes' payloads.

**The MCP tool count goes 4 → 5.** This is the one deliberate break of Gate F-2's PP-3 "exactly four
tools" pin. Every pin that hardcodes the tool count/set is enumerated in §6.1 and is an **in-scope
test-author edit** — the sanctioned exception to "the builder does not touch tests" (the test-author
updates the pins; the builder never edits a pin to make its own code pass).

---

## 1. What is cleanly readable TODAY (the assembly substrate — verified, do NOT re-derive)

All three sections below are already produced by shipped helpers in
`src/worldmonitor/graph/queries.py`, each a bounded, parameterized, read-only Neo4j read. The four F-2
MCP tools already wrap the first three verbatim.

| Section | Helper (`graph/queries.py`) | Shape | Bound |
|---|---|---|---|
| `entity` | `get_entity(client, entity_id=…)` | `dict[str, Any]` node props incl. `prov_*`; `None` if absent | one node |
| `neighbors` | `get_neighbors(client, entity_id=…, hops=…)` | `list[dict[str, Any]]` neighbour node props (each carries its own `prov_*`) | `read_guards.NEIGHBOR_RESULT_LIMIT` (500) + `HOP_CAP` (4), ADR 0064 |
| `provenance` | `get_provenance(client, entity_id=…)` | `dict[str, str]` of the node's `prov_*` keys; `{}` iff node absent | one node |

### 1.1 Merge history — the honest availability finding (drives §3.3)

The backlog row's phrase "merge history **from existing query helpers**" is optimistic: there is a merge
audit trail, but it is **not** in `graph/queries.py` and **not** graph-readable — it lives in **Postgres**
and its read helpers require a SQLAlchemy `Session`:

- **`merge_audit` table** (`db/models.py::MergeAudit`: `canonical_id`, `source_ids[]`, `score`,
  `decision`, `reason`, `created_at`) — the per-decision trail ("which sources collapsed and why").
  Read pattern: `select(MergeAudit).where(MergeAudit.canonical_id == …)` (see `api/review.py`,
  `resolution/signoff.py`).
- **`canonical_id_ledger` table** (`db/models.py::CanonicalIdLedger`, append-only) — superseded-id →
  survivor aliases. Read helper: `resolution/canonical.resolve_durable(session, alias)` (the durable
  mirror of nomenklatura `get_referents`).

Neo4j nodes carry `prov_*` and the per-property **witness map** (`prov_witnesses`) — i.e. *which sources
witnessed which property* — but **not** *which source ids were collapsed into this canonical* (the merge
lineage). `writer.py::resolve_node_id` is an alias-on-read lookup that returns the *current node*, not a
merge-history reader (and is noted dead in prior review).

**Consequence for the lockstep design:** the REST surface *has* a DB session (`api/deps.py::get_db`); the
**stdio MCP server has none** — `build_server` takes only a `Neo4jClient`, and the stdio trust boundary /
12-factor story is Neo4j-only (ADR 0063). Wiring Postgres into the stdio MCP server is **new plumbing**
that would (a) expand this XS/S gate, (b) complicate the stdio transport contract, and (c) break the
"ONE shared graph-only helper both surfaces call" lockstep (§3.2). Per the coordinator's explicit steer
and CLAUDE.md's "keep the gate small," **merge history is a recorded absence in slice 1** (§3.3), with
the read path identified above and a revisit trigger in ADR 0122.

---

## 2. Scope (exact files) — mirrored verbatim into `.claude/gate.scope`

| Path | Why (in scope) |
|---|---|
| `src/worldmonitor/graph/queries.py` | **The shared assembly helper** `get_entity_dossier(client, *, entity_id, hops=1) -> dict \| None` — composes the three existing helpers into the fixed shape (§4). The single assembly point (F-5 lockstep convention). |
| `src/worldmonitor/api/graph.py` | New route `GET /entities/{entity_id}/dossier` (auth-gated, id-shape-validated, hops-clamped) calling the shared helper; 404 when it returns `None`. |
| `src/worldmonitor/mcp/server.py` | New module-level `tool_get_entity_dossier(client, entity_id, hops=1)` + register the **fifth** tool in the shared `_register_read_tools` with the F-2 conventions (annotations, `structured_output=True`, `{error, hint}` envelope). |
| `tests/unit/test_graph_queries.py` | Unit: the shared helper assembles the four sections; `None` when entity absent; merge-history sentinel present. |
| `tests/unit/test_api_graph.py` | Unit: REST route (200 assembled / 404 absent / 401 no-auth / 422 injection id / hops clamp) **+ the REST↔MCP parity test** (§6.4). |
| `tests/unit/test_mcp_server.py` | Unit: the new tool (happy / absent-raises / injection-raises / read-only / clamp) **+ PP-3 pin updates** (§6.1). |
| `tests/unit/test_mcp_http_auth.py` | Pin updates: the two HTTP tool-set assertions + the parity `expected_names` go 4 → 5 (§6.1). |
| `tests/integration/test_mcp_stdio.py` | Pin updates: the two wire `tools/list` set assertions go 4 → 5; add a `get_entity_dossier` `tools/call` over the wire (§6.3). |
| `tests/integration/test_api_graph_read.py` | Integration: `GET /entities/{id}/dossier` against a real Neo4j testcontainer (200 assembled incl. `prov_*`; 404 absent). |
| `tests/property/test_prop_dossier_provenance.py` | **NEW `@given`** — the provenance-surface invariant (§3.5 / §6.2). |
| `tests/property/test_prop_mcp_stdout_purity.py` | Extend: add `tool_get_entity_dossier` to the stdout-purity property coverage (the new stdio tool must be stdout-pure). |
| `deploy/hermes/config.yaml` | Config pin: add `get_entity_dossier` to the `tools.include` allowlist; update the `# EXACTLY the 4 read tools` comment to 5 (§6.1). |
| `docs/decisions/0122-entity-dossier-deterministic.md` | This gate's ADR (PROPOSED → ACCEPTED at the merging PR). |
| `docs/decisions/README.md` | Regenerated ADR index (`scripts/gen_adr_index.py`) — the `adr-index` CI check. |
| `docs/reviews/GATE_F3_ENTITY_DOSSIER_SPEC.md` | This spec. |

**Out of scope / must not change:** `graph/read_guards.py` (reuse the existing caps — no new constant
needed; the dossier's neighbours are already bounded by `get_neighbors`), `mcp/auth.py`, the four
existing routes/tools' bodies, `api/deps.py`, the resolution/ merge-audit code, `db/models.py` (**no new
table, no migration** — merge history is a recorded absence, not new plumbing).

---

## 3. Locked invariants + design decisions every change must hold

### 3.1 CLAUDE.md invariants (carried, not relitigated)

- **G1 provenance on every node AND edge — carried on the SURFACE.** The dossier returns `prov_*`
  verbatim (the `entity` section carries its own `prov_*`; each `neighbors` element carries its own
  `prov_*`; the `provenance` section is the node's `prov_*` map). The dossier **must never present an
  entity without its provenance** — this is the provenance-*surface* analogue of G1 and is enforced by
  the mandatory property test (§3.5).
- **Append-only / read-only.** The helper and both surfaces call `execute_read` **only** — no
  write/MERGE/SET/DELETE, no write session. The recording-fake "explode on any write path" test extends
  to the new tool.
- **Canonical-canonical only via the guard — untouched.** This gate performs **no** resolution, merge,
  or canonicalisation; it reads already-resolved nodes. The catastrophic-merge guard is not on this path.
- **Bounded / no-injection.** id-shape validated **before** any read (REST: `Path(pattern=ID_PATTERN)`;
  MCP: `read_guards.validate_entity_id`); `hops` clamped to `HOP_CAP` via `clamp_hops`; ids are bound
  params, never string-interpolated. Reused verbatim from the sibling routes/tools.
- **STDOUT PURITY (stdio).** The new tool logs (on the rejection path) to stderr and surfaces a JSON-RPC
  error frame — the stdout-purity property test is extended to cover it.

### 3.2 One shared helper — REST + MCP lockstep (the F-5 convention)

There is **exactly one** assembly point: `graph/queries.py::get_entity_dossier`. The REST route and the
MCP tool are **thin pass-throughs** — each validates the id / clamps `hops`, calls the helper, and maps
`None` to its surface's not-found idiom. Neither surface assembles the dossier itself, so the two can
never drift. The **parity test** (§6.4) pins this: for the same seeded input, the REST body and the MCP
tool result are byte-identical (as decoded JSON). This mirrors F-2's PP-1 parity discipline and F-5's
"shared helper so REST + MCP stay lockstep."

The helper takes **only** the `Neo4jClient` — the dependency both surfaces already have. It does **not**
take a DB `Session` (which only REST has), which is exactly why merge history is a recorded absence
(§3.3): a Session-taking helper would be un-satisfiable by the stdio MCP surface without new plumbing.

### 3.3 Merge history — RECORDED ABSENCE (locked decision, ADR 0122 D3)

The dossier schema **always includes** a `merge_history` key whose value in slice 1 is a fixed,
machine-readable sentinel:

```json
"merge_history": { "status": "not_assembled", "available": false }
```

- `status` is a fixed enum (`"not_assembled"` for slice 1); `available` is a boolean. **No free prose** —
  this satisfies both "recorded absence" (explicit, not a silent omission) and "NO free text" (§3.4).
- Rationale: the merge trail is Postgres-only and the MCP surface has no Session (§1.1); wiring it in is
  new plumbing that would break the graph-only lockstep and blow the gate size.
- Why a **present key** rather than omitting it: a present key lets consumers (Hermes/CLI) rely on the
  field and its **later population** (from `resolution.canonical.resolve_durable` + the `merge_audit`
  trail) **without a breaking shape change**. Silent omission is what CLAUDE.md's provenance/audit
  posture discourages.
- Revisit trigger (ADR 0122): populate `merge_history` once the MCP surface has a DB-session context (or
  we accept a REST-only enrichment) — a separate gate.

### 3.4 Response is a pure, deterministic function of graph state

Every section is traceable to a query helper; the payload contains **no free text** and **no wall-clock
stamp**. `assembled_at` is deliberately **omitted** in slice 1: (i) it is not traceable to a query helper
(it is not graph state), (ii) it makes the payload non-deterministic (in tension with a *deterministic*
assembly gate and with byte-parity), and (iii) adding it later is backward-compatible. Recorded as a
non-goal with a revisit trigger (§7, ADR 0122).

### 3.5 Property-test discipline — DECISION: YES, one mandatory `@given`

Unlike F-2 (which recorded *no* property test — pure metadata polish, no invariant touched), F-3 is a
**new data-exposure surface** and **does** touch a CLAUDE.md invariant: **provenance exposure**. The
dossier is an aggregation view of a possibly-`Person` entity; the load-bearing guarantee is that it can
**never launder provenance away** — it must never present an entity stripped of its `prov_*` / provenance
section. That is a genuine surface-invariant, so per CLAUDE.md build-discipline a `@given` test is
**required** (§6.2). This is recorded as a **decision, not an omission**, and is the mirror-image of
F-2 §3.1.

---

## 4. Response schema (the deterministic dossier)

`get_entity_dossier(client, *, entity_id, hops=1)` returns `None` if `get_entity` is `None` (absent),
else:

```jsonc
{
  "entity":       { /* get_entity result: node props incl. prov_* — required, non-null */ },
  "neighbors":    [ /* get_neighbors result: bounded list of neighbour node dicts (each w/ prov_*) */ ],
  "provenance":   { /* get_provenance result: the node's prov_* map — non-empty for any present node */ },
  "merge_history":{ "status": "not_assembled", "available": false }  /* recorded absence, §3.3 */
}
```

- **Top-level keys are fixed and additive-friendly** (populating `merge_history` or adding a field later
  is backward-compatible; renaming/removing a key is the only breaking change — the shape lock-in noted
  in ADR 0122's reversibility).
- `neighbors` is the **1-hop** neighbourhood by default (`hops=1`), clamped to `HOP_CAP` and capped at
  `NEIGHBOR_RESULT_LIMIT` — inherited from `get_neighbors`, no new bound.
- Return type annotation `dict[str, Any]` (consistent with the existing routes/tools; drives the MCP
  SDK's `structured_output` object-schema derivation — permissive, `additionalProperties`).

### 4.1 Surface behaviour (thin pass-throughs)

| Surface | Signature | Not-found | id reject | hops |
|---|---|---|---|---|
| REST | `GET /entities/{entity_id}/dossier?hops=1` behind `get_principal` + `get_neo4j` | helper `None` → `HTTPException(404, "Entity not found")` (mirrors `read_entity`) | `Path(pattern=ID_PATTERN)` → 422 | `Query(ge=1)=1`, `clamp_hops(...)` before the helper (mirrors `read_neighbors`) |
| MCP | `get_entity_dossier(entity_id, hops=1)` in `_register_read_tools`; module fn `tool_get_entity_dossier(client, entity_id, hops=1)` | helper `None` → `raise _tool_error("entity not found", <hint>)` (mirrors `tool_get_entity`) | `_require_valid_id` → `_tool_error("invalid entity id", …)` | `read_guards.clamp_hops(hops)` (mirrors `tool_get_neighbors`) |

MCP registration mirrors the F-2 shape exactly: the shared `read_only_annotations =
ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)`, `title="Get entity
dossier"`, `structured_output=True`. `error` token `"entity not found"` is **reused** (identical to
`tool_get_entity`) so the not-found signal is consistent across tools.

---

## 5. Acceptance criteria (crisp)

- **AC-1 shared helper.** `get_entity_dossier` exists in `graph/queries.py`, calls **only** the three
  existing helpers (no new Cypher, no write), returns the §4 shape, and returns `None` iff `get_entity`
  is `None`. Both surfaces call it; neither assembles independently.
- **AC-2 REST route.** `GET /entities/{id}/dossier` returns 200 + the §4 body for a present entity; 404
  for an absent id; 401 without a token; 422 for an injection-shaped id; `hops` is clamped.
- **AC-3 MCP tool.** `get_entity_dossier` is registered on **both** transports via the shared site with
  the F-2 annotations + `structured_output=True` + non-null `outputSchema`; returns the §4 dict for a
  present entity; raises the `{error, hint}` envelope (`"entity not found"` / `"invalid entity id"`) on
  absent / injection input; touches only `execute_read`.
- **AC-4 tool set is exactly the FIVE.** Every pin in §6.1 asserts
  `{get_entity, get_neighbors, get_provenance, find_paths, get_entity_dossier}` — no more, no less — on
  stdio, HTTP, and the wire `tools/list`.
- **AC-5 lockstep parity.** For the same seeded input, the REST body and the MCP tool result are
  byte-identical as decoded JSON (§6.4).
- **AC-6 provenance-surface invariant.** The `@given` property test holds: any assembled dossier for a
  present entity carries a **non-empty** `provenance` section AND an `entity` section that includes every
  `prov_*` key, AND the `merge_history` sentinel (§6.2).
- **AC-7 merge-history recorded absence.** The dossier always carries
  `merge_history == {"status": "not_assembled", "available": false}` (no Postgres read, no Session
  dependency in the helper).
- **AC-8 no regression.** All existing MCP/REST/property tests stay green (aside from the §6.1 pin
  updates); the four existing tools'/routes' payloads are unchanged.

---

## 6. Named tests + the full pin-update list

### 6.1 PIN-UPDATE LIST — every hardcoded tool count/set (4 → 5) — TEST-AUTHOR ONLY

This is the sanctioned test-edit surface. The **test-author** makes every edit below; the **builder**
never touches a pin. Add `get_entity_dossier` to each set / update each count/comment:

| # | File : locus | Current | Action |
|---|---|---|---|
| P-1 | `tests/unit/test_mcp_server.py:15` (module docstring) | "registers exactly the **4** tools" | → 5 |
| P-2 | `tests/unit/test_mcp_server.py:~197` comment + `test_tool_set_is_exactly_the_four` (`~199-206`) | set literal `{get_entity, get_neighbors, get_provenance, find_paths}` | add `get_entity_dossier`; rename test → `..._exactly_the_five` |
| P-3 | `tests/unit/test_mcp_server.py:352` `_ALL_TOOL_NAMES` frozenset | the four names | add `get_entity_dossier` (propagates to `_list_tools`'s set assertion at `:359` and the F-2 annotation/schema loops at `:390`/`:413`/`:418`, which will then also assert the dossier tool's annotations + non-null `outputSchema`) |
| P-4 | `tests/unit/test_mcp_server.py` `test_all_tools_have_output_schema` (`~413+`) | per-tool `outputSchema` shape checks keyed by name | add a `get_entity_dossier` `outputSchema is not None` + `type == "object"` assertion |
| P-5 | `tests/unit/test_mcp_http_auth.py:349-376` `test_inv_s1_readonly_http_server_registers_exactly_four_tools` | set literal (`:371-376`) + test name | add `get_entity_dossier`; rename → `..._five_tools` |
| P-6 | `tests/unit/test_mcp_http_auth.py:404-407` (stdio set assertion) | the four | add `get_entity_dossier` |
| P-7 | `tests/unit/test_mcp_http_auth.py:442-444` parity `expected_names` | the four | add `get_entity_dossier` |
| P-8 | `tests/integration/test_mcp_stdio.py:16-19` (module docstring, tool enumeration) | lists the four | add the fifth |
| P-9 | `tests/integration/test_mcp_stdio.py:291-294` wire `tools/list` `assert names == {four}` | the four | add `get_entity_dossier` |
| P-10 | `tests/integration/test_mcp_stdio.py:397` AC-4 annotations `assert names == {four}` | the four | add `get_entity_dossier` |
| P-11 | `deploy/hermes/config.yaml:11` `tools.include: [..4..]  # EXACTLY the 4 read tools` | the four | add `get_entity_dossier`; comment → "**5** read tools" |

**Verified NOT a pin (no change needed):** `tests/integration/test_mcp_http_transport.py` has tool
*calls* (`get_entity`) and a 401 `tools/list`, but **no** tool-set equality assertion. There is **no**
REST route-set inventory test (`test_mcp_http_auth.py:382` asserts only the `/mcp` route exists — HTTP
transport, unrelated). There is **no** compose-boot MCP live-smoke assertion yet (F-8 unbuilt), so CI
`compose-boot` needs no change. Optional (non-CI-gated) doc sync: `VERIFIED_API.md` if it enumerates the
tool set — nice-to-have, not required for green.

### 6.2 NEW property test (mandatory `@given`) — `tests/property/test_prop_dossier_provenance.py`

- `test_prop_dossier_always_carries_provenance` — `@given` a synthetic entity whose props include ≥1
  `prov_*` key (drive the shared helper against a recording fake whose `get_entity` returns those props
  and `get_provenance` returns the `prov_*` subset). Assert, for the assembled dossier:
  (a) `"provenance"` key present and **non-empty**; (b) every `prov_*` key in the provenance section is
  also present on `dossier["entity"]`; (c) `merge_history == {"status": "not_assembled", "available":
  false}`. Encodes the provenance-surface invariant (§3.5) — the surface analogue of G1: a present node
  is never presented without its provenance. (Heed the memory note: wrap any container-backed body in
  try/finally to avoid connection leaks — but this test uses a pure in-process fake, no DB.)

### 6.3 Integration — stdio wire (`tests/integration/test_mcp_stdio.py`)

- `test_stdio_get_entity_dossier` — over the spawned server against a real Neo4j testcontainer:
  `tools/call get_entity_dossier {entity_id: "p1"}` → not `isError`; the decoded result carries
  `entity.prov_source_id`, a `neighbors` list, a non-empty `provenance`, and the `merge_history` sentinel.
  `{entity_id: "zzz-absent"}` → `isError` with a recoverable `{"error": "entity not found", …}` envelope.
  Plus the P-9/P-10 set-assertion updates.

### 6.4 REST↔MCP parity + REST unit (`tests/unit/test_api_graph.py`) and REST integration

- `test_dossier_rest_mcp_parity` — with **one** shared recording fake: call the REST route (via the app
  `TestClient`) and `tool_get_entity_dossier(fake, "A")`; assert the REST JSON body **deep-equals** the
  MCP tool's returned dict for the same `entity_id`/`hops`. Pins §3.2 (both are thin pass-throughs of the
  ONE helper) → AC-5.
- `test_dossier_returns_assembled_sections` — REST 200; body has exactly the four top-level keys; entity
  carries `prov_*`; provenance non-empty; merge_history sentinel.
- `test_dossier_absent_entity_404` / `test_dossier_requires_auth_401` /
  `test_dossier_rejects_injection_id_422` / `test_dossier_hops_clamped`.
- Integration `tests/integration/test_api_graph_read.py::test_dossier_over_testcontainer` — real Neo4j:
  200 assembled (incl. `prov_*`) for a seeded node; 404 absent.

### 6.5 Unit — shared helper (`tests/unit/test_graph_queries.py`)

- `test_get_entity_dossier_assembles_sections` — against a recording fake, the helper composes
  `get_entity`/`get_neighbors`/`get_provenance` into the §4 shape; merge_history sentinel present.
- `test_get_entity_dossier_none_when_absent` — fake `get_entity` → `None` ⇒ helper returns `None` (and
  never calls `get_neighbors`/`get_provenance`, so an absent entity does 1 read, not 3).

### 6.6 Unit — MCP tool (`tests/unit/test_mcp_server.py`, beyond the §6.1 pins)

- `test_get_entity_dossier_tool_returns_sections` — `tool_get_entity_dossier(fake, "A")` → §4 dict.
- `test_get_entity_dossier_absent_raises` — absent → `ToolError` with `{"error":"entity not found",…}`.
- `test_get_entity_dossier_injection_raises` — `_INJECTION_ID` → `{"error":"invalid entity id",…}` and
  **no** `execute_read` (validated before any query).
- `test_get_entity_dossier_read_only` — driving the tool touches only `execute_read` (extends the
  recording-fake write-path guard).
- Extend `tests/property/test_prop_mcp_stdout_purity.py` to drive `tool_get_entity_dossier` (stdout
  stays pure on both happy and raise paths).

---

## 7. NON-goals (explicit — do NOT build here)

- **Slice 2: the LLM narrative** — gateway-only LLM prose, mandatory `sources[]` array, CTI `framework`
  param (diamond-model / kill-chain / ACH) recorded in the **egress audit**. A *separate future gate*
  with its own ADR; it carries the person-affecting weight + egress that slice 1 deliberately avoids.
- **Merge-history plumbing** — no `Session` in the helper, no new table, no migration, no MCP DB wiring.
  Merge history is a recorded absence (§3.3); populating it is a later gate.
- **F-5 `summary` context-budget flag** — the dossier's `neighbors` is the full bounded list, not
  `{count, sample[3]}`; when F-5 lands, the dossier adopts the shared summary helper (revisit trigger).
- **`assembled_at` / any wall-clock or non-graph field** — non-deterministic, not traceable to a helper
  (§3.4).
- **Typed per-field Pydantic response model** — F-7 (OpenAPI artifact) territory; keep `dict[str, Any]`
  consistent with the existing routes/tools.
- **New Cypher / any change to the four existing helpers, routes, or tools' payloads.**

---

## 8. Slice breakdown

**ONE slice.** The shared helper, both thin surfaces, the pin updates, and the parity + property tests
are tightly coupled: the parity test (§6.4) cannot pass until *both* surfaces exist, and splitting REST
from MCP would ship a half-lockstep surface. So it all lands as one individually-mergeable PR.

- **Slice 1 — deterministic `get_entity_dossier` (REST + MCP + shared helper).** Production: (a)
  `graph/queries.py::get_entity_dossier` (compose the three helpers; `None` on absent; merge_history
  sentinel); (b) `api/graph.py` route `GET /entities/{entity_id}/dossier` (auth, id-pattern, hops-clamp,
  404-on-None); (c) `mcp/server.py` — `tool_get_entity_dossier` + register the fifth tool in
  `_register_read_tools` with the F-2 conventions. Tests: all of §6 (incl. the §6.1 pin updates by the
  test-author, the §6.2 mandatory `@given`, and the §6.4 parity test). Config: `deploy/hermes/config.yaml`
  include-list. ADR 0122 → **ACCEPTED** at the merging PR (per the 0117-0121 convention); regenerate the
  ADR index.

**No sanctioned split.** If a reviewer insists on two PRs, the only clean seam is "helper + REST first;
MCP + pin updates + parity second" — but that defers the lockstep guarantee, so the default is one slice.

---

## 9. Open items for the test-author / builder

1. **Pin discipline (§6.1).** The test-author owns every 4→5 edit; the builder must not touch a pin to
   make its own code pass. After the builder runs, `git diff` the eleven loci and confirm each set now
   contains exactly the five names.
2. **`None` short-circuit.** The helper must return `None` **before** calling `get_neighbors` /
   `get_provenance` when `get_entity` is `None` — `test_get_entity_dossier_none_when_absent` pins that an
   absent entity does one read, not three.
3. **`merge_history` sentinel is a constant**, not derived — `{"status": "not_assembled", "available":
   false}`. Do not read Postgres; do not add a `Session` param to the helper.
4. **Parity test determinism (§6.4).** Drive both surfaces off the **same** fake so the comparison is a
   pure function of graph state (no wall-clock in the payload — §3.4). Deep-equal the decoded JSON.
5. **MCP `structured_output` fallback.** As in F-2, pin `get_entity_dossier`'s `outputSchema is not None`
   (a `dict[str, Any]` return builds a permissive object schema); a silent fallback would surface here.
6. **`hops` clamp both surfaces** via `read_guards.clamp_hops` (mirrors `read_neighbors` /
   `tool_get_neighbors`); do not re-implement a literal cap. The dossier needs **no** new `read_guards`
   constant.
7. **Hermes config comment.** Update `deploy/hermes/config.yaml:11`'s trailing comment to "5 read tools"
   when adding `get_entity_dossier` to `tools.include`.
8. **Run full `pytest -m "not integration"` + the MCP stdio and REST graph integration suites locally**
   (Docker is available here — testcontainers). Run `ruff format --check .` repo-wide before push.
