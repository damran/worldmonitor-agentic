# GATE L1 — LLM-egress choke-point hardening — BUILD SPEC

- **Owning decision:** ADR 0104 (`docs/decisions/0104-llm-egress-chokepoint-hardening.md`), status PROPOSED.
- **Source:** Fable LLM/Hermes architecture review, 2026-07-05 (L1 items 1–5). Anchors verified against tree `a50c078`.
- **Governance:** `human_fork: false`, `person_affecting: false`, `human_cosign: n/a` (non-person-affecting
  egress-hardening; no `resolution/**` or person-affecting path touched — see ADR 0104 §Person-affecting analysis).
- **Scope discipline:** L1 = the *non-person-affecting* hardening that makes the **advisory operator-discretion**
  sovereignty model (F1, ADR 0094 D2) honest, non-bypassable, and auditable. The **enforced-classification egress
  gate (L2) is OUT** and is not built here. So are F2 / F4 / L3 / L4 (§8).
- **Two slices, both branched from `origin/master`, both individually mergeable, no code dependency between them:**
  - **L1-a** — egress audit + choke-point (items 1, 2, 3). `.claude/gate.scope` in this repo is set to L1-a. **First.**
  - **L1-b** — caller attribution + role-gate (items 4, 5). Gets its **own** `.claude/gate.scope` when it starts.

The test-author writes RED tests first; the builder makes them GREEN without weakening any FROZEN invariant.

---

## 1. Verified current state (do not re-derive; confirm if editing)

| Fact | Location |
|---|---|
| The only real external-SDK imports in `src/worldmonitor` are `import litellm` in `llm/gateway.py:22` + `llm/claude_shim.py:18-19`. **Zero** `openai`/`anthropic` imports. Mentions of "litellm" in `api/llm.py`, `settings.py`, `modes.py` are docstring/comment text, **not imports** → the choke-point test **MUST use AST, not grep**. | grep confirmed |
| Pre-call emit (completeness) | `gateway.py:83-93` |
| Usage attached to record **after** the call, never re-emitted; `emit` does not serialize `usage` | `gateway.py:110-114`, `egress_log.py:49-65` |
| Audit gated on `settings.llm_egress_log_enabled` (default `True`) | `gateway.py:92`, `settings.py:304` |
| Per-mode `data_left_perimeter` is available as `mode_record.data_left_perimeter` (already computed at `gateway.py:70,87`) | `modes.py:54-83` |
| `caller_tag="hermes"` hardcoded; `_principal` present but unused | `api/llm.py:128,86` |
| `/v1` gated on `Depends(get_principal)` = any valid token; REST layer has **no** role check today | `api/llm.py:84`, `deps.py:48-59` |
| MCP requires `worldmonitor:graph-read` role via the Zitadel claim `urn:zitadel:iam:org:project:roles` (a Mapping role→{orgId}) | `mcp/auth.py:33,37,65,75-78` |
| `Principal.claims` carries the full verified claims dict (so a REST role check reads `principal.claims[<roles-claim>]`) | `authz/oidc.py:23-35` |

---

## 2. Slice L1-a — egress audit + choke-point

### 2.1 Files (allowed globs — this is `.claude/gate.scope`)

```
docs/decisions/0104-llm-egress-chokepoint-hardening.md   # this gate's ADR (already written)
docs/decisions/README.md                                 # regen ADR index (builder step 1)
docs/reviews/GATE_L1_LLM_EGRESS_HARDENING_SPEC.md         # this spec
src/worldmonitor/llm/gateway.py                           # items 2 + 3 (post-call emit, fail-closed)
src/worldmonitor/llm/egress_log.py                        # item 2 (emit serializes usage)
src/worldmonitor/settings.py                              # comment-only if touched; NO new field
tests/unit/test_llm_gateway.py                            # UPDATE the success-emit-count test (see 2.5)
tests/test_llm_egress_chokepoint.py                       # NEW — P-CHOKE exhaustive AST scan (repo-root → quality job)
tests/property/test_prop_llm_chokepoint.py                # NEW — P-CHOKE @given detector non-vacuity
tests/property/test_prop_llm_egress_hardening.py          # NEW — P-EGRESS-FAILCLOSED + P-EGRESS-USAGE
.claude/gate.scope
```

**Not in scope for L1-a** (enforced by the freeze, §7): `llm/modes.py`, `llm/claude_shim.py`, `api/**`, `authz/**`,
`resolution/**`, `graph/**`, `db/**`.

### 2.2 Item 1 — machine-enforced choke-point contract

**Contract:** no module under `src/worldmonitor` **outside the `llm/` package** may import an external LLM SDK.
Forbidden top-level modules: `{"litellm", "openai", "anthropic"}`. Forbidden forms (all): `import X`, `import X.y`,
`import X as z`, `from X import ...`, `from X.y import ...`. The `llm/` package (`src/worldmonitor/llm/**`) is the sole
allowlisted home.

**Implementation — AST, not grep.** A helper walks `ast.parse(source)` for each `.py` under `src/worldmonitor`,
collecting `ast.Import` (`alias.name.split(".")[0]`) and `ast.ImportFrom` (`node.module.split(".")[0]`, ignoring
relative imports where `node.level > 0`). A file is a **violation** iff its path is **not** under `src/worldmonitor/llm/`
**and** it imports a forbidden top-level module. Grep is forbidden here — `api/llm.py`/`settings.py`/`modes.py` contain
the string "litellm" in prose and would false-positive.

**Secondary (defence-in-depth) dynamic-import check:** also flag `importlib.import_module("litellm")` /
`__import__("openai")` etc. — i.e. a `Call` to `importlib.import_module` or `__import__` whose first positional arg is a
constant string whose top-level module is forbidden — when it appears outside `llm/`. This is a best-effort AST check;
document it as such.

**Where:** the guard lives at repo-root `tests/test_llm_egress_chokepoint.py` (like `test_contract_consistency.py`) so
it runs in the `quality` job under `pytest -m "not integration"` with no new CI step. Resolve the repo root from
`Path(__file__).resolve().parents[1]` — never from cwd.

### 2.3 Item 2 — post-call usage record + `emit` serializes usage

**Gateway (`gateway.py`) — the exact required control flow inside `chat()`** (preserves INV-S2-EGRESS ordering):

```
active_mode = mode or self._active_mode
mode_record = REGISTRY[active_mode]
model, api_base, api_key = self._resolve_call_params(active_mode)
target_host = _extract_target_host(api_base, active_mode)
external = mode_record.data_left_perimeter

# ── item 3 (fail-closed) — BEFORE any emit and BEFORE the provider call ──
if external and not self._settings.llm_egress_log_enabled:
    raise LLMGatewayError(
        "external LLM egress refused: audit is disabled "
        "(llm_egress_log_enabled=False) for an external mode — no durable audit, no egress"
    )

record = EgressRecord(..., usage=None)          # unchanged fields; usage starts None
if self._settings.llm_egress_log_enabled:
    egress_log.emit(record)                     # PRE-CALL completeness record (INV-S2-EGRESS) — UNCHANGED

try:
    response = litellm.completion(model, messages, **call_kwargs)
except Exception as exc:
    raise LLMGatewayError(...) from exc          # UNCHANGED

usage_val = getattr(response, "usage", None)
if usage_val is not None:
    record.usage = usage_val                     # in-place enrich (unchanged)
if self._settings.llm_egress_log_enabled:
    egress_log.emit(record)                      # NEW — POST-CALL record carrying usage (item 2)
return response
```

Notes the builder MUST honour:
- Both emits are guarded by the **same** `llm_egress_log_enabled` condition; the fail-closed check in item 3 guarantees
  external modes always have it `True`, so an external success always produces **two** records.
- LOCAL with logging disabled → **no** emits (pre or post), call proceeds. LOCAL/external with logging enabled → pre-call
  record (usage None) **then** post-call record (usage set). Failure after a pre-call emit → **one** record only.
- Do **not** change the whole-module `import litellm` / `litellm.completion(...)` call pattern (tests monkeypatch
  `litellm.completion`).

**`egress_log.emit` (`egress_log.py`) — serialize usage when present.** When `record.usage is not None`, defensively
extract `prompt_tokens` / `completion_tokens` / `total_tokens` via `getattr(record.usage, name, None)` and include them
in **both** the log line and `extra=` (e.g. `extra` keys `llm_usage_prompt_tokens`, `llm_usage_completion_tokens`,
`llm_usage_total_tokens`, and a `usage=…` token in the format string). When `record.usage is None` (the pre-call
record), the line MUST be well-formed without usage (no crash, no `None`-token noise required). **Never** log message
content or the api key (ADR 0091 §3). A tiny private helper for the token extraction is fine.

*(Optional, RECOMMENDED for legibility, not mandatory: add a `phase` field to `EgressRecord` with a default —
`"attempt"` pre-call, `"completed"` post-call. A new field WITH a default is additive and safe. If added, it must not
break the FROZEN `test_llm_egress_completeness.py`. Mandatory contract = usage serialization + the post-call emit; phase
is builder discretion.)*

### 2.4 Item 3 — fail-closed refuse

Covered by the control flow in 2.3. The refusal raises `LLMGatewayError` **before** any emit or provider call; no egress
record is written (nothing left the perimeter). A `WARNING`-level log of the refusal is RECOMMENDED (not an
`EgressRecord`, not mandatory). LOCAL is unaffected.

### 2.5 REQUIRED update to an existing unit test (expected, not scope creep)

`tests/unit/test_llm_gateway.py :: test_successful_call_writes_exactly_one_egress_record_with_token_usage`
(lines ~207-256) asserts `len(captured_records) == 1` on a **successful** call. Item 2 makes success emit **two**
records. **Update this test** to the new contract:
- On a successful call there are **exactly two** emitted records: `captured_records[0].usage is None` (pre-call),
  `captured_records[1].usage is not None` with `total/prompt/completion == 33/11/22` (post-call). Assert the pre-call
  record precedes the post-call record (it is `[0]`), and that both are the **same mutated object** (mutable
  `EgressRecord` enriched in place) — i.e. `captured_records[0] is captured_records[1]` and its final `.usage` is set.
- Keep `test_provider_exception_surfaces_as_gateway_error_and_record_still_exists` semantics: on **failure** there is
  still **≥ 1** record (the pre-call one); its `>= 1` assertion already survives — do not weaken it.

`tests/property/test_llm_egress_completeness.py` stays **byte-unchanged and green**: it clears `captured`/`events`
per call, reads `captured[0]` (the pre-call record) and uses `events.index("log")` (first occurrence), all of which
survive a second post-call emit. If the test-author believes it must change, that is a red flag — STOP and escalate.

### 2.6 L1-a property invariants (@given — RED-first)

For each: **NAME · STATEMENT · GENERATOR · ORACLE · NON-VACUITY.** Reuse the spy/monkeypatch patterns from the existing
`tests/property/test_llm_egress_completeness.py` (patch `worldmonitor.llm.egress_log.emit` + `litellm.completion`) and
`_make_test_settings`.

**P-CHOKE (exhaustive guard) — in `tests/test_llm_egress_chokepoint.py`.**
- *Statement:* every `.py` under `src/worldmonitor` **outside** `src/worldmonitor/llm/` is free of `litellm`/`openai`/
  `anthropic` imports (AST-detected).
- *Generator:* deterministic **exhaustive** enumeration of all such files (NOT sampled — you want to check every file).
- *Oracle:* the AST detector returns an empty violation list; on failure the assertion names the offending file(s) +
  module(s).
- *Non-vacuity:* proven by the companion metamorphic test (below), and by the fact that `llm/gateway.py` +
  `llm/claude_shim.py` (which DO import litellm) are correctly **excluded** — assert the detector, if run without the
  `llm/` exclusion, WOULD flag them (sanity anchor), then confirm it does not with the exclusion.

**P-CHOKE-DETECTOR (metamorphic non-vacuity) — in `tests/property/test_prop_llm_chokepoint.py`.**
- *Statement:* the detector flags a forbidden import placed **outside** `llm/`, ignores the same string in a
  **docstring/comment**, and ignores a forbidden import **inside** `llm/`.
- *Generator:* `@given` over (forbidden module ∈ {litellm, openai, anthropic}) × (import form ∈ {`import X`,
  `import X.y`, `import X as z`, `from X import a`}) × (placement ∈ {module-body, inside-a-function}) × (a decoy: the
  same module name embedded in a docstring/comment/string literal). Build a synthetic source string and a synthetic
  virtual path; feed to the detector helper.
- *Oracle:* detector returns a violation **iff** a real import (any form, any placement) of a forbidden module appears
  under a non-`llm/` path; returns **no** violation for the docstring/comment/string-literal decoy and for a
  forbidden import under an `llm/` path.
- *Non-vacuity:* a grep-based or always-empty detector fails the decoy case and/or the real-import case.

**P-EGRESS-FAILCLOSED — in `tests/property/test_prop_llm_egress_hardening.py`.**
- *Statement:* for an **external** mode (`data_left_perimeter == True`) with `llm_egress_log_enabled == False`,
  `gateway.chat()` raises `LLMGatewayError` and `litellm.completion` is **never** called; for LOCAL with logging
  disabled, and for any mode with logging enabled, the call proceeds and `litellm.completion` **is** called.
- *Generator:* `@given` over `mode ∈ LLMMode` × `egress_enabled ∈ {True, False}`. Build `Settings` via a helper mirroring
  `_make_test_settings(llm_mode=…, llm_egress_log_enabled=…)`.
- *Oracle:* spy on `litellm.completion` (records call count) + spy on `egress_log.emit`. For `(external, disabled)`:
  `pytest.raises(LLMGatewayError)`, provider-spy count == 0, emit count == 0. For `(external, enabled)`,
  `(LOCAL, enabled)`, `(LOCAL, disabled)`: no raise, provider-spy count == 1.
- *Non-vacuity:* an always-raise impl fails the LOCAL-disabled + external-enabled cases; a never-raise impl (today's
  behaviour) fails external-disabled.

**P-EGRESS-USAGE — in `tests/property/test_prop_llm_egress_hardening.py`.**
- *Statement:* a **successful** call whose response carries a `usage` object produces, in order, (a) a pre-call record
  with `usage is None` emitted **before** `litellm.completion`, and (b) a post-call record with `usage is not None`
  carrying the response's token counts emitted **after** `litellm.completion`; and `egress_log.emit`'s serialization
  includes those token counts (assert via `caplog`/the record).
- *Generator:* `@given` over `mode ∈ LLMMode` (logging enabled) × generated `(prompt, completion, total)` token integers
  attached to the fake `ModelResponse.usage`.
- *Oracle:* an event list + captured-records list (as in the existing completeness test); assert `events` == `[emit,
  provider, emit]` order (pre-emit < provider < post-emit); `captured[0].usage is None`; final record `.usage` reflects
  the generated counts; and that a `caplog`-captured log line for the post-call emit exposes the token counts
  (proving `emit` serializes usage).
- *Non-vacuity:* today's single-emit code fails (no second record with usage); an `emit` that ignores usage fails the
  serialization assertion.

---

## 3. Slice L1-b — caller attribution + role-gate

### 3.1 Files (this becomes L1-b's own `.claude/gate.scope` on its own branch)

```
docs/reviews/GATE_L1_LLM_EGRESS_HARDENING_SPEC.md            # this spec (reference)
src/worldmonitor/authz/roles.py                              # NEW — role constant + claim helper
src/worldmonitor/api/deps.py                                 # NEW dep: require_llm_role
src/worldmonitor/api/llm.py                                  # items 4 + 5 (caller_tag + require_llm_role)
tests/unit/test_api_llm_endpoint.py                          # UPDATE _FakeVerifier to grant the role (see 3.4)
tests/property/test_api_llm_gateway_delegation.py            # UPDATE fake verifiers to grant the role (see 3.4)
tests/property/test_prop_llm_role_and_caller.py              # NEW — P-CALLER + P-ROLE
tests/unit/test_authz_roles.py                               # NEW (optional) — unit-level role-helper cases
.claude/gate.scope                                           # replaced with the L1-b globs when the slice starts
```

### 3.2 Item 5 — dedicated `worldmonitor:llm` role gate

- **`authz/roles.py` (new).** Define `ZITADEL_PROJECT_ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"` and
  `WM_LLM_ROLE = "worldmonitor:llm"`, plus a pure helper `principal_has_role(principal: Principal, role: str) -> bool`
  that reads `principal.claims.get(ZITADEL_PROJECT_ROLES_CLAIM)` and returns `isinstance(roles, Mapping) and role in
  roles` (mirrors `mcp/auth.py:_has_graph_read_role`, but in the REST layer, so `mcp/auth.py` stays FROZEN). This is a
  fresh, deliberate copy of the claim URN — do **not** refactor `mcp/auth.py` to share it (that widens blast radius);
  add a one-line comment noting the sibling definition in `mcp/auth.py`.
- **`api/deps.py` (add).** `require_llm_role(request: Request) -> Principal` — resolves the principal (via the existing
  `get_principal` logic) and raises `HTTPException(status_code=403, detail="Missing required role: worldmonitor:llm")`
  when `principal_has_role(principal, WM_LLM_ROLE)` is False; otherwise returns the principal. It must 401 (not 403) when
  there is no authenticated principal at all (unauthenticated ≠ forbidden) — i.e. reuse `get_principal` (which 401s) and
  then apply the 403 role check. **Do not** modify `get_principal` itself.
- **`api/llm.py` (change).** Swap the route dependency `Depends(get_principal)` → `Depends(require_llm_role)` so the
  handler both enforces the role and receives the authenticated `Principal`.

**Provisioning note (docs-only, no runtime code):** the Hermes service-principal must be granted the `worldmonitor:llm`
project role in Zitadel before `/v1` is usable by Hermes. Because Hermes is not yet host-operational (L4 deferred), this
introduces **no live regression**. Record this in the ADR (done) — do not add a provisioning script.

### 3.3 Item 4 — honest caller attribution

In `api/llm.py`, derive the audit caller from the authenticated principal:
`caller_tag = _principal.subject or "hermes"` (fall back to the current `"hermes"` default only when the subject is
empty). Pass it through the **existing** `gateway.chat(messages, caller_tag=…)` signature (no gateway change needed —
`caller_tag` already flows to the `EgressRecord`). Do not log message content (the existing no-leak test stays green).

### 3.4 REQUIRED updates to existing tests (expected — the auth contract changed)

Adding the role gate makes any existing `/v1` test whose verifier returns role-less claims start getting **403** instead
of 200. Update the fake verifiers to grant the new role so the existing behavioural assertions stay valid:
- `tests/unit/test_api_llm_endpoint.py` — `_FakeVerifier.verify` (currently returns `{"sub": "user-123"}`) → return
  `{"sub": "user-123", "urn:zitadel:iam:org:project:roles": {"worldmonitor:llm": {}}}`.
- `tests/property/test_api_llm_gateway_delegation.py` — `_FakeVerifier.verify` (returns `{"sub": "user-123"}`) → same
  addition of the `worldmonitor:llm` role claim. `_RejectingVerifier` stays as-is (it still 401s before any role check).

These updates keep INV-S3a-GATEWAY / -AUTH / -NOSTREAM green under the new gate; the **new** no-role→403 behaviour is
covered by P-ROLE (§3.5), not by weakening the existing tests.

### 3.5 L1-b property invariants (@given — RED-first)

Reuse the `_SpyGateway` + `TestClient` + fake-verifier + `_openai_body()` patterns from
`tests/property/test_api_llm_gateway_delegation.py`.

**P-CALLER — in `tests/property/test_prop_llm_role_and_caller.py`.**
- *Statement:* for an authenticated `/v1` request from a principal **holding** `worldmonitor:llm`, the gateway receives
  `caller_tag == principal.subject`; when the subject is empty, `caller_tag` falls back to the default (`"hermes"`).
- *Generator:* `@given` over generated `subject` strings (include empty) × valid `_openai_body()`. The fake verifier
  returns `{"sub": subject, "urn:zitadel:iam:org:project:roles": {"worldmonitor:llm": {}}}`.
- *Oracle:* a `_SpyGateway` records `caller_tag`; assert response 200 and `spy.calls[0]["caller_tag"] == (subject or
  "hermes")`.
- *Non-vacuity:* the hardcoded `"hermes"` (today) fails for any generated subject that is non-empty and ≠ `"hermes"`.

**P-ROLE — in `tests/property/test_prop_llm_role_and_caller.py`.**
- *Statement:* a valid token **without** `worldmonitor:llm` → **403** on `POST /v1/chat/completions` and the gateway is
  **never** called; a valid token **with** the role → **200** and the gateway is called exactly once.
- *Generator:* `@given` over valid `_openai_body()` × a role-set that either omits `worldmonitor:llm` (may include decoy
  roles like `worldmonitor:graph-read`, or be empty/absent) or includes it. Two module-level apps (role-granting vs
  role-omitting verifier), or one verifier parameterised by the drawn role-set.
- *Oracle:* `_SpyGateway`; for the no-role case assert `resp.status_code == 403` and `len(spy.calls) == 0`; for the
  with-role case assert `200` and `len(spy.calls) == 1`. (A tokenless request still 401s via `get_principal`, not 403 —
  assert this too, to prove unauthenticated ≠ forbidden.)
- *Non-vacuity:* an always-403 impl fails the with-role case; the current always-allow behaviour fails the no-role case.

---

## 4. Builder task lists (ordered)

**L1-a (do these; keep everything else byte-identical):**
1. Regenerate the ADR index: `uv run python scripts/gen_adr_index.py` (adds the 0104 row to `docs/decisions/README.md`),
   then `uv run python scripts/gen_adr_index.py --check` passes.
2. `egress_log.py`: make `emit` serialize `usage` (line + `extra=`) when present; safe/well-formed when `None`.
3. `gateway.py`: add the fail-closed refuse (external + audit-disabled → `LLMGatewayError` before any emit/provider
   call); add the post-call second emit carrying usage. Follow the exact control flow in §2.3.
4. Update `tests/unit/test_llm_gateway.py` success-emit test to the two-emit contract (§2.5).
5. Ensure the RED tests (`tests/test_llm_egress_chokepoint.py`, `tests/property/test_prop_llm_chokepoint.py`,
   `tests/property/test_prop_llm_egress_hardening.py`) pass.
6. Optional comment-only touch to `settings.py:302-304` documenting the external-mode fail-closed exception. No new field.

**L1-b (separate PR from `origin/master`, own gate.scope):**
1. `authz/roles.py` (new): constants + `principal_has_role`.
2. `api/deps.py`: `require_llm_role` (401 if unauthenticated, 403 if role missing, else return principal).
3. `api/llm.py`: swap the route dep to `require_llm_role`; derive `caller_tag = _principal.subject or "hermes"`.
4. Update the two existing fake verifiers to grant the role (§3.4).
5. Ensure P-CALLER + P-ROLE pass; keep INV-S3a-* tests green.

---

## 5. Acceptance criteria (all measurable; both slices)

- **FULL** `uv run pytest -m "not integration"` GREEN (repo-wide, not just new files) — the quality job runs exactly
  this. No Docker / no integration test required for L1 (pure logic + FastAPI `TestClient`).
- All new `@given` properties GREEN: L1-a → P-CHOKE (+ detector metamorphic), P-EGRESS-FAILCLOSED, P-EGRESS-USAGE;
  L1-b → P-CALLER, P-ROLE.
- The choke-point guard is **non-vacuous**: the detector metamorphic proves it flags an injected external-SDK import
  outside `llm/` and ignores docstring mentions + `llm/`-internal imports.
- Existing FROZEN-adjacent tests stay green: `tests/property/test_llm_egress_completeness.py` **byte-unchanged**; the
  two updated verifiers (L1-b) and the updated success-emit test (L1-a) reflect the new contracts only (no weakening).
- `ruff format --check .` (REPO-WIDE) clean; `ruff check .` clean; `uv run pyright` clean.
- `uv run python scripts/gen_adr_index.py --check` passes with the 0104 row present (L1-a regenerates the index).
- `quality` + `security` CI checks green before merge. `gh pr checks <N> --watch` before any merge.
- ADR 0104 `human_cosign: n/a` stays as written; **no** person-affecting cosign is added (the diff never touches
  `resolution/**`). Status flips PROPOSED → ACCEPTED at merge per the main-loop convention.

---

## 6. Invariants the checker MUST reproduce

- **INV-CHOKE** — no `src/worldmonitor` module outside `llm/` imports `litellm`/`openai`/`anthropic` (AST); adding such
  an import turns `tests/test_llm_egress_chokepoint.py` RED.
- **INV-FAILCLOSED** — external mode + `llm_egress_log_enabled=False` ⇒ `LLMGatewayError` raised, `litellm.completion`
  never called, no `EgressRecord` emitted. LOCAL unaffected.
- **INV-USAGE** — a successful external call emits a post-call `EgressRecord` whose `usage` carries the response token
  counts, and `emit` serializes those counts; the pre-call completeness record (usage `None`, before the provider call)
  is preserved (INV-S2-EGRESS intact).
- **INV-CALLER** — `/v1` egress audit's `caller_tag` == the authenticated `principal.subject` (fallback only on empty).
- **INV-ROLE** — `/v1` requires `worldmonitor:llm`; a valid token lacking it → 403 with the gateway never called; a
  tokenless request → 401 (unauthenticated ≠ forbidden).
- **INV-S2/S3a preserved** — INV-S2-EGRESS (write-before-call ordering), INV-S2-DEFAULT (LOCAL default, no egress),
  INV-S2-LABEL (per-mode labels), INV-S3a-GATEWAY/-AUTH/-NOSTREAM all remain green.

---

## 7. FROZEN (byte-unchanged — the checker verifies `git diff` touches none of these)

- The entire **`resolution/**`** (clustering, thresholds, `signoff.py`, `guard.py`/sensitivity, `gold.py`, `eval.py`,
  `pipeline.py`, `statements.py`, `projector.py`, `merge`/referents/canonical), **`graph/**`** (incl. `graph/writer.py`),
  and **`db/models.py`** + **`db/migrations/**`**. L1 has no reason to touch the person-affecting write path.
- **`llm/modes.py`** — the ADR-0091 locked three-mode `REGISTRY`, including each mode's `data_left_perimeter`,
  `confidentiality`, and `badge`. L1 only **reads** `mode_record.data_left_perimeter`; it changes no per-mode semantics.
- **`llm/claude_shim.py`** — the CLAUDE_HEADLESS shim (it legitimately imports litellm inside `llm/`).
- **`mcp/auth.py`** — its role→scope map + `ZITADEL_ROLE_CLAIM` + `worldmonitor:graph-read` semantics. L1-b defines its
  own role helper in `authz/roles.py`; it does not refactor the MCP module.
- **`api/middleware.py`** — dual-path bearer/session auth dispatch (the role gate is a route dependency, not middleware).
- **`authz/oidc.py`** — `Principal` / `ZitadelTokenVerifier` (L1-b adds `authz/roles.py`, a new file, and does not edit
  `oidc.py`).
- **`tests/property/test_llm_egress_completeness.py`** — stays green **unchanged** (see §2.5).
- **`get_principal`** in `deps.py` — unchanged; `require_llm_role` is additive and composes on top of it.

---

## 8. OUT OF SCOPE (do NOT build here)

- **L2 — enforced-classification egress gate.** DROPPED per F1 = advisory operator-discretion (ADR 0094 D2). L1
  hardens the *advisory* model; it does not classify or block payloads by data classification.
- **F2 — durable/append-only egress audit** (content-fingerprint + entity-manifest, tamper-evident store off stdlib
  logging). Backlog.
- **F4 — the Phase-6 self-improvement gate.** Backlog (Phase 6).
- **L3 — make LOCAL real** (working Ollama-backed confidential default). Backlog.
- **L4 — unblock the Hermes run** (host-operational S4/S5). Backlog, blocked on the user's deploy-and-verify.
- Any change to ER/thresholds/merge/guard/gold/scores/erasure/statements/migrations, streaming SSE (`/v1` `stream:true`
  stays a 400 per INV-S3a-NOSTREAM), or the ADR-0091 three-mode registry semantics.
