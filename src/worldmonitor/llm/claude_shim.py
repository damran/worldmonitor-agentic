"""Claude headless shim — runs ``claude -p`` via an argv-list subprocess (ADR 0091, Phase-3 S2).

CLAUDE.md hostile-data rules enforced here:
- The shim always uses an **argv LIST** — never ``shell=True`` or string interpolation.
- A ``timeout`` is always set (subprocess deadline prevents hangs).
- stdout is treated as **untrusted** string content: placed into the response as-is,
  never ``eval()``'d, never re-executed, never passed to a shell.

This module is only imported and registered in ``litellm.custom_provider_map`` when
``CLAUDE_HEADLESS`` mode is active (off by default — ADR 0091 §2).
"""

from __future__ import annotations

import subprocess
from typing import Any, cast

import litellm
from litellm.types.utils import Choices, Message, ModelResponse  # type: ignore[import-untyped]


class ClaudeShim(litellm.CustomLLM):  # type: ignore[attr-defined]
    """LiteLLM CustomLLM adapter that runs ``claude -p`` as a sandboxed subprocess.

    Registered in ``litellm.custom_provider_map`` ONLY when ``CLAUDE_HEADLESS`` mode
    is selected (ADR 0091 §2: off by default).

    CLAUDE.md hostile-data invariants:
    - argv LIST: ``[binary, "-p", prompt]`` — never ``shell=True``.
    - timeout: always present — no unbounded subprocess.
    - stdout: untrusted string → placed as content only, no ``eval``, no re-execution.
    """

    def __init__(self, binary: str, timeout: float) -> None:
        super().__init__()
        self._binary = binary
        self._timeout = timeout

    def completion(  # type: ignore[override]
        self,
        model: str,
        messages: list[Any],
        **kwargs: Any,
    ) -> ModelResponse:
        """Run ``claude -p <prompt>`` and return stdout as the response content.

        ARGV LIST — never ``shell=True`` (CLAUDE.md hostile-data rule).
        stdout is untrusted: placed as content only, never ``eval()``'d or re-executed.
        """
        model_response: ModelResponse = kwargs.get("model_response") or ModelResponse()
        # Use litellm's per-call timeout if provided, else fall back to the configured default.
        raw_timeout = kwargs.get("timeout")
        deadline: float = float(raw_timeout) if raw_timeout is not None else self._timeout

        # Extract the last user message as the prompt.
        # NEVER interpolated into a shell string — passed as a standalone argv element.
        prompt = _extract_prompt(messages)

        # ARGV LIST — never shell=True, never string interpolation of untrusted data.
        result = subprocess.run(
            [self._binary, "-p", prompt],
            capture_output=True,
            timeout=deadline,
            text=True,
        )

        # Treat stdout as untrusted string content (no eval, no shell re-execution).
        content: str = result.stdout

        model_response.choices = [
            Choices(
                finish_reason="stop",
                index=0,
                message=Message(content=content, role="assistant"),
            )
        ]
        model_response.model = model
        return model_response

    def acompletion(  # type: ignore[override]
        self,
        model: str,
        messages: list[Any],
        **kwargs: Any,
    ) -> Any:
        """Async variant — delegates to the sync completion (not used in Phase-3 S2)."""
        return self.completion(model, messages, **kwargs)


def _extract_prompt(messages: list[Any]) -> str:
    """Extract the last user-role message content as the prompt string.

    The returned string is NEVER interpolated into a shell string or ``eval()``'d —
    it is passed as a standalone element in the argv list (CLAUDE.md hostile-data rule).
    """
    for msg in reversed(messages):
        if isinstance(msg, dict):
            msg_d = cast(dict[str, Any], msg)
            if msg_d.get("role") == "user":
                return str(msg_d.get("content", ""))
    # Fallback: concatenate all message contents without shell joining.
    parts: list[str] = []
    for m in messages:
        if isinstance(m, dict):
            m_d = cast(dict[str, Any], m)
            parts.append(str(m_d.get("content", "")))
    return " ".join(parts)
