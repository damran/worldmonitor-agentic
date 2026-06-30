# 0091 — Phase-3 S2: the LiteLLM gateway + three-mode confidential selector

- **Status:** Accepted (2026-06-30)
- **Date:** 2026-06-30
- **Gate:** Phase-3 slice **S2** (the service-side LLM-egress choke point). Companion spec:
  `docs/reviews/GATE_S2_LLM_GATEWAY_SPEC.md`. Umbrella: ADR 0089 (D3).
- **Milestone:** Phase 3 (`docs/40_ROADMAP.md:71`). Independent of S1; prerequisite for S5 (operator
  console hosts the selector). S3/S4 may use the gateway but do not block on it.
- **human_fork:** false. The selector contract (3 modes, always-visible confidentiality label, default
  Local) is **user-finalized** (2026-06-30) and person/sovereignty-relevant → treated as locked input,
  not an open fork. The transport choice (LiteLLM) + module layout are **reversible** → default +
  reversal-cost + revisit-trigger recorded below (no human stop).
- **Realizes:** ADR 0089 **D3** ("LLM = hybrid via our LiteLLM gateway, operator-selectable with visible
  confidentiality"). Honours the data-sovereignty principle (ingestion is pull-only; the only sanctioned
  outbound paths are opt-in Telegram + the optional external LLM — this gateway *is* that LLM path and
  its audit control).

## Context

`src/worldmonitor/llm/` exists but is empty. CLAUDE.md locks **LiteLLM for service-side LLM use**
(Hermes' own agent-side model is Hermes' concern, `hermes model`). Today no service-side code calls an
LLM; S2 builds the choke point **before** any caller exists, so the egress-audit invariant is true from
the first call rather than retrofitted.

The data-sovereignty principle (`data-sovereignty-ingestion-pull-only`) states our entity data **never**
leaves the perimeter except via two opt-in paths. An LLM completion that ships entity text to Anthropic
or OpenRouter is exactly such a perimeter crossing. Therefore the gateway is not merely a convenience
wrapper — it is the **single auditable egress point** for service-side model use, and every call through
it must leave a sovereignty audit record naming the mode, the confidentiality status, and whether data
left the perimeter.

The operator must never be surprised about where their data goes. ADR 0089 D3 fixed the user's decision:
exactly **three** modes, each **permanently labeled** with its confidentiality status at selection time,
**defaulting to Local (no egress)**. External modes are off by default and opt-in.

**LiteLLM API surface (confirmed at spec time so the builder does not re-investigate — the S1 lesson):**
- `litellm.completion(model, messages, api_base=None, api_key=None, **kwargs)` (sync) /
  `litellm.acompletion(...)` (async) return an OpenAI-shaped `ModelResponse`:
  `.choices[0].message.content`, `.usage.{prompt_tokens,completion_tokens,total_tokens}`, `.model`.
- **Ollama (Local):** `model="ollama_chat/<name>"`, `api_base="http://localhost:11434"` — no key, no
  egress (loopback).
- **OpenRouter:** `model="openrouter/<name>"`, `api_key=<OpenRouter key>` (or `OPENROUTER_API_KEY`).
- **Claude headless (`claude -p`):** a LiteLLM **CustomLLM** — subclass `litellm.CustomLLM`, implement
  `completion(self, *args, **kwargs) -> litellm.ModelResponse` (and `acompletion`), register via
  `litellm.custom_provider_map = [{"provider": "<name>", "custom_handler": <instance>}]`, then call
  `litellm.completion(model="<name>/<label>", ...)`. The handler runs `claude -p` as an **argv-list
  subprocess** (never a shell string), with a timeout, and treats stdout as **untrusted** (CLAUDE.md
  hostile-data rule) before placing it into `model_response.choices[0].message.content`.

## Decision

**Build `src/worldmonitor/llm/` as the single OpenAI-compatible LLM-egress choke point: one gateway
method routes every service-side LLM call through `litellm`, attaches the active mode's confidentiality
label, and writes a per-call egress record before the provider is contacted. On top sits a three-mode
confidential selector (Local default + Claude-headless opt-in + OpenRouter opt-in), each mode
permanently labeled with its confidentiality status.** Concretely:

### 1. The gateway is the only egress path (reversible)
`src/worldmonitor/llm/gateway.py` exposes an `LLMGateway` with a single public completion entry
(`chat()` / `completion()`). It resolves the active mode from settings (or an explicit per-call
override), looks up the mode's litellm route + confidentiality label, **writes the egress record
first**, then calls `litellm.completion(...)`. There is **no other public surface** that reaches a
provider — every service-side LLM call in the codebase goes through this one method. Provider failures
are surfaced as a typed gateway error; the egress record is written regardless (attempt is auditable).

### 2. The three modes, each permanently labeled (locked — user-finalized)

| Mode (`LLMMode`) | LiteLLM route | Confidentiality | Data leaves perimeter | Default |
|---|---|---|---|---|
| `LOCAL` | `ollama_chat/<model>` @ `http://localhost:11434` | **Confidential — no egress** | No (loopback) | **DEFAULT (on)** |
| `CLAUDE_HEADLESS` | CustomLLM shim running `claude -p` | **External egress → Anthropic** | Yes | off |
| `OPENROUTER` | `openrouter/<model>` | **External egress → OpenRouter** | Yes | off |

`src/worldmonitor/llm/modes.py` holds the `LLMMode` enum and a registry mapping each mode → its litellm
model string + base_url + a `Confidentiality` value + a human-readable badge. **Construction-time
invariant:** a mode entry with no confidentiality label cannot be constructed/registered (the label is a
required, non-empty field of the registry record) — so the selector can never present a mode whose
confidentiality status is unknown. `CLAUDE_HEADLESS` carries a documented **ToS-gray / brittle** caveat:
a consumer Claude subscription used programmatically may break or violate terms; the clean external route
is the Anthropic API, but the user explicitly wants the `claude -p` route available, off by default.

### 3. Per-call egress logging — the sovereignty audit trail (reversible)
`src/worldmonitor/llm/egress_log.py` defines a structured per-call record: mode, confidentiality status,
target host, `data_left_perimeter: bool`, model string, timestamp, token usage (when the response
carries it), and the caller/purpose tag. The gateway emits one record **before** the provider call (so a
crashing/timing-out external call is still audited) and enriches it with token usage on success. The
record is emitted via the standard `logging` facility (structured `extra=...`, mirroring
`sandbox/container_runner.py`) — **not** a new datastore (single-tenant, ADR 0042; no new store, per the
Phase-3 consequences). A settings toggle can disable emission, but **never** the write-before-call
ordering: disabling the toggle is out of the audited path only by operator choice, and the gateway has
no bypass that skips both the label and the record.

### 4. Selector defaults to Local; settings mirror the `mcp_*` pattern (locked default + reversible plumbing)
`Settings` (settings.py) gains an `--- LLM gateway (Phase-3 S2, ADR 0091) ---` block mirroring the
existing `mcp_*` field conventions (plain fields, `Field(...)` validation, `SecretStr` for secrets):
- `llm_mode: Literal["local", "claude_headless", "openrouter"] = "local"` — **the selector default is
  Local**; with no operator override the active mode is `LOCAL` (confidential / no egress).
- `llm_ollama_base_url: str = "http://localhost:11434"`, `llm_ollama_model: str = <default local model>`.
- `llm_openrouter_api_key: SecretStr = SecretStr("")`, `llm_openrouter_model: str = ""`.
- `llm_claude_binary: str = "claude"`, `llm_claude_model_label: str = <label>`,
  `llm_claude_timeout_seconds` (subprocess deadline).
- `llm_egress_log_enabled: bool = True`.

The runtime selector (the operator flipping modes live) is wired by **S5** (the operator console);
S2 ships the gateway + registry + settings default + the per-call override parameter the console will
drive. S2 does **not** build a UI.

## Three S2 invariants (the contract — full assertions in the gate spec)
- **INV-S2-EGRESS — egress-logging completeness.** No LLM call can reach a provider without the gateway
  first writing an egress record. The gateway is the only public egress surface; there is no bypass.
- **INV-S2-DEFAULT — selector default is Local.** With no operator override, the active mode is `LOCAL`
  (confidential / no egress).
- **INV-S2-LABEL — confidentiality label always present.** Every registered mode exposes a non-empty
  confidentiality status + badge at selection time; a mode without one cannot be constructed/registered.

## Alternatives considered
- **Plain `httpx` to each provider instead of LiteLLM.** Rejected — CLAUDE.md locks LiteLLM for
  service-side LLM use; LiteLLM gives one OpenAI-shaped call site + the `ollama_chat`/`openrouter`
  prefixes + the CustomLLM hook for the `claude -p` shim for free. (Reversible — see below.)
- **A LiteLLM **proxy server** (separate process) rather than the SDK.** Rejected for S2 — a proxy adds
  a deployment surface and moves the egress-audit point out of our process, weakening the single
  in-process choke point. The SDK keeps the audit record co-located with the caller. Revisitable if
  multiple services need shared routing.
- **Anthropic API (not `claude -p`) as the only external Anthropic route.** It is the *clean* route and
  is implicitly reachable via an OpenRouter-style key path, but the user explicitly wants the `claude -p`
  headless route available (off by default, caveated). Both can coexist; S2 ships the `claude -p` shim
  as decided.
- **Per-mode confidentiality as a doc note rather than a construct-time field.** Rejected — a label that
  can be omitted is a label that will eventually be missing on the surface the operator reads. Making it
  a required registry field makes INV-S2-LABEL structural, not aspirational.
- **No egress log / rely on provider-side logs.** Rejected — sovereignty is *our* control; the audit
  record must exist on our side, at the boundary, naming whether data left.

## Reversibility
- **Locked (not a fork):** the selector contract — 3 modes, always-visible confidentiality label,
  default Local. Person/sovereignty-relevant and user-finalized; changing it is a new user decision.
- **Reversible:** LiteLLM-as-transport + the module layout (`gateway`/`modes`/`egress_log`/`claude_shim`).
  - **Reversal cost:** low. The public surface is one gateway method returning an OpenAI-shaped result;
    swapping LiteLLM for direct `httpx` (or a LiteLLM proxy) changes only the gateway internals, not
    callers, the registry, or the egress record. Removing a provider is deleting a registry row.
  - **Revisit trigger:** (a) LiteLLM proves too heavy / a security concern surfaces in its provider
    tail → drop to direct `httpx` per provider; (b) multiple services need shared routing/budgeting →
    promote to a LiteLLM proxy; (c) Anthropic ships a supported headless/API path → drop the `claude -p`
    shim's ToS caveat (or replace the shim). No data-shape lock-in, no deletion, nothing public-facing.

## Consequences
- One new **auditable egress choke point** (the gateway) with operator-visible confidentiality, in place
  before any service-side caller exists.
- `litellm` is added as a runtime dependency (`pyproject.toml`). Its provider tail is broad but used
  only through three pinned routes; the gateway never enables arbitrary providers from caller input.
- New modules under `src/worldmonitor/llm/`; additive `Settings` fields (defaults keep Local/no-egress).
  No new datastore; the egress record uses the existing `logging` facility. Single-tenant (ADR 0042).
- **Not person-affecting** (service-side completion routing; no ER threshold, no individual-affecting
  score). External modes ship **off**; turning one on is an explicit, labeled operator opt-in.
- The runtime selector UI is **S5**; S3/S4 may consume the gateway. The `claude -p` shim is off by
  default and carries its ToS-gray caveat in code + docs.

## Invariant gate note
S2 touches an **egress / data-sovereignty path**, so per CLAUDE.md build-discipline it carries a
**strong primary test** covering all three invariants at the unit level, with **providers mocked** (CI
never contacts a real LLM, never spawns `claude`). The egress-completeness and label-presence invariants
are good `@given` property candidates (over generated modes / call sequences: a provider is never
reached without a prior egress record; every registered mode yields a non-empty confidentiality label).
Exact list in `GATE_S2_LLM_GATEWAY_SPEC.md`.

## References
- ADR 0089 (Phase-3 umbrella, **D3** the LLM-gateway decision), ADR 0090 (S1 sibling — structure
  mirrored here). CLAUDE.md ("LiteLLM for service-side LLM use"; hostile-data rule; build-discipline
  reversibility classification). `data-sovereignty-ingestion-pull-only` principle. ADR 0042 (single
  tenant). `docs/50_AGENT_LAYER.md`.
</content>
</invoke>
