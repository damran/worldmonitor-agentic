# GATE S2 — LiteLLM gateway + three-mode confidential selector

> Phase-3 slice S2 (the service-side LLM-egress choke point). ADRs: **0091** (this gate) under umbrella
> **0089** (D3). **You write code to this spec; do not relitigate the ADR decisions.** The selector
> contract (3 modes, always-visible confidentiality label, default Local) is **user-finalized and
> locked** — do not redesign it. LiteLLM-as-transport is locked by CLAUDE.md. The LiteLLM API surface is
> written into §7 so you do NOT re-investigate it (the S1 lesson).

## 1. Goal (one sentence)
Make `src/worldmonitor/llm/` the **single OpenAI-compatible LLM-egress choke point** for all
service-side LLM use — one gateway method through `litellm`, a per-call egress audit record written
before any provider is contacted, and a three-mode confidential selector (Local default, Claude-headless
opt-in, OpenRouter opt-in) where every mode is permanently labeled with its confidentiality status.

## 2. Scope — exact files (also `.claude/gate.scope`)
**In scope (touch only these):**
- `src/worldmonitor/llm/gateway.py` — **NEW.** `LLMGateway`; sole public `chat()`/`completion()` entry;
  resolves the active mode, **writes the egress record first**, then calls `litellm.completion(...)`;
  attaches the confidentiality label; the ONLY egress path. Typed gateway error on provider failure.
- `src/worldmonitor/llm/modes.py` — **NEW.** `LLMMode` enum + `Confidentiality` + a registry record
  carrying litellm model string, base_url, **non-empty** confidentiality status + human-readable badge,
  and `data_left_perimeter`. Construction rejects a record without a confidentiality label.
- `src/worldmonitor/llm/egress_log.py` — **NEW.** The structured per-call egress record dataclass +
  the emit function (standard `logging`, structured `extra=`, mirroring `sandbox/container_runner.py`).
  No new datastore.
- `src/worldmonitor/llm/claude_shim.py` — **NEW.** The LiteLLM `CustomLLM` adapter running `claude -p`
  via an **argv-list subprocess (never shell)**, with a timeout, stdout treated as **untrusted**.
  Off by default (only constructed/registered when `llm_mode == "claude_headless"` or explicitly selected).
- `src/worldmonitor/llm/__init__.py` — export the public surface (`LLMGateway`, `LLMMode`); keep thin.
- `src/worldmonitor/settings.py` — **only** the `--- LLM gateway (Phase-3 S2, ADR 0091) ---` field
  block (additive; defaults keep Local / no-egress / egress-log on). Mirror the `mcp_*` style.
- `pyproject.toml` — add `litellm` to `[project] dependencies` (pin `>=` a verified-at-build version).
- `tests/unit/test_llm_gateway.py`, `tests/unit/test_llm_modes.py`,
  `tests/property/test_llm_egress_completeness.py` — **NEW** (names below).
- `docs/decisions/0091-llm-gateway-confidential-selector.md`, this spec — these documents.

**Out of scope (do NOT touch):** every S1/frozen file (`src/worldmonitor/mcp/*`,
`src/worldmonitor/authz/oidc.py`, `scripts/dev/zitadel_provision.sh`, the S1 test/doc files); any Hermes
compose/deploy file (S3); the operator console / SSE / Jinja chat page + the runtime mode-flip UI (S5);
`§4b` fine-tuning and `§4c` param/rule auto-tuning (Phase 6); any active/write/enrich MCP tool. **No new
datastore. No real network/LLM call or `claude` spawn in tests.**

## 3. Locked invariants (the S2 contract)
- **INV-S2-EGRESS — egress-logging completeness.** No LLM call can reach a provider without the gateway
  **first** writing an egress record. The gateway's completion method is the only public egress surface;
  there is no path to `litellm.completion(...)` that skips the record. The record is written **before**
  the provider call (so a failing/timing-out external call is still audited) and is enriched with token
  usage on success.
- **INV-S2-DEFAULT — selector default is Local.** With no operator override (no `llm_mode` env, no
  per-call mode argument), the gateway's active mode is `LOCAL` (confidential / no egress, Ollama
  loopback). The default route never sends data off-perimeter.
- **INV-S2-LABEL — confidentiality label always present.** Every registered mode exposes a **non-empty**
  confidentiality status **and** badge at selection time; a registry record without a confidentiality
  label **cannot be constructed** (raises at construction). The selector can never surface a mode whose
  confidentiality status is unknown.
- **Carried invariants (must not regress):** data-sovereignty — the only off-perimeter LLM path is this
  gateway, and crossing it is always audited + labeled; external modes ship **off**. Hostile-data — the
  `claude -p` shim uses an argv list (never a shell), a timeout, and treats stdout as untrusted. No
  secret leakage — `llm_openrouter_api_key` is a `SecretStr`, never logged/echoed (the egress record
  records the target host + mode, **never** the key or message content).

## 4. Primary test mandate (egress/sovereignty boundary → strong primary test)
This gate touches an egress/sovereignty path, so the primary test is **property-flavored**, backed by
example tests for the named invariants. **All providers are mocked** — monkeypatch `litellm.completion`
(and `litellm.acompletion` if async) to a spy; never spawn `claude`; never open a socket.

### 4a. PRIMARY — `tests/property/test_llm_egress_completeness.py` (`@given`)
- **INV-S2-EGRESS (headline):** over generated call sequences across all three modes (and explicit
  per-call overrides), assert with a **spy `litellm.completion`** + a **captured egress log** that for
  **every** call the egress record is emitted **before** the provider spy is invoked, and the spy is
  **never** reached without a preceding record. (Ordering: assert the record's timestamp/sequence
  precedes the spy call, e.g. via a shared recorder that appends `("log", ...)` then `("provider", ...)`
  and assert `"log"` index < `"provider"` index for every call.) Even when the provider spy raises, a
  record exists (audited attempt).
- **INV-S2-LABEL:** over **every** `LLMMode` value, the resolved registry record has a non-empty
  confidentiality status and a non-empty badge; and constructing a registry record with an empty/missing
  confidentiality label **raises**.
- **INV-S2-DEFAULT (no-egress property):** for the `LOCAL` mode the egress record has
  `data_left_perimeter is False` and the target host is loopback; for `CLAUDE_HEADLESS`/`OPENROUTER` it
  is `True`. (metamorphic: confidential mode in ⇒ no-egress flag out.)
- Use `deadline=None` if registry/monkeypatch setup makes per-example timing flaky (builder-flake memory).

### 4b. Example tests — `tests/unit/test_llm_gateway.py`
- **INV-S2-DEFAULT:** an `LLMGateway` built from default `Settings` (no `llm_mode` set) resolves
  `LOCAL`; the litellm call it makes uses `model="ollama_chat/..."` + the loopback `api_base`; the
  egress record says no egress.
- **INV-S2-EGRESS:** the gateway has no public method reaching `litellm.completion` other than
  `chat()`/`completion()`; a successful call writes exactly one record (enriched with token usage from
  the mocked `ModelResponse`); a provider exception still leaves a record and surfaces a typed gateway
  error (not a raw litellm exception leaking provider internals).
- **per-call override:** passing an explicit mode argument routes to that mode's litellm model string +
  records that mode's confidentiality/egress flag (the hook S5 will drive); absent ⇒ settings default.
- **no-secret-leak:** with `llm_openrouter_api_key` set, neither the egress record nor any log line
  contains the key bytes or the message content.
- **claude shim (mocked subprocess):** monkeypatch the subprocess runner — assert the shim is invoked
  with an **argv list** (not a shell string), with a timeout, and that its stdout is passed through as
  untrusted content (no `eval`, no shell). The shim is only registered when its mode is selected.

### 4c. Example tests — `tests/unit/test_llm_modes.py`
- **INV-S2-LABEL:** the registry has exactly the three modes `{LOCAL, CLAUDE_HEADLESS, OPENROUTER}`;
  each exposes a non-empty confidentiality status + badge; constructing a record with an empty
  confidentiality label raises; `LOCAL.data_left_perimeter is False` and the two externals are `True`.
- the `CLAUDE_HEADLESS` badge/record carries the documented ToS-gray / brittle caveat string.

## 5. Acceptance criteria (all must be green)
1. `tests/property/test_llm_egress_completeness.py` passes: no provider call without a prior egress
   record over all generated sequences; every mode yields a non-empty confidentiality label; the
   no-egress flag matches the mode.
2. INV-S2-EGRESS, INV-S2-DEFAULT, INV-S2-LABEL each have a passing named example test.
3. The gateway is the **only** public surface that reaches `litellm.completion` (no bypass) — asserted.
4. Default (no override) routes to `LOCAL`/Ollama loopback with `data_left_perimeter is False`.
5. The `claude -p` shim uses an argv list + timeout + untrusted-stdout handling; it is **off by default**
   and only registered when its mode is selected. No test spawns `claude` or opens a socket.
6. `llm_openrouter_api_key` is a `SecretStr`; no key/message bytes appear in any log/record (asserted).
7. `litellm` added to `pyproject.toml` `[project] dependencies` (verified version).
8. Ruff + Pyright(strict on `src/`) clean; `ruff format --check .` clean repo-wide; CI `quality` +
   `security` green before merge.

## 6. Slice breakdown (1–3 independently-mergeable builder slices)
- **S2a — modes + egress record (no provider).** `modes.py` (`LLMMode`, `Confidentiality`, registry with
  the construct-time label invariant) + `egress_log.py` (record dataclass + emit). Lands `test_llm_modes.py`
  (INV-S2-LABEL) and the label/no-egress-flag property. No `litellm` call yet → no `litellm` dep needed
  to merge this slice if it imports nothing from `litellm`; otherwise add the dep here. Mergeable alone.
- **S2b — the gateway choke point.** `gateway.py` routing every call through `litellm.completion` with
  the write-record-first ordering + typed error; the `Settings` LLM block (default Local); `pyproject`
  `litellm` dep. Lands the PRIMARY egress-completeness property + `test_llm_gateway.py` (INV-S2-EGRESS,
  INV-S2-DEFAULT, override, no-secret-leak). Providers mocked. Mergeable after S2a.
- **S2c — the `claude -p` CustomLLM shim.** `claude_shim.py` (argv-list subprocess, timeout, untrusted
  stdout) + its registration wired so `CLAUDE_HEADLESS` resolves to it; the shim unit test (mocked
  subprocess). Off by default. Mergeable after S2b. (May fold into S2b if small; keep ≤ 3 slices.)

Each slice ships its own tests and is green on its own.

## 7. Notes for the builder — the LiteLLM API surface (confirmed; do not re-investigate)
- `litellm.completion(model, messages, api_base=None, api_key=None, **kwargs) -> ModelResponse`
  (sync); `litellm.acompletion(...)` is the async twin. `ModelResponse` is OpenAI-shaped:
  `.choices[0].message.content`, `.usage.{prompt_tokens,completion_tokens,total_tokens}`, `.model`.
- **Local (Ollama chat):** `model="ollama_chat/<name>"`, `api_base="http://localhost:11434"`. No key,
  loopback, no egress. This is the default.
- **OpenRouter:** `model="openrouter/<name>"`, `api_key=<OpenRouter key>` (or env `OPENROUTER_API_KEY`).
- **Claude headless (CustomLLM):** subclass `litellm.CustomLLM`; implement
  `def completion(self, *args, **kwargs) -> litellm.ModelResponse` (and `async def acompletion(...)` if
  async is used). Register via `litellm.custom_provider_map = [{"provider": "<name>", "custom_handler":
  <instance>}]`, then call `litellm.completion(model="<name>/<label>", ...)`. The handler builds the
  `ModelResponse` (set `model_response.choices[0].message.content = <subprocess stdout>`). Run `claude -p`
  as `subprocess.run([binary, "-p", ...], capture_output=True, timeout=..., text=True)` — **argv list,
  never `shell=True`, never string interpolation of untrusted data**; treat stdout as untrusted content
  (no `eval`, no further shell). Confirm the exact `claude -p` flags + the `CustomLLM` method signature
  against the installed `litellm` version at build time; keep the registration in one place.
- Mirror `settings.py` `mcp_*` conventions for the new fields (plain field + inline comment; `SecretStr`
  for the OpenRouter key; `Field(..., gt=0)` for the timeout). Default `llm_mode="local"`.
- Treat all model output as hostile data (CLAUDE.md). The egress record names mode/host/flags/usage —
  **never** the API key or the message content.
</content>
