# 0121 — MCP contract polish: tool annotations, typed output schemas, structured error envelopes

- **Status:** PROPOSED (2026-07-23)
- **Date:** 2026-07-23
- **human_fork:** false — reversible metadata polish on the read-only MCP tool surface; no
  product/architecture fork. Each of the three sub-decisions has a sensible default and a cheap
  reversal (full reversal cost + revisit triggers below).
- **person_affecting:** false — the four tools are read-only graph reads; this gate changes **no** ER
  threshold, guard mode, sensitivity park, or individual-affecting score, and touches **no** person's
  data shape. It adds read-only annotations + a derived output schema and restructures error content.
  Provenance is returned verbatim, unchanged. No CLAUDE.md invariant (provenance / ER / merge /
  canonical-id) is touched (so no mandatory `@given` test — recorded in the spec §3.1).
- **human_cosign:** not required — reversible, non-person-affecting, non-ER-adjacent metadata polish.
  (Unlike the 0117-0120 CTI connectors, this gate is not ER-adjacent and touches no merge path; the
  cost-directive posture is to reserve cosign for irreversible / person-affecting changes.)
- **Backlog/roadmap:** `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row F-2 (P0 / XS);
  `docs/40_ROADMAP.md` "F-2 is queued, scheduled post-S4" (verified still-standing 2026-07-23).
- **Spec:** `docs/reviews/GATE_F2_MCP_CONTRACT_POLISH_SPEC.md`.

## Context

Our graph-read MCP surface (ADR 0063 stdio, ADR 0090 authenticated HTTP) exposes exactly four
read-only tools — `get_entity`, `get_neighbors`, `get_provenance`, `find_paths` — registered in one
shared place (`_register_read_tools` in `src/worldmonitor/mcp/server.py`) so the stdio and HTTP
transports never drift. Today those tools carry **no** MCP behavioral annotations, advertise **no**
output schema, and raise **bare-string** `ToolError`s. A host/agent (Hermes) therefore cannot tell that
the tools are read-only/idempotent without calling them, cannot validate or shape their output against a
schema, and gets un-parseable free-text on errors.

Backlog row F-2 asks for the standard MCP contract polish — `readOnlyHint`/`idempotentHint`
annotations, typed output schemas, and structured `{error, hint}` envelopes — as **one XS gate with no
behavior change**. This ADR records the three sub-decisions and the one real constraint the installed
SDK imposes.

**Installed SDK (verified, not assumed):** the official **`mcp` SDK `1.28.1`** (`pyproject.toml`
`mcp>=1.28`; `uv.lock` `1.28.1`), whose bundled FastMCP is imported as `mcp.server.fastmcp.FastMCP`
(**not** the standalone `fastmcp` PyPI package). It natively supports `add_tool(..., title=,
annotations=ToolAnnotations(...), structured_output=True)` and derives `outputSchema` from the return
annotation. **No version upgrade is needed** for any part of this gate.

## Decision

Apply three additive changes in the single shared registration site (both transports inherit them):

**D1 — Behavioral annotations.** Register each tool with
`annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)` and a
human-readable `title`. `readOnlyHint=true` is structurally proven (every tool calls `execute_read`
only; the recording-fake test raises on any write path). `openWorldHint=false` because the domain is
the closed, self-hosted resolved graph. `idempotentHint=true` is honest (a read has no additional
effect on repeat) and is set per the row's explicit request, acknowledging the MCP spec notes it is
"meaningful only when `readOnlyHint == false`" — harmless and explicit. `destructiveHint` is left unset
(meaningful only when not read-only).

**D2 — Typed output schemas.** Pass `structured_output=True` to each `add_tool`, letting the SDK derive
`outputSchema` from the **existing** return annotations. This adds `structuredContent` to each result
and `outputSchema` to each tool listing. Crucially, the SDK computes the unstructured `content` with the
**same** `_convert_to_content(result)` call it uses today, so the existing text payload is
**byte-identical**; the new fields are purely additive. The list-returning tools' `structuredContent` is
SDK-wrapped as `{"result": […]}` while their `content` blocks are unchanged. Return types are **not**
reshaped (no richer per-field models — that would change payload bytes and is out of scope).

**D3 — Structured error envelope (raise-based).** Introduce `_tool_error(error, hint)` returning
`ToolError(json.dumps({"error": error, "hint": hint}))`, and swap the four bare `ToolError("…")` sites
to it, keeping the `error` tokens identical to today's strings (`"invalid entity id"`,
`"entity not found"`) so the error *signal* is unchanged. On the wire the client sees `isError=true`
with `content` text `Error executing tool <name>: {"error": …, "hint": …}`.

The gate holds a strict **no-behavior-change** contract: happy-path `content` bytes are unchanged
(byte-identical), and only additive metadata (`annotations`, `outputSchema`, `structuredContent`) plus
**error-content** structure are added. This ADR flips to **ACCEPTED** at the gate-completing PR.

## Alternatives considered

- **A1 — Return-based clean error envelope** (`return CallToolResult(isError=True, content=[…])` so the
  error text is pure JSON with no `Error executing tool <name>: ` prefix). **Rejected:** verified from
  SDK source, when `structured_output` is enabled `convert_result` validates a returned
  `CallToolResult`'s `structuredContent` against the **success** output-model, so an error envelope
  `{"error","hint"}` **fails validation on the `list`-returning tools** (`get_neighbors`/`find_paths`
  require `{"result": …}`) and the client gets a messy validation string. Return-based clean envelopes
  and typed output schemas are mutually incompatible in `1.28.1` for those tools. Chose the raise-based
  envelope (D3) and accept the prefix as a documented cosmetic wart (revisit trigger below).
- **A2 — Reshape the two list tools to a dict payload** (`{"neighbors": …}` / `{"paths": …}`, matching
  REST) to enable both clean return-based envelopes and no-wrap schemas. **Rejected:** changes the
  happy-path payload bytes — a behavior change the row forbids — and would break existing unit tests.
- **A3 — Richer per-field typed output models** (explicit Pydantic entity/edge models instead of the
  SDK-derived permissive `dict[str, Any]` schema). **Rejected for this XS gate:** would reshape/serialize
  payloads differently (behavior change) and is a larger design. Deferred (revisit trigger c).
- **A4 — Upgrade the `mcp` SDK.** **Rejected / unnecessary:** `1.28.1` already supports annotations and
  structured output natively. Had a capability been missing, an upgrade would be surfaced here as an
  explicit decision, not a silent bump.
- **A5 — Do only annotations, defer output schemas + envelopes.** **Rejected:** the row bundles all
  three as one XS gate; all three are trivially small and land in the same registration site.

## Reversibility

**Reversible** (both `human_fork` and `person_affecting` = false).

- **Reversal cost:** revert one production file (`server.py` — the `add_tool` kwargs, the `_tool_error`
  helper, and the four raise-site swaps) plus the added test assertions. **No** data migration, **no**
  schema/store change, **no** stored artifacts, **no** change to REST. Trivial, single-commit reversal.
- **Revisit triggers:**
  - (a) The `mcp` SDK exposes an **un-prefixed structured tool-error** path → adopt a return-based
    clean `{error, hint}` envelope (retires the D3 prefix wart).
  - (b) **F-5** (the `summary` context-budget flag) reshapes `get_neighbors`/`find_paths` payloads →
    revisit the output-schema wrapping and, if the payloads become dict-shaped, reconsider return-based
    envelopes (A1/A2 unlock).
  - (c) A consumer (Hermes) needs **richer per-field typed schemas** → introduce explicit output models
    as a follow-up, behavior-changing gate (A3).
  - (d) A `mcp` SDK major upgrade changes the `ToolAnnotations` / `structured_output` API → re-verify
    §1 of the spec against the new version.

## Consequences

- Hosts/agents can reason about the four tools (read-only, idempotent, closed-world) and validate their
  output against an advertised schema without calling them; error content is machine-parseable.
- One registration site keeps stdio and HTTP identical (INV-S1 no-drift).
- The `Error executing tool <name>: ` prefix remains on error content until revisit trigger (a) — an
  accepted, documented limitation.
- No CLAUDE.md invariant is touched → no new property test (recorded in the spec §3.1); the existing
  MCP property tests (stdout purity, auth boundary) remain green as regression pins.
