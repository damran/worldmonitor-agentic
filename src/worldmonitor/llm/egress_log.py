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


def emit(record: EgressRecord) -> None:
    """Emit a structured per-call egress audit record via stdlib logging.

    Mirrors the structured ``extra=`` style from ``sandbox/container_runner.py``.
    NEVER emits the API key or message content (INV-S2-EGRESS + ADR 0091 §3).
    """
    logger.info(
        "llm-egress mode=%s target=%s data_left_perimeter=%s model=%s caller=%s",
        record.mode.value,
        record.target_host,
        record.data_left_perimeter,
        record.model,
        record.caller_tag,
        extra={
            "llm_mode": record.mode.value,
            "llm_confidentiality": record.confidentiality,
            "llm_target_host": record.target_host,
            "llm_data_left_perimeter": record.data_left_perimeter,
            "llm_model": record.model,
            "llm_timestamp": record.timestamp.isoformat(),
            "llm_caller_tag": record.caller_tag,
        },
    )
