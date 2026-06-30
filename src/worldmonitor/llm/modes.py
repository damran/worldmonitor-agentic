"""LLM mode registry — three-mode confidential selector (ADR 0091, Phase-3 S2).

INV-S2-LABEL: every registered mode has a non-empty confidentiality status and badge;
a ModeRecord without either label **cannot be constructed** (raised in __post_init__).
INV-S2-DEFAULT: LOCAL is the default mode; data never leaves the perimeter for LOCAL.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class LLMMode(enum.Enum):
    """Three-mode confidential selector (ADR 0091 §2 — user-finalized, locked)."""

    LOCAL = "local"
    CLAUDE_HEADLESS = "claude_headless"
    OPENROUTER = "openrouter"


@dataclass
class ModeRecord:
    """Registry entry for one LLM mode — routing metadata + confidentiality label.

    INV-S2-LABEL (construction-time): ``confidentiality`` and ``badge`` MUST be
    non-empty non-None strings.  ``__post_init__`` raises ``ValueError`` if either
    is falsy or None so no code path can register a mode without a label.
    """

    model: str  # litellm model string, e.g. "ollama_chat/llama3.2"
    base_url: str | None  # provider base URL; None when not needed
    confidentiality: str  # non-empty status; raises ValueError if empty/None
    badge: str  # non-empty human-readable operator label
    data_left_perimeter: bool  # True iff data leaves the perimeter

    def __post_init__(self) -> None:
        if not self.confidentiality:
            raise ValueError(
                f"ModeRecord.confidentiality must be a non-empty string; "
                f"got {self.confidentiality!r}. "
                "INV-S2-LABEL: every mode MUST carry a confidentiality status so the "
                "selector never presents a mode whose status is unknown (ADR 0091 §2)."
            )
        if not self.badge:
            raise ValueError(
                f"ModeRecord.badge must be a non-empty string; got {self.badge!r}. "
                "The badge is the operator-visible human-readable label shown at "
                "selection time (ADR 0091 §2)."
            )


# ── Registry (locked by ADR 0091 §2, user-finalized — exactly three modes) ─────────────
REGISTRY: dict[LLMMode, ModeRecord] = {
    LLMMode.LOCAL: ModeRecord(
        model="ollama_chat/llama3.2",
        base_url="http://localhost:11434",
        confidentiality="Confidential — no egress",
        badge="Local (Ollama — confidential, no data leaves the perimeter)",
        data_left_perimeter=False,
    ),
    LLMMode.CLAUDE_HEADLESS: ModeRecord(
        model="claude_shim/claude",
        base_url=None,
        confidentiality=(
            "External egress → Anthropic "
            "(ToS-gray caveat: programmatic consumer-subscription use may violate terms; "
            "use the Anthropic API for a clean external route)"
        ),
        badge=(
            "Claude headless (claude -p shim) — external egress → Anthropic; "
            "ToS-brittle caveat: consumer subscription, off by default"
        ),
        data_left_perimeter=True,
    ),
    LLMMode.OPENROUTER: ModeRecord(
        model="openrouter/openai/gpt-4o",
        base_url=None,
        confidentiality="External egress → OpenRouter",
        badge="OpenRouter — external egress → OpenRouter (opt-in, off by default)",
        data_left_perimeter=True,
    ),
}
