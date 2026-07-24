# 0125 — MCP prompts as analyst playbooks (`entity-workup`, `freshness-audit`)

- **Status:** PROPOSED (flips to ACCEPTED at the gate-completing PR — the 0117–0124 convention)
- **Date:** 2026-07-24
- **human_fork:** false — a reversible, additive, read-only **guidance surface**. Two declarative text
  prompts registered beside the five read tools; no product/architecture fork. Each scoping call (validate-
  and-reject vs escape-and-include for args; REST-reference vs a new freshness MCP tool; no Hermes config
  change) has a sensible default, a cheap reversal, and a recorded revisit trigger (below). Not marked OPEN.
- **person_affecting:** false — see "Person-affecting reasoning" below. A prompt is **static guidance text**:
  it returns **no** graph node/edge and **no** data about any Person, makes **no** change to the live system
  (no ER threshold, guard mode, sensitivity park, score, or model/param promotion), performs **no**
  inference/scoring/attribution/resolution, has **zero** egress, and writes **nothing** (no table, no
  migration, no graph write).
- **human_cosign:** not required — reversible, non-person-affecting, read-only text (per the cost directive:
  reserve cosign for irreversible / person-affecting changes).
- **Backlog/roadmap:** `docs/fable-review/91_OG_HARVEST_BACKLOG.md` §B row **F-4** (P1 / S, one gate).
- **Spec:** `docs/reviews/GATE_F4_MCP_PROMPTS_SPEC.md`.
- **Builds on:** ADR 0063 (stdio FastMCP + shared `read_guards`), 0090 (authenticated HTTP MCP transport),
  0121 / F-2 (annotations + typed output schemas + `{error, hint}` envelope + the strip-SDK-prefix wire-error
  idiom), 0122 / F-3 (the 5th tool `get_entity_dossier`; shared-registration precedent), 0123 / F-1 (the
  6-state freshness surface + `GET /sources/freshness`; the freshness MCP tool is a recorded deferral),
  0124 / F-5 (the `summary` flag — **F-4 lands on top of F-5**), 0042 (single-tenant).

## Context

Backlog row F-4 asks for **MCP prompts as analyst playbooks** — `entity-workup` and `freshness-audit` —
"declarative step+purpose workflows for Hermes. One gate, both transports, arg length-caps tested." MCP
*prompts* are a first-class surface distinct from *tools*: a named, parameterised **text template** a host
(Hermes) or a human can retrieve to orient a workflow. They call nothing and return no data.

Facts established against the installed **`mcp==1.28.1`** (source-read + a live probe; do not re-derive —
spec §1):

1. **Registration is native and one-line:** `server.add_prompt(Prompt.from_function(fn, name="entity-workup",
   title=…, description=…))` (hyphenated names require the explicit `name=`). Args are derived from the fn
   signature; a defaulted param (`connector_id: str = ""`) is correctly `required=False`. A `str`-returning fn
   renders to a single `user` `TextContent` message; the wire result is `GetPromptResult`.
2. **Both transports are automatic.** FastMCP registers the `prompts/list` + `prompts/get` handlers
   unconditionally at construction and advertises the `prompts` capability, so a registered prompt surfaces
   over **both** stdio (`build_server`) and `streamable_http_app()` (`build_http_app`) with **no extra mount**.
3. **Prompts/* are auth-gated identically to tools/*.** `streamable_http_app()` wraps the *entire* MCP
   endpoint at one route in `RequireAuthMiddleware(required_scopes)`; there is no per-method allowlist, so
   `prompts/list`/`prompts/get` already require the `worldmonitor:read` scope (401 without a bearer, 403
   without the role) with no `mcp/auth.py` change.
4. **A raised prompt fn surfaces cleanly.** `Prompt.render` wraps a raise as
   `ValueError("Error rendering prompt <name>: <original>")` (logged to the SDK logger → stderr via our
   `configure_stderr_logging`), so a hostile argument yields a JSON-RPC error frame, never a crash or a
   stdout-corrupting traceback. The prefix is stripped exactly like F-2's tool prefix.
5. **Freshness has no MCP tool.** ADR 0123 (F-1) D5 deliberately kept the MCP server Neo4j-only and deferred
   `get_source_freshness`; freshness lives entirely in Postgres behind `GET /sources/freshness`. So the
   `freshness-audit` playbook must reference the **REST** endpoint, not an MCP tool.

The prompt **text is a versioned contract** — pinned verbatim so a host can rely on it and so a later reword
is a deliberate change, not drift.

## Decision

Ship **two declarative, read-only MCP prompts** — `entity-workup` and `freshness-audit` — registered once and
exposed on both transports.

- **D1 — Two prompts, declarative step+purpose, leads-not-verdicts.** `entity-workup(entity_id)` walks the
  five read tools in order (`get_entity` → `get_provenance` → `get_neighbors` → `find_paths` →
  `get_entity_dossier`), each step naming the tool, its purpose, and the argument shape, framed as *ranked
  hypotheses with provenance for human review, never an automated verdict, and never merge/attribute/label a
  person*. `freshness-audit(connector_id="")` walks `GET /sources/freshness` and interprets the six
  freshness states (disabled/error/no_data/very_stale/stale/fresh, per ADR 0123) as evidence of collection
  gaps for human review. The exact text is pinned in the spec §4 and by a verbatim-template test. The prompts
  contain no chain-of-thought and no imperative that could be read as an instruction to bypass a guard.

- **D2 — One shared registration site, both transports.** A new `_register_prompts(server)` (taking **no
  `Neo4jClient`** — prompts are pure text) is called by **both** `build_server` and `build_http_app` right
  after `_register_read_tools`. Exactly one registration site → the stdio and HTTP prompt surfaces cannot
  drift (a no-drift test pins it). Prompts/* inherit the HTTP `RequireAuthMiddleware` unchanged.

- **D3 — Args are validate-and-reject, reusing the tools' guard.** Each argument is **length-capped first**
  (`_PROMPT_ARG_MAX_LEN = 256`) then **shape-validated** by the shared `read_guards.validate_entity_id`
  (ID_PATTERN) — the same predicate the five tools use. `entity_id` is required and non-empty; `connector_id`
  defaults to `""` (= all instances, validation skipped for empty). A validated arg is drawn only from the
  canonical-id alphabet `[A-Za-z0-9:._-]`, so the interpolated text carries no quote/newline/brace/`$` — there
  is no injection surface. Prompts interpolate the arg only as **plain template text**, never into anything
  executable.

- **D4 — Clean `{error, hint}` envelope on a hostile arg.** A `_prompt_error(error, hint) -> ValueError`
  (mirroring F-2's `_tool_error`, but a `ValueError` because that is what the SDK's `get_prompt` surfaces)
  gives oversize → `{"error":"argument too long"}` and injection-shaped → `{"error":"invalid argument"}`. The
  envelope and any log line never reflect the raw hostile bytes; the log goes to stderr (STDOUT PURITY holds).

- **D5 — No Hermes config change.** `deploy/hermes/config.yaml`'s `tools.include` is a *tool*-specific
  allowlist; there is no prompts allowlist key and no JSON-Schema for the config. Prompts are a distinct,
  capability-free surface Hermes discovers via `prompts/list`. No change now; a prompts allowlist is a recorded
  revisit trigger if a confirmed v0.17.0 config key and an operator need appear.

- **D6 — One cheap property test, by decision.** F-4 touches no CLAUDE.md invariant, so the mandatory-`@given`
  rule does not apply; we nonetheless add one property test (the *hostile-arg never crashes / never corrupts
  stdout / accept-iff-valid-and-under-cap* guarantee) because that is precisely the class an example test
  under-samples. Recorded as a decision, not an omission (mirroring ADR 0121 §3.1 / 0124).

This ADR flips to **ACCEPTED** at the gate-completing PR.

## Alternatives considered

- **A1 — Escape-and-include the args (accept any under-cap string, `json.dumps`-escape it into the text)
  instead of validate-and-reject.** Rejected: it would hand the caller an id the named tool would then reject
  (playbook/tool inconsistency), require escaping logic, and *not* deliver the backlog's "injection-shaped →
  clean error" (it passes hostile-shaped ids through as text). Reusing `validate_entity_id` loses no
  legitimate canonical id and removes the injection surface entirely. Revisit only if a legitimate id shape
  outside ID_PATTERN ever appears (it would first have to be accepted by the tools).
- **A2 — Enforce the length cap via a pydantic `Annotated[str, Field(max_length=…)]` on the fn signature.**
  Rejected: pydantic's `validate_call` would raise a `ValidationError` with a noisy pydantic message instead
  of our structured `{error, hint}` token, and it would not give the length-then-shape ordering. In-body
  validation (mirroring `_require_valid_id`) yields the consistent envelope. (The plain `str` annotation is
  kept so the arg still surfaces as a normal `prompts/list` argument.)
- **A3 — Ship a `get_source_freshness` MCP tool and have `freshness-audit` call it.** Rejected: that reopens
  ADR 0123 A1/D5 (wire Postgres into the deliberately Neo4j-only MCP server, new compose infra, a widened
  trust boundary) — its own deliberate ADR, not a rider on a P1/S prompts gate. The prompt references the
  existing REST surface, which fully serves the need. Revisit trigger = F-1's own (the freshness MCP tool
  when Hermes is host-operational).
- **A4 — Add a `prompts` allowlist to `deploy/hermes/config.yaml`.** Rejected (D5): no such key is confirmed
  in the Hermes v0.17.0 config reference, and inventing one is speculative; prompts are read-only,
  capability-free, and safe by construction. Recorded as a revisit trigger.
- **A5 — Make the prompts dynamic (embed a live entity summary / a rendered freshness snapshot).** Rejected:
  that would make a prompt fetch from the graph/Postgres (giving it a client + a DB session), turn it into a
  data surface with its own provenance/staleness/auth questions, and blur the tool/prompt boundary. Prompts
  stay static guidance; live data is what the tools/REST are for.
- **A6 — More prompts now (e.g. a merge-review or attribution playbook).** Rejected for scope: the backlog
  names exactly these two; keep the gate small. Adding a third later is a trivial additive follow-up.

## Person-affecting reasoning

A prompt is a **text template**. It returns no graph node or edge, embeds no entity data, and names no
specific person. Reasoned out:

1. **Guidance, not data.** The payload is a fixed workflow parameterised only by a validated id-shaped token;
   it contains no facts about any Person and performs no inference/attribution/scoring/resolution.
2. **No change to the live system.** CLAUDE.md's person-affecting sign-off gates cover *changes* affecting a
   real person (ER thresholds, individual-affecting scores, model/param promotion). A prompt persists nothing,
   decides nothing, and mutates no threshold/guard/score.
3. **Leads-not-verdicts is baked into the text.** `entity-workup` explicitly frames its output as ranked
   hypotheses for human review and explicitly says not to merge/attribute/label a person from the workup —
   reinforcing the invariant rather than eroding it.
4. **Zero egress, read-only, same gate, same audience.** No LLM, no external transmission, no write. Prompts/*
   ride the same `worldmonitor:read` auth as the tools (single-tenant, D1 ADR 0042) — neither lowers the bar
   nor widens who can see it.

**Conclusion:** not person-affecting in the CLAUDE.md sense. **Revisit** if a future prompt (i) embeds live
person-level data, (ii) begins driving a person-affecting decision, or (iii) gains egress.

## Reversibility

**Reversible** (`human_fork=false`, `person_affecting=false`).

- **Reversal cost: low** — remove `_register_prompts` (and its two call sites), the two thin prompt fns + two
  template constants, the arg-guard helpers + one constant, and the new/appended tests. **No** data migration,
  **no** schema/store change, **no** new table, **no** stored artifact, **no** tool/auth change. The one soft
  lock-in is the **prompt text as contract** — a new, additive, auth-gated surface with no locked-in consumer
  yet (Hermes is not yet host-operational); rewording later is a deliberate contract evolution, not silent
  drift.
- **Revisit triggers:**
  - (a) **A `get_source_freshness` MCP tool** ships (F-1 revisit) → update `freshness-audit` to name the tool
    alongside the REST endpoint.
  - (b) **F-1's freshness vocabulary / `/sources/freshness` shape changes** → `freshness-audit` is the coupled
    contract to update (the six states + field names are quoted from ADR 0123).
  - (c) **A confirmed Hermes prompts allowlist key** + an operator need → scope Hermes to these two prompts in
    `deploy/hermes/config.yaml`.
  - (d) **A third playbook is wanted** → additive; register beside these two.
  - (e) **F-5 is not on `master` at F-4 build time** → drop the one `summary=true` sentence from
    `entity-workup` (spec §10).

## Invariant gate note

F-4 touches **no** CLAUDE.md invariant (no provenance-stamping change — prompts return no node/edge; no
ER/merge/threshold; no canonical-id resolution; no guard; no write; no egress). So a `@given` is **not**
mandated by build-discipline. We nonetheless include **one** cheap property test
(`test_prop_mcp_prompt_args.py`): for arbitrary argument strings, the thin prompt fn (1) keeps stdout
byte-empty on both paths, (2) accepts **iff** `len ≤ _PROMPT_ARG_MAX_LEN and validate_entity_id(arg)` and then
contains the arg verbatim, and (3) otherwise raises a clean `{error, hint}` `ValueError` that does not reflect
the hostile bytes. Recorded as a **decision, not an omission** (mirroring ADR 0121 §3.1 / ADR 0124).

## Consequences

- Hermes and human analysts get two first-class, discoverable, read-only playbooks over both MCP transports —
  a workflow for working up one entity (as leads) and a workflow for auditing source freshness (as collection
  gaps) — without adding a tool, a datastore, a write path, or an egress path.
- The tool surface stays exactly five; `prompts/list` does not perturb the tool pins. Prompts/* are auth-gated
  identically to tools/* on HTTP by construction. Both transports stay lockstep via one registration site.
- The freshness playbook honestly points at the REST surface (no freshness MCP tool exists), and the arg
  guards reuse the tools' own `validate_entity_id` + a length cap, so a hostile argument is a clean error, not
  a crash. **No CLAUDE.md invariant is touched; no migration, no new store, no write, no egress; single-tenant.**
