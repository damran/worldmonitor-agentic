# Gate F-2 — MCP contract polish

> Backlog: `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-2** — "MCP contract polish —
> `readOnlyHint`/`idempotentHint` annotations, typed output schemas, structured `{error, hint}`
> envelopes on our 4 tools. One XS gate, no behavior change. **P0 / XS**."
> Roadmap: `docs/40_ROADMAP.md` (CTI on-ramp bullet) — "**F-2** is queued, scheduled post-S4."
> Verified 2026-07-23: that line still stands; S-4 (Ransomware.live, ADR 0120) closed at `875960d`.
> ADR: `docs/decisions/0121-mcp-contract-polish.md` (PROPOSED).

## 0. What this gate is (and is NOT)

**Is:** additive, metadata-only polish to the **existing four** graph-read MCP tools, applied in the
**one** place they are registered (`_register_read_tools` in `src/worldmonitor/mcp/server.py`, shared
by both the stdio and HTTP transports). Three concrete additions:

1. **Behavioral annotations** — `readOnlyHint`, `idempotentHint`, `openWorldHint` (+ a human-readable
   `title`) on every tool, so a host/agent can reason about the tools without calling them.
2. **Typed output schemas** — enable the SDK's native structured-output so each tool advertises an
   `outputSchema` and returns a `structuredContent` alongside the existing text payload.
3. **Structured error envelope** — replace the four bare `ToolError("…")` messages with a
   JSON `{"error", "hint"}` envelope, so error content is machine-parseable and actionable.

**Is NOT** (explicit non-goals — see §7): the F-5 `summary` context-budget flag, F-8 CI live-smoke,
F-9 `describe_tool`, F-10 JMESPath projection, F-11 resources, F-12 client module, **any `mcp`/FastMCP
version upgrade** (the installed SDK already supports everything this gate needs — see §2), any change
to the four tools' **happy-path payload bytes**, and any richer per-field typed models that would
reshape those payloads.

**No new tool is added.** The current surface is exactly four tools (`get_entity`, `get_neighbors`,
`get_provenance`, `find_paths`). F-1's freshness MCP tool is a *separate, not-yet-built* backlog row;
this gate must keep the tool set at exactly four.

---

## 1. Installed SDK — what is natively supported (verified, do NOT re-derive)

- **Package:** the official **`mcp` SDK, version `1.28.1`** (`pyproject.toml`: `mcp>=1.28`; `uv.lock`:
  `1.28.1`). Our server imports `from mcp.server.fastmcp import FastMCP` — this is FastMCP **bundled in
  the `mcp` SDK**, *not* the standalone `fastmcp` PyPI package. Do not confuse the two.
- **Annotations — NATIVE.** `FastMCP.add_tool(fn, name=, title=, description=, annotations=…,
  structured_output=…)` accepts `annotations: mcp.types.ToolAnnotations | None`. `ToolAnnotations`
  (in `mcp/types.py`) has fields `title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`,
  `openWorldHint`. `list_tools()` surfaces them on each `MCPTool.annotations`, and `tools/list` puts
  them on the wire.
- **Typed output schemas — NATIVE.** `add_tool(..., structured_output=True)` derives an `outputSchema`
  from the function's **return annotation** and makes the tool return **both** unstructured `content`
  **and** `structuredContent`. Derivation rules that matter here (`mcp/server/fastmcp/utilities/func_metadata.py`):
  - `dict[str, Any]` / `dict[str, str]` (string keys) → a RootModel object schema; `structuredContent`
    is **the dict itself** (no wrapper). `dict[str, str]` schema constrains values to strings.
  - `list[dict[str, Any]]` → a **wrapped** model; `structuredContent` is `{"result": [ … ]}`. The
    `outputSchema` is `{"type":"object","properties":{"result":{"type":"array",…}}}`.
  - If the SDK cannot build a schema for a return type it **silently falls back to no schema**
    (`outputSchema` stays `None`). Our four return types all build successfully — but the test-author
    MUST pin `outputSchema is not None` per tool to catch a silent fallback (acceptance AC-2).
- **Conclusion: no upgrade is necessary.** Both asks are first-class in `1.28.1`. If (contrary to the
  above) a needed capability were missing, an upgrade would be a **flagged decision in ADR 0121**, not
  a silent bump. It is not missing; no upgrade is proposed.

### 1.1 The load-bearing "no behavior change" fact (verified from SDK source)

When `structured_output=True`, `FuncMetadata.convert_result` computes the unstructured content with the
**same** call it uses today — `_convert_to_content(result)` on the **same raw helper output** — and only
*additionally* returns `structuredContent`. Therefore:

> **The existing `CallToolResult.content` text block(s) are byte-identical before and after this gate.**
> `structuredContent` (on results) and `outputSchema` / `annotations` (on the tool listing) are **new,
> additive** fields — they are the gate's deliverable, not a violation of "no behavior change."

Nuance the parity-test author must know: `_convert_to_content` renders a **list** return as **one text
block per item** (each `pydantic_core.to_json(item, fallback=str, indent=2)`), not a single JSON-array
block. So `get_neighbors`/`find_paths` today already emit N text blocks; that stays exactly N blocks.

### 1.2 The error-envelope constraint (verified from SDK source) — drives the design in §5

A `ToolError` raised **inside** a tool is re-wrapped by `Tool.run` as
`ToolError(f"Error executing tool {name}: {original}")`, then surfaced by the low-level handler as
`isError=true` + `TextContent(text=<that string>)`. There is **no** un-prefixed raise path.

A tool may instead **return** a `CallToolResult(isError=True, …)` verbatim — but when `output_schema`
is set, `convert_result` **validates `result.structuredContent` against the success output-model**
(e.g. `list`-tools require `{"result": …}`), so a returned error envelope `{"error","hint"}` fails
validation on `get_neighbors`/`find_paths` and the client gets a messy validation string instead. So
**return-based clean envelopes and typed output schemas are mutually incompatible in `1.28.1` for the
list-returning tools.** This gate therefore uses the **raise-based** envelope (§5, ADR 0121 D3); the
`"Error executing tool <name>: "` prefix is an accepted, documented cosmetic wart with a revisit
trigger — not a blocker.

---

## 2. Scope (exact files)

Editable surface — mirrored verbatim into `.claude/gate.scope`:

| Path | Why |
|---|---|
| `src/worldmonitor/mcp/server.py` | The only production edit: `_register_read_tools` (annotations + `structured_output`), a new `_tool_error` helper, 4 raise-site swaps. |
| `tests/unit/test_mcp_server.py` | Extend: annotation + output-schema + error-envelope assertions; keep the existing read-only/tool-set regressions. |
| `tests/unit/test_mcp_http_auth.py` | Extend: HTTP surface carries the **same** annotations (no-drift, INV-S1-READONLY). |
| `tests/integration/test_mcp_stdio.py` | Extend: `tools/list` advertises annotations + `outputSchema`; happy-path content parity pin; wire-level error envelope. |
| `docs/decisions/0121-mcp-contract-polish.md` | This gate's ADR (PROPOSED). |
| `docs/decisions/README.md` | Regenerated ADR index (via `scripts/gen_adr_index.py`) — the `adr-index` CI check. |
| `docs/reviews/GATE_F2_MCP_CONTRACT_POLISH_SPEC.md` | This spec. |

**Out of scope / must not change:** `graph/queries.py`, `graph/read_guards.py`, `api/graph.py` (REST),
`mcp/auth.py`, the property tests (`tests/property/test_prop_mcp_stdout_purity.py`,
`tests/property/test_mcp_auth_boundary.py`) — they must stay **green untouched** as regression pins.

---

## 3. Locked invariants every change must hold (CLAUDE.md + the MCP gate's own INV-S1)

- **G1 provenance is untouched.** The tools still return `prov_*` verbatim from the same query helpers;
  annotations/schemas neither strip nor alter provenance. The stdio integration test's
  `prov_source_id`-present assertion stays green (and is re-pinned in `structuredContent`, AC-4).
- **Append-only / read-only (INV-S1-READONLY).** Every tool still calls `execute_read` **only**; the
  recording-fake test that explodes on any write path stays green. Annotations *declare* this
  (`readOnlyHint=true`); they must not become the *only* evidence — the structural read-only test
  remains the source of truth.
- **No drift between transports (INV-S1).** Annotations/schemas/envelope are added in the shared
  `_register_read_tools`, so the stdio (`build_server`) and HTTP (`build_http_app`) surfaces expose an
  identical annotated tool set. There remains exactly one registration site.
- **STDOUT PURITY.** Unchanged and re-pinned: the existing `@given` stdout-purity property test stays
  green; the error-envelope change routes diagnostics to stderr and JSON frames to stdout as today.
- **Bounded / no-injection.** Unchanged: id-shape validation before any read; hops clamp to the shared
  `HOP_CAP`. This gate touches none of it.

### 3.1 Property-test discipline — recorded decision: NO new property test

F-2 touches **no** CLAUDE.md invariant (provenance / ER / merge / catastrophic-merge / canonical-id).
It adds read-only **metadata** (annotations, output schemas) and restructures **error** content; it
changes no resolution logic, no threshold, no guard, no data shape, and no happy-path payload byte.
Therefore the mandatory-`@given` rule (CLAUDE.md build-discipline) does **not** apply and **no
`tests/property/` addition is required**. This absence is a **decision, not an omission**. The two
existing MCP property tests remain green as regression pins.

---

## 4. Per-tool contract table (name → annotations → output → error)

All four tools are **read-only** and interact with a **closed** domain (our own resolved graph, never an
open external world), so:

| Tool | `readOnlyHint` | `idempotentHint` | `openWorldHint` | `destructiveHint` | `title` | Return type (UNCHANGED) | `structuredContent` shape | `outputSchema` |
|---|---|---|---|---|---|---|---|---|
| `get_entity`     | `true` | `true` | `false` | *(unset)* | "Get entity" | `dict[str, Any]` | the entity dict | object (permissive) |
| `get_neighbors`  | `true` | `true` | `false` | *(unset)* | "Get neighbors" | `list[dict[str, Any]]` | `{"result": [ … ]}` | object w/ `result: array` |
| `get_provenance` | `true` | `true` | `false` | *(unset)* | "Get provenance" | `dict[str, str]` | the prov dict | object, string values |
| `find_paths`     | `true` | `true` | `false` | *(unset)* | "Find paths" | `list[dict[str, Any]]` | `{"result": [ … ]}` | object w/ `result: array` |

**Justifications**

- **`readOnlyHint=true`** — structurally proven: every tool calls `client.execute_read` only (the
  recording-fake test raises on any write path). This is the tool set's defining invariant.
- **`idempotentHint=true`** — a read has no additional effect when repeated with the same arguments.
  Honest nuance: the MCP spec notes `idempotentHint` is "meaningful only when `readOnlyHint == false`";
  we set it `true` anyway because it is *true* and the backlog row **explicitly** requests it. Harmless
  and explicit. (Documented in ADR 0121 D1.)
- **`openWorldHint=false`** — the domain of interaction is the closed, self-hosted resolved graph, not
  an open world of external entities.
- **`destructiveHint` unset** — meaningful only when `readOnlyHint == false`; leaving it unset avoids
  implying it carries meaning here.
- **`title`** — human-readable label via the top-level `title=` param of `add_tool` (recommended,
  zero-risk polish; a SHOULD, not a hard acceptance gate).

**Error envelope (all four tools)** — every error path raises a `ToolError` whose message is a JSON
object `{"error": <short machine token>, "hint": <actionable guidance>}`:

| Raise site | `error` | `hint` (guidance, wording at builder's discretion) |
|---|---|---|
| `_require_valid_id` (bad/injection id) | `"invalid entity id"` | must match the canonical id shape (`ID_PATTERN`); pass a resolved canonical id |
| `tool_get_entity` (absent) | `"entity not found"` | no resolved node has that id; verify the id or traverse from a known node |
| `tool_get_provenance` (absent) | `"entity not found"` | same — the graph guarantees provenance on every present node, so absent ⇒ no node |

`error` values MUST match the current bare-string tokens (`"invalid entity id"`, `"entity not found"`)
so the *signal* is unchanged; only the *shape* becomes structured. The client-visible error content is
`isError=true` with text `Error executing tool <name>: {"error": …, "hint": …}` (the SDK prefix is an
accepted constraint — §1.2, ADR 0121 D3).

---

## 5. The no-behavior-change guarantee, as testable acceptance criteria

> Strict interpretation (per the coordinator): **happy-path responses are byte-identical**; only
> **additive metadata** (annotations, `outputSchema`, `structuredContent`) is added and **error
> responses** become structured.

**Parity pins (must hold):**

- **PP-1 happy-path content parity.** For each tool and a fixed seeded input, the JSON decoded from the
  `content` text block(s) is deep-equal to the matching `graph.queries` helper's return value — and the
  number/order of `content` blocks is unchanged (one block for the dict tools; one block per item for
  the list tools). `structuredContent` is *additionally* present but the `content` blocks are untouched.
- **PP-2 error-signal parity.** An absent-id / injection-id call still surfaces as `isError=true`
  (unit: still raises `ToolError`). Only the error *content* gains the `{error, hint}` structure; the
  `error` token is unchanged.
- **PP-3 tool-set parity.** Still exactly `{get_entity, get_neighbors, get_provenance, find_paths}` —
  `test_tool_set_is_exactly_the_four` stays green.
- **PP-4 read-only / provenance parity.** The recording-fake write-path guard test and the
  `prov_source_id`-present integration assertion stay green.

---

## 6. Named tests + locations

Extend existing files (no new test module needed).

**`tests/unit/test_mcp_server.py`** (drives `build_server` + thin tool fns against the recording fake):

- `test_all_tools_annotated_readonly_idempotent_closedworld` — `list_tools()`; for each of the four,
  `annotations.readOnlyHint is True`, `annotations.idempotentHint is True`, `annotations.openWorldHint
  is False`. (AC-1)
- `test_all_tools_have_output_schema` — for each tool, `outputSchema is not None` (proves
  `structured_output` took effect; guards the silent-fallback case). (AC-2)
- `test_tool_titles_present` — each tool has a non-empty `title`. (soft / SHOULD)
- `test_error_message_is_structured_envelope` — `tool_get_entity(fake, <absent>)` raises `ToolError`;
  `json.loads(str(exc.value))` yields `{"error": "entity not found", "hint": <non-empty str>}`. Same
  for `tool_get_provenance` absent, and for `_INJECTION_ID` → `{"error": "invalid entity id", …}`.
  (AC-3)
- **Keep unchanged / regression:** `test_tool_set_is_exactly_the_four`, the read-only/injection/
  bound-param/clamp tests, `test_stdout_clean_when_tool_logs_and_raises` (still raises `ToolError`).

**`tests/unit/test_mcp_http_auth.py`** (HTTP no-drift):

- `test_http_tools_carry_same_annotations` — build the HTTP app's server (via the shared registration)
  and assert its four tools carry `readOnlyHint=true` identical to stdio (INV-S1-READONLY no-drift).

**`tests/integration/test_mcp_stdio.py`** (real Neo4j, JSON-RPC over the spawned server):

- `test_stdio_tools_list_advertises_annotations_and_schema` — the `tools/list` frame: each tool dict
  has `annotations.readOnlyHint == true` and a non-null `outputSchema`. (AC-4)
- `test_stdio_happy_path_content_unchanged` — **PP-1 pin:** `get_entity(p1)` — the `content` text
  block decodes to exactly the seeded entity props (incl. `prov_source_id`), and `structuredContent`
  is additionally present and deep-equal to that dict. `get_neighbors(p1)` — the `content` blocks (one
  per neighbour) decode to the neighbour dicts and `structuredContent == {"result": […]}`. (AC-4/PP-1)
- `test_stdio_error_envelope_on_wire` — `get_entity(<absent>)` and `get_entity(_INJECTION_ID)`: the
  error frame is `isError=true`, and a `{"error","hint"}` JSON object is recoverable from
  `content[0].text` with `error == "entity not found"` / `"invalid entity id"` respectively (the test
  strips the known `Error executing tool <name>: ` prefix, then `json.loads`). (AC-3 on the wire)
- **Keep unchanged / regression:** the existing stdout-purity + neighbors/paths-bounded tests (the
  `_result_objs` helper already tolerates `structuredContent`).

---

## 7. NON-goals (explicit)

- **F-5 `summary` context-budget flag** (`{count, sample[3]}` on `get_neighbors`/`find_paths`) — a
  *separate* gate; it changes the happy-path payload and needs a shared REST+MCP helper. Not here.
- **F-8 MCP live-smoke in CI** — separate XS rider on compose-boot.
- **F-9 `describe_tool` + compressed `tools/list`** — pointless at 4 tools; trigger-based, not now.
- **F-10/F-11/F-12** (JMESPath projection / resources / client module) — out.
- **Any `mcp`/FastMCP version upgrade** — unnecessary (§1); `1.28.1` supports everything. Were it
  necessary, it would be a *flagged decision in ADR 0121*, not a silent bump.
- **Reshaping happy-path payloads** — e.g. aligning the MCP list tools to REST's `{"neighbors": …}` /
  `{"paths": …}` wrapper, or replacing `dict[str, Any]` with richer per-field Pydantic models. Both
  would change payload bytes → out. "Typed output schema" here means the SDK-derived schema over the
  *existing* return types.
- **A prefix-free error text** — would require return-based envelopes, which collide with typed output
  schemas on the list tools (§1.2). Documented constraint + revisit trigger in ADR 0121; not now.

---

## 8. Slice breakdown

**One slice** (this is an XS, single-file production change). The three asks are trivially small and
all land in `_register_read_tools` + one helper + four raise-site swaps.

- **Slice 1 — MCP contract polish (annotations + output schemas + `{error,hint}` envelope).**
  Production: in `src/worldmonitor/mcp/server.py` — (a) add a module-level
  `_tool_error(error: str, hint: str) -> ToolError` returning `ToolError(json.dumps({"error": error,
  "hint": hint}))`; (b) swap the four `raise ToolError("…")` sites to `raise _tool_error("…", "…")`
  with the tokens in §4; (c) in `_register_read_tools`, pass `title=…`,
  `annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)`, and
  `structured_output=True` to each `add_tool`. Tests: all of §6. ADR 0121 → **ACCEPTED** at the
  merging PR (per the 0117-0120 convention); regenerate the ADR index.

**Optional split-point (fallback, only if a reviewer wants two PRs):** 1a = annotations + output
schemas (add_tool kwargs; AC-1/AC-2/AC-4/PP-1); 1b = the `_tool_error` helper + raise-site swaps
(AC-3/PP-2). They are orthogonal (different lines, different tests) and independently mergeable. Default
is the single slice; do not split without cause.

---

## 9. Open items for the test-author / builder

1. **Silent output-schema fallback** — pin `outputSchema is not None` per tool (AC-2). If any tool's
   return type unexpectedly fails schema-build in `1.28.1`, that surfaces here rather than shipping a
   half-typed surface.
2. **Prefix stripping in the wire error test** — the SDK prepends `Error executing tool <name>: `;
   the test recovers the envelope by locating the first `{` (or stripping the known prefix) then
   `json.loads`. Don't assert the whole text equals the JSON.
3. **`ToolAnnotations` import** — `from mcp.types import ToolAnnotations`.
4. **Envelope key order / determinism** — `json.dumps({"error": …, "hint": …})` (insertion order is
   stable in CPython 3.12); no need for `sort_keys` but it is harmless.
5. **HTTP no-drift test** — reuse the shared `_register_read_tools`; you need a stub token verifier to
   build the HTTP app (see the existing `test_mcp_http_auth.py` patterns) — assert annotations, not
   auth.
6. **Do not touch** the property tests or `graph/queries.py`; keep the four `error` tokens identical to
   today's bare strings (PP-2).
7. **Run full `pytest -m "not integration"` + the MCP integration tests locally** (Docker is available
   here — the stdio integration suite spawns a real server against a testcontainer Neo4j).
