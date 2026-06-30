"""Unit tests — Phase-3 Gate S2: LLM mode registry (INV-S2-LABEL invariants).

Covers spec §4c:
- the registry has exactly the three modes {LOCAL, CLAUDE_HEADLESS, OPENROUTER};
- each exposes a non-empty confidentiality status + badge;
- constructing a registry record with an empty/missing confidentiality label raises;
- LOCAL.data_left_perimeter is False; the two external modes are True;
- CLAUDE_HEADLESS badge/record carries the documented ToS-gray / brittle caveat string.

RED TODAY:
    ``ModuleNotFoundError: No module named 'worldmonitor.llm.modes'``

BUILDER CONTRACT (names the implementation MUST match):

    worldmonitor.llm.modes
        LLMMode(enum.Enum)
            LOCAL, CLAUDE_HEADLESS, OPENROUTER

        ModeRecord                        # must be importable under this name
            model: str                    # litellm model string
            base_url: str | None          # provider base URL
            confidentiality: str          # NON-EMPTY required field; raises if empty/None
            badge: str                    # non-empty human-readable label
            data_left_perimeter: bool     # True iff data leaves the perimeter on this mode

        REGISTRY: dict[LLMMode, ModeRecord]
            # Keys == {LLMMode.LOCAL, LLMMode.CLAUDE_HEADLESS, LLMMode.OPENROUTER} — exactly 3.

    Confidentiality labels (locked — ADR 0091 Table, user-finalized):
        LOCAL            → "Confidential — no egress"     (data_left_perimeter=False)
        CLAUDE_HEADLESS  → "External egress → Anthropic"   (data_left_perimeter=True)
                           badge MUST mention "ToS" or "brittle" (the documented caveat)
        OPENROUTER       → "External egress → OpenRouter"  (data_left_perimeter=True)
"""

from __future__ import annotations

from typing import Any

import pytest

# ── Imports that MUST fail until builder creates modes.py ──────────────────────────────
# All tests in this file are RED (ModuleNotFoundError at collection) until modes.py exists.
from worldmonitor.llm.modes import REGISTRY, LLMMode, ModeRecord  # noqa: F401

# ── Expected mode set ──────────────────────────────────────────────────────────────────

_EXPECTED_MODES = frozenset({LLMMode.LOCAL, LLMMode.CLAUDE_HEADLESS, LLMMode.OPENROUTER})
_EXTERNAL_MODES = (LLMMode.CLAUDE_HEADLESS, LLMMode.OPENROUTER)


# ── INV-S2-LABEL: registry completeness ───────────────────────────────────────────────


def test_registry_has_exactly_three_modes() -> None:
    """The registry maps exactly {LOCAL, CLAUDE_HEADLESS, OPENROUTER} — no more, no fewer.

    Exactly three modes (locked by user decision, ADR 0091 §2).  A builder cannot add a
    fourth unlabeled mode: it would appear here as an unexpected key.  Removing a mode
    would fail the subset check from the other direction.
    """
    actual = frozenset(REGISTRY.keys())
    assert actual == _EXPECTED_MODES, (
        f"REGISTRY keys mismatch.  "
        f"Expected exactly {_EXPECTED_MODES!r}, got {actual!r}.  "
        f"ADR 0091 §2 locks exactly three modes (user-finalized)."
    )


@pytest.mark.parametrize("mode", list(LLMMode))
def test_every_mode_has_non_empty_confidentiality(mode: LLMMode) -> None:
    """INV-S2-LABEL: REGISTRY[mode].confidentiality is a non-empty string for every mode."""
    record = REGISTRY[mode]
    assert isinstance(record.confidentiality, str) and record.confidentiality, (
        f"REGISTRY[{mode!r}].confidentiality must be a non-empty str; "
        f"got {record.confidentiality!r}.  "
        f"INV-S2-LABEL: every mode MUST carry a confidentiality status so the selector "
        f"never surfaces a mode whose status is unknown (ADR 0091 §2)."
    )


@pytest.mark.parametrize("mode", list(LLMMode))
def test_every_mode_has_non_empty_badge(mode: LLMMode) -> None:
    """INV-S2-LABEL: REGISTRY[mode].badge is a non-empty string for every mode."""
    record = REGISTRY[mode]
    assert isinstance(record.badge, str) and record.badge, (
        f"REGISTRY[{mode!r}].badge must be a non-empty str; got {record.badge!r}.  "
        f"The badge is the human-readable label shown to the operator at selection time."
    )


@pytest.mark.parametrize("mode", list(LLMMode))
def test_every_mode_has_model_string(mode: LLMMode) -> None:
    """REGISTRY[mode].model is a non-empty litellm model string for every mode."""
    record = REGISTRY[mode]
    assert isinstance(record.model, str) and record.model, (
        f"REGISTRY[{mode!r}].model must be a non-empty litellm model string; got {record.model!r}."
    )


# ── INV-S2-DEFAULT: data_left_perimeter matches the mode table ─────────────────────────


def test_local_data_left_perimeter_is_false() -> None:
    """INV-S2-DEFAULT: LOCAL mode never sends data off-perimeter (loopback Ollama).

    LOCAL is the default mode; with no operator override, data NEVER leaves the perimeter.
    Verifies the locked ADR 0091 table entry for LOCAL.
    """
    record = REGISTRY[LLMMode.LOCAL]
    assert record.data_left_perimeter is False, (
        f"REGISTRY[LOCAL].data_left_perimeter must be False (data stays on-perimeter, "
        f"Ollama loopback); got {record.data_left_perimeter!r}.  "
        f"INV-S2-DEFAULT: the default mode must never send data off-perimeter (ADR 0091 §2)."
    )


@pytest.mark.parametrize("mode", list(_EXTERNAL_MODES))
def test_external_modes_data_left_perimeter_is_true(mode: LLMMode) -> None:
    """INV-S2-DEFAULT: CLAUDE_HEADLESS and OPENROUTER both send data off-perimeter.

    These are the opt-in external modes; selecting either means data leaves the perimeter.
    The egress record will surface this as data_left_perimeter=True to the audit log and
    (in S5) to the operator console.
    """
    record = REGISTRY[mode]
    assert record.data_left_perimeter is True, (
        f"REGISTRY[{mode!r}].data_left_perimeter must be True (external provider, data "
        f"leaves perimeter); got {record.data_left_perimeter!r}.  "
        f"ADR 0091 §2 table: CLAUDE_HEADLESS → Anthropic, OPENROUTER → OpenRouter."
    )


def test_local_model_string_uses_ollama_chat_prefix() -> None:
    """LOCAL mode's litellm model string uses the 'ollama_chat/' prefix (spec §7).

    This pins the litellm routing prefix so the gateway correctly routes to Ollama.
    """
    record = REGISTRY[LLMMode.LOCAL]
    assert record.model.startswith("ollama_chat/"), (
        f"LOCAL model string must start with 'ollama_chat/'; got {record.model!r}.  "
        f"Spec §7: Local (Ollama): model='ollama_chat/<name>'."
    )


def test_local_base_url_is_loopback() -> None:
    """LOCAL mode's base_url is the Ollama loopback address (spec §7 default)."""
    record = REGISTRY[LLMMode.LOCAL]
    assert record.base_url is not None, "LOCAL mode must have a base_url (Ollama endpoint)"
    loopback_markers = ("localhost", "127.0.0.1", "::1")
    assert any(h in str(record.base_url) for h in loopback_markers), (
        f"LOCAL base_url must be a loopback address; got {record.base_url!r}.  "
        f"Spec §7: api_base='http://localhost:11434'."
    )


def test_openrouter_model_string_uses_openrouter_prefix() -> None:
    """OPENROUTER mode's litellm model string uses the 'openrouter/' prefix (spec §7)."""
    record = REGISTRY[LLMMode.OPENROUTER]
    assert record.model.startswith("openrouter/"), (
        f"OPENROUTER model string must start with 'openrouter/'; got {record.model!r}.  "
        f"Spec §7: OpenRouter: model='openrouter/<name>'."
    )


# ── CLAUDE_HEADLESS: ToS-gray / brittle caveat (ADR 0091 §2) ──────────────────────────


def test_claude_headless_badge_carries_tos_or_brittle_caveat() -> None:
    """CLAUDE_HEADLESS badge/confidentiality carries the ToS-gray / brittle caveat.

    ADR 0091 §2: 'CLAUDE_HEADLESS carries a documented ToS-gray / brittle caveat: a
    consumer Claude subscription used programmatically may break or violate terms; the
    clean external route is the Anthropic API.'  This caveat MUST be visible in the
    badge or confidentiality label that the operator sees at selection time.

    Checks for any of: 'ToS', 'terms', 'Terms', 'brittle', 'caveat' — case-insensitive.
    """
    record = REGISTRY[LLMMode.CLAUDE_HEADLESS]
    combined = (record.badge + " " + record.confidentiality).lower()
    caveat_markers = ("tos", "terms", "brittle", "caveat", "gray", "grey")
    assert any(m in combined for m in caveat_markers), (
        f"CLAUDE_HEADLESS badge/confidentiality must carry the ToS-gray/brittle caveat.  "
        f"badge={record.badge!r}, confidentiality={record.confidentiality!r}.  "
        f"ADR 0091 §2: the caveat must be visible to the operator at selection time.  "
        f"Expected one of {caveat_markers!r} (case-insensitive) in the combined text."
    )


# ── INV-S2-LABEL: construction-time invariant ──────────────────────────────────────────


@pytest.mark.parametrize("bad_label", ["", None])
def test_empty_confidentiality_label_raises_at_construction(bad_label: Any) -> None:
    """INV-S2-LABEL: ModeRecord with empty/None confidentiality CANNOT be instantiated.

    The label is a required, non-empty field.  This is a construction-time guard so no
    code path can register a mode without a confidentiality label — making INV-S2-LABEL
    structural rather than aspirational (ADR 0091 §2 rejected alternative: 'Per-mode
    confidentiality as a doc note').

    # CONTRACT: ModeRecord(model=..., base_url=..., confidentiality=<empty/None>,
    #            badge=..., data_left_perimeter=...) raises ValueError or TypeError.
    """
    with pytest.raises((ValueError, TypeError)):
        ModeRecord(
            model="ollama_chat/llama3.2",
            base_url="http://localhost:11434",
            confidentiality=bad_label,
            badge="Some Badge",
            data_left_perimeter=False,
        )


@pytest.mark.parametrize("bad_label", ["", None])
def test_empty_badge_raises_at_construction(bad_label: Any) -> None:
    """INV-S2-LABEL: ModeRecord with empty/None badge CANNOT be instantiated.

    The badge is the operator-visible human-readable label.  An empty badge would leave
    the operator without context at selection time.

    # CONTRACT: ModeRecord(model=..., base_url=..., confidentiality=...,
    #            badge=<empty/None>, data_left_perimeter=...) raises ValueError or TypeError.
    """
    with pytest.raises((ValueError, TypeError)):
        ModeRecord(
            model="ollama_chat/llama3.2",
            base_url="http://localhost:11434",
            confidentiality="Confidential — no egress",
            badge=bad_label,
            data_left_perimeter=False,
        )
