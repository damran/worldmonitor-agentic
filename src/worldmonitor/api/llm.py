"""OpenAI-compatible LLM HTTP endpoint (Phase-3 Gate S3a, ADR 0092).

Exposes the in-process ``LLMGateway`` over HTTP so the remote Hermes container
routes every model call through the sovereignty choke point (S2 egress audit +
confidential selector).

Route: ``POST /v1/chat/completions`` — auth-gated (``Depends(get_principal)``).

INV-S3a-GATEWAY: the ONLY path to an LLM is ``LLMGateway.chat()``.  This module
does NOT import ``litellm``; all egress goes through the injected gateway.

INV-S3a-AUTH: bearer-gated — 401 without a valid Zitadel token (same gate as
``api/graph.py``; the Hermes service-principal bearer satisfies it).

INV-S3a-NOSTREAM: ``stream: true`` returns an explicit 400 (SSE streaming is
deferred to S5, the operator console).

The client ``model`` field is **informational** (ADR 0092 §2): the server-side
``Settings.llm_mode`` confidential selector decides the real provider backend;
the route always passes ``mode=None`` to ``gateway.chat()`` so the gateway's
configured default applies.  A client CANNOT select an egress backend by wire
field — that would route around the server-side sovereignty choice.

``LLMGateway.chat()`` is synchronous (calls ``litellm.completion``).  The async
handler calls it via ``fastapi.concurrency.run_in_threadpool`` so the event loop
is not blocked (ADR 0092 §4).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from worldmonitor.api.deps import get_llm_gateway, get_principal
from worldmonitor.authz.oidc import Principal
from worldmonitor.llm.gateway import LLMGateway, LLMGatewayError

__all__ = ["router"]

_LOG = logging.getLogger(__name__)

router = APIRouter(tags=["llm"])


# ── OpenAI request models ─────────────────────────────────────────────────────


class _Message(BaseModel):
    """A single chat message (role + content), OpenAI wire shape."""

    role: str
    content: str


class _ChatCompletionRequest(BaseModel):
    """OpenAI-compatible ``POST /v1/chat/completions`` request body.

    The ``model`` field is **informational** — the server-side
    ``Settings.llm_mode`` selector decides the real provider backend (ADR 0092
    §2).  Optional sampling parameters (``temperature``, ``max_tokens``,
    ``top_p``) are accepted for wire compatibility but are NOT forwarded as
    gateway mode/route selectors in S3a; the gateway resolves its own backend.
    """

    model: str
    messages: list[_Message] = Field(min_length=1)
    stream: bool = False
    # Optional sampling params — accepted, not forwarded as mode selectors.
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None


# ── Route ─────────────────────────────────────────────────────────────────────


@router.post("/v1/chat/completions")
async def chat_completions(
    request: _ChatCompletionRequest,
    _principal: Annotated[Principal, Depends(get_principal)],
    gateway: Annotated[LLMGateway, Depends(get_llm_gateway)],
) -> dict[str, Any]:
    """OpenAI-compatible chat completion (ADR 0092, Phase-3 S3a).

    Every completion is delegated to the injected ``LLMGateway`` — this route
    has no path that contacts a provider or ``litellm`` directly
    (INV-S3a-GATEWAY).

    The client ``model`` field is informational; the server-side
    ``Settings.llm_mode`` confidential selector decides the real backend.
    A client cannot select an external egress mode by wire field (ADR 0092 §2).

    ``stream: true`` is rejected with an explicit 400 — SSE streaming is
    deferred to S5 (the operator console).  The gateway is never called for a
    streaming request (INV-S3a-NOSTREAM).

    Provider failures surface as a clean 502 with a generic detail; no raw
    exception text, no stack trace, no provider internals are exposed.
    """
    # INV-S3a-NOSTREAM: reject streaming requests explicitly; never silently
    # downgrade to a non-streaming body (that would mislead an OpenAI client).
    if request.stream:
        raise HTTPException(
            status_code=400,
            detail=(
                "streaming is not supported on this endpoint "
                "(deferred to the operator console, Phase-3 S5)"
            ),
        )

    # Convert the validated pydantic messages to the plain-dict shape the
    # gateway expects (gateway.chat signature: list[dict[str, Any]]).
    messages: list[dict[str, Any]] = [
        {"role": m.role, "content": m.content} for m in request.messages
    ]

    # INV-S3a-GATEWAY: the only path to a model is via the injected gateway.
    # mode=None — the server-side Settings.llm_mode selector decides the backend;
    # the client model field is deliberately NOT translated to a gateway mode.
    # caller_tag="hermes" — egress audit attributes every Hermes model call.
    try:
        resp = await run_in_threadpool(gateway.chat, messages, caller_tag="hermes")
    except LLMGatewayError:
        # Typed gateway error → clean 502.  No provider internals, no traceback.
        _LOG.warning("LLMGatewayError from gateway.chat (caller_tag=hermes)")
        raise HTTPException(
            status_code=502,
            detail="LLM gateway error — upstream provider unavailable",
        ) from None

    # Map the litellm ModelResponse to an OpenAI-shaped JSON body.
    # Use getattr defensively throughout — ModelResponse is partially typed.
    content: str = ""
    role: str = "assistant"
    choices: Any = getattr(resp, "choices", None)
    if choices and len(choices) > 0:
        msg: Any = getattr(choices[0], "message", None)
        if msg is not None:
            raw_content: Any = getattr(msg, "content", "")
            raw_role: Any = getattr(msg, "role", "assistant")
            content = str(raw_content) if raw_content is not None else ""
            role = str(raw_role) if raw_role is not None else "assistant"

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    usage_obj: Any = getattr(resp, "usage", None)
    if usage_obj is not None:
        prompt_tokens = int(getattr(usage_obj, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage_obj, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage_obj, "total_tokens", 0) or 0)

    raw_model: Any = getattr(resp, "model", None)
    resp_model: str = str(raw_model) if raw_model is not None else request.model

    raw_id: Any = getattr(resp, "id", None)
    resp_id: str = str(raw_id) if raw_id is not None else f"chatcmpl-{uuid.uuid4().hex}"

    return {
        "id": resp_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resp_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": role, "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    }
