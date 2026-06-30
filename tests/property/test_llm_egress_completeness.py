"""PRIMARY property test — Phase-3 Gate S2: LiteLLM egress completeness.

This is the ``@given`` oracle for:

- **INV-S2-EGRESS**: for EVERY call across all three modes (including per-call overrides),
  the egress record is emitted **before** ``litellm.completion`` is invoked; the spy is
  never reached without a preceding record.  Even when the spy raises, the record exists
  (audited attempt).

- **INV-S2-LABEL**: over every ``LLMMode`` value, the resolved registry record has a
  non-empty confidentiality status AND a non-empty badge; constructing a ``ModeRecord``
  with an empty/None confidentiality label raises at construction.

- **INV-S2-DEFAULT (no-egress metamorphic)**: ``LOCAL`` → ``data_left_perimeter is False``
  and the target host is loopback; ``CLAUDE_HEADLESS`` / ``OPENROUTER`` → ``True``.
  Confidential mode in ⇒ no-egress flag out — a builder cannot fake this by hardcoding
  ``False``: the property is asserted over all three modes.

RED TODAY:
    ``ModuleNotFoundError: No module named 'worldmonitor.llm.modes'``
    (modes.py / egress_log.py / gateway.py do not exist yet)

──────────────────────────────────────────────────────────────────────────────────────────
BUILDER CONTRACTS — the implementation MUST match these names exactly:

    worldmonitor.llm.modes
        LLMMode(enum.Enum)
            LOCAL, CLAUDE_HEADLESS, OPENROUTER

        ModeRecord                           # CONTRACT: export as ModeRecord
            model: str                       # litellm model string e.g. "ollama_chat/llama3.2"
            base_url: str | None             # provider base URL; None if not needed
            confidentiality: str             # non-empty; raises ValueError/TypeError if "" or None
            badge: str                       # non-empty human-readable label
            data_left_perimeter: bool        # True iff data leaves the perimeter

        REGISTRY: dict[LLMMode, ModeRecord]  # CONTRACT: exactly 3 entries, keyed by LLMMode

    worldmonitor.llm.egress_log
        EgressRecord                         # CONTRACT: MUTABLE dataclass
            mode: LLMMode
            confidentiality: str
            target_host: str                 # loopback ("localhost"/"127.0.0.1") for LOCAL
            data_left_perimeter: bool
            model: str
            timestamp: datetime
            usage: object | None             # None before call; enriched in-place after success
            caller_tag: str

        emit(record: EgressRecord) -> None   # CONTRACT: called via module reference in gateway.py
            # The gateway must NOT do `from worldmonitor.llm.egress_log import emit` (direct
            # import would shadow the monkeypatch).  Use `egress_log.emit(record)` so patching
            # `worldmonitor.llm.egress_log.emit` intercepts every gateway call.

    worldmonitor.llm.gateway
        LLMGateway(settings: Settings)
            chat(messages, *, mode=None) -> Any
                # CONTRACT: calls emit(record) BEFORE litellm.completion(...)
                # CONTRACT: `import litellm` (whole module), calls `litellm.completion(...)`.
                # Do NOT `from litellm import completion` — breaks the monkeypatch.
                # Raises LLMGatewayError (not the raw litellm exception) on provider failure.

        LLMGatewayError(Exception)           # CONTRACT: the gateway's own typed error class
──────────────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# ── Imports that MUST fail until builder delivers llm/*.py ──────────────────────────────
# Every test in this file is RED (ModuleNotFoundError at collection time) until the
# builder creates the three modules.  Do NOT stub these — the error is the correct red.
from worldmonitor.llm.egress_log import EgressRecord  # noqa: F401
from worldmonitor.llm.gateway import LLMGateway, LLMGatewayError  # noqa: F401
from worldmonitor.llm.modes import REGISTRY, LLMMode, ModeRecord  # noqa: F401
from worldmonitor.settings import Settings

# ── Hypothesis settings ─────────────────────────────────────────────────────────────────
# deadline=None: per repo convention (builder-flake lesson) — gateway construction +
# monkeypatching can exceed the 200ms per-example default on a loaded runner.

_SETTINGS = hyp_settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# ── Helpers ─────────────────────────────────────────────────────────────────────────────


def _make_fake_response(model: str = "ollama_chat/llama3.2") -> MagicMock:
    """Fake litellm.ModelResponse with the OpenAI-shaped structure (spec §7).

    CONTRACT: .choices[0].message.content, .usage.{prompt_tokens,completion_tokens,
    total_tokens}, .model — the gateway reads these on success to enrich the record.
    """
    resp = MagicMock()
    resp.model = model
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "fake-hypothesis-response"
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = 7
    resp.usage.completion_tokens = 13
    resp.usage.total_tokens = 20
    return resp


def _make_test_settings(llm_mode: str = "local") -> Settings:
    """Minimal Settings for gateway tests — no .env read (CI-safe, per test_settings.py).

    The new llm_* fields are additive (ADR 0091); pydantic ignores them until the builder
    adds them to Settings.  After the builder adds the fields, the kwargs take effect.
    """
    return Settings(  # type: ignore[call-arg]
        llm_mode=llm_mode,
        llm_ollama_model="llama3.2",
        llm_ollama_base_url="http://localhost:11434",
        llm_openrouter_model="openai/gpt-4o",
        llm_openrouter_api_key="test-fake-or-key",
        llm_claude_binary="claude",
        llm_claude_model_label="claude-test",
        llm_claude_timeout_seconds=30,
        llm_egress_log_enabled=True,
        _env_file=None,
    )


# ── Strategies ──────────────────────────────────────────────────────────────────────────

_ALL_MODES = st.sampled_from(list(LLMMode))

_CALL_SPEC = st.fixed_dictionaries(
    {
        "mode": _ALL_MODES,
        "provider_raises": st.booleans(),
    }
)

# Sequences of 1–5 calls covering mixed-mode + mixed-success scenarios.
_CALL_SEQ = st.lists(_CALL_SPEC, min_size=1, max_size=5)


# ── PROPERTY 1: INV-S2-EGRESS + INV-S2-DEFAULT ─────────────────────────────────────────


@given(call_specs=_CALL_SEQ)
@_SETTINGS
def test_egress_record_emitted_before_every_provider_call(
    call_specs: list[dict[str, Any]],
) -> None:
    """INV-S2-EGRESS + INV-S2-DEFAULT: ordering + no-egress metamorphic over call sequences.

    Over generated call sequences across all three modes (per-call override via mode=):

    (a) ORDERING — the egress record MUST be emitted BEFORE ``litellm.completion`` is called.
        A shared event list records ``"log"`` on emit and ``"provider"`` on the spy; asserts
        ``events.index("log") < events.index("provider")`` for every call.

    (b) AUDIT COMPLETENESS — even when the provider spy raises (simulating a network/timeout
        failure), the record MUST still be present.  The gateway must audit the attempt before
        contacting the provider.

    (c) NO-EGRESS METAMORPHIC — for every call the egress record's ``data_left_perimeter``
        must equal ``(mode != LLMMode.LOCAL)``.  LOCAL is the only confidential mode; every
        external mode must set the flag to ``True``.

    (d) LOOPBACK TARGET HOST — when ``data_left_perimeter is False`` (LOCAL), the
        ``target_host`` in the record must be a loopback address (localhost / 127.0.0.1).

    A tautology CANNOT pass this:
    - Emitting after the provider call ⇒ fails (b) on provider-raises cases or (a) ordering.
    - Skipping the emit ⇒ fails (a) "log in events" assertion.
    - Hardcoding data_left_perimeter=False ⇒ fails (c) for CLAUDE_HEADLESS / OPENROUTER.
    """
    events: list[str] = []  # "log" | "provider" — cleared before each call
    captured: list[Any] = []  # EgressRecord captured by reference (mutable — usage enriched)
    current: dict[str, Any] = {"raises": False}

    def _emit_spy(record: Any) -> None:
        events.append("log")
        captured.append(record)

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        events.append("provider")
        if current["raises"]:
            raise RuntimeError("fake-provider-failure-in-hypothesis")
        return _make_fake_response()

    gateway = LLMGateway(_make_test_settings())

    # CONTRACT: `worldmonitor.llm.egress_log.emit` is the patch target.
    # CONTRACT: `litellm.completion` is the patch target (not a direct-imported ref).
    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_emit_spy),
        patch("litellm.completion", side_effect=_provider_spy),
    ):
        for i, spec in enumerate(call_specs):
            events.clear()
            captured.clear()
            current["raises"] = spec["provider_raises"]

            with contextlib.suppress(LLMGatewayError):
                gateway.chat(
                    messages=[{"role": "user", "content": "hypothesis-test"}],
                    mode=spec["mode"],
                )

            # (a) + (b): a record MUST be emitted for every call (attempt audited).
            assert "log" in events, (
                f"call #{i} mode={spec['mode']!r} raises={spec['provider_raises']}: "
                f"no egress record was emitted.  INV-S2-EGRESS requires the gateway to "
                f"call emit() before contacting the provider.  events={events!r}"
            )

            # (a) ORDERING: log entry must precede the provider call.
            if "provider" in events:
                log_idx = events.index("log")
                prov_idx = events.index("provider")
                assert log_idx < prov_idx, (
                    f"call #{i} mode={spec['mode']!r}: egress record emitted AFTER the "
                    f"provider call (log@{log_idx} >= provider@{prov_idx}).  "
                    f"INV-S2-EGRESS: the record MUST be written before the provider is "
                    f"contacted so a failing/timing-out call is still audited.  "
                    f"events={events!r}"
                )

            # (c) NO-EGRESS METAMORPHIC: data_left_perimeter matches the mode.
            if captured:
                rec = captured[0]  # record is emitted once (CONTRACT: mutable, enriched in-place)
                expected_left_perimeter = spec["mode"] != LLMMode.LOCAL
                assert rec.data_left_perimeter is expected_left_perimeter, (
                    f"call #{i} mode={spec['mode']!r}: data_left_perimeter mismatch.  "
                    f"Expected {expected_left_perimeter!r} (LOCAL→False, externals→True), "
                    f"got {rec.data_left_perimeter!r}.  "
                    f"INV-S2-DEFAULT: LOCAL is the ONLY mode where data stays on-perimeter."
                )

                # (d) LOOPBACK TARGET HOST for LOCAL mode.
                if spec["mode"] is LLMMode.LOCAL:
                    loopback = ("localhost", "127.0.0.1", "::1")
                    assert any(h in rec.target_host for h in loopback), (
                        f"call #{i} LOCAL mode: target_host must be loopback; "
                        f"got {rec.target_host!r}.  "
                        f"INV-S2-DEFAULT: LOCAL routes to Ollama on the loopback interface."
                    )


# ── PROPERTY 2: INV-S2-LABEL — every mode has non-empty confidentiality and badge ───────


@given(mode=_ALL_MODES)
@_SETTINGS
def test_all_modes_have_non_empty_confidentiality_and_badge(mode: LLMMode) -> None:
    """INV-S2-LABEL: REGISTRY[mode].confidentiality and .badge are ALWAYS non-empty.

    The selector can NEVER surface a mode whose confidentiality status is unknown.
    This property runs over all three ``LLMMode`` values (Hypothesis generates each one);
    a builder cannot pass it by leaving even one mode unlabeled.

    (ADR 0091 §2: 'Per-mode confidentiality as a doc note rather than a construct-time
    field' was explicitly REJECTED because a doc note can be omitted at registration time.)
    """
    record = REGISTRY[mode]

    assert record.confidentiality, (
        f"REGISTRY[{mode!r}].confidentiality is empty/falsy: {record.confidentiality!r}.  "
        f"INV-S2-LABEL: every registered mode MUST carry a non-empty confidentiality status "
        f"so the selector can never present a mode whose status is unknown."
    )
    assert isinstance(record.confidentiality, str), (
        f"REGISTRY[{mode!r}].confidentiality must be a str; "
        f"got {type(record.confidentiality).__name__!r}"
    )
    assert record.badge, (
        f"REGISTRY[{mode!r}].badge is empty/falsy: {record.badge!r}.  "
        f"INV-S2-LABEL: every registered mode MUST carry a non-empty human-readable badge."
    )
    assert isinstance(record.badge, str), (
        f"REGISTRY[{mode!r}].badge must be a str; got {type(record.badge).__name__!r}"
    )


# ── STRUCTURAL: empty/None confidentiality label raises at ModeRecord construction ───────


@pytest.mark.parametrize("bad_label", ["", None])
def test_empty_confidentiality_label_raises_at_construction(bad_label: Any) -> None:
    """INV-S2-LABEL (structural): ModeRecord with empty/None confidentiality CANNOT be created.

    Making the label a REQUIRED, non-empty, CONSTRUCTION-TIME field is what makes
    INV-S2-LABEL structural rather than aspirational: no code path can register a mode
    without a confidentiality label because it is impossible to construct the record.

    # CONTRACT: ModeRecord(model=..., base_url=..., confidentiality=<bad>, badge=...,
    #            data_left_perimeter=...) raises ValueError or TypeError.
    # The exact exception type is implementation-defined (dataclass __post_init__ raises
    # ValueError; pydantic ValidationError inherits from ValueError).
    """
    with pytest.raises((ValueError, TypeError)):
        ModeRecord(
            model="ollama_chat/llama3.2",
            base_url="http://localhost:11434",
            confidentiality=bad_label,
            badge="Test Badge",
            data_left_perimeter=False,
        )
