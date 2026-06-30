# GATE S3a — OpenAI-compatible HTTP endpoint exposing the LLM gateway

> Phase-3 slice **S3a** (expose the in-process S2 gateway over HTTP for the remote Hermes container).
> ADRs: **0092** (this gate) under umbrella **0089** (D3), extending **0091** (the gateway) and reusing
> the **0090/0068** auth boundary. **You write code to this spec; do not relitigate the ADR decisions.**
> The Option-A choice (a thin FastAPI OpenAI shim wrapping `LLMGateway.chat()`, NOT a separate LiteLLM
> proxy) is **user-resolved and locked** — do not redesign it. The gateway is the **only** egress path —
> the route must never reach a provider/`litellm` directly. Facts you'd otherwise re-investigate are in
> §3 and §7 (the S1/S2 lesson); use them.

## 1. Goal (one sentence)
Add a thin, auth-gated, OpenAI-compatible `POST /v1/chat/completions` route to the **existing** FastAPI app
that wraps `LLMGateway.chat()` so the remote Hermes container routes **every** model call through our
in-process sovereignty choke point (the S2 egress audit + confidential selector), returning an
OpenAI-shaped chat-completion response — with **no** silent stream downgrade and **no** provider bypass.

## 2. Scope — exact files (also `.claude/gate.scope`)
**In scope (touch only these):**
- `src/worldmonitor/api/llm.py` — **NEW.** An `APIRouter` with `POST /v1/chat/completions`, gated by
  `Depends(get_principal)`. Pydantic request model (OpenAI-shaped); delegates **only** to the injected
  `LLMGateway` via `run_in_threadpool`; maps the `ModelResponse` → an OpenAI-shaped JSON body; explicit
  4xx on `stream: true`; typed `LLMGatewayError` → clean 5xx. **Does NOT import `litellm`.**
- `src/worldmonitor/api/deps.py` — **additive only.** Add `get_llm_gateway(request) -> LLMGateway`
  returning `request.app.state.llm_gateway` (mirror the existing `get_neo4j` at `deps.py:28`). Do not
  modify `get_principal` / `get_neo4j` / `get_db`.
- `src/worldmonitor/api/main.py` — **additive only.** In `create_app`: add an optional
  `llm_gateway: LLMGateway | None = None` parameter; build `LLMGateway(settings)` once when not injected;
  set `app.state.llm_gateway = llm_gateway`; `app.include_router(llm_router)`. Do not change existing
  wiring/middleware order.
- `tests/unit/test_api_llm_endpoint.py` — **NEW** (example tests; names in §4b).
- `tests/property/test_api_llm_gateway_delegation.py` — **NEW** (the primary property test; §4a).
- `docs/decisions/0092-llm-http-openai-endpoint.md`, this spec — these documents.

**Out of scope (do NOT touch):** the entire S2 `llm/` package (`gateway.py` / `modes.py` /
`egress_log.py` / `claude_shim.py` — consume `LLMGateway` as-is, do not modify it); `settings.py` (the
S2 `llm_*` fields already exist — S3a adds none); all S1/frozen files (`mcp/*`, `authz/oidc.py`,
`api/middleware.py`, `zitadel_provision.sh`); the Hermes compose service + `config.yaml` (that is
**S3b**); SSE streaming + the runtime operator mode-flip UI + the Jinja chat page (that is **S5**); any
active/write/enrich MCP tool (Phase 6). **No new datastore. No real network/LLM call in tests.**

## 3. Confirmed facts — the auth path + the gateway surface (do NOT re-investigate)

**`get_principal` accepts a Zitadel bearer — the service-principal path Hermes uses (cited):**
- `src/worldmonitor/api/middleware.py:82-106` — `AuthMiddleware._authorize`: when an `Authorization`
  header is present it dispatches to `_authenticate`, which parses `Bearer <jwt>`, validates it via the
  injected `TokenVerifier` (`ZitadelTokenVerifier`, JWKS/RS256), and on success sets
  `request.scope["state"]["principal"]` (`middleware.py:104-105`). A missing/malformed/invalid bearer →
  **401** (`middleware.py:93-102`). This is the same bearer Hermes authenticates with (ADR 0090).
- `src/worldmonitor/api/deps.py:37-48` — `get_principal(request)` reads
  `request.scope.get("state", {}).get("principal")` and raises **401** if it is not a `Principal`. A
  route declaring `Depends(get_principal)` therefore runs only after a valid bearer (or browser session).
- **Conclusion (builder: rely on this):** gating `/v1/chat/completions` with `Depends(get_principal)`
  gives the exact 401-without-token / 200-with-valid-bearer contract the graph REST API already has, and
  it is satisfied by Hermes' service-principal bearer. No new auth code is needed.

**The gateway surface (S2, ADR 0091 — consume as-is):**
- `worldmonitor.llm.gateway.LLMGateway(settings).chat(messages: list[dict[str, Any]], *, mode=None,
  caller_tag: str = "gateway") -> ModelResponse` — **synchronous** (calls `litellm.completion`). It
  writes the egress audit record **before** contacting the provider, resolves the active mode (Local
  default), and returns an OpenAI-shaped litellm `ModelResponse`:
  `.choices[0].message.content`, `.choices[0].message.role`, `.usage.{prompt_tokens,completion_tokens,
  total_tokens}`, `.model`, `.id` (when present).
- On any provider failure it raises `worldmonitor.llm.gateway.LLMGatewayError` (the raw litellm/provider
  exception is already swallowed inside the gateway — never re-wrap it to expose internals).
- **S3a passes `mode=None`** (let the server-side `Settings.llm_mode` selector decide — INV-S3a-GATEWAY /
  ADR 0092 §2). It must **not** translate the client request's `model` field into a gateway `mode` — that
  would let a client pick an external/egress backend by wire field. Use `caller_tag="hermes"` (or a
  per-route constant) so the egress audit attributes the call.

**The app.state DI + auth-test pattern to mirror (cited):**
- `src/worldmonitor/api/deps.py:28-34` — `get_neo4j` returns `request.app.state.neo4j_client`. Mirror it
  exactly for `get_llm_gateway`.
- `src/worldmonitor/api/main.py:70-120` — `create_app` injection pattern (`neo4j_client=None` →
  app.state). Add `llm_gateway=None` the same way; `app.state.llm_gateway = llm_gateway` near line 117.
- `tests/unit/test_api_graph.py:115-195` — the `_client(verifier, ...)` + `_FakeVerifier` + `_auth()`
  (`{"Authorization": "Bearer good"}`) + 401-without-token harness. Mirror it: build the app with
  `create_app(settings=Settings(environment="test"), verifier=_FakeVerifier(), llm_gateway=<spy>)`.

## 4. Primary test mandate (egress/sovereignty + auth boundary → strong primary test)
This gate sits on the egress/sovereignty boundary **and** the auth boundary, so the primary test is
**property-flavored**, backed by example tests for the named invariants. **The gateway is a spy/fake** —
never construct a real `LLMGateway` that would call `litellm`; never open a socket; never contact an LLM.

### 4a. PRIMARY — `tests/property/test_api_llm_gateway_delegation.py` (`@given`)
- **INV-S3a-GATEWAY (headline):** over generated valid OpenAI request bodies (varying `messages` content
  /roles, `model` strings, optional sampling params) sent **with a valid bearer**, assert with a **spy
  gateway** (records every `chat(...)` call) that **every** served `200` corresponds to **exactly one**
  `gateway.chat(...)` delegation, the spy received the request's `messages`, and **no** request was served
  without going through the spy. The route module **never** imports `litellm` (assert
  `"litellm" not in sys.modules`-style is too global — instead assert the route module has no `litellm`
  attribute / does not reference it; the load-bearing assertion is *every served call hits the spy*).
- **INV-S3a-AUTH (no-bypass-without-auth):** over the same generated bodies sent **without** a token (and
  with a malformed/invalid bearer via a rejecting `_FakeVerifier`), assert the response is **401** and the
  spy gateway was **never** called (the model is never reached for an unauthenticated request).
- **INV-S3a-NOSTREAM:** over generated bodies with `stream: true`, assert the response is an explicit 4xx
  and the spy gateway was **never** called (no silent non-streaming body, no wasted egress).
- Use `settings=deadline=None` style (`@settings(deadline=None)`) if TestClient/app-build per example
  makes timing flaky (builder-flake memory). Build the app once per test where possible.

### 4b. Example tests — `tests/unit/test_api_llm_endpoint.py`
- **INV-S3a-AUTH (mirror `test_api_graph.py:164`):** `POST /v1/chat/completions` with no header → **401**;
  with a valid bearer (`_FakeVerifier` + `_auth()`) → **200**; assert no gateway call on the 401.
- **INV-S3a-GATEWAY + OpenAI-shape:** a valid authenticated request delegates to the spy gateway exactly
  once with the posted `messages`, and the response body is OpenAI-shaped: `object == "chat.completion"`,
  `choices[0].message.{role,content}` carry the gateway `ModelResponse` content, `usage` carries the
  token counts, `id` + `model` + `created` present. (Spy returns a canned OpenAI-shaped fake response.)
- **INV-S3a-NOSTREAM:** `{"stream": true, ...}` → explicit 4xx (assert the exact status the route uses,
  e.g. 400 or 422) with a clear "streaming not supported" detail; gateway **not** called.
- **typed-error → clean 5xx:** a spy gateway whose `chat` raises `LLMGatewayError` → the route returns a
  clean **502/503** (assert the chosen status) with a **generic** detail; assert the body contains **no**
  provider-internal text / no stack trace.
- **request validation:** a malformed body (missing `messages`, wrong types) → **422** (pydantic), gateway
  **not** called.
- **no-secret / no-message leak:** with a captured log, assert no log line emitted by the **route** contains
  the message content or any secret (the gateway owns the single egress audit record; the route adds no
  second copy of message text).
- **run_in_threadpool:** assert the sync `gateway.chat` is invoked off the event loop (e.g. the spy records
  it ran, and the route handler is `async def`); a blocking spy must not deadlock the TestClient.
- **`model` field is informational:** a request with `model: "gpt-4o"` still delegates with `mode=None`
  (the spy asserts it was called with `mode=None` / no client-driven mode) — the client cannot select an
  egress backend by wire field.

## 5. Acceptance criteria (all must be green)
1. `tests/property/test_api_llm_gateway_delegation.py` passes: every served `/v1/chat/completions`
   response goes through the spy gateway; unauthenticated + `stream: true` requests never reach the gateway.
2. INV-S3a-GATEWAY, INV-S3a-AUTH, INV-S3a-NOSTREAM each have a passing named example test.
3. The route is the **only** new model path and reaches the model **only** via the injected `LLMGateway`
   (no `litellm` import in `api/llm.py`; asserted) — no provider bypass.
4. Unauthenticated request → **401** (mirrors `api/graph.py`); valid Zitadel bearer → **200**.
5. `stream: true` → explicit 4xx, never a silent non-streaming body (asserted, gateway not called).
6. `LLMGatewayError` → clean 502/503 with a generic detail, no provider internals / stack trace (asserted);
   malformed body → 422.
7. No secret / message-content logged by the route beyond the gateway's own audit (asserted).
8. `get_llm_gateway` mirrors `get_neo4j` (app.state); `create_app` injects an optional `llm_gateway=` +
   `include_router`s the llm router; the S2 `llm/` package and `settings.py` are **unchanged**.
9. The async route calls the **sync** `LLMGateway.chat` via `run_in_threadpool` (event loop not blocked).
10. Ruff + Pyright (strict on `src/`) clean; `ruff format --check .` clean repo-wide; CI `quality` +
    `security` green before merge.

## 6. Slice breakdown (1-3 independently-mergeable builder slices)
- **S3a-1 — the route + DI wiring + auth/no-stream contract.** `api/llm.py` (the OpenAI request/response
  models, `Depends(get_principal)`, delegate to the injected gateway via `run_in_threadpool`, explicit
  4xx on `stream: true`, typed-error→5xx mapping) + `get_llm_gateway` in `deps.py` + the `create_app`
  injection/`include_router` in `main.py`. Lands `tests/unit/test_api_llm_endpoint.py` (INV-S3a-AUTH,
  INV-S3a-NOSTREAM, OpenAI-shape, typed-error, validation, no-leak) with a **spy gateway**. Mergeable
  alone — this is the whole functional slice.
- **S3a-2 — the delegation/no-bypass property suite.** `tests/property/test_api_llm_gateway_delegation.py`
  (INV-S3a-GATEWAY headline + the auth-no-bypass + no-stream-no-call properties) over generated bodies.
  Mergeable after S3a-1. (May fold into S3a-1 if small; keep <= 2 slices — this gate is intentionally tiny.)

Each slice ships its own tests and is green on its own.

## 7. Notes for the builder
- **The gateway is the only egress path.** `api/llm.py` must not `import litellm` and must have no branch
  that reaches a provider directly. The single model call is `gateway.chat(messages, caller_tag="hermes")`
  with `mode=None`. (INV-S3a-GATEWAY.)
- **Do not modify the S2 gateway** to fit the route — consume `LLMGateway.chat` / `LLMGatewayError` as-is.
  If the `ModelResponse` shape needs accessing, use `getattr`/`.choices[0].message` defensively (it is the
  partially-typed litellm response; the gateway already returns it). Treat model output as hostile data
  (CLAUDE.md): the route copies content into the OpenAI body but never `eval`s it or shells out.
- **`run_in_threadpool`:** `from fastapi.concurrency import run_in_threadpool`; the handler is `async def`
  and does `resp = await run_in_threadpool(gateway.chat, messages, caller_tag="hermes")`. `chat` is sync.
- **OpenAI response shape to emit** (minimal, enough for an OpenAI client): `{"id": <resp.id or a uuid>,
  "object": "chat.completion", "created": <epoch>, "model": <resp.model or request.model>, "choices":
  [{"index": 0, "message": {"role": "assistant", "content": <resp.choices[0].message.content>},
  "finish_reason": "stop"}], "usage": {"prompt_tokens": ..., "completion_tokens": ..., "total_tokens":
  ...}}`. Pull usage from `resp.usage` defensively (may be absent → omit or zero).
- **OpenAI request model** (pydantic): `model: str`, `messages: list[Message]` (each `{role: str,
  content: str}`, min length 1), `stream: bool = False`, plus optional pass-through sampling params
  (`temperature`, `max_tokens`, `top_p`) accepted but — in S3a — **not** forwarded as gateway mode/route
  selectors (the gateway resolves the backend). Validate; reject malformed → 422.
- **`stream: true` 4xx (INV-S3a-NOSTREAM):** return a 400 (or 422) with a clear detail like
  `"streaming is not supported on this endpoint (deferred to the operator console)"`. Never answer a
  streaming request with a single non-streaming body.
- **The `model` field is informational** (ADR 0092 §2): document it in the route docstring + OpenAPI
  description so an operator knows the server-side selector decides the real backend. The runtime
  mode-flip + SSE are S5; the Hermes compose/config wiring is S3b — out of scope here.
- Mirror the test harness in `tests/unit/test_api_graph.py:115-195` for app construction + auth.
</content>
