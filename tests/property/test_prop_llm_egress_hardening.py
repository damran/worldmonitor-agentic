"""Gate L1-a PRIMARY invariants — P-EGRESS-FAILCLOSED + P-EGRESS-USAGE (ADR 0104 items 2 + 3,
spec `docs/reviews/GATE_L1_LLM_EGRESS_HARDENING_SPEC.md` §2.3–2.4, §2.6).

Reuses the spy/monkeypatch idiom and ``_make_test_settings`` helper from
``tests/property/test_llm_egress_completeness.py`` (patch ``worldmonitor.llm.egress_log.emit`` +
``litellm.completion``); that file stays byte-unchanged.

────────────────────────────────────────────────────────────────────────────────────────
P-EGRESS-FAILCLOSED
────────────────────────────────────────────────────────────────────────────────────────
**NAME:** P-EGRESS-FAILCLOSED.

**STATEMENT (INV-FAILCLOSED):** for an EXTERNAL mode (``REGISTRY[mode].data_left_perimeter is
True``) with ``llm_egress_log_enabled=False``, ``gateway.chat()`` raises ``LLMGatewayError`` and
``litellm.completion`` is NEVER called and NO ``EgressRecord`` is emitted; for LOCAL with logging
disabled, and for ANY mode with logging enabled, the call proceeds and ``litellm.completion`` IS
called.

**GENERATOR:** ``@given`` over ``mode ∈ LLMMode`` × ``egress_enabled ∈ {True, False}``. Settings
built via ``_make_test_settings(llm_mode=mode.value, llm_egress_log_enabled=egress_enabled)``.
Externality is read from ``REGISTRY[mode].data_left_perimeter`` (never hardcoded) so the property
holds for whichever modes the locked ADR-0091 registry actually marks external.

**ORACLE:** a provider-call spy (``litellm.completion``) and an emit spy
(``worldmonitor.llm.egress_log.emit``) record call counts. For ``(external, disabled)``:
``pytest.raises(LLMGatewayError)``, provider-spy count == 0, emit-spy count == 0 (nothing left
the perimeter, not even an audited attempt). For ``(external, enabled)``, ``(LOCAL, enabled)``,
``(LOCAL, disabled)``: no raise, provider-spy count == 1.

**NON-VACUITY:** an always-raise implementation fails the LOCAL-disabled and external-enabled
cases (it would incorrectly block calls that should proceed); today's never-raise behaviour (no
fail-closed check at all) fails the external-disabled case (it lets litellm.completion run with
zero audit). RED TODAY: no fail-closed check exists in ``gateway.py`` yet.

────────────────────────────────────────────────────────────────────────────────────────
P-EGRESS-USAGE
────────────────────────────────────────────────────────────────────────────────────────
**NAME:** P-EGRESS-USAGE.

**STATEMENT (INV-USAGE):** a successful call whose response carries a ``usage`` object produces,
in order, (a) a PRE-call record with ``usage is None`` emitted BEFORE ``litellm.completion``, and
(b) a POST-call record with ``usage is not None`` carrying the response's token counts emitted
AFTER ``litellm.completion``; and ``egress_log.emit``'s serialization actually exposes those
token counts (asserted via ``caplog``), proving ``emit`` does more than hold ``usage`` on the
object.

**GENERATOR:** ``@given`` over ``mode ∈ LLMMode`` (logging always enabled — this property is
about usage capture, not about the fail-closed gate) × generated ``(prompt_tokens,
completion_tokens, total_tokens)`` integers attached to a fake ``ModelResponse.usage``.

**ORACLE:** an event list + captured-records list (as in the existing completeness test) assert
``events == ["emit", "provider", "emit"]`` (pre-emit < provider < post-emit); exactly 2 records
are captured; ``captured[0].usage is None``; ``captured[1].usage`` carries the generated token
counts; AND at least one ``caplog``-captured log line (fired by the REAL ``egress_log.emit``,
which this test's spy WRAPS rather than replaces, so the genuine ``logger.info`` call actually
fires) exposes all three generated token counts — proving ``emit`` serializes usage rather than
merely receiving a record whose ``.usage`` happens to be set.

**NON-VACUITY:** today's single-emit code fails immediately (``events`` has no second "emit";
``len(captured) == 1``, not 2). An ``emit`` that serializes nothing (usage held only on the
Python object, never logged) fails the caplog-exposure assertion even after the gateway grows a
second emit call.
"""

from __future__ import annotations

import copy
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

import worldmonitor.llm.egress_log as egress_log
from worldmonitor.llm.gateway import LLMGateway, LLMGatewayError
from worldmonitor.llm.modes import REGISTRY, LLMMode
from worldmonitor.settings import Settings

_HYP_SETTINGS = hyp_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

_HYP_SETTINGS_WITH_FIXTURE = hyp_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ── Helpers (mirror test_llm_egress_completeness.py + test_llm_gateway.py) ──


def _make_test_settings(
    *, llm_mode: str = "local", llm_egress_log_enabled: bool = True
) -> Settings:
    """Minimal Settings for gateway tests — no .env read (CI-safe)."""
    return Settings(  # type: ignore[call-arg]
        llm_mode=llm_mode,
        llm_ollama_model="llama3.2",
        llm_ollama_base_url="http://localhost:11434",
        llm_openrouter_model="openai/gpt-4o",
        llm_openrouter_api_key="test-fake-or-key",
        llm_claude_binary="claude",
        llm_claude_model_label="claude-test",
        llm_claude_timeout_seconds=30,
        llm_egress_log_enabled=llm_egress_log_enabled,
        _env_file=None,
    )


def _make_fake_response(
    prompt_tokens: int = 7, completion_tokens: int = 13, total_tokens: int = 20
) -> MagicMock:
    """Fake litellm.ModelResponse — OpenAI-shaped (spec §7 of the S2 gate)."""
    resp = MagicMock()
    resp.model = "fake-model"
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "fake-property-test-response"
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.usage.total_tokens = total_tokens
    return resp


_ALL_MODES = st.sampled_from(list(LLMMode))
_BOOLS = st.booleans()
_TOKEN_INT = st.integers(min_value=1, max_value=999_999)


# ── P-EGRESS-FAILCLOSED ─────────────────────────────────────────────────────────────────


@given(mode=_ALL_MODES, egress_enabled=_BOOLS)
@_HYP_SETTINGS
def test_external_egress_without_audit_fails_closed_else_call_proceeds(
    mode: LLMMode, egress_enabled: bool
) -> None:
    """P-EGRESS-FAILCLOSED / INV-FAILCLOSED: external+audit-disabled refuses the call;
    every other (mode, egress_enabled) combination proceeds to the provider.
    """
    settings = _make_test_settings(llm_mode=mode.value, llm_egress_log_enabled=egress_enabled)
    external = REGISTRY[mode].data_left_perimeter

    provider_calls: list[Any] = []
    emit_calls: list[Any] = []

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        provider_calls.append((args, kwargs))
        return _make_fake_response()

    def _emit_spy(record: Any) -> None:
        emit_calls.append(record)

    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_emit_spy),
        patch("litellm.completion", side_effect=_provider_spy),
    ):
        gateway = LLMGateway(settings)

        if external and not egress_enabled:
            with pytest.raises(LLMGatewayError) as exc_info:
                gateway.chat(messages=[{"role": "user", "content": "fail-closed-probe"}])
            assert not isinstance(exc_info.value, type(None))  # the raise itself is the oracle

            assert len(provider_calls) == 0, (
                f"mode={mode!r} (external, audit-disabled): litellm.completion must NEVER be "
                f"called when a durable audit cannot be written; got {len(provider_calls)} "
                f"call(s).  INV-FAILCLOSED (ADR 0104 item 3): no durable audit => no external "
                f"egress."
            )
            assert len(emit_calls) == 0, (
                f"mode={mode!r} (external, audit-disabled): no EgressRecord may be emitted "
                f"(nothing left the perimeter, so nothing is audited); got "
                f"{len(emit_calls)} record(s)."
            )
        else:
            gateway.chat(messages=[{"role": "user", "content": "proceed-probe"}])
            assert len(provider_calls) == 1, (
                f"mode={mode!r} external={external!r} egress_enabled={egress_enabled!r}: "
                f"expected exactly 1 litellm.completion call (this combination must proceed); "
                f"got {len(provider_calls)}.  INV-FAILCLOSED: only (external AND "
                f"audit-disabled) refuses; LOCAL stays freely toggle-able, and any mode with "
                f"logging enabled proceeds."
            )


# ── P-EGRESS-USAGE ──────────────────────────────────────────────────────────────────────


def _record_exposes_token_counts(
    record: logging.LogRecord, prompt_tokens: int, completion_tokens: int, total_tokens: int
) -> bool:
    """True iff this caplog LogRecord's formatted message OR its extra=-derived attributes
    expose all three generated token counts — i.e. egress_log.emit SERIALIZED the usage,
    not merely received a record whose .usage attribute happened to be set.
    """
    message = record.getMessage()
    if all(str(value) in message for value in (prompt_tokens, completion_tokens, total_tokens)):
        return True
    numeric_extra_values = {
        value
        for key, value in vars(record).items()
        if isinstance(value, int) and ("token" in key.lower() or "usage" in key.lower())
    }
    return {prompt_tokens, completion_tokens, total_tokens}.issubset(numeric_extra_values)


@given(
    mode=_ALL_MODES, prompt_tokens=_TOKEN_INT, completion_tokens=_TOKEN_INT, total_tokens=_TOKEN_INT
)
@_HYP_SETTINGS_WITH_FIXTURE
def test_successful_call_emits_pre_and_post_records_with_serialized_usage(
    caplog: pytest.LogCaptureFixture,
    mode: LLMMode,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
) -> None:
    """P-EGRESS-USAGE / INV-USAGE: pre-call (no usage) then post-call (usage) emit, in
    order around the provider call; the post-call emit's log line actually exposes the
    generated token counts (via caplog), proving egress_log.emit serializes usage.

    The emit spy WRAPS the real ``egress_log.emit`` (captured before patching) rather than
    replacing it, so the genuine ``logger.info(...)`` call still fires and caplog can
    observe what the real serialization logic does with ``record.usage``.
    """
    caplog.clear()
    caplog.set_level(logging.INFO, logger="worldmonitor.llm.egress_log")

    settings = _make_test_settings(llm_mode=mode.value, llm_egress_log_enabled=True)

    real_emit = egress_log.emit  # captured BEFORE patching -- the true implementation

    events: list[str] = []
    captured: list[Any] = []

    def _wrapping_emit_spy(record: Any) -> None:
        events.append("emit")
        # Snapshot the state AT EMIT TIME — the gateway mutates one record in place, so a live
        # reference would show only the final (enriched) state after chat() returns.
        captured.append(copy.copy(record))
        real_emit(record)  # let the REAL emit() run on the LIVE record so logger.info(...) fires

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        events.append("provider")
        return _make_fake_response(prompt_tokens, completion_tokens, total_tokens)

    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_wrapping_emit_spy),
        patch("litellm.completion", side_effect=_provider_spy),
    ):
        gateway = LLMGateway(settings)
        gateway.chat(messages=[{"role": "user", "content": "usage-property-probe"}])

    assert events == ["emit", "provider", "emit"], (
        f"mode={mode!r}: expected event order ['emit', 'provider', 'emit'] (pre-call "
        f"completeness emit, then the provider call, then the post-call usage emit); "
        f"got {events!r}.  INV-USAGE / INV-S2-EGRESS ordering (ADR 0104 item 2)."
    )
    assert len(captured) == 2, (
        f"mode={mode!r}: expected exactly 2 emitted EgressRecords on a successful call "
        f"(pre-call + post-call, ADR 0104 item 2); got {len(captured)}."
    )

    pre_record, post_record = captured[0], captured[1]

    assert pre_record.usage is None, (
        f"mode={mode!r}: the PRE-call record's usage must be None (the provider has not "
        f"responded yet); got {pre_record.usage!r}."
    )
    assert post_record.usage is not None, (
        f"mode={mode!r}: the POST-call record's usage must be populated with the "
        f"response's token counts; got None."
    )
    assert getattr(post_record.usage, "prompt_tokens", None) == prompt_tokens, (
        f"mode={mode!r}: post-call record.usage.prompt_tokens expected {prompt_tokens}, "
        f"got {getattr(post_record.usage, 'prompt_tokens', None)!r}."
    )
    assert getattr(post_record.usage, "completion_tokens", None) == completion_tokens, (
        f"mode={mode!r}: post-call record.usage.completion_tokens expected "
        f"{completion_tokens}, got {getattr(post_record.usage, 'completion_tokens', None)!r}."
    )
    assert getattr(post_record.usage, "total_tokens", None) == total_tokens, (
        f"mode={mode!r}: post-call record.usage.total_tokens expected {total_tokens}, "
        f"got {getattr(post_record.usage, 'total_tokens', None)!r}."
    )

    exposed = any(
        _record_exposes_token_counts(rec, prompt_tokens, completion_tokens, total_tokens)
        for rec in caplog.records
    )
    assert exposed, (
        f"mode={mode!r} tokens=(prompt={prompt_tokens}, completion={completion_tokens}, "
        f"total={total_tokens}): no caplog-captured log line exposes the token usage "
        f"counts.  INV-USAGE requires egress_log.emit() to SERIALIZE record.usage (in the "
        f"log line and/or extra=) once populated -- not just hold it on the Python object.  "
        f"captured messages={[rec.getMessage() for rec in caplog.records]!r}"
    )
