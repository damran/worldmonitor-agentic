# 0092 — Phase-3 S3a: an OpenAI-compatible HTTP endpoint exposing the LLM gateway

- **Status:** Accepted (2026-06-30)
- **Date:** 2026-06-30
- **Gate:** Phase-3 slice **S3a** (expose the S2 gateway over HTTP for the remote Hermes container).
  Companion spec: `docs/reviews/GATE_S3A_LLM_HTTP_ENDPOINT_SPEC.md`. Umbrella: ADR 0089 (D3).
- **Milestone:** Phase 3 (`docs/40_ROADMAP.md:71`). Depends on S2 (ADR 0091, merged master `1530030`)
  and reuses the S1/REST auth boundary (ADR 0090 / 0068). Prerequisite for **S3b** (the Hermes compose
  service + `config.yaml` that points its model at this endpoint). Independent of S4/S5.
- **human_fork:** false. The architectural fork — *how does a separate-container Hermes route its model
  calls through our in-process sovereignty choke point* — was **resolved with the user this session
  (2026-06-30)**: a thin OpenAI-compatible route in our existing FastAPI app that wraps
  `LLMGateway.chat()`. This **extends** ADR 0091 (it does not reverse 0091's rejection of a separate
  LiteLLM proxy). Reversible; reversal cost + revisit trigger recorded below.
- **Realizes:** ADR 0089 **D3** (the LLM gateway is the sovereignty choke point) for the **remote-Hermes
  topology** fixed by ADR 0089 D2 / ADR 0090 (Hermes is a separate container authenticating with a
  Zitadel bearer).

## Context

S2 (ADR 0091) built `src/worldmonitor/llm/gateway.py::LLMGateway` as an **in-process Python class** — the
single service-side LLM-egress choke point. Every call through `LLMGateway.chat(messages, *, mode=None,
caller_tag=...)` writes a per-call egress audit record **before** the provider is contacted (INV-S2-EGRESS),
resolves the active mode from the three-mode confidential selector (Local default, INV-S2-DEFAULT), and
attaches a non-empty confidentiality label (INV-S2-LABEL). It returns an OpenAI-shaped litellm
`ModelResponse` and raises `LLMGatewayError` on provider failure (the raw provider exception never escapes).

The problem this slice solves: **Hermes runs as a separate container** (ADR 0089 D2 / ADR 0090 —
Hermes is the NousResearch agent runtime in its own container, connecting to us over HTTP+Zitadel-bearer).
A separate process **cannot import the in-process `LLMGateway` class**. If Hermes were instead pointed at
Ollama / OpenRouter / Anthropic **directly**, every Hermes model call would bypass the gateway — no egress
audit record, no confidential-selector enforcement, no sovereignty control at the boundary. That is exactly
the perimeter crossing ADR 0091's context says must always be audited and labeled. So the gateway must be
reachable **over the network**, while remaining the **only** egress path, with the audit + selector + the
three S2 invariants still governing every Hermes call.

The auth boundary already exists and is already designed for Hermes to consume. The REST graph surface
(`api/graph.py`, ADR 0062) gates every route behind `get_principal`, which is satisfied by the Zitadel
**bearer** path in `AuthMiddleware._authenticate` (`api/middleware.py:82-106`) — the same
service-principal bearer Hermes uses for its MCP connection (ADR 0090). Reusing that gate means the LLM
HTTP endpoint inherits our single auth model with no new auth surface.

Hermes (like most agent runtimes) speaks the **OpenAI `/v1/chat/completions`** wire shape for its model
backend. Exposing the gateway behind that exact shape lets Hermes' stock OpenAI-compatible client point
at us with only a `base_url` + bearer change — no Hermes-side adapter, no fork of its model layer.

## Decision

**Add a thin OpenAI-compatible `POST /v1/chat/completions` route to the existing FastAPI app that wraps
`LLMGateway.chat()`. It is gated by the same `get_principal` Zitadel-bearer dependency as the graph REST
routes, it NEVER contacts a provider or `litellm` directly (the gateway is the sole egress path so the S2
egress audit + confidential selector still fire on every Hermes call), and it returns an OpenAI-shaped
chat-completion response.** This keeps the sovereignty choke point in-process and extends ADR 0091.
Concretely (Option A):

### 1. A thin OpenAI-compatible route — the gateway is still the only egress path
A new `src/worldmonitor/api/llm.py` exposes an `APIRouter` with `POST /v1/chat/completions`, gated by
`Depends(get_principal)` exactly like `api/graph.py`. It accepts an OpenAI-shaped request (a pydantic
model: `model: str`, `messages: list[{role, content}]`, optional `stream: bool = False`, optional sampling
params), and its handler **only** path to a model is `LLMGateway.chat(messages, caller_tag="hermes")`. The
route module **does not import `litellm`** and has no branch that reaches a provider directly — every
completion is delegated to the injected gateway, so INV-S2-EGRESS / the confidential selector / the
confidentiality label all still govern the call. The handler maps the gateway's `ModelResponse` into an
OpenAI-shaped JSON body (`id`, `object: "chat.completion"`, `created`, `model`, `choices[0].message`,
`usage`).

### 2. The OpenAI `model` field is informational — the server-side selector decides the backend
The request's `model` field is accepted for wire compatibility but is **not** authoritative: the gateway's
**settings-resolved mode** (Local default, ADR 0091 §4) decides the real backend. This is documented in
the route docstring + the OpenAPI description so an operator is never surprised. In S3a the confidential
selector is **server-side configuration** (`Settings.llm_mode`); making it **runtime-flippable** by the
operator is **S5** (the operator console). S3a does not read the client `model` to pick a provider, and it
does not let a client select an external/egress mode by wire field — that would route around the
server-side sovereignty choice.

### 3. No silent stream downgrade — `stream: true` is an explicit error in S3a
SSE streaming is **deferred to S5** (the operator console hosts the first SSE endpoint, per the Phase-3
memory). In S3a a request with `stream: true` returns an **explicit 4xx** naming streaming as unsupported;
the route **never** silently answers a streaming request with a single non-streaming body (which would
mislead Hermes / any OpenAI client into mis-parsing the response). This is INV-S3a-NOSTREAM.

### 4. Sync gateway called off the event loop; singleton gateway injected via app.state
`LLMGateway.chat` is **synchronous** (it calls `litellm.completion`). The async route invokes it via
`fastapi.concurrency.run_in_threadpool(gateway.chat, messages, caller_tag=...)` so the event loop is not
blocked. The gateway is constructed **once** at app build time and injected onto `app.state` with a
`get_llm_gateway(request)` dependency in `api/deps.py`, mirroring the existing `get_neo4j` app.state
pattern (ADR 0062 DI-for-testability) — so tests inject a fake/spy gateway and assert the route always
delegates to it. `create_app` (`api/main.py`) gains `app.state.llm_gateway = LLMGateway(settings)` (with
an optional `llm_gateway=` injection parameter for tests) and `include_router`s the new llm router.

### 5. Typed failures surface as a clean 5xx — no provider internals leak
A `LLMGatewayError` from the gateway (the typed wrapper that already hides the raw litellm/provider
exception) surfaces from the route as a clean **502/503** with a generic detail — never a raw stack trace
and never provider-internal text. The request body is validated by the pydantic request model (malformed →
422). The route logs no secret and no message content beyond what the gateway already audits (the gateway
owns the egress record; the route adds no second copy of message text).

## The four S3a invariants (the contract — full assertions in the gate spec)

- **INV-S3a-GATEWAY — no egress bypass.** Every `/v1/chat/completions` call goes through
  `LLMGateway.chat()` (so the S2 egress audit + confidential selector still fire). The route has **no**
  path that contacts a provider or `litellm` directly, and the route module does not import `litellm`. A
  gateway egress record is produced per served request.
- **INV-S3a-AUTH — auth-gated.** The endpoint is behind `get_principal`; an unauthenticated request gets
  **401** (mirroring `api/graph.py`'s auth contract and the bearer path Hermes uses).
- **INV-S3a-NOSTREAM — no silent stream downgrade.** `stream: true` returns an explicit error status,
  never a silent non-streaming body.
- **Carried (must not regress):** typed `LLMGatewayError` surfaces as a clean 5xx (no provider internals /
  stack trace leak); the OpenAI request body is validated (pydantic); no secret/message-content logging
  beyond the gateway's own audit; INV-S2-EGRESS/DEFAULT/LABEL are unweakened (the route adds no second
  egress path and cannot select an external mode by wire field).

## Alternatives considered

- **A standalone LiteLLM proxy server (separate process) in front of the providers.** Rejected — this is
  a **revisit of ADR 0091**, which already rejected a proxy for S2 because it moves the egress-audit point
  **out of our process** and adds a deployment surface, weakening the single in-process choke point. The
  FastAPI shim keeps the audit record co-located with the gateway, in-process, behind our existing auth.
  Promotion to a proxy remains the recorded revisit trigger if multi-service shared routing is later
  needed (ADR 0091 reversibility (b)).
- **Point Hermes directly at Ollama / OpenRouter / Anthropic (skip the gateway).** Rejected — every Hermes
  model call would bypass the gateway: **no egress audit, no confidential selector, no sovereignty control
  at the boundary**. This is precisely the unaudited perimeter crossing ADR 0089 D3 / ADR 0091 exist to
  prevent. The whole point of S3a is to keep Hermes' model traffic inside the choke point.
- **A bespoke (non-OpenAI) HTTP shape for the gateway.** Rejected — Hermes speaks OpenAI
  `/v1/chat/completions` natively; a bespoke shape forces a Hermes-side adapter and forks its model layer
  for no benefit. The OpenAI shape is a de-facto interop standard and costs us only a thin request/response
  mapping.
- **A separate FastAPI app / port for the LLM endpoint.** Rejected for S3a — a second app means a second
  auth wiring + a second ingress to secure. Mounting the route in the existing app reuses `AuthMiddleware`
  + `get_principal` verbatim (one auth model, one ingress). Splitting it out is reversible later if the LLM
  surface needs independent scaling.
- **Silently downgrade `stream: true` to a single non-streaming response.** Rejected — it misleads an
  OpenAI client that asked for SSE into mis-parsing the body. An explicit 4xx is honest; SSE arrives in S5.
- **Support SSE streaming now.** Deferred — the memory earmarks the first SSE endpoint for the S5 operator
  console; adding streaming here widens S3a past a thin shim and duplicates plumbing S5 will own.

## Reversibility

- **Resolved fork (decided with the user this session, not relitigated):** route Hermes' model calls
  through our in-process gateway via a FastAPI OpenAI shim (Option A), extending ADR 0091.
- **Reversible:** the shim itself (the `/v1` route + the app.state gateway singleton + the OpenAI mapping).
  - **Reversal cost:** **low.** Delete `api/llm.py`, drop the `include_router` line + the app.state gateway
    wiring + the `get_llm_gateway` dep. No data-shape lock-in, no deletion of stored data, no migration —
    the gateway, the egress record format, and `Settings` are untouched. Hermes is not yet wired to it
    until S3b, so reversal before S3b is free.
  - **Revisit trigger:** (a) multiple services need shared LLM routing/budgeting → promote to a LiteLLM
    proxy per ADR 0091 reversibility (b) (and re-open this ADR + 0091 together); (b) the operator console
    (S5) needs streaming → add an SSE route then (S3a's explicit-4xx contract is the placeholder); (c) the
    LLM surface needs independent scaling/ingress → split to its own app/port.
  - Reversible (low cost) per CLAUDE.md build-discipline classification → **no human fork** beyond the
    Option-A choice already made with the user.

## Consequences

- The remote Hermes container can route **every** model call through our in-process sovereignty choke
  point: the S2 egress audit + confidential selector + the three S2 invariants now govern Hermes' LLM
  traffic, not just internal callers. ADR 0089 D3 is realized for the remote topology.
- One new **authenticated** application route (`POST /v1/chat/completions`) on the **existing** ingress,
  behind the **existing** Zitadel-bearer gate — no new auth surface, no new port, no new datastore.
- New module `src/worldmonitor/api/llm.py`; additive changes to `api/deps.py` (a `get_llm_gateway` dep) and
  `api/main.py` (gateway on `app.state` + `include_router`). `Settings` and the `llm/` package are
  unchanged (S3a consumes the S2 gateway; it does not modify it).
- The confidential selector is **server-side config** in S3a (the client `model` field is informational);
  runtime operator mode-flip + SSE streaming are **S5**. The Hermes compose service + `config.yaml`
  pointing at this endpoint are **S3b**.
- **Not person-affecting** (service-side completion routing, read-shaped; no ER threshold, no
  individual-affecting score). External modes still ship **off** (the server-side default is Local /
  no-egress, per ADR 0091); a client cannot turn one on via the wire `model` field. Single-tenant
  (D1 / ADR 0042).

## Invariant gate note

S3a sits on the **egress / sovereignty boundary** (it is the network face of the LLM choke point) **and**
the **auth boundary**, so per CLAUDE.md build-discipline it carries a **strong primary test**: over
generated request bodies + auth states, **no served `/v1/chat/completions` call ever reaches a model
except through the injected `LLMGateway`** (asserted with a spy gateway), every served call yields exactly
one gateway delegation, an unauthenticated request **never** reaches the gateway (401 first), and
`stream: true` is **always** an explicit error (never a silent body). Backed by example tests for
auth-gating (mirroring `tests/unit/test_api_graph.py:164`), the typed-error→clean-5xx mapping, and
no-secret/no-message leakage. Providers are **mocked** (the gateway is a spy/fake; CI never contacts a real
LLM). Exact list in `GATE_S3A_LLM_HTTP_ENDPOINT_SPEC.md`.

## References

- ADR 0091 (the in-process `LLMGateway` this route wraps; its proxy-rejection that S3a extends rather than
  reverses; its three invariants INV-S2-EGRESS/DEFAULT/LABEL). ADR 0090 (S1 — remote Hermes over
  HTTP+Zitadel-bearer; the same bearer this route reuses). ADR 0089 (Phase-3 umbrella, D2 remote Hermes /
  D3 LLM gateway). ADR 0062 (the graph REST surface + `get_principal` + app.state DI pattern this route
  mirrors). ADR 0068 (`AuthMiddleware` bearer path). ADR 0042 (single tenant). Memory
  `phase-3-hermes-decisions` (S3 wiring: Hermes MCP→S1, model→this endpoint; SSE earmarked for S5).
  CLAUDE.md (data-sovereignty principle; build-discipline reversibility classification; hostile-data rule).
</content>
</invoke>
