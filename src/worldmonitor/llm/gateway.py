"""LLM gateway — single egress choke point for service-side LLM use (ADR 0091, Phase-3 S2).

INV-S2-EGRESS: the gateway writes exactly one egress record BEFORE contacting the provider
so a failing/timing-out call is still audited.  Provider failures surface as a typed
``LLMGatewayError``; the raw litellm exception never escapes.

INV-S2-DEFAULT: with no operator override, the active mode is ``LOCAL`` (confidential /
no egress, Ollama loopback).

INV-S2-LABEL: every mode has a non-empty confidentiality status + badge, enforced by the
registry at construction time (``modes.py``).

The gateway is the ONLY public LLM egress surface; there is no bypass.
"""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime
from typing import Any

import litellm

import worldmonitor.llm.egress_log as egress_log
from worldmonitor.llm.egress_log import EgressRecord
from worldmonitor.llm.modes import REGISTRY, LLMMode
from worldmonitor.settings import Settings


class LLMGatewayError(Exception):
    """Typed gateway error — wraps all provider failures (ADR 0091 §1).

    The raw litellm / provider exception is NEVER allowed to escape the gateway;
    callers receive this typed error so provider internals do not leak.
    """


class LLMGateway:
    """Single LLM egress choke point.

    Every service-side LLM call goes through ``chat()`` / ``completion()``.
    No public method other than those two reaches ``litellm.completion``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._active_mode = LLMMode(settings.llm_mode)
        # Register the claude shim ONLY when CLAUDE_HEADLESS mode is active (off by default).
        if self._active_mode is LLMMode.CLAUDE_HEADLESS:
            self._register_claude_shim()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        mode: LLMMode | None = None,
        caller_tag: str = "gateway",
    ) -> Any:
        """Route an LLM completion through the egress choke point.

        INV-S2-EGRESS: writes the egress record BEFORE contacting the provider so
        even a failing/timing-out call is audited.
        INV-S2-DEFAULT: defaults to the settings-configured mode (``LOCAL`` by default).
        Raises ``LLMGatewayError`` on provider failure — never the raw exception.

        ``import litellm`` (whole module) is required here so that patching
        ``litellm.completion`` in tests intercepts every gateway call.
        """
        active_mode = mode if mode is not None else self._active_mode
        mode_record = REGISTRY[active_mode]

        # The per-call override (which the S5 operator console drives) may select
        # CLAUDE_HEADLESS on a gateway built in another mode; ensure the shim is
        # registered for whichever mode is actually resolved (idempotent, off otherwise).
        if active_mode is LLMMode.CLAUDE_HEADLESS:
            self._register_claude_shim()

        model, api_base, api_key = self._resolve_call_params(active_mode)
        target_host = _extract_target_host(api_base, active_mode)

        # INV-S2-EGRESS: write the record BEFORE the provider call.
        # A crashing / timing-out provider call is still audited.
        record = EgressRecord(
            mode=active_mode,
            confidentiality=mode_record.confidentiality,
            target_host=target_host,
            data_left_perimeter=mode_record.data_left_perimeter,
            model=model,
            timestamp=datetime.now(tz=UTC),
            caller_tag=caller_tag,
        )
        if self._settings.llm_egress_log_enabled:
            egress_log.emit(record)

        # Call litellm — WHOLE-MODULE import so `litellm.completion` is patchable by tests.
        # Do NOT `from litellm import completion` — that would break the monkeypatch.
        call_kwargs: dict[str, Any] = {}
        if api_base is not None:
            call_kwargs["api_base"] = api_base
        if api_key is not None:
            call_kwargs["api_key"] = api_key

        try:
            response = litellm.completion(model, messages, **call_kwargs)  # type: ignore[no-untyped-call]
        except Exception as exc:
            raise LLMGatewayError(
                f"provider call failed (mode={active_mode.value!r}): {type(exc).__name__}: {exc}"
            ) from exc

        # Enrich the record in-place with token usage from the response (mutable EgressRecord).
        # Use getattr to avoid attribute-access type errors on the partially-typed ModelResponse.
        usage_val = getattr(response, "usage", None)
        if usage_val is not None:
            record.usage = usage_val

        return response

    # Alias: completion() → chat() (spec §4b allows either entry point).
    completion = chat

    # ── Private helpers (not in the public callable surface) ─────────────────────────────

    def _resolve_call_params(self, mode: LLMMode) -> tuple[str, str | None, str | None]:
        """Return (model_string, api_base, api_key) for the given mode."""
        s = self._settings
        if mode is LLMMode.LOCAL:
            return (
                f"ollama_chat/{s.llm_ollama_model}",
                s.llm_ollama_base_url,
                None,
            )
        elif mode is LLMMode.OPENROUTER:
            key = s.llm_openrouter_api_key.get_secret_value()
            return (
                f"openrouter/{s.llm_openrouter_model}",
                None,
                key or None,
            )
        else:  # CLAUDE_HEADLESS
            return (
                f"claude_shim/{s.llm_claude_model_label}",
                None,
                None,
            )

    def _register_claude_shim(self) -> None:
        """Register the ClaudeShim custom provider in ``litellm.custom_provider_map``.

        Off by default — called ONLY when CLAUDE_HEADLESS mode is active (ADR 0091 §2).
        Idempotent: updates an existing entry rather than appending duplicates.
        """
        from worldmonitor.llm.claude_shim import ClaudeShim

        shim = ClaudeShim(
            binary=self._settings.llm_claude_binary,
            timeout=self._settings.llm_claude_timeout_seconds,
        )
        existing: set[str] = {
            str(entry.get("provider", "")) for entry in litellm.custom_provider_map
        }
        if "claude_shim" not in existing:
            litellm.custom_provider_map.append({"provider": "claude_shim", "custom_handler": shim})
        else:
            for entry in litellm.custom_provider_map:
                if entry.get("provider") == "claude_shim":
                    entry["custom_handler"] = shim
                    break


# ── Module-level helper (not on the class — excluded from vars(LLMGateway)) ───────────


def _extract_target_host(api_base: str | None, mode: LLMMode) -> str:
    """Derive a loggable target hostname from the provider base URL or mode."""
    if api_base:
        parsed = urllib.parse.urlparse(api_base)
        return parsed.hostname or api_base
    if mode is LLMMode.OPENROUTER:
        return "openrouter.ai"
    if mode is LLMMode.CLAUDE_HEADLESS:
        return "claude-subprocess"
    return "unknown"
