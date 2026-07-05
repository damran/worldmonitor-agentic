"""Per-call LLM egress audit record (ADR 0091, Phase-3 S2).

INV-S2-EGRESS: the gateway writes exactly one EgressRecord per call BEFORE contacting
the provider, so a failing/timing-out external call is still audited.

The ``emit()`` function uses stdlib ``logging`` with ``extra=`` (mirroring
``sandbox/container_runner.py`` style).  It MUST NOT log the API key or message content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from worldmonitor.llm.modes import LLMMode

logger = logging.getLogger(__name__)


@dataclass
class EgressRecord:
    """Structured per-call LLM egress audit record.

    MUTABLE: ``.usage`` is ``None`` before the provider call and is enriched
    in-place on success (the gateway sets ``record.usage = response.usage``).

    Fields logged: mode, confidentiality, target_host, data_left_perimeter,
    model, timestamp, usage, caller_tag.
    NEVER logged: api_key, message content (ADR 0091 §3).
    """

    mode: LLMMode
    confidentiality: str
    target_host: str
    data_left_perimeter: bool
    model: str
    timestamp: datetime
    caller_tag: str
    usage: object | None = field(default=None)


def _extract_usage_tokens(usage: object) -> dict[str, int | None]:
    """Defensively extract token counts from a provider-shaped ``usage`` object.

    ``usage`` is typically a litellm/OpenAI ``Usage`` object (or a test double); we never
    assume its exact type, only that it MAY carry these three attributes (ADR 0104 item 2).
    """
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def emit(record: EgressRecord) -> None:
    """Emit a structured per-call egress audit record via stdlib logging.

    Mirrors the structured ``extra=`` style from ``sandbox/container_runner.py``.
    NEVER emits the API key or message content (INV-S2-EGRESS + ADR 0091 §3).

    When ``record.usage`` is populated (the post-call record, ADR 0104 item 2), the token
    counts are serialized into BOTH the log line and ``extra=`` so a call's spend actually
    lands in the audit. When ``record.usage`` is ``None`` (the pre-call completeness
    record), the line is well-formed with no usage token/noise.
    """
    extra: dict[str, object] = {
        "llm_mode": record.mode.value,
        "llm_confidentiality": record.confidentiality,
        "llm_target_host": record.target_host,
        "llm_data_left_perimeter": record.data_left_perimeter,
        "llm_model": record.model,
        "llm_timestamp": record.timestamp.isoformat(),
        "llm_caller_tag": record.caller_tag,
    }

    if record.usage is not None:
        tokens = _extract_usage_tokens(record.usage)
        extra["llm_usage_prompt_tokens"] = tokens["prompt_tokens"]
        extra["llm_usage_completion_tokens"] = tokens["completion_tokens"]
        extra["llm_usage_total_tokens"] = tokens["total_tokens"]
        logger.info(
            "llm-egress mode=%s target=%s data_left_perimeter=%s model=%s caller=%s "
            "usage=(prompt=%s, completion=%s, total=%s)",
            record.mode.value,
            record.target_host,
            record.data_left_perimeter,
            record.model,
            record.caller_tag,
            tokens["prompt_tokens"],
            tokens["completion_tokens"],
            tokens["total_tokens"],
            extra=extra,
        )
        return

    logger.info(
        "llm-egress mode=%s target=%s data_left_perimeter=%s model=%s caller=%s",
        record.mode.value,
        record.target_host,
        record.data_left_perimeter,
        record.model,
        record.caller_tag,
        extra=extra,
    )
