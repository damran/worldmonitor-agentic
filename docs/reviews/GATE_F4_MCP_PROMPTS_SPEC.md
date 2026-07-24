# Gate F-4 — MCP prompts as analyst playbooks (`entity-workup`, `freshness-audit`)

> Backlog: `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-4** — "MCP prompts as analyst
> playbooks — `entity-workup`, `freshness-audit` — declarative step+purpose workflows for Hermes. One
> gate, both transports, arg length-caps tested. **P1 / S**."
> ADR: `docs/decisions/0125-mcp-prompts.md` (PROPOSED → ACCEPTED at the gate-completing PR, per the
> 0117–0124 convention).
> Predecessors relied on: ADR 0063 (stdio FastMCP + shared `read_guards`), 0090 (authenticated HTTP MCP
> transport), 0121 / Gate F-2 (annotations + typed output schemas + `{error, hint}` envelope + the
> "strip-the-SDK-prefix" wire-error idiom), 0122 / Gate F-3 (the 5th tool `get_entity_dossier`),
> 0123 / Gate F-1 (the 6-state freshness surface + `GET /sources/freshness`; the freshness MCP tool is a
> **recorded deferral**), 0124 / Gate F-5 (the `summary` flag — F-4 lands **on top of** F-5), 0042
> (single-tenant).

> **F-4 ordering (load-bearing):** this gate's fleet starts **after** F-5 has merged to `master`. The
> coordinator moves this spec + `docs/decisions/0125-mcp-prompts.md` into the repo and applies
> `.claude/gate.scope` (§gate.scope) at that point. The `entity-workup` playbook references the F-5
> `summary=true` flag; if F-5 is somehow not on `master` when F-4 builds, drop that one sentence (see §10).

---

## 0. What this gate is (and is NOT)

**Is:** two **declarative, read-only MCP prompts** — `entity-workup` and `freshness-audit` — registered in
**one** shared place in `src/worldmonitor/mcp/server.py` so **both** transports (stdio `build_server` and
authenticated HTTP `build_http_app`) expose them identically. A prompt is a named, versioned **text
template** that walks an analyst / Hermes through a step+purpose workflow over the existing read surfaces.
It renders to a single `user` text message; it calls no tool, opens no DB session, and returns no graph
data. Each prompt takes one string argument (`entity_id`; optional `connector_id`) that is **length-capped
and shape-validated** before it is interpolated into the template.

**Is NOT** (explicit non-goals — see §8):
- **No new tool.** The tool set stays **exactly five** (`get_entity`, `get_neighbors`, `get_provenance`,
  `find_paths`, `get_entity_dossier`). Prompts are a *separate* MCP surface; `list_tools()` is unchanged.
- **No dynamic / templated tool RESULTS inside prompts.** A prompt never fetches from Neo4j/Postgres and
  never embeds a live query result or an entity's data. It is static guidance text parameterised only by
  the validated argument. (This is why the prompts need no `Neo4jClient` and no DB session.)
- **No freshness MCP tool.** F-1 (ADR 0123 D5) deliberately deferred `get_source_freshness`; the MCP
  server stays Neo4j-only. The `freshness-audit` prompt therefore points the analyst at the REST endpoint
  `GET /sources/freshness`, not at a (non-existent) MCP tool.
- **No Hermes config change.** `deploy/hermes/config.yaml`'s `tools.include` is a *tool*-specific
  allowlist; there is no prompts allowlist key, and adding one is speculative (§5.4, ADR 0125 D5 / A4).
- **No `mcp`/FastMCP version bump, no JMESPath projection, no resources, no client module** (F-10/F-11/F-12
  are separate rows).

---

## 1. Installed SDK — the prompts API (verified against `mcp==1.28.1`, do NOT re-derive)

Empirically confirmed by driving the installed SDK (`.venv/bin/python`) and by reading its source
(`.venv/lib/python3.12/site-packages/mcp/server/fastmcp/{server.py,prompts/base.py,prompts/manager.py}`).

- **Package:** the official **`mcp` SDK `1.28.1`**; `from mcp.server.fastmcp import FastMCP` is FastMCP
  **bundled in `mcp`**, not the standalone `fastmcp` PyPI package (same as F-2/F-3).
- **Registration — NATIVE, two equivalent forms:**
  - `server.add_prompt(Prompt.from_function(fn, name=…, title=…, description=…))` — from
    `mcp.server.fastmcp.prompts.base.Prompt`.
  - `@server.prompt(name=…, title=…, description=…)` decorator (wraps the same `Prompt.from_function`).
  - **Hyphenated names require the explicit `name=` kwarg** (a Python fn name can't contain `-`). Confirmed:
    `Prompt.from_function(entity_workup, name="entity-workup")` registers a prompt whose wire name is
    `entity-workup`.
- **Argument declaration + validation:** `Prompt.from_function` derives the argument list from the fn
  signature via `func_metadata`. A parameter **without** a default → `PromptArgument(required=True)`; a
  parameter **with** a default → `required=False`. Confirmed on the wire: `entity_id` → `required=True`,
  `connector_id: str = ""` → `required=False`. The fn is wrapped in pydantic `validate_call`, which only
  enforces the *declared type* (`str`) — it does **not** enforce a length or shape cap. **Length + shape
  caps must be enforced inside the fn body** (this gate's design, §5).
- **Types:** `Prompt` (name/title/description/arguments/fn), `PromptArgument` (name/description/required),
  `Message`/`UserMessage`/`AssistantMessage` (`role` + `content: ContentBlock`), and the wire result
  `mcp.types.GetPromptResult` (`description` + `messages: list[PromptMessage]`, each
  `{role, content:{type:"text", text:…}}`). **A prompt fn returning a `str` is converted to a single
  `UserMessage` `TextContent`.** Confirmed: `get_prompt("entity-workup", {"entity_id":"Q42"})` →
  `GetPromptResult`, `messages[0].role=="user"`, `messages[0].content.type=="text"`, text contains `Q42`.
- **Both transports — automatic, no extra mount:** FastMCP registers the `prompts/list` + `prompts/get`
  handlers **unconditionally at construction** (`server.py` `set_default_handlers`, lines 311–312), and
  advertises the `prompts` capability. So the moment a prompt is `add_prompt`-ed it is visible over **both**
  the stdio JSON-RPC loop **and** the `streamable_http_app()`. **`build_http_app` needs NOTHING extra** for
  `prompts/list` + `prompts/get` to surface.
- **Auth boundary — prompts/* are gated identically to tools/*:** `streamable_http_app()` mounts the entire
  MCP JSON-RPC endpoint at the single route `self.settings.streamable_http_path` (our `/mcp`) wrapped in
  **`RequireAuthMiddleware(streamable_http_app, required_scopes, …)`** (`server.py` lines 1011–1016). There
  is **no per-method allowlist** — every method (`tools/list`, `tools/call`, `prompts/list`, `prompts/get`,
  …) flows through the *same* wrapped endpoint and requires the same `worldmonitor:read` scope. So
  `prompts/list` and `prompts/get` are **automatically 401 without a bearer and 403 without the role**,
  exactly like the tools. No `mcp/auth.py` change is needed or wanted (§3, §6.3).

### 1.1 Error-wrapping mechanics (verified — drives the §5 design and the wire-error tests)

- A prompt fn that **raises** is wrapped by `Prompt.render` as
  `ValueError("Error rendering prompt <name>: <original>")` (`prompts/base.py:182`), then `FastMCP.get_prompt`
  re-raises `ValueError(str(e))` (`server.py:1081-1083`) **and logs `logger.exception` to the SDK logger**.
  Confirmed: an over-cap `entity_id` surfaces as
  `ValueError: Error rendering prompt entity-workup: {"error": "...", "hint": "..."}`.
  → The wire/JSON-RPC error carries the **`Error rendering prompt <name>: ` prefix** ahead of our JSON
  envelope. The test-author strips this known prefix, then `json.loads` — the **same idiom** F-2 uses for the
  tool prefix `Error executing tool <name>: ` (only the prefix string differs).
- A **missing required argument** is caught *before* the fn runs: `render` raises
  `ValueError("Missing required arguments: {'entity_id'}")` (`prompts/base.py:149`) — **no** `Error rendering
  prompt` prefix, and our fn body never executes. Confirmed.
- **STDOUT PURITY holds on the prompt error path:** `get_prompt`'s `logger.exception` propagates to the root
  logger, which `configure_stderr_logging()` binds to a **stderr** `StreamHandler`. Confirmed: with the
  child process's stderr suppressed, stdout stayed byte-clean on the error path. The stdio integration test
  re-pins this for `prompts/get` on a hostile arg.

---

## 2. Scope (exact files)

| Path | Change | Why |
|---|---|---|
| `src/worldmonitor/mcp/server.py` | **edit (only prod file)** | Add `_PROMPT_ARG_MAX_LEN`, `_prompt_error`, `_require_valid_prompt_arg`, two module-level thin fns (`prompt_entity_workup`, `prompt_freshness_audit`), the two template constants, `_register_prompts(server)`, and one call to it inside **both** `build_server` and `build_http_app` (right after `_register_read_tools`). |
| `tests/unit/test_mcp_prompts.py` | **new** | Prompt-set pin, prompts/list arg pins, prompts/get happy + hostile, tool-set-undisturbed pin, HTTP-no-drift (non-network). |
| `tests/property/test_prop_mcp_prompt_args.py` | **new** | One cheap `@given` over arbitrary arg strings (by decision, not mandate — §3.1). |
| `tests/integration/test_mcp_stdio.py` | **edit (append tests)** | `prompts/list` + `prompts/get` over the spawned stdio server; stdout-purity on the hostile-arg error frame. |
| `tests/integration/test_mcp_http_transport.py` | **edit (append tests)** | `prompts/list`/`prompts/get` gated by the SAME `RequireAuthMiddleware` (401 no-bearer, 403 no-role, 200 + playbook with role). |
| `docs/decisions/0125-mcp-prompts.md` | **new** | This gate's ADR (PROPOSED → ACCEPTED at the merging PR). |
| `docs/decisions/README.md` | **regenerate** | `python scripts/gen_adr_index.py` — the `adr-index` CI check scans `docs/decisions/(\d{4})-*.md`; the new ADR must be indexed. |
| `docs/reviews/GATE_F4_MCP_PROMPTS_SPEC.md` | **new** | This spec. |

**Out of scope / must NOT change (stay green as regression pins):** `graph/queries.py`, `graph/read_guards.py`
(import `validate_entity_id`, don't edit it), `api/graph.py`, `mcp/auth.py`, `deploy/hermes/config.yaml`,
`tests/unit/test_mcp_server.py`, `tests/unit/test_mcp_http_auth.py`, `tests/property/test_prop_mcp_stdout_purity.py`,
`tests/property/test_mcp_auth_boundary.py`. In particular `test_tool_set_is_exactly_the_five` (in
`test_mcp_server.py`) stays **untouched and green** — it is the proof that adding prompts did not perturb the
5-tool surface.

---

## 3. Locked invariants every change must hold

- **G1 provenance — untouched (N/A, honestly).** Prompts return **no graph nodes or edges** — they are
  static guidance text. There is no provenance to carry or strip. The five tools the playbooks *point at*
  still return `prov_*` verbatim (unchanged by this gate); the stdio integration `prov_source_id` assertion
  stays green.
- **Append-only / read-only.** Prompts open no session and issue no Cypher (`execute_read` or otherwise).
  The recording-fake write-path guard tests stay green. `_register_prompts` takes **no `Neo4jClient`**.
- **No drift between transports (INV-S1).** Prompts are registered in the shared `_register_prompts`, called
  by both `build_server` and `build_http_app`. There is exactly one registration site; a no-drift test pins
  the HTTP server exposes the identical two prompts.
- **Prompts/* equally gated on HTTP (INV-S1-AUTH / INV-S1-ROLE).** By construction (§1) `prompts/list` and
  `prompts/get` ride the same `RequireAuthMiddleware`. A wire test pins 401-without-bearer and 403-without-role
  for a `prompts/*` request — not merely for `tools/*`.
- **STDOUT PURITY.** The prompt error path logs to stderr and surfaces a JSON-RPC error frame on stdout; the
  existing `@given` stdout-purity property stays green and a new stdio integration test re-pins it for a
  hostile prompt arg.
- **Bounded / no-injection args.** Every prompt arg is (a) length-capped to `_PROMPT_ARG_MAX_LEN` **first**,
  then (b) shape-validated via the shared `read_guards.validate_entity_id` (ID_PATTERN) — the SAME predicate
  the tools use. A hostile arg (oversize or injection-shaped) yields a clean `{error, hint}` error, never a
  crash, and never reaches interpolation. Because a validated arg is drawn only from the canonical-id
  alphabet `[A-Za-z0-9:._-]`, the interpolated text can carry no quote/newline/brace/`$` — **there is no
  injection surface** in the rendered template (belt: prompts still interpolate as plain text, never into
  anything executed).

### 3.1 Property-test discipline — recorded decision: ONE cheap property test (by decision, not mandate)

F-4 touches **no** CLAUDE.md invariant (no provenance-stamping change, no ER/merge/threshold, no
canonical-id resolution, no guard, no write, no egress) — it is a declarative read-only text surface. So the
build-discipline **mandatory-`@given`** rule does **not** apply. We nonetheless include **one** cheap
property test (mirroring ADR 0124's decision, and reusing the existing MCP stdout-purity property harness),
because the load-bearing guarantee here — *a hostile argument never crashes and never corrupts stdout* — is
exactly the class a single example test under-samples. This is a **decision, not an omission** (recorded in
ADR 0125 §Invariant-gate-note). The two existing MCP property tests remain green as regression pins.

---

## 4. The prompt contract (VERBATIM text — versioned; pin exactly)

Both templates are pure functions of the single validated argument. The test-author pins the rendered text
against these templates (e.g. `assert text == _ENTITY_WORKUP_TEMPLATE.format(entity_id="Q42")`). Wording is
**contract**: changing it later is an ADR-worthy contract evolution, not a silent edit.

### 4.1 `entity-workup`

- **name:** `entity-workup`  · **title:** `Entity workup`
- **description** (surfaced in `prompts/list`): *"Declarative, read-only playbook: work up a single resolved
  entity across the five graph-read tools (get_entity → get_provenance → get_neighbors → find_paths →
  get_entity_dossier) as ranked leads for human review — never an automated verdict."*
- **arg:** `entity_id: str` (required)
- **rendered text** (`_ENTITY_WORKUP_TEMPLATE.format(entity_id=<validated>)`):

```
Entity workup playbook — entity_id={entity_id}

Purpose: assemble a provenance-complete picture of one resolved entity as ranked leads
for a human analyst. This is a read-only orientation aid, never an automated verdict.

Each step names one graph-read tool, its purpose, and the argument shape to pass. Work
through them in order.

Step 1 - Anchor the entity.
  Tool: get_entity(entity_id={entity_id})
  Purpose: confirm the node exists and read its properties and provenance
  (prov_source_id, prov_retrieved_at, prov_reliability, prov_source_record). If the tool
  reports "entity not found", stop: this id is not in the resolved graph.

Step 2 - Read the provenance.
  Tool: get_provenance(entity_id={entity_id})
  Purpose: record where each fact about this entity came from before drawing any
  inference. Every node carries provenance; weigh a single-source or low-reliability fact
  as a weaker lead.

Step 3 - Map the immediate neighbourhood.
  Tool: get_neighbors(entity_id={entity_id}, hops=1)
  Purpose: list the entities one edge away and how they connect. On a high-degree node,
  pass summary=true first for a {{count, sample}} taste before requesting the full list.

Step 4 - Trace a specific connection.
  Tool: find_paths(from_id={entity_id}, to_id=<a second resolved id>, max_hops=<within
  the hop cap>)
  Purpose: when you have a hypothesis linking this entity to another, look for bounded
  paths between them. No path within the hop bound is not proof of no relationship.

Step 5 - Assemble the dossier.
  Tool: get_entity_dossier(entity_id={entity_id}, hops=1)
  Purpose: retrieve the deterministic entity + neighbours + provenance + merge_history
  bundle in a single call for the written workup.

Framing: report ranked hypotheses with their provenance and confidence for human review;
do not merge, attribute, or label a person from this workup. Surface the leads and their
sources and let a human decide.
```

> Note for the builder: `{{count, sample}}` is a literal `{count, sample}` after `.format()` (doubled braces
> escape the format call). The only real substitution field is `{entity_id}`.

### 4.2 `freshness-audit`

- **name:** `freshness-audit`  · **title:** `Freshness audit`
- **description** (surfaced in `prompts/list`): *"Declarative, read-only playbook: audit source freshness via
  GET /sources/freshness and interpret the six freshness states as evidence of collection gaps for human
  review."*
- **arg:** `connector_id: str = ""` (optional; empty = audit all instances)
- **`{scope_line}`** is computed from the validated `connector_id`:
  - `connector_id == ""` → `Scope: all connector instances.`
  - non-empty → `Scope: focus on connector instances whose connector_id == {connector_id}.`
- **rendered text** (`_FRESHNESS_AUDIT_TEMPLATE.format(scope_line=<computed>)`):

```
Source freshness audit playbook

{scope_line}

Purpose: assess how current the ingested sources are, so an analyst can weight findings by
the freshness of their underlying feeds. A stale or missing source is a lead about a
collection gap, not a verdict about the world.

Freshness is served by the read-only REST endpoint GET /sources/freshness (auth-gated,
single-tenant). There is no freshness MCP tool in this version - use the REST surface. The
response lists, per connector instance: connector_id, an opaque instance_id, the raw
status, the derived freshness_status, last_success_at, age_seconds, plus a summary
count-by-state and the configured staleness budget.

Step 1 - Pull the freshness surface.
  Call: GET /sources/freshness
  Purpose: read the current per-instance freshness_status and the summary counts.

Step 2 - Read each instance's freshness_status. It is one of six values, in priority
order:
  - disabled    administratively off; expect no data, not a fault.
  - error       auto-hard-disabled after repeated failures; a collection gap to escalate
                to a human operator.
  - no_data     active but never had a successful ingest; treat downstream coverage as
                absent.
  - very_stale  last success older than the very-stale budget; findings may be badly out
                of date.
  - stale       last success older than the stale budget; weight findings accordingly.
  - fresh       last success within budget.

Step 3 - Summarise the gaps.
  Purpose: from the summary counts, report how many instances are error / no_data /
  very_stale / stale versus fresh. Rank error and no_data instances first - those are the
  collection gaps most likely to bias an analysis.

Framing: freshness is operational metadata about pipelines, never data about a person.
Read it as evidence of collection coverage and gaps and surface those gaps to a human; do
not treat a stale or missing source as a factual claim about any entity.
```

> The six states, their priority order, the field names, and "no MCP tool — use REST" are quoted directly
> from ADR 0123 (F-1) D1/D3/D5. If F-1's vocabulary or the endpoint shape changes, this prompt is the coupled
> contract to update (a recorded revisit trigger in ADR 0125).

---

## 5. Argument design + length caps + hostile-arg behaviour

### 5.1 The arg/cap table

| Prompt | Arg | Required | Type | Cap (chars) | Shape validation | On violation |
|---|---|---|---|---|---|---|
| `entity-workup` | `entity_id` | **yes** | `str` | `≤ _PROMPT_ARG_MAX_LEN` | `read_guards.validate_entity_id` (ID_PATTERN) | clean `{error,hint}` `ValueError` |
| `freshness-audit` | `connector_id` | no (default `""`) | `str` | `≤ _PROMPT_ARG_MAX_LEN` | empty allowed; **if non-empty**: `validate_entity_id` | clean `{error,hint}` `ValueError` |

- **`_PROMPT_ARG_MAX_LEN = 256`** — a new module constant in `mcp/server.py`. Generous versus real canonical
  ids (Wikidata Q-numbers, LEI = 20, GeoNames, ISO-3166, `opensanctions:…`, `threatfox`/`urlhaus`/`sslbl`/
  `ransomware_live`/`feodo`), while clearly rejecting a hostile blob. Pinned by a test.
- **Validation order is load-bearing: length FIRST, shape SECOND.** `validate_entity_id`'s regex
  `^[A-Za-z0-9:._-]+$` matches arbitrarily long in-alphabet strings, so the length cap is a *distinct*
  necessary guard (the backlog's "arg length-caps tested"). Capping first also avoids running the regex on an
  adversarial giant string.
- **Empty handling.** `validate_entity_id("")` is `False` (the `+` requires ≥1 char). `entity-workup` requires
  a non-empty id (empty → reject). `freshness-audit` treats `""`/omitted as the "all instances" sentinel and
  skips validation for it.

### 5.2 Design decision — VALIDATE-AND-REJECT (reuse `validate_entity_id`), not escape-and-include

Chosen because it (a) reuses the exact existing convention (`_require_valid_id` / `ID_PATTERN` /
`validate_entity_id`); (b) directly delivers the backlog's "injection-shaped → clean error"; (c) means the
rendered text ever only interpolates a canonical-alphabet token, so there is **no injection surface** and no
escaping logic; and (d) keeps the playbook consistent with the tool it names — it never hands the caller an id
that `get_entity` would then reject. No legitimate canonical id is lost (the ID_PATTERN alphabet is a superset
of every canonical id shape the system uses). The rejected alternative (accept any under-cap string and
`json.dumps`-escape it into the text) is recorded in ADR 0125 A1.

### 5.3 The error envelope helper (mirror `_tool_error`, but a `ValueError`)

Add `_prompt_error(error: str, hint: str) -> ValueError` returning
`ValueError(json.dumps({"error": error, "hint": hint}))`, and `_require_valid_prompt_arg(value, *, field,
allow_empty=False) -> None` that:
1. if `allow_empty` and `value == ""` → return (no error);
2. if `len(value) > _PROMPT_ARG_MAX_LEN` → `logger.warning(...)` (stderr) + `raise _prompt_error("argument
   too long", "…")`;
3. if not `read_guards.validate_entity_id(value)` → `logger.warning(...)` + `raise _prompt_error("invalid
   argument", "must match the canonical id shape (ID_PATTERN)")`.

The `logger.warning` **must not echo the raw arg unbounded** (log `field` + length + a short `repr` slice
only) — the error envelope carries a fixed token + hint and never reflects the hostile bytes. On the wire the
client sees `Error rendering prompt <name>: {"error": …, "hint": …}` (strip the prefix, then `json.loads` —
§1.1).

### 5.4 Hermes config / prompts allowlist — verified: NO change

`deploy/hermes/config.yaml` has a `mcp_servers.worldmonitor.tools.include: [5 tools]` allowlist but **no
prompts key**, and there is **no JSON-Schema** for the config (it is a template with a "confirm exact key vs
v0.17.0" comment). Prompts are a distinct, read-only, capability-free MCP surface that Hermes discovers via
`prompts/list`; the tool allowlist does not govern them. **This gate makes no Hermes config change**
(ADR 0125 D5). Builder open item (§10): if the Hermes v0.17.0 config reference *does* define a prompts
allowlist and the operator wants Hermes scoped to exactly these two, that is a one-line follow-up — record it
as a revisit trigger, do not invent a speculative key now.

---

## 6. Registration design (both transports)

### 6.1 Where it slots

Add `_register_prompts(server: FastMCP) -> None` beside `_register_read_tools`. It registers exactly the two
prompts (via `server.add_prompt(Prompt.from_function(fn, name=…, title=…, description=…))`, hyphenated names
requiring the explicit `name=`). It takes **no `Neo4jClient`** (prompts are pure text). Call it in **both**
builders immediately after tool registration:

- `build_server(...)`: `_register_read_tools(server, client); _register_prompts(server)`
- `build_http_app(...)`: `_register_read_tools(server, client); _register_prompts(server)`

### 6.2 Thin functions (unit/property-testable without a JSON-RPC loop)

Two module-level fns — `prompt_entity_workup(entity_id: str) -> str` and
`prompt_freshness_audit(connector_id: str = "") -> str` — each validate their arg (`_require_valid_prompt_arg`)
then `return TEMPLATE.format(...)`. Registered directly (they *are* the prompt fns). Tests drive these
directly for the wide input sweep (mirrors the tool-fn pattern), and drive `server.get_prompt(...)` for the
wire shape.

### 6.3 Auth — nothing to do

Prompts/* inherit `RequireAuthMiddleware` from `streamable_http_app()` (§1). `mcp/auth.py` is **not touched**;
the role→scope map is unchanged. The gate *adds a test* proving `prompts/*` is gated, it does not add gating.

---

## 7. Both-transports test plan + named tests

### 7.1 `tests/unit/test_mcp_prompts.py` (new — drives `build_server` + thin fns; no network)

- `test_prompt_set_is_exactly_the_two` — `build_server(...).list_prompts()` names == `{"entity-workup",
  "freshness-audit"}`.
- `test_prompts_do_not_disturb_the_five_tools` — the SAME built server still `list_tools()` == the five tool
  names (prompts add no tool). **The `prompts/list` surface does not perturb the tool pins.**
- `test_prompt_args_declared` — `entity-workup` declares `entity_id` `required=True`; `freshness-audit`
  declares `connector_id` `required=False`.
- `test_entity_workup_renders_all_five_tools_and_id` — `get_prompt("entity-workup", {"entity_id":"Q42"})` →
  a `GetPromptResult` whose single `user`/`text` message contains `Q42` and the substrings `get_entity`,
  `get_provenance`, `get_neighbors`, `find_paths`, `get_entity_dossier`, and a leads-not-verdicts phrase.
- `test_entity_workup_text_is_verbatim_template` — rendered text `==` `_ENTITY_WORKUP_TEMPLATE.format(
  entity_id="Q42")` (contract pin).
- `test_freshness_audit_all_and_scoped` — `{}` → text contains `Scope: all connector instances.` and all six
  state tokens + `GET /sources/freshness`; `{"connector_id":"threatfox"}` → text contains
  `connector_id == threatfox`.
- `test_freshness_audit_text_is_verbatim_template` — both forms `==` the template (contract pin).
- `test_prompt_arg_oversize_rejected` — an `entity_id` of length `_PROMPT_ARG_MAX_LEN+1` → the thin fn raises
  a `ValueError` whose message `json.loads`-es to `{"error":"argument too long","hint":<non-empty str>}`; the
  raw oversize bytes do **not** appear in the message.
- `test_prompt_arg_injection_rejected` — `prompt_entity_workup('") DELETE //')` → `ValueError` →
  `{"error":"invalid argument", "hint":<non-empty>}`.
- `test_prompt_missing_required_arg` — `get_prompt("entity-workup", {})` raises `ValueError` matching
  `Missing required arguments`.
- `test_http_registers_same_two_prompts_no_drift` — build an HTTP `FastMCP` (via `_register_read_tools` +
  `_register_prompts`, the S1 stub-verifier pattern) and assert its `list_prompts()` names == the stdio
  server's == `{"entity-workup","freshness-audit"}` (INV-S1 no-drift, non-network).

### 7.2 `tests/property/test_prop_mcp_prompt_args.py` (new — one `@given`, by decision §3.1)

Reuse the `_SETTINGS`/`capfd` harness of `test_prop_mcp_stdout_purity.py`. Strategy: canonical-shaped ids ∪
injection-shaped strings ∪ `st.text()` ∪ a few over-cap strings. For each arg `s`, call the thin fn under
`configure_stderr_logging()` and assert:
1. **stdout stays byte-empty** on both the accept and reject paths (`capfd.out == ""`).
2. **accept iff** `len(s) ≤ _PROMPT_ARG_MAX_LEN and validate_entity_id(s)` — and when accepted the returned
   text **contains `s` verbatim** and is a non-empty `str`.
3. **reject otherwise** — the fn raises a `ValueError` whose message `json.loads`-es to an `{"error","hint"}`
   dict, and the raw `s` is **not** present unbounded in that message (no reflection).
Include a `freshness-audit` variant where `""` is accepted (all-instances) and non-empty follows the same rule.

### 7.3 `tests/integration/test_mcp_stdio.py` (append — spawned server, JSON-RPC over stdio)

- `test_stdio_prompts_list_and_get` — send `prompts/list` (assert the two names + `entity_id` required /
  `connector_id` optional) and `prompts/get entity-workup {entity_id:p1}` (assert `result.messages[0]` is a
  `user`/`text` message containing `p1` and the five tool names). Assert **stdout is only JSON-RPC frames**
  (reuse the existing per-line purity check) and no `Traceback` on stdout.
- `test_stdio_prompt_hostile_arg_is_clean_error_frame` — `prompts/get entity-workup {entity_id:<injection>}`
  and `{entity_id:<over-cap>}` surface as JSON-RPC **error** frames (not tracebacks); the `{error,hint}`
  envelope is recoverable after stripping the `Error rendering prompt <name>: ` prefix; stderr carries the
  diagnostic (logs route to stderr).

### 7.4 `tests/integration/test_mcp_http_transport.py` (append — real HTTP, in-proc fake verifier)

- `test_prompts_list_requires_bearer_401` — `POST /mcp {method:"prompts/list"}` with **no** Authorization →
  **401** (prompts/* gated identically to tools/*), token not echoed.
- `test_prompts_get_without_role_403` — valid bearer **without** the role → **403 insufficient_scope** on a
  `prompts/get` request; body leaks no token/claim/traceback.
- `test_prompts_get_with_role_returns_playbook_200` — valid bearer **with** the role → **200**, and the parsed
  SSE result frame's `messages[0].content.text` contains the requested `entity_id` and the five tool names.

**Regression (unchanged, must stay green):** every existing tool test in all four files, both existing MCP
property tests, `test_mcp_server.py::test_tool_set_is_exactly_the_five`, and the F-2 wire-error / annotation
pins.

---

## 8. NON-goals (explicit)

- **A new MCP tool of any kind** (esp. a freshness tool) — the surface stays exactly five tools; freshness is
  REST-only (ADR 0123 D5). Out.
- **Dynamic / templated tool RESULTS inside a prompt** — a prompt never queries Neo4j/Postgres or embeds live
  data; it is static guidance parameterised only by the validated arg. Out (and this is why prompts take no
  client).
- **A Hermes `deploy/hermes/config.yaml` prompts allowlist** — no such key exists; adding one is speculative
  (§5.4). Out (revisit trigger only).
- **Escaping-instead-of-validating the args** — rejected in favour of reuse-`validate_entity_id`-and-reject
  (§5.2, ADR 0125 A1). Out.
- **A server-side `connector_id` filter on `GET /sources/freshness`** — the endpoint returns all instances
  (ADR 0123 D3); the prompt's `connector_id` narrows the analyst's attention client-side, it is not a new
  query param. Out.
- **`describe_prompt` / prompt pagination / prompt "resources" / a client module / JMESPath projection**
  (F-9/F-10/F-11/F-12) — out.
- **Any `mcp`/FastMCP version bump** — unnecessary; `1.28.1` supports everything (§1). Out.
- **Changing any tool's happy-path payload bytes** — this gate adds a sibling surface, it does not touch tools.

---

## 9. Slice breakdown

**ONE slice** (P1 / S; a single additive production edit in one file plus tests). The two prompts, the
validation helpers, and the two-line wiring into the builders all land together; splitting them would create
a half-registered surface with no benefit.

- **Slice 1 — MCP analyst-playbook prompts (`entity-workup`, `freshness-audit`).**
  Production (`src/worldmonitor/mcp/server.py`): add `_PROMPT_ARG_MAX_LEN`, the two `_*_TEMPLATE` constants,
  `_prompt_error`, `_require_valid_prompt_arg`, `prompt_entity_workup`, `prompt_freshness_audit`,
  `_register_prompts(server)`, and call it in `build_server` and `build_http_app`.
  Tests: all of §7 (new `test_mcp_prompts.py` + `test_prop_mcp_prompt_args.py`; appended tests in the two
  integration files). ADR 0125 → **ACCEPTED** at the merging PR; regenerate the ADR index.

*(No sanctioned split point. If a reviewer insists on two PRs: 1a = `entity-workup` + helpers + its tests; 1b
= `freshness-audit` + its tests. They are orthogonal, but the default is one slice — do not split without
cause.)*

---

## 10. Open items for the test-author / builder

1. **Confirm F-5 is on `master` before building.** The `entity-workup` template references `summary=true`
   (F-5). If, at F-4 build time, `git log origin/master` does **not** include the F-5 merge, delete the single
   `Step 3` sentence "On a high-degree node, pass summary=true first for a `{count, sample}` taste…" (and its
   `{{count, sample}}`) from the template — everything else is F-5-independent. Note the decision in the PR.
2. **Prefix stripping in the wire tests.** The SDK prepends **`Error rendering prompt <name>: `** (NOT
   `Error executing tool …`); strip that exact prefix, then `json.loads`. The missing-required-arg path has
   **no** prefix (`Missing required arguments: {…}`) — assert on that path separately.
3. **`Prompt` import.** `from mcp.server.fastmcp.prompts.base import Prompt` (or use `@server.prompt(name=…)`);
   hyphenated names **require** the explicit `name=` kwarg.
4. **`{{…}}` escaping in the entity-workup template.** The literal `{count, sample}` must be written as
   `{{count, sample}}` in the Python template string so `.format(entity_id=…)` leaves it as `{count, sample}`.
   Pin the rendered (post-`.format`) text, and assert the substitution field count is exactly one.
5. **Validation order + no reflection.** Length cap FIRST, then `validate_entity_id`; the error envelope and
   any log line must NOT echo the raw hostile arg unbounded (log `field`+length+short repr slice only).
6. **Do NOT edit** `read_guards.py`, `graph/queries.py`, `mcp/auth.py`, `deploy/hermes/config.yaml`, or the
   existing tool/auth/property tests. `test_tool_set_is_exactly_the_five` must stay green untouched.
7. **Run locally:** full `pytest -m "not integration"` + the MCP integration suites (Docker is available;
   stdio spawns a real server against a testcontainer Neo4j, and the HTTP suite uses the in-proc fake
   verifier). Also `ruff format --check .` repo-wide and `python scripts/gen_adr_index.py --check`.
8. **Person-affecting / cosign:** none required — declarative read-only text, `person_affecting=false`,
   `human_fork=false` (ADR 0125). No pause.

---

## gate.scope (to apply at fleet start)

> Written by the **F-4 fleet** (NOT by this planning task). One path glob per line; the scope-guard hook
> enforces it.

```
src/worldmonitor/mcp/server.py
tests/unit/test_mcp_prompts.py
tests/property/test_prop_mcp_prompt_args.py
tests/integration/test_mcp_stdio.py
tests/integration/test_mcp_http_transport.py
docs/decisions/0125-mcp-prompts.md
docs/decisions/README.md
docs/reviews/GATE_F4_MCP_PROMPTS_SPEC.md
```
