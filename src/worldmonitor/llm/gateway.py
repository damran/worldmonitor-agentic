"""LLM gateway — single egress choke point for service-side LLM use (ADR 0091, Phase-3 S2;
hardened by ADR 0104, Gate L1-a).

INV-S2-EGRESS: the gateway writes an egress record BEFORE contacting the provider so a
failing/timing-out call is still audited.  Provider failures surface as a typed
``LLMGatewayError``; the raw litellm exception never escapes.

INV-USAGE (ADR 0104 item 2): on a SUCCESSFUL call the SAME record is enriched in place with
the response's token usage and re-emitted — a successful call therefore writes exactly TWO
egress records (pre-call, usage=None; post-call, usage populated); a failure after the
pre-call emit writes exactly one.

INV-FAILCLOSED (ADR 0104 item 3): an EXTERNAL mode (``data_left_perimeter is True``) with
``llm_egress_log_enabled=False`` refuses the call (raises ``LLMGatewayError`` before any
emit or provider call) — no durable audit, no external egress. LOCAL stays freely
toggle-able.

INV-S2-DEFAULT: with no operator override, the active mode is ``LOCAL`` (confidential /
no egress, Ollama loopback).

INV-S2-LABEL: every mode has a non-empty confidentiality status + badge, enforced by the
registry at construction time (``modes.py``).

INV-CHOKE (ADR 0104 item 1): machine-enforced (not just documented) — no
``src/worldmonitor`` module outside ``llm/`` may import ``litellm``/``openai``/``anthropic``
(``tests/test_llm_egress_chokepoint.py``). The gateway is the ONLY public LLM egress
surface; there is no bypass.

Durable audit (ADR 0105, Gate F2): a new, DEFAULT-OFF ``llm_egress_durable_enabled`` flag
makes the accountability record durable and tamper-evident on top of the stdlib emits above
(byte-unchanged, still firing). When the flag is off (the default) behaviour is byte-identical
to L1 — see :mod:`worldmonitor.llm.egress_audit` for the writer + the invariants it adds
(INV-DURABLE-COMPLETE, INV-DURABLE-APPENDONLY, INV-DURABLE-FAILCLOSED, INV-DURABLE-USAGE).
"""

from __future__ import annotations

import logging
import urllib.parse
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import litellm
from sqlalchemy.orm import Session

import worldmonitor.llm.egress_audit as egress_audit
import worldmonitor.llm.egress_log as egress_log
from worldmonitor.llm.egress_log import EgressRecord
from worldmonitor.llm.modes import REGISTRY, LLMMode
from worldmonitor.settings import Settings

logger = logging.getLogger(__name__)


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

    def __init__(
        self,
        settings: Settings,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._settings = settings
        self._active_mode = LLMMode(settings.llm_mode)
        # Durable-audit DB seam (ADR 0105 / Gate F2): a sessionmaker injected at construction.
        # Default None preserves every existing (pre-F2) gateway construction unchanged.
        self._session_factory = session_factory
        # Register the claude shim ONLY when CLAUDE_HEADLESS mode is active (off by default).
        if self._active_mode is LLMMode.CLAUDE_HEADLESS:
            self._register_claude_shim()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        mode: LLMMode | None = None,
        caller_tag: str = "gateway",
        entity_ids: list[str] | None = None,
    ) -> Any:
        """Route an LLM completion through the egress choke point.

        INV-S2-EGRESS: writes the egress record BEFORE contacting the provider so
        even a failing/timing-out call is audited.
        INV-USAGE (ADR 0104 item 2): on success, re-emits the SAME record after enriching
        it with token usage — two records total (pre usage=None, post usage populated).
        INV-FAILCLOSED (ADR 0104 item 3): an external mode with the audit disabled refuses
        the call before any emit or provider contact.
        INV-S2-DEFAULT: defaults to the settings-configured mode (``LOCAL`` by default).
        Raises ``LLMGatewayError`` on provider failure — never the raw exception.

        ``entity_ids`` (ADR 0105 / Gate F2): an OPTIONAL, caller-declared list of canonical
        entity ids whose data the caller asserts is in ``messages`` — recorded honestly on
        the durable attempt row's ``entity_manifest`` (``None`` when not declared, e.g. every
        ``/v1`` wire caller; never content-derived, SF-2). No effect when the durable audit
        is off (the default).

        INV-DURABLE-COMPLETE / INV-DURABLE-FAILCLOSED (ADR 0105 / Gate F2): when
        ``llm_egress_durable_enabled`` is also on, a durable Postgres "attempt" row commits
        before the provider is contacted; for an EXTERNAL mode that commit (or an unwired
        sink) is fail-closed (no durable audit ⇒ no egress) — LOCAL proceeds best-effort. A
        second, best-effort "completed" row carries token usage post-call for both modes.
        With the flag off (the default) behaviour is byte-identical to L1.

        ``import litellm`` (whole module) is required here so that patching
        ``litellm.completion`` in tests intercepts every gateway call.
        """
        active_mode = mode if mode is not None else self._active_mode
        mode_record = REGISTRY[active_mode]
        external = mode_record.data_left_perimeter

        # ── ADR 0104 item 3 (fail-closed) — BEFORE any emit, BEFORE resolving call params
        # (so a refused call never even touches a provider secret), and BEFORE the provider
        # call itself. No durable audit => no external egress. LOCAL remains freely
        # toggle-able: disabling the audit for a confidential, on-perimeter call affects no
        # external accountability.
        if external and not self._settings.llm_egress_log_enabled:
            raise LLMGatewayError(
                "external LLM egress refused: audit is disabled "
                "(llm_egress_log_enabled=False) for an external mode — no durable audit, "
                "no egress"
            )

        # ADR 0105 / Gate F2: the durable obligation is a SEPARATE flag (SF-4) — it only
        # tightens once the master llm_egress_log_enabled is already on.
        durable_on = (
            self._settings.llm_egress_log_enabled and self._settings.llm_egress_durable_enabled
        )

        # ── INV-DURABLE-FAILCLOSED (ADR 0105 / Gate F2) — the durable obligation is active
        # for an EXTERNAL mode but no durable sink is wired: refuse before any provider
        # contact (an unwired gateway cannot silently egress unaudited).
        if external and durable_on and self._session_factory is None:
            raise LLMGatewayError(
                "external LLM egress refused: durable audit is enabled "
                "(llm_egress_durable_enabled=True) but no durable sink is wired — "
                "no durable audit, no egress"
            )

        # The per-call override (which the S5 operator console drives) may select
        # CLAUDE_HEADLESS on a gateway built in another mode; ensure the shim is
        # registered for whichever mode is actually resolved (idempotent, off otherwise).
        if active_mode is LLMMode.CLAUDE_HEADLESS:
            self._register_claude_shim()

        model, api_base, api_key = self._resolve_call_params(active_mode)
        target_host = _extract_target_host(api_base, active_mode)

        call_id = str(uuid.uuid4())

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

        # ── ADR 0105 / Gate F2 — durable PRE-call ("attempt") row ────────────────────────
        if durable_on and self._session_factory is not None:
            attempt_row = egress_audit.build_attempt_row(
                call_id,
                record,
                egress_audit.fingerprint_messages(messages),
                entity_ids,
            )
            if external:
                # Fail-closed: the commit MUST succeed BEFORE the provider call.
                try:
                    egress_audit.write_row(self._session_factory, attempt_row)
                except Exception as exc:
                    raise LLMGatewayError(
                        "external LLM egress refused: durable audit write failed — "
                        "no durable audit, no egress"
                    ) from exc
            else:
                # LOCAL best-effort: a sink failure must not break a confidential,
                # on-perimeter call (SF-5, the fail-closed asymmetry).
                try:
                    egress_audit.write_row(self._session_factory, attempt_row)
                except Exception:
                    logger.warning(
                        "durable egress audit write failed (LOCAL, best-effort)",
                        exc_info=True,
                    )

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

        # ADR 0104 item 2 — POST-CALL second emit carrying usage, so a call's token spend
        # actually lands in the audit. Guarded by the same flag as the pre-call emit; the
        # fail-closed check above guarantees external modes always have it True, so an
        # external success always produces exactly two records.
        if self._settings.llm_egress_log_enabled:
            egress_log.emit(record)

        # ── ADR 0105 / Gate F2 — durable POST-call ("completed") row, best-effort for BOTH
        # modes: a sink failure here must never undo an already-audited crossing.
        if durable_on and self._session_factory is not None:
            try:
                egress_audit.write_row(
                    self._session_factory,
                    egress_audit.build_completed_row(call_id, record),
                )
            except Exception:
                logger.warning(
                    "durable egress usage row write failed (best-effort; crossing already audited)",
                    exc_info=True,
                )

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
