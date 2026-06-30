"""Example tests — Phase-3 Gate S3a: OpenAI-compatible LLM HTTP endpoint (ADR 0092).

Covers spec §4b invariant-by-invariant:
  INV-S3a-AUTH     401 without token; no gateway call on 401; 200 with valid bearer.
  INV-S3a-GATEWAY  valid auth → delegates to spy exactly once with posted messages;
                   OpenAI-shaped response body (object, choices, usage, id, model, created);
                   mode=None regardless of request 'model' field (sovereignty).
  INV-S3a-NOSTREAM stream:true → explicit 4xx; clear detail; gateway not called.
  Typed error      LLMGatewayError → clean 502/503; generic detail; no provider internals.
  Validation       Missing 'messages' → 422; gateway not called.
  No-leak          Route emits no log line containing the request message content.
  run_in_threadpool Route handler is async def; sync spy does not deadlock TestClient.

BUILDER CONTRACT — names the implementation MUST match:
    worldmonitor.api.llm                                   # NEW
        router: APIRouter                                  # POST /v1/chat/completions
    worldmonitor.api.deps
        get_llm_gateway(request: Request) -> LLMGateway   # CONTRACT: get_llm_gateway
    worldmonitor.api.main
        create_app(..., llm_gateway: LLMGateway | None = None)  # additive param
    worldmonitor.llm.gateway
        LLMGatewayError(Exception)

RED TODAY:
    ModuleNotFoundError: No module named 'worldmonitor.api.llm'
    (and create_app() lacks llm_gateway= param; get_llm_gateway not yet in deps.py)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

# CONTRACT: triggers ModuleNotFoundError at collection until builder creates api/llm.py
from worldmonitor.api.llm import router as llm_router  # noqa: F401  # CONTRACT: router
from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.llm.gateway import LLMGatewayError
from worldmonitor.settings import Settings

# ── canned fake ModelResponse ──────────────────────────────────────────────────────────

_CANNED_CONTENT = "The answer is 42."
_CANNED_ROLE = "assistant"
_CANNED_MODEL = "fake-model-v1"
_CANNED_ID = "chatcmpl-fake-unit-001"
_CANNED_PROMPT_TOKENS = 10
_CANNED_COMPLETION_TOKENS = 20
_CANNED_TOTAL_TOKENS = 30


class _FakeMessage:
    def __init__(self, content: str = _CANNED_CONTENT, role: str = _CANNED_ROLE) -> None:
        self.content = content
        self.role = role


class _FakeChoice:
    def __init__(self) -> None:
        self.message = _FakeMessage()


class _FakeUsage:
    prompt_tokens: int = _CANNED_PROMPT_TOKENS
    completion_tokens: int = _CANNED_COMPLETION_TOKENS
    total_tokens: int = _CANNED_TOTAL_TOKENS


class _FakeModelResponse:
    """Duck-types enough of litellm ModelResponse for the route's mapping code."""

    def __init__(self) -> None:
        self.choices: list[_FakeChoice] = [_FakeChoice()]
        self.usage: _FakeUsage = _FakeUsage()
        self.model: str = _CANNED_MODEL
        self.id: str = _CANNED_ID


# ── spy gateways ───────────────────────────────────────────────────────────────────────


class _SpyGateway:
    """Records every chat() call; never calls litellm; returns canned ModelResponse."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.calls.clear()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        mode: Any = None,
        caller_tag: str = "gateway",
    ) -> _FakeModelResponse:
        self.calls.append({"messages": messages, "mode": mode, "caller_tag": caller_tag})
        return _FakeModelResponse()


class _ErrorSpyGateway:
    """Always raises LLMGatewayError from chat(); records the call."""

    # Sentinel that must NOT appear in the HTTP response body (provider internals must stay inside).
    INTERNAL_DETAIL = "InternalProviderError: SECRET_KEY_DETAILS_XYZ_INTERNAL"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reset(self) -> None:
        self.calls.clear()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        mode: Any = None,
        caller_tag: str = "gateway",
    ) -> Any:
        self.calls.append({"messages": messages, "mode": mode, "caller_tag": caller_tag})
        raise LLMGatewayError(f"provider call failed (mode='local'): {self.INTERNAL_DETAIL}")


# ── fake verifier ──────────────────────────────────────────────────────────────────────


class _FakeVerifier:
    """Accepts token 'good'; rejects everything else (mirrors test_api_graph exactly)."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


# ── helpers ────────────────────────────────────────────────────────────────────────────


def _client(gateway: object) -> TestClient:
    """Build an app + TestClient with an injected fake gateway and the accepting verifier.

    CONTRACT: create_app must accept llm_gateway= keyword parameter (builder adds this).
    """
    app = create_app(
        settings=Settings(environment="test"),
        verifier=_FakeVerifier(),
        llm_gateway=gateway,  # type: ignore[call-arg]  # CONTRACT: llm_gateway= on create_app
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    """Return the valid Authorization header (mirrors test_api_graph._auth())."""
    return {"Authorization": "Bearer good"}


def _valid_body(*, model: str = "gpt-4o") -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Hello, world!"}],
    }


# ── INV-S3a-AUTH: 401 without token; 200 with valid bearer ───────────────────────────


def test_post_chat_completions_without_token_returns_401() -> None:
    """INV-S3a-AUTH (mirror test_api_graph:164): no token → 401, gateway never called."""
    spy = _SpyGateway()
    c = _client(spy)
    resp = c.post("/v1/chat/completions", json=_valid_body())
    assert resp.status_code == 401, (
        f"unauthenticated request must return 401, got {resp.status_code}: {resp.text!r}"
    )
    # The gateway must not be reached at all for an unauthenticated request.
    assert len(spy.calls) == 0, (
        f"gateway must NOT be called for unauthenticated request, "
        f"got {len(spy.calls)} calls: {spy.calls!r}"
    )


def test_post_chat_completions_with_valid_bearer_returns_200() -> None:
    """INV-S3a-AUTH: valid bearer → 200."""
    spy = _SpyGateway()
    c = _client(spy)
    resp = c.post("/v1/chat/completions", json=_valid_body(), headers=_auth())
    assert resp.status_code == 200, (
        f"valid bearer must return 200, got {resp.status_code}: {resp.text!r}"
    )


# ── INV-S3a-GATEWAY + OpenAI shape ────────────────────────────────────────────────────


def test_valid_request_delegates_to_spy_exactly_once_with_posted_messages() -> None:
    """INV-S3a-GATEWAY: valid auth → exactly one gateway.chat() with the posted messages."""
    spy = _SpyGateway()
    c = _client(spy)
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "What is 2 + 2?"},
    ]
    body = {"model": "gpt-4o", "messages": messages}
    resp = c.post("/v1/chat/completions", json=body, headers=_auth())
    assert resp.status_code == 200

    assert len(spy.calls) == 1, (
        f"expected exactly 1 gateway.chat() call, got {len(spy.calls)}: {spy.calls!r}"
    )
    assert spy.calls[0]["messages"] == messages, (
        f"spy received {spy.calls[0]['messages']!r}, expected {messages!r}"
    )


def test_response_body_is_openai_shaped() -> None:
    """INV-S3a-GATEWAY: response is OpenAI-shaped (object, choices, usage, id, model, created)."""
    spy = _SpyGateway()
    c = _client(spy)
    resp = c.post("/v1/chat/completions", json=_valid_body(), headers=_auth())
    assert resp.status_code == 200

    body = resp.json()
    # 'object' field must equal "chat.completion"
    assert body.get("object") == "chat.completion", (
        f"'object' must be 'chat.completion', got {body.get('object')!r}"
    )
    # choices[0].message.{role,content} carry the gateway ModelResponse content
    assert "choices" in body and len(body["choices"]) >= 1, (
        f"response must have at least one choice, got: {body!r}"
    )
    msg = body["choices"][0]["message"]
    assert "role" in msg, f"choices[0].message must have 'role', got: {msg!r}"
    assert "content" in msg, f"choices[0].message must have 'content', got: {msg!r}"
    assert msg["role"] == _CANNED_ROLE, f"expected role={_CANNED_ROLE!r}, got {msg['role']!r}"
    assert msg["content"] == _CANNED_CONTENT, (
        f"expected content={_CANNED_CONTENT!r}, got {msg['content']!r}"
    )
    # usage carries token counts from the gateway ModelResponse
    assert "usage" in body, f"response must have 'usage', got: {body!r}"
    usage = body["usage"]
    assert usage.get("prompt_tokens") == _CANNED_PROMPT_TOKENS, (
        f"prompt_tokens: expected {_CANNED_PROMPT_TOKENS}, got {usage.get('prompt_tokens')!r}"
    )
    assert usage.get("completion_tokens") == _CANNED_COMPLETION_TOKENS, (
        f"completion_tokens: expected {_CANNED_COMPLETION_TOKENS},"
        f" got {usage.get('completion_tokens')!r}"
    )
    assert usage.get("total_tokens") == _CANNED_TOTAL_TOKENS, (
        f"total_tokens: expected {_CANNED_TOTAL_TOKENS}, got {usage.get('total_tokens')!r}"
    )
    # id, model, created must be present
    assert "id" in body, f"response must have 'id', got: {body!r}"
    assert body["id"], f"response 'id' must be non-empty, got {body['id']!r}"
    assert "model" in body, f"response must have 'model', got: {body!r}"
    assert "created" in body, f"response must have 'created', got: {body!r}"
    assert isinstance(body["created"], int), (
        f"'created' must be an integer epoch timestamp, got {body['created']!r}"
    )


# ── INV-S3a-NOSTREAM: stream:true → explicit 4xx, gateway not called ─────────────────


def test_stream_true_returns_explicit_4xx_gateway_not_called() -> None:
    """INV-S3a-NOSTREAM: stream:true → explicit 4xx + clear detail; gateway not called."""
    spy = _SpyGateway()
    c = _client(spy)
    body = {**_valid_body(), "stream": True}
    resp = c.post("/v1/chat/completions", json=body, headers=_auth())

    # Must be an explicit 4xx (spec says 400 or 422 with a clear detail).
    assert resp.status_code in (400, 422), (
        f"stream:true must return 400 or 422, got {resp.status_code}: {resp.text!r}"
    )
    # The response detail must mention streaming is unsupported.
    resp_text = resp.text.lower()
    assert "stream" in resp_text or "streaming" in resp_text, (
        f"error detail must mention streaming, got: {resp.text!r}"
    )
    # Gateway must not be called (no wasted egress).
    assert len(spy.calls) == 0, (
        f"gateway must NOT be called when stream:true is rejected, "
        f"got {len(spy.calls)} calls: {spy.calls!r}"
    )


# ── Typed error → clean 5xx, no provider internals ────────────────────────────────────


def test_llm_gateway_error_surfaces_as_clean_5xx_without_provider_internals() -> None:
    """LLMGatewayError → clean 502/503 with generic detail; no provider internals leak."""
    error_spy = _ErrorSpyGateway()
    c = _client(error_spy)
    resp = c.post("/v1/chat/completions", json=_valid_body(), headers=_auth())

    # Route must surface a clean 5xx — neither the raw exception nor a 500 dump.
    assert resp.status_code in (502, 503), (
        f"LLMGatewayError must yield 502 or 503, got {resp.status_code}: {resp.text!r}"
    )
    body_text = resp.text

    # Provider-internal text must NOT appear in the response body.
    assert _ErrorSpyGateway.INTERNAL_DETAIL not in body_text, (
        f"provider-internal error detail leaked into response: {body_text!r}"
    )
    assert "InternalProviderError" not in body_text, (
        f"provider exception class leaked into response: {body_text!r}"
    )
    assert "SECRET_KEY_DETAILS" not in body_text, (
        f"provider secret detail leaked into response: {body_text!r}"
    )
    # No Python stack trace in the response body.
    assert "Traceback" not in body_text, (
        f"'Traceback' found in 5xx response — stack trace must not be exposed: {body_text!r}"
    )
    # Response must have a non-empty 'detail' field (FastAPI HTTPException convention).
    resp_json = resp.json()
    assert "detail" in resp_json, f"5xx response must have a 'detail' field, got: {resp_json!r}"
    detail = str(resp_json["detail"])
    assert detail, "5xx 'detail' must be non-empty (generic error message for the client)"


# ── Request validation: missing 'messages' → 422, gateway not called ─────────────────


def test_malformed_body_missing_messages_returns_422_gateway_not_called() -> None:
    """Request validation: missing 'messages' → 422 (Pydantic); no gateway call."""
    spy = _SpyGateway()
    c = _client(spy)
    resp = c.post("/v1/chat/completions", json={"model": "gpt-4o"}, headers=_auth())
    assert resp.status_code == 422, (
        f"missing 'messages' must return 422 (Pydantic validation), "
        f"got {resp.status_code}: {resp.text!r}"
    )
    assert len(spy.calls) == 0, (
        f"gateway must NOT be called for an invalid request body, "
        f"got {len(spy.calls)} calls: {spy.calls!r}"
    )


def test_malformed_body_wrong_message_type_returns_422_gateway_not_called() -> None:
    """Validation: messages must be list of dicts — wrong type → 422; no gateway call."""
    spy = _SpyGateway()
    c = _client(spy)
    resp = c.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": "not a list"},
        headers=_auth(),
    )
    assert resp.status_code == 422, (
        f"wrong messages type must return 422, got {resp.status_code}: {resp.text!r}"
    )
    assert len(spy.calls) == 0, (
        f"gateway must NOT be called for a malformed request, "
        f"got {len(spy.calls)} calls: {spy.calls!r}"
    )


# ── No-leak: route does not log message content ────────────────────────────────────────


def test_route_does_not_log_message_content(caplog: pytest.LogCaptureFixture) -> None:
    """No-leak: the route must not emit any log line containing the message content.

    The gateway owns the single egress audit record; the route must not create a
    second copy of message text in its own log output.
    """
    spy = _SpyGateway()
    c = _client(spy)
    # A sentinel string unique enough that any log line containing it is a definite leak.
    secret_content = "SENSITIVE_MESSAGE_CONTENT_DO_NOT_LOG_XYZ_LEAK_SENTINEL"
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": secret_content}],
    }
    with caplog.at_level(logging.DEBUG, logger="worldmonitor.api.llm"):
        resp = c.post("/v1/chat/completions", json=body, headers=_auth())

    assert resp.status_code == 200
    for record in caplog.records:
        msg = record.getMessage()
        assert secret_content not in msg, (
            f"message content leaked into route log: "
            f"logger={record.name!r}, level={record.levelname!r}, message={msg!r}"
        )


# ── model field is informational: mode=None regardless of model string ─────────────────


def test_model_gpt4o_delegates_with_mode_none() -> None:
    """INV-S3a-GATEWAY + sovereignty: model='gpt-4o' still delegates with mode=None.

    The client cannot select the egress backend by setting the 'model' wire field.
    The server-side settings selector (LLMMode) decides the real backend (ADR 0092 §2).
    """
    spy = _SpyGateway()
    c = _client(spy)
    resp = c.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert len(spy.calls) == 1
    assert spy.calls[0]["mode"] is None, (
        f"mode must be None for model='gpt-4o', got {spy.calls[0]['mode']!r}; "
        "the client model field must NEVER select the egress backend"
    )


def test_model_matching_llmmode_value_still_delegates_mode_none() -> None:
    """Sovereignty: model='local' (an LLMMode enum value) must NOT become gateway mode.

    The most adversarial wire value is one that matches an LLMMode enum value.
    The spy must still be called with mode=None — not mode=LLMMode.LOCAL or 'local'.
    """
    spy = _SpyGateway()
    c = _client(spy)
    # 'local' is the string value of LLMMode.LOCAL — the most adversarial model string.
    resp = c.post(
        "/v1/chat/completions",
        json={"model": "local", "messages": [{"role": "user", "content": "hello"}]},
        headers=_auth(),
    )
    assert resp.status_code == 200, (
        f"model='local' should be accepted as informational, got {resp.status_code}: {resp.text!r}"
    )
    assert len(spy.calls) == 1
    assert spy.calls[0]["mode"] is None, (
        f"mode must be None for model='local' (LLMMode.LOCAL value), "
        f"got {spy.calls[0]['mode']!r}; the wire model field must never select egress backend"
    )


# ── run_in_threadpool: route handler is async def; sync spy does not deadlock ─────────


def test_route_handler_is_async_def() -> None:
    """run_in_threadpool prerequisite: the /v1/chat/completions handler must be async def.

    A sync handler would block the event loop when gateway.chat() (sync) is called;
    the handler MUST be async and must invoke the sync gateway via run_in_threadpool.
    """
    # CONTRACT: llm_router is the APIRouter from worldmonitor.api.llm
    chat_route = next(
        (r for r in llm_router.routes if hasattr(r, "path") and r.path == "/v1/chat/completions"),
        None,
    )
    assert chat_route is not None, (
        "POST /v1/chat/completions route not found on llm_router — "
        "builder must register it on the APIRouter"
    )
    endpoint = getattr(chat_route, "endpoint", None)
    assert endpoint is not None, "route has no endpoint attribute"
    assert asyncio.iscoroutinefunction(endpoint), (
        f"route handler {endpoint!r} must be async def so the sync gateway.chat() "
        "can be called via run_in_threadpool without blocking the event loop"
    )


def test_sync_spy_does_not_deadlock_test_client() -> None:
    """run_in_threadpool: a spy that blocks briefly must not deadlock TestClient."""

    class _SlowSpyGateway:
        """Spy that sleeps briefly to simulate synchronous blocking I/O."""

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def chat(
            self,
            messages: list[dict[str, Any]],
            *,
            mode: Any = None,
            caller_tag: str = "gateway",
        ) -> _FakeModelResponse:
            time.sleep(0.01)  # simulate sync blocking I/O
            self.calls.append({"messages": messages, "mode": mode, "caller_tag": caller_tag})
            return _FakeModelResponse()

    slow_spy = _SlowSpyGateway()
    c = _client(slow_spy)
    resp = c.post("/v1/chat/completions", json=_valid_body(), headers=_auth())
    assert resp.status_code == 200, (
        f"sync spy with blocking sleep must not deadlock TestClient, "
        f"got {resp.status_code}: {resp.text!r}"
    )
    assert len(slow_spy.calls) == 1, "slow spy must have been called exactly once"


# ── caller_tag: the route uses the documented hermes tag ──────────────────────────────


def test_route_uses_hermes_caller_tag() -> None:
    """The route must pass caller_tag='hermes' so egress audit attributes Hermes calls.

    ADR 0092 §1 / spec §3: caller_tag='hermes' (or a constant equal to 'hermes') is
    used so the per-call egress audit record attributes every Hermes model call.
    """
    spy = _SpyGateway()
    c = _client(spy)
    resp = c.post("/v1/chat/completions", json=_valid_body(), headers=_auth())
    assert resp.status_code == 200
    assert len(spy.calls) == 1
    assert spy.calls[0]["caller_tag"] == "hermes", (
        f"route must pass caller_tag='hermes', got {spy.calls[0]['caller_tag']!r}; "
        "the egress audit must attribute every Hermes model call with 'hermes'"
    )
