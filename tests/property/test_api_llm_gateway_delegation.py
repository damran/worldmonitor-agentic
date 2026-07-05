"""PRIMARY property test — Phase-3 Gate S3a: LLM HTTP endpoint gateway delegation.

Tests the egress/sovereignty boundary over generated inputs:
  INV-S3a-GATEWAY  every authenticated /v1/chat/completions call → exactly ONE
                   gateway.chat() invocation, receiving the posted messages and
                   mode=None (client 'model' field NEVER becomes gateway mode).
  INV-S3a-AUTH     unauthenticated / invalid-bearer → 401, spy NEVER called.
  INV-S3a-NOSTREAM stream:true → explicit 4xx, spy NEVER called, never a silent body.

BUILDER CONTRACT — the implementation MUST match these names exactly:

    worldmonitor.api.llm                               # NEW MODULE
        router: APIRouter                              # POST /v1/chat/completions,
                                                       # Depends(get_principal)
    worldmonitor.api.deps
        get_llm_gateway(request: Request) -> LLMGateway   # mirrors get_neo4j exactly
    worldmonitor.api.main
        create_app(..., llm_gateway: LLMGateway | None = None)  # additive param

RED TODAY:
    ModuleNotFoundError: No module named 'worldmonitor.api.llm'
    (api/llm.py does not exist; create_app() also lacks llm_gateway= param)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# CONTRACT: triggers ModuleNotFoundError at collection until builder creates api/llm.py
from worldmonitor.api.llm import router as llm_router  # noqa: F401  # CONTRACT: router
from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.settings import Settings

# ── canned fake ModelResponse ──────────────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self, content: str = "The answer is 42.", role: str = "assistant") -> None:
        self.content = content
        self.role = role


class _FakeChoice:
    def __init__(self) -> None:
        self.message = _FakeMessage()


class _FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 20
    total_tokens: int = 30


class _FakeModelResponse:
    def __init__(self) -> None:
        self.choices: list[_FakeChoice] = [_FakeChoice()]
        self.usage: _FakeUsage = _FakeUsage()
        self.model: str = "fake-model-v1"
        self.id: str = "chatcmpl-fake-prop-001"


# ── spy gateway (records every chat() call, never calls litellm) ───────────────────────


class _SpyGateway:
    """Records every chat() invocation; returns a canned response; never calls litellm."""

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


# ── fake verifiers ─────────────────────────────────────────────────────────────────────


class _FakeVerifier:
    """Accepts token 'good'; rejects everything else (mirrors test_api_graph)."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {
            "sub": "user-123",
            "urn:zitadel:iam:org:project:roles": {"worldmonitor:llm": {}},
        }


class _RejectingVerifier:
    """Rejects every token unconditionally — models a malformed/invalid bearer."""

    def verify(self, token: str) -> Mapping[str, Any]:
        raise InvalidTokenError("always rejected")


# ── module-level app + spy (built once; spy reset per example) ─────────────────────────
# Both create_app() calls will raise TypeError until the builder adds llm_gateway= param.
# The import above raises ModuleNotFoundError first — either way, correct red state.

_SPY = _SpyGateway()
_REJECTING_SPY = _SpyGateway()

# CONTRACT: create_app must accept llm_gateway= (additive keyword parameter)
_APP_ACCEPT = create_app(
    settings=Settings(environment="test"),
    verifier=_FakeVerifier(),
    llm_gateway=_SPY,  # type: ignore[call-arg]  # CONTRACT: llm_gateway= param on create_app
)
_CLIENT_ACCEPT = TestClient(_APP_ACCEPT, raise_server_exceptions=False)

_APP_REJECT = create_app(
    settings=Settings(environment="test"),
    verifier=_RejectingVerifier(),
    llm_gateway=_REJECTING_SPY,  # type: ignore[call-arg]
)
_CLIENT_REJECT = TestClient(_APP_REJECT, raise_server_exceptions=False)


# ── Hypothesis strategies ──────────────────────────────────────────────────────────────

_ROLE = st.sampled_from(["user", "assistant", "system"])

# Printable ASCII (safe for JSON encoding; TestClient json= handles escaping).
_CONTENT = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126),
    min_size=1,
    max_size=200,
)

_MESSAGE = st.fixed_dictionaries({"role": _ROLE, "content": _CONTENT})
_MESSAGES = st.lists(_MESSAGE, min_size=1, max_size=5)

# Include LLMMode-named strings to prove sovereignty: model strings that MATCH LLMMode
# enum values ('local', 'claude_headless', 'openrouter') must STILL produce mode=None.
_MODEL_STR = st.one_of(
    st.sampled_from(
        [
            "gpt-4o",
            "gpt-3.5-turbo",
            "claude-3-sonnet-20240229",
            "local",  # matches LLMMode.LOCAL — must NOT become gateway mode
            "claude_headless",  # matches LLMMode.CLAUDE_HEADLESS — must NOT become mode
            "openrouter",  # matches LLMMode.OPENROUTER — must NOT become mode
        ]
    ),
    st.text(
        alphabet=st.characters(min_codepoint=65, max_codepoint=122, categories=("Lu", "Ll")),
        min_size=1,
        max_size=20,
    ),
)


@st.composite
def _openai_body(draw: st.DrawFn) -> dict[str, Any]:
    """A valid OpenAI-shaped request body with varied messages, model, optional params."""
    body: dict[str, Any] = {
        "messages": draw(_MESSAGES),
        "model": draw(_MODEL_STR),
    }
    if draw(st.booleans()):
        body["temperature"] = draw(st.floats(min_value=0.0, max_value=2.0, allow_nan=False))
    if draw(st.booleans()):
        body["max_tokens"] = draw(st.integers(min_value=1, max_value=4096))
    if draw(st.booleans()):
        body["top_p"] = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
    return body


_PROP_SETTINGS = hyp_settings(
    max_examples=100,
    deadline=None,  # per repo convention: app-build/TestClient can be slow on busy runners
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ── helper ─────────────────────────────────────────────────────────────────────────────


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer good"}


# ── INV-S3a-GATEWAY: authenticated request → exactly one spy.chat() call ──────────────


@given(body=_openai_body())
@_PROP_SETTINGS
def test_every_authenticated_request_hits_spy_exactly_once(body: dict[str, Any]) -> None:
    """INV-S3a-GATEWAY (headline): no 200 can bypass the spy gateway.

    Over generated valid OpenAI bodies with a valid bearer:
    - response is 200
    - spy.chat() called EXACTLY once per served request
    - spy received the posted messages unchanged
    - spy called with mode=None (sovereignty: client model field NEVER → gateway mode)
    - api/llm.py has no litellm attribute (no direct provider bypass path)

    A tautology cannot pass: any implementation that calls gateway.chat() zero or more
    than once per request fails; any that passes mode != None fails. The no-litellm-attr
    check fails if the builder imports litellm at module level in api/llm.py.
    """
    _SPY.reset()
    resp = _CLIENT_ACCEPT.post("/v1/chat/completions", json=body, headers=_auth())
    assert resp.status_code == 200, (
        f"valid auth + valid body must yield 200, got {resp.status_code}: {resp.text!r}"
    )
    assert len(_SPY.calls) == 1, (
        f"expected exactly 1 gateway.chat() call per served request, "
        f"got {len(_SPY.calls)}: {_SPY.calls!r}"
    )
    call = _SPY.calls[0]
    # Messages must pass through to the gateway unchanged.
    assert call["messages"] == body["messages"], (
        f"spy received messages {call['messages']!r}, expected {body['messages']!r}"
    )
    # SOVEREIGNTY: client-supplied model field must NEVER become gateway mode.
    assert call["mode"] is None, (
        f"mode must be None (server-side selector decides backend), "
        f"got {call['mode']!r}; request model={body.get('model')!r} — "
        "a client must not select an egress backend by wire field (ADR 0092 §2)"
    )
    # Route module must not import litellm at module level (no direct provider bypass).
    import worldmonitor.api.llm as _llm_mod  # already cached in sys.modules; no I/O

    assert not hasattr(_llm_mod, "litellm"), (
        "api/llm.py must not import litellm — all LLM egress must go through the injected gateway"
    )


# ── INV-S3a-AUTH: no-token → 401, spy NEVER called ───────────────────────────────────


@given(body=_openai_body())
@_PROP_SETTINGS
def test_unauthenticated_no_token_returns_401_spy_never_called(body: dict[str, Any]) -> None:
    """INV-S3a-AUTH: no Authorization header → 401, gateway spy never reached.

    The auth middleware must reject BEFORE the route body executes — the gateway
    spy call count must be 0 for every unauthenticated request (no model call, no egress).
    """
    _SPY.reset()
    # No Authorization header at all.
    resp = _CLIENT_ACCEPT.post("/v1/chat/completions", json=body)
    assert resp.status_code == 401, (
        f"no-token request must yield 401, got {resp.status_code}: {resp.text!r}"
    )
    assert len(_SPY.calls) == 0, (
        f"gateway must NEVER be called for unauthenticated request, "
        f"got {len(_SPY.calls)} calls: {_SPY.calls!r}"
    )


@given(body=_openai_body())
@_PROP_SETTINGS
def test_invalid_bearer_returns_401_spy_never_called(body: dict[str, Any]) -> None:
    """INV-S3a-AUTH: invalid bearer (rejecting verifier) → 401, spy never reached.

    A rejecting verifier (rejects ALL tokens) models a malformed / wrong-issuer /
    expired bearer. The gateway spy must never be called.
    """
    _REJECTING_SPY.reset()
    resp = _CLIENT_REJECT.post(
        "/v1/chat/completions",
        json=body,
        headers={"Authorization": "Bearer invalid-garbage-token"},
    )
    assert resp.status_code == 401, (
        f"invalid bearer must yield 401, got {resp.status_code}: {resp.text!r}"
    )
    assert len(_REJECTING_SPY.calls) == 0, (
        f"gateway must NEVER be called for invalid bearer, "
        f"got {len(_REJECTING_SPY.calls)} calls: {_REJECTING_SPY.calls!r}"
    )


# ── INV-S3a-NOSTREAM: stream:true → explicit 4xx, spy NEVER called ────────────────────


@given(body=_openai_body())
@_PROP_SETTINGS
def test_stream_true_returns_explicit_4xx_spy_never_called(body: dict[str, Any]) -> None:
    """INV-S3a-NOSTREAM: stream:true → explicit 4xx; never a silent non-streaming 200.

    The route must NEVER silently answer a streaming request with a non-streaming body
    (that would mislead an OpenAI client into mis-parsing the response). The spy must
    never be called — no wasted egress — so there is no SSE downgrade path of any kind.
    """
    _SPY.reset()
    streaming_body = {**body, "stream": True}
    resp = _CLIENT_ACCEPT.post("/v1/chat/completions", json=streaming_body, headers=_auth())
    # Must be an explicit error, never a silent downgrade.
    assert 400 <= resp.status_code < 500, (
        f"stream:true must return an explicit 4xx, got {resp.status_code}: {resp.text!r}"
    )
    assert resp.status_code != 200, (
        "stream:true must NEVER be silently downgraded to a 200 non-streaming response "
        "(INV-S3a-NOSTREAM — SSE streaming is deferred to S5)"
    )
    # Spy must not be called: no model call for a rejected streaming request.
    assert len(_SPY.calls) == 0, (
        f"gateway must NEVER be called when stream:true is rejected (no wasted egress), "
        f"got {len(_SPY.calls)} calls: {_SPY.calls!r}"
    )


# ── Sovereignty: model field is informational over the full model-string space ─────────


@given(model=_MODEL_STR, messages=_MESSAGES)
@_PROP_SETTINGS
def test_model_field_never_selects_gateway_mode(model: str, messages: list[dict[str, Any]]) -> None:
    """Sovereignty: client model string NEVER becomes gateway mode.

    Over all generated model strings — including strings that MATCH LLMMode enum values
    ('local', 'claude_headless', 'openrouter') — the spy must always be called with
    mode=None. A client must not be able to route around the server-side sovereignty
    selector by setting the wire 'model' field (ADR 0092 §2).
    """
    _SPY.reset()
    body: dict[str, Any] = {"model": model, "messages": messages}
    resp = _CLIENT_ACCEPT.post("/v1/chat/completions", json=body, headers=_auth())
    # Only check mode when the request was accepted (200). A 422 for a truly invalid
    # model string is acceptable, but if the route accepted the request it MUST have
    # delegated with mode=None.
    if resp.status_code == 200:
        assert len(_SPY.calls) == 1, (
            f"expected 1 gateway call for model={model!r}, got {len(_SPY.calls)}"
        )
        assert _SPY.calls[0]["mode"] is None, (
            f"mode must be None regardless of model={model!r}, "
            f"got mode={_SPY.calls[0]['mode']!r}; "
            "a client MUST NOT be able to select an egress backend by wire field"
        )
