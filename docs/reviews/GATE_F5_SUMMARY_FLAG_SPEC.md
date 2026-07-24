# Gate F-5 — `summary` context-budget flag on `get_neighbors` / `find_paths`

> Backlog: `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-5** — "`summary` context-budget flag —
> `{count, sample[3]}` on `get_neighbors`/`find_paths`, shared helper so REST + MCP stay lockstep. **One
> gate. P1 / S**."
> ADR: `docs/decisions/0124-summary-context-budget-flag.md` (PROPOSED → ACCEPTED at the merging PR, per
> the 0117-0123 convention).
> Predecessors relied on: ADR 0062 (REST read routes), ADR 0063 (stdio MCP + shared `read_guards`),
> ADR 0064 (read caps — `NEIGHBOR_RESULT_LIMIT`/`PATH_RESULT_LIMIT`, and the **no-`ORDER BY`** decision),
> ADR 0090 (authenticated HTTP MCP), ADR 0121 / Gate F-2 (MCP annotations + typed output schemas +
> `{error, hint}` envelopes), ADR 0122 / Gate F-3 (`get_entity_dossier` — the shared-helper REST↔MCP
> lockstep + parity-test precedent), ADR 0042 (single-tenant), ADR 0095 (statement log = SoR; Neo4j =
> derived projection).

## 0. What this gate is (and is NOT)

**Is:** ONE additive, opt-in **context-budget flag** on the two *list-returning* graph reads —
`get_neighbors` and `find_paths` — exposed on **both** surfaces (REST query param `?summary=true`; MCP
tool arg `summary: bool = False`). When set, the surface returns a compact `{count, sample}` envelope
instead of the full list. Exactly one **shared, pure** helper (`graph/queries.py::summarize_result`)
produces the `{count, sample}` payload, so REST and MCP are byte-identical by construction (the F-3
lockstep convention). Read-only, zero new Cypher, zero egress, zero resolution/scoring.

**Is NOT** (explicit non-goals — see §7): **F-10 JMESPath response projection** (server-side field
projection — a *separate* gate, queued **behind** F-5, NOT part of it); any change to `get_entity` /
`get_provenance` / `get_entity_dossier` (the dossier stays **full-fat** — §3.6); any change to the
**normal-mode** (summary-absent) payload bytes; any `ORDER BY` / pagination on the underlying queries
(ADR 0064 deferred that; §3.4); any new per-field typed model; any `mcp`/FastMCP version bump.

**The MCP tool count stays FIVE.** No tool is added or removed — `summary` is a new *argument* on two
existing tools. Gate F-2's PP-3 / F-3's AC-4 "exactly five tools" pins stay **green untouched**.

---

## 1. What is true TODAY (verified from source + empirical SDK/FastAPI probes — do NOT re-derive)

### 1.1 The two list surfaces

| Surface | Function | Return annotation TODAY | Body TODAY |
|---|---|---|---|
| REST | `api/graph.py::read_neighbors` | `dict[str, list[dict[str, Any]]]` | `{"neighbors": [...]}` |
| REST | `api/graph.py::read_paths` | `dict[str, list[dict[str, Any]]]` | `{"paths": [...]}` |
| MCP  | `mcp/server.py::tool_get_neighbors` | `list[dict[str, Any]]` | list of neighbour dicts |
| MCP  | `mcp/server.py::tool_find_paths` | `list[dict[str, Any]]` | list of `{nodes, relationships}` |

Both list tools are registered in the shared `_register_read_tools` with `structured_output=True`
(ADR 0121). For a `list[dict[str, Any]]` return the SDK derives `outputSchema =
{"type":"object","properties":{"result":{"type":"array",…}},"required":["result"]}`, emits **one content
block per item**, and sets `structuredContent = {"result":[…]}`.

### 1.2 The `structured_output` conflict — the crux of this gate (empirically verified, mcp 1.28.1)

A summary return is a **dict** `{count, sample}` while normal mode returns a **list**. A single tool has
**one** static return annotation driving **one** `outputSchema`, so the two shapes collide. Probes
(`scratchpad/probe_sdk.py`, `probe2.py`) established, against the installed `mcp==1.28.1`:

- **P-a — a dict returned from a `list[dict]`-annotated tool FAILS.** `convert_result` validates the
  returned value against the list output-model `{"result":[…]}`; a bare `{count, sample}` raises
  `ToolError: … 1 validation error for <tool>Output`. So you **cannot** just conditionally return a dict
  from a list-annotated tool.
- **P-b — a UNION return annotation `list[dict[str, Any]] | dict[str, Any]` is byte-transparent in
  normal mode.** With that annotation, a **list** return still yields: N content blocks (one per item,
  byte-identical), and `structuredContent = {"result":[…]}` — **identical** to today. A **dict** return
  yields: ONE content block = the bare `{count, sample}`, and `structuredContent = {"result": {count,
  sample}}` (the dict wrapped under `result`). **The ONLY thing that changes is the static
  `outputSchema`:** `result` goes from `{"type":"array",…}` to
  `{"anyOf":[{array},{object}],"title":"Result"}`.
- **P-c — FastAPI uses the return annotation as the response_model.** Returning `{count, sample}` from a
  route annotated `dict[str, list[dict[str, Any]]]` is a **500** (`ResponseValidationError`: `count` is an
  `int`, not a `list`). Widening the annotation to `dict[str, Any]` makes both `{"neighbors":[…]}`
  (unchanged, 200) and `{count, sample}` (200) serialize cleanly. Widening is backward-compatible: the
  normal payload is byte-identical.

### 1.3 The underlying query order is NOT deterministic today (drives §3.4)

`get_neighbors` — `MATCH … RETURN DISTINCT properties(m) … LIMIT 500` — has **no `ORDER BY`**.
`find_paths` — `shortestPath(…) … LIMIT 50` — has **no `ORDER BY`**. ADR 0064 recorded this explicitly:
"The `LIMIT` has **no `ORDER BY`**: it returns an arbitrary bounded subset … deterministic ordering /
pagination is a noted future enhancement, not v1 scope." So there is **no** "existing deterministic query
order" to take a first-3 from; determinism must be established at the summary layer (§3.4).

---

## 2. Scope (exact files)

| Path | Why (in scope) |
|---|---|
| `src/worldmonitor/graph/queries.py` | **The one shared helper** `summarize_result(items, *, sample_size=3) -> dict[str, Any]` (pure; no client, no Cypher) → `{"count": len(items), "sample": <deterministic first ≤3>}`. **The existing `get_neighbors`/`find_paths` Cypher is NOT touched** (no `ORDER BY`, ADR 0064). |
| `src/worldmonitor/api/graph.py` | `read_neighbors` + `read_paths` gain `summary: bool = False` query param; return annotation widened `dict[str, list[dict[str, Any]]]` → `dict[str, Any]` (§1.2 P-c); summary branch returns `summarize_result(...)`. |
| `src/worldmonitor/mcp/server.py` | `tool_get_neighbors` + `tool_find_paths` (and their closures in `_register_read_tools`) gain `summary: bool = False`; return annotation widened `list[dict[str, Any]]` → `list[dict[str, Any]] \| dict[str, Any]` (§1.2 P-b); summary branch returns `summarize_result(...)`. |
| `tests/unit/test_graph_queries.py` | Unit: `summarize_result` count/sample/cap/determinism. |
| `tests/unit/test_api_graph.py` | Unit: REST summary (neighbors + paths); normal-mode envelopes still `{"neighbors"}`/`{"paths"}`; **both REST↔MCP summary parity tests** (§6.4). |
| `tests/unit/test_mcp_server.py` | Unit: MCP summary (neighbors + paths); the **outputSchema pin update** (§6.1, the ONE sanctioned break); PP-1 normal-mode pins stay green. |
| `tests/property/test_prop_summary_count_consistency.py` | **NEW `@given`** metamorphic — `count == len(full)`, sample ⊆ full, `len(sample) ≤ 3` (§6.2). |
| `tests/integration/test_mcp_stdio.py` | Integration: summary over the wire (§6.3). Set-assertions **unchanged** (still five tools). |
| `docs/decisions/0124-summary-context-budget-flag.md` | This gate's ADR (PROPOSED → ACCEPTED at merge). |
| `docs/decisions/README.md` | Regenerated ADR index (`scripts/gen_adr_index.py`) — the `adr-index` CI check. |
| `docs/reviews/GATE_F5_SUMMARY_FLAG_SPEC.md` | This spec. |

Optional (nice-to-have, not required for green): `tests/property/test_prop_mcp_stdout_purity.py` — add a
`summary=True` leg to the neighbours/paths stdout-purity coverage (summary is a happy path, adds no error
path, so this is belt-and-suspenders, not mandatory).

**Out of scope / must NOT change:** `graph/read_guards.py` (reuse the existing caps; no new guard
constant — `sample_size=3` is a default arg on `summarize_result`, not a guard); the `get_neighbors` /
`find_paths` **Cypher** (no `ORDER BY`); `get_entity` / `get_provenance` / `get_entity_dossier` and their
routes/tools (the dossier stays full-fat, §3.6); `mcp/auth.py`; `mcp/server.py`'s annotations / `{error,
hint}` envelope / stdout-config; the F-2 PP-1 content-parity pins; both existing MCP property tests as
regression pins.

> **F-1 fleet coordination:** F-1's fleet currently owns `.claude/gate.scope`. Per the coordinator's
> instruction this spec does **not** write that file — the scope lines are listed in §10 for the
> coordinator to write when the F-5 fleet starts. F-5 and F-1 both edit `mcp/server.py` and
> `api/graph.py`; sequence F-5 **after** F-1 lands (or rebase) so the two gates don't collide.

---

## 3. Locked invariants + design decisions

### 3.1 CLAUDE.md invariants (carried, not relitigated)

- **G1 provenance — NOT laundered.** Summary mode reduces **cardinality**, never per-record provenance:
  each element of `sample` is a **full** neighbour/path dict carrying its own `prov_*` verbatim (same
  bytes the full list would carry), and `count` is a scalar. This is the same lossiness class as ADR
  0064's `LIMIT` (a bounded subset), not a provenance-stripping projection (which would be F-10, a
  non-goal). The full, provenance-complete list remains available by omitting the flag; the **dossier**
  stays full-fat (§3.6).
- **Append-only / read-only.** `summarize_result` is pure (a list→dict transform); the surfaces still
  call `execute_read` only. The recording-fake write-path guard tests stay green.
- **Canonical-canonical only via the guard — untouched.** No resolution, merge, or canonicalisation on
  this path.
- **Bounded / no-injection — untouched.** id-shape validation and `clamp_hops` are unchanged and run
  **before** any read exactly as today; `summary` is applied to the already-bounded result **after** the
  read. The bound is unchanged: `count` ≤ the existing cap (`NEIGHBOR_RESULT_LIMIT` / `PATH_RESULT_LIMIT`).
- **STDOUT PURITY (stdio) — untouched.** `summary` adds no new raise path; existing stdout-purity holds.

### 3.2 One shared helper — REST + MCP lockstep (the F-3 convention)

There is **exactly one** place the summary payload is shaped: `graph/queries.py::summarize_result`. Both
surfaces call it on the list they already fetched; neither shapes it independently, so the two can never
drift. The **parity tests** (§6.4) pin this: for the same seeded input, the REST summary body deep-equals
the MCP tool's summary return — both `{count, sample}`. Mirrors F-3's `test_dossier_rest_mcp_parity`.

`summarize_result` takes **only** an in-memory `list[dict]` (the dependency both surfaces already hold —
the fetched result), not a client and not a `Session`. It performs no I/O, so it is deterministic and
trivially unit/property-testable.

### 3.3 The summary payload shape (locked)

```jsonc
{
  "count":  <int>,                 // len(the full bounded result the non-summary call would return)
  "sample": [ /* ≤ sample_size (=3) full neighbour/path dicts, each with its prov_* verbatim */ ]
}
```

- `count` is the length of the **full** result list (the same list normal mode returns), i.e. bounded by
  the existing `NEIGHBOR_RESULT_LIMIT` (500) / `PATH_RESULT_LIMIT` (50). It is NOT a graph-wide degree —
  it is honestly "how many the full call would return, under the existing cap" (documented; revisit
  trigger in ADR 0124 if a true unbounded degree is ever wanted — that is a `count(*)` query, out here).
- `sample` elements are the **existing** full dicts (no new projection logic — that's F-10), just fewer.
- `sample_size` is fixed at **3** (the backlog literal `sample[3]`) as `summarize_result`'s default arg;
  not configurable in v1 (revisit trigger: promote to a `read_guards` constant / setting if needed).
- `len(sample) == min(sample_size, count)`.

### 3.4 Determinism — DECISION: deterministic sample via an in-helper canonical sort (no query change)

The underlying query order is **non-deterministic today** (§1.3, ADR 0064 deferred `ORDER BY`).
`summarize_result` establishes a deterministic sample **without touching the Cypher**:

```
sample = sorted(items, key=lambda d: json.dumps(d, sort_keys=True, default=str))[:sample_size]
```

- **Field-agnostic total order:** the canonical-JSON string is a total order over any JSON-serialisable
  dict, so ONE `summarize_result` works for both neighbours (dicts keyed by `id` + props) and paths
  (`{nodes, relationships}`) — no per-caller sort key, no assumption about fields.
- **Guarantee:** for a result **at or below** the existing cap (the overwhelming common case), the full
  result SET is deterministic (Neo4j returns all matches under the `LIMIT`), so `sorted(...)[:3]` is a
  **fully deterministic** sample — two identical calls return the identical 3 samples.
- **Honest residual:** for a hub node **above** the cap, the *set* returned is ADR-0064
  arbitrary-bounded-subset non-deterministic, so the sample inherits that residual. F-5 introduces **no
  new** non-determinism; it removes it for the common case.
- **Why not `ORDER BY` in the query (the task's fallback):** adding `ORDER BY` to the shared
  `get_neighbors`/`find_paths` Cypher would (a) reopen ADR 0064's deliberately-deferred sort-cost
  decision, (b) change the truncation *selection* for `>`cap hubs in **normal** mode (a payload-selection
  behaviour change beyond F-5's remit), and (c) enlarge a P1/S gate. The in-helper sort delivers the
  determinism the flag needs at a fraction of the blast radius. `ORDER BY` + keyset pagination is the
  recorded revisit trigger (ADR 0124), aligned with ADR 0064's own deferral.

### 3.5 The MCP `outputSchema` change — DECISION: option (b), a union return annotation

Per §1.2, the coherent, least-breaking way to let one tool return either a list or `{count, sample}` is a
**union return annotation** `list[dict[str, Any]] | dict[str, Any]`. Empirically (P-b) this:

- **preserves every normal-mode byte** — N content blocks, byte-identical text, `structuredContent =
  {"result":[…]}`, so **all F-2 PP-1 content-parity pins stay green when `summary` is absent** (the
  compatibility requirement); and
- **changes ONLY the static `outputSchema`** — `result` becomes `{"anyOf":[{array},{object}]}` instead of
  `{"type":"array"}`. This is **additive contract evolution** (a superset that still admits the array),
  explicitly ALLOWED by the coordinator; the ONE pin that asserts the old array shape is updated (§6.1).
- Summary-mode `structuredContent` is `{"result": {count, sample}}` (the SDK wraps the dict under
  `result`); the summary **content block** is the bare `{count, sample}` (byte-identical to the helper),
  which is what the wire parity/summary tests assert (§6.3), mirroring F-2 PP-1 (content, not
  structuredContent, is the byte-parity oracle).

Rejected alternatives (why not): **(a)** "list truncated + a count" — a count cannot ride a bare list;
**(c) full-union with `ORDER BY`, or MCP-gets-a-different-arg** — a divergent MCP arg breaks the backlog's
"REST + MCP stay lockstep" and the shared-helper story. See ADR 0124 Alternatives.

### 3.6 The dossier stays full-fat (DECISION: out of scope)

`get_entity_dossier` composes `get_neighbors` **directly** (calling the query helper, not the
route/tool), and does so **without** the `summary` flag — the flag lives at the surface layer
(`read_neighbors` / `tool_get_neighbors`), not inside the `get_neighbors` query helper. So the dossier's
`neighbors` section is unaffected and remains the full bounded list. Adopting the summary helper into the
dossier is a **later** enhancement (F-3's own recorded revisit trigger), not F-5.

### 3.7 Property-test discipline — DECISION: no invariant touched; ONE cheap metamorphic pin included

F-5 touches **no** CLAUDE.md invariant (no provenance stamping change, no ER/merge/threshold, no
canonical-id, no write) — it is read-only **shaping** of an already-bounded read, surfacing *less* data,
never more (§3.1). So the mandatory-`@given` build-discipline rule does **not** apply. This is a
**decision, not an omission** (mirroring F-2 §3.1). We nonetheless include **one** cheap metamorphic
`@given` (§6.2) because the load-bearing correctness guarantee — *the summary must never disagree with the
full list* (`count == len(full)`) — is exactly a metamorphic relation and is essentially free to pin.

---

## 4. Surface behaviour (thin, both call the ONE helper)

| Surface | Signature (added `summary`) | `summary=False` (unchanged) | `summary=True` |
|---|---|---|---|
| REST neighbors | `GET /entities/{id}/neighbors?hops=1&summary=false` → `dict[str, Any]` | `{"neighbors": get_neighbors(...)}` (byte-identical to today) | `summarize_result(get_neighbors(...))` |
| REST paths | `GET /paths?from=&to=&max_hops=1&summary=false` → `dict[str, Any]` | `{"paths": find_paths(...)}` (byte-identical) | `summarize_result(find_paths(...))` |
| MCP neighbors | `tool_get_neighbors(client, entity_id, hops=1, summary=False)` → `list[dict[str, Any]] \| dict[str, Any]` | `get_neighbors(...)` (list; byte-identical) | `summarize_result(get_neighbors(...))` |
| MCP paths | `tool_find_paths(client, from_id, to_id, max_hops=1, summary=False)` → `list[dict[str, Any]] \| dict[str, Any]` | `find_paths(...)` (list; byte-identical) | `summarize_result(find_paths(...))` |

- REST param: `summary: Annotated[bool, Query()] = False`. MCP arg: `summary: bool = False` (surfaces as
  an optional boolean in the tool `inputSchema` — no pin asserts its absence).
- id-shape validation + `clamp_hops` run first, unchanged; `summarize_result` is applied to the fetched,
  already-bounded list.
- The MCP closures in `_register_read_tools` mirror the thin-fn signatures (add `summary`, widen the
  return annotation); the F-2 annotations / `title` / `structured_output=True` / `{error, hint}` envelope
  are unchanged.

---

## 5. Acceptance criteria (crisp)

- **AC-1 shared helper.** `graph/queries.py::summarize_result(items, *, sample_size=3)` returns
  `{"count": len(items), "sample": <deterministic canonical-sorted first ≤ sample_size>}`; pure (no
  client, no Cypher, no I/O); the existing `get_neighbors`/`find_paths` Cypher is unchanged.
- **AC-2 REST.** `?summary=true` on `/entities/{id}/neighbors` and `/paths` returns 200 + `{count,
  sample}`; **without** `summary` (or `summary=false`) the body is **byte-identical** to today
  (`{"neighbors":[…]}` / `{"paths":[…]}`); auth/id-validation/clamp unchanged.
- **AC-3 MCP.** `summary=True` on `get_neighbors`/`find_paths` returns `{count, sample}` (thin fn) /
  a one-block `{count, sample}` content + `structuredContent={"result":{count,sample}}` (wire); **without**
  `summary` the response is byte-identical to today (N blocks, `structuredContent={"result":[…]}`); both
  tools carry a non-null `outputSchema` whose `result` admits the array branch (the `anyOf`).
- **AC-4 lockstep parity.** For the same seeded input, the REST summary body deep-equals the MCP tool's
  summary return (`{count, sample}`) — for **both** neighbors and paths (§6.4).
- **AC-5 count-consistency metamorphic.** The `@given` pin holds: `summarize_result(x)["count"] ==
  len(x)`, `sample` is a subsequence-by-membership of `x`, and `len(sample) == min(3, len(x))` — for
  arbitrary `x` (§6.2).
- **AC-6 determinism.** `summarize_result(x)` is a pure function of `x` (same `x` → same `{count,
  sample}`), and `sample` is stable under input reordering of the same set (canonical sort).
- **AC-7 tool set unchanged / no regression.** The five-tool set pins, all F-2 PP-1 content-parity pins,
  the REST normal-mode envelope pins, and both MCP property tests stay green — the **only** sanctioned
  edit to an existing pin is the single outputSchema-shape assertion in §6.1. The dossier is unchanged.

---

## 6. Named tests + the full sanctioned pin-update list

### 6.1 PIN-UPDATE LIST — the ONE existing pin that changes (TEST-AUTHOR ONLY)

The union annotation changes exactly one existing assertion. The **test-author** makes this edit; the
**builder** never edits a pin to make its own code pass.

| # | File : locus | Current assertion | Action |
|---|---|---|---|
| P-1 | `tests/unit/test_mcp_server.py::test_all_tools_have_output_schema` — the `for name in ("get_neighbors", "find_paths")` branch (currently `assert result_prop is not None and result_prop.get("type") == "array"`) | pins `result.type == "array"` | Relax to accept the union: `result_prop is not None` **and** (`result_prop.get("type") == "array"` **or** `any(b.get("type") == "array" for b in result_prop.get("anyOf", []))`). Keeps the "still describes an array" guarantee while admitting the `object` (summary) branch. |

**Verified NOT pins (stay green untouched — do NOT edit):**

- **All F-2 PP-1 content-parity tests** (`test_get_neighbors_content_and_structured_content_pp1`,
  `test_find_paths_content_and_structured_content_pp1` in `test_mcp_server.py`; `test_stdio_happy_path_content_unchanged`
  in `test_mcp_stdio.py`) — they drive **normal** mode (`summary` defaults `False`); probe P-b proves N
  blocks + `structuredContent={"result":[…]}` are byte-identical under the union.
- **Five-tool set pins** (`test_tool_set_is_exactly_the_five`, `_ALL_TOOL_NAMES`, the HTTP/stdio set
  assertions in `test_mcp_http_auth.py`, the wire `assert names == {…}` in `test_mcp_stdio.py`) — no
  tool is added/removed.
- **`test_http_tools_carry_same_annotations_and_schema_as_stdio`** (`test_mcp_http_auth.py`) — asserts
  `outputSchema is not None` (union is non-null) and annotation parity; unchanged.
- **`test_stdio_tools_list_advertises_annotations_and_schema`** — asserts `outputSchema is not None` only.
- **REST normal-mode envelope pins** (`test_neighbors_envelope_is_neighbors_list`,
  `test_paths_envelope_is_paths_list`) — normal mode still returns `{"neighbors"}` / `{"paths"}`.
- **`get_entity` / `get_provenance` / `get_entity_dossier` schema branches** of
  `test_all_tools_have_output_schema` — those tools are untouched.
- **No OpenAPI/response-model CI drift check exists** (F-7 unbuilt), and `VERIFIED_API.md` does not
  enumerate the neighbors/paths bodies — so the REST return-annotation widening needs no doc/CI update.
  There is no `compose-boot` MCP live-smoke assertion yet (F-8 unbuilt).

### 6.2 NEW property test (chosen metamorphic pin) — `tests/property/test_prop_summary_count_consistency.py`

- `test_prop_summary_count_equals_full_len` — `@given` a `list[dict]` of arbitrary JSON-serialisable
  dicts, assert on `summarize_result(items)`: (a) `["count"] == len(items)`; (b) every element of
  `["sample"]` is an element of `items`; (c) `len(["sample"]) == min(3, len(items))`; (d) determinism:
  `summarize_result(items) == summarize_result(items)` and `== summarize_result(list(reversed(items)))`
  (stable under input reordering — the canonical sort). Pure in-process; **no DB, no container** (heed
  the memory note about connection leaks — not applicable here, but keep the body allocation-free of any
  client).

### 6.3 Integration — stdio wire (`tests/integration/test_mcp_stdio.py`)

- `test_stdio_get_neighbors_summary` — over the spawned server against a real Neo4j testcontainer, seed a
  node with ≥1 neighbour; `tools/call get_neighbors {entity_id: "p1", summary: true}` → not `isError`;
  the single content block decodes to `{count, sample}` with `count == len(queries.get_neighbors(client,
  entity_id="p1", hops=1))`, `isinstance(sample, list)`, `len(sample) <= 3`, and each `sample` element
  carries `prov_source_id` (provenance not laundered). Optionally a `find_paths {... , summary: true}`
  leg with the analogous assertions. **The wire set-assertions in this file are unchanged (still five
  tools).**

### 6.4 REST↔MCP parity + REST unit (`tests/unit/test_api_graph.py`)

- `test_neighbors_summary_rest_mcp_parity` — with **one** shared recording fake carrying ≥4 neighbours
  (so the cap-at-3 sample is exercised): call REST `/entities/A/neighbors?summary=true` (via `TestClient`)
  and `tool_get_neighbors(fake, "A", summary=True)` (imported locally, fail-soft per the F-3 idiom);
  assert the REST body deep-equals the MCP return, and both `== {"count": 4, "sample": <3 dicts>}`.
- `test_paths_summary_rest_mcp_parity` — the analogous parity for `find_paths` (`/paths?...&summary=true`
  vs `tool_find_paths(fake, "A", "B", ..., summary=True)`).
- `test_neighbors_summary_shape` — REST `?summary=true` returns exactly the keys `{"count", "sample"}`,
  `count == len(seeded)`, `len(sample) <= 3`, sample elements carry `prov_*`.
- `test_paths_summary_shape` — analogous for `/paths`.
- `test_neighbors_normal_mode_unchanged` — `/entities/A/neighbors` (no `summary`) body is still exactly
  `{"neighbors": [...]}` (regression: the widened annotation didn't change normal output). *(May reuse
  the existing `test_neighbors_envelope_is_neighbors_list`.)*

### 6.5 Unit — shared helper (`tests/unit/test_graph_queries.py`)

- `test_summarize_result_count_and_sample` — `summarize_result([…5 dicts…])` → `count == 5`, `len(sample)
  == 3`, each `sample` element ∈ input.
- `test_summarize_result_below_sample_size` — a 2-element input → `count == 2`, `len(sample) == 2`.
- `test_summarize_result_empty` — `[]` → `{"count": 0, "sample": []}`.
- `test_summarize_result_deterministic` — same set, different order → identical `{count, sample}`.

### 6.6 Unit — MCP tools (`tests/unit/test_mcp_server.py`, beyond the §6.1 pin)

- `test_get_neighbors_summary_returns_envelope` — `tool_get_neighbors(fake, "A", summary=True)` → `{count,
  sample}` (`count == len(seeded)`, `len(sample) <= 3`, sample elements carry `prov_*`).
- `test_find_paths_summary_returns_envelope` — analogous for `tool_find_paths(..., summary=True)`.
- `test_get_neighbors_summary_read_only` — driving the summary path touches only `execute_read`.
- **Regression (keep green):** the two PP-1 content-parity tests, the five-tool set test, the read-only /
  injection / clamp tests.

---

## 7. NON-goals (explicit — do NOT build here)

- **F-10 JMESPath response projection** — server-side *field* projection. Queued **behind** F-5, a
  separate gate; `sample` here is the **full** dict with fewer rows, never a projected/sub-selected
  field set. Do not add any projection param.
- **The dossier adopting summary** — `get_entity_dossier` stays full-fat (§3.6); a later enhancement.
- **`ORDER BY` / keyset pagination on the underlying queries** — ADR 0064 deferred it; determinism is met
  at the summary layer (§3.4). Recorded revisit trigger in ADR 0124.
- **A true unbounded degree count** (`count(*)` ignoring the cap) — `count` here is the length of the
  existing bounded result (§3.3). A separate query if ever wanted.
- **A configurable `sample_size` / a new `read_guards` constant** — fixed at 3 (backlog literal); revisit
  trigger only.
- **Any `get_entity` / `get_provenance` change, any new tool, any `mcp` version bump, any change to the
  F-2 annotations / `{error, hint}` envelope.**

---

## 8. Slice breakdown

**ONE slice** (P1/S). The shared helper, both surfaces, the parity + property tests, and the single pin
update are tightly coupled — the parity tests (§6.4) cannot pass until *both* surfaces exist, and
splitting REST from MCP would ship a half-lockstep flag. All lands as one individually-mergeable PR.

- **Slice 1 — `summary` context-budget flag (REST + MCP + shared helper).** Production: (a)
  `graph/queries.py::summarize_result` (pure `{count, sample}`, canonical-sorted deterministic sample,
  `sample_size=3`; existing Cypher untouched); (b) `api/graph.py` — `read_neighbors` / `read_paths` gain
  `summary: bool = False` + widened `dict[str, Any]` annotation + summary branch; (c) `mcp/server.py` —
  `tool_get_neighbors` / `tool_find_paths` (+ their `_register_read_tools` closures) gain `summary: bool =
  False` + union return annotation + summary branch. Tests: all of §6 (incl. the §6.1 pin update by the
  test-author, the §6.2 metamorphic `@given`, and the §6.4 parity tests). ADR 0124 → **ACCEPTED** at the
  merging PR; regenerate the ADR index.

**No sanctioned split.** If a reviewer insists on two PRs, the only clean seam is "helper + REST first;
MCP + pin update + parity second" — but that defers the lockstep guarantee, so the default is one slice.

---

## 9. Open items for the test-author / builder

1. **Union annotation, not two functions.** The MCP tools keep ONE function each with return annotation
   `list[dict[str, Any]] | dict[str, Any]`; probe P-b proves normal mode stays byte-identical. Do **not**
   register a second/variant tool.
2. **REST annotation MUST widen** to `dict[str, Any]` (probe P-c: the narrow annotation 500s on the summary
   return). Confirm the normal-mode body is unchanged after widening (`test_neighbors_envelope_is_neighbors_list`
   stays green).
3. **`summarize_result` is pure and lives in `queries.py`** — no `Neo4jClient`, no `Session`, no Cypher.
   It does NOT modify `get_neighbors`/`find_paths`. Apply it at the **surface** layer so the dossier
   (which calls `get_neighbors` directly) is unaffected (§3.6).
4. **Determinism = in-helper canonical sort** (`json.dumps(..., sort_keys=True, default=str)`), NOT a
   query `ORDER BY` (§3.4). Pin it with the reorder-invariance assertion (§6.2 d).
5. **The single pin update (§6.1 P-1)** is the ONLY existing pin the test-author touches; after the
   builder runs, `git diff` `test_all_tools_have_output_schema` and confirm no other pin changed, and that
   all F-2 PP-1 content-parity tests are still green.
6. **`count` semantics** = `len(the full bounded result)`, under the existing `NEIGHBOR_RESULT_LIMIT` /
   `PATH_RESULT_LIMIT` — not a graph-wide degree (§3.3). Assert `count == len(get_neighbors(...))` in the
   integration test.
7. **Parity drives the SAME fake for both surfaces** (§6.4), deep-equal the decoded JSON — the summary
   payload is a pure function of the fetched list (no wall-clock, no non-graph field).
8. **Run full `pytest -m "not integration"` + the MCP stdio and REST graph integration suites locally**
   (Docker is available — testcontainers). Run `ruff format --check .` repo-wide before push. Sequence
   after F-1 lands (shared `mcp/server.py` + `api/graph.py`); rebase on master if needed.

---

## 10. gate.scope (to apply at fleet start)

> The F-1 fleet currently owns `.claude/gate.scope`; this gate does NOT write it. The coordinator writes
> these lines (one path glob per line) into `.claude/gate.scope` when the F-5 fleet starts:

```
src/worldmonitor/graph/queries.py
src/worldmonitor/api/graph.py
src/worldmonitor/mcp/server.py
tests/unit/test_graph_queries.py
tests/unit/test_api_graph.py
tests/unit/test_mcp_server.py
tests/property/test_prop_summary_count_consistency.py
tests/property/test_prop_mcp_stdout_purity.py
tests/integration/test_mcp_stdio.py
docs/decisions/0124-summary-context-budget-flag.md
docs/decisions/README.md
docs/reviews/GATE_F5_SUMMARY_FLAG_SPEC.md
```
