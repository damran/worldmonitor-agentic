"""Unit tests — Gate F2: durable, append-only LLM-egress audit (ADR 0105), §4 of
``docs/reviews/GATE_F2_DURABLE_EGRESS_AUDIT_SPEC.md``.

Covers:
- ``fingerprint_messages`` golden cases (known input -> stable digest; empty messages;
  unicode; a hostile non-serializable value does not raise).
- ``build_attempt_row`` / ``build_completed_row`` column mapping (phase,
  fingerprint/manifest vs tokens, ``call_id`` carried across both rows).
- ``write_row`` calls ``add`` + ``commit`` + ``close`` (in that order) and propagates a
  commit failure while STILL closing the session (finally-block discipline).
- P-DORMANT (metamorphic, may live here per the spec): with the default-off
  ``llm_egress_durable_enabled=False``, the durable session factory is NEVER invoked, across
  every mode and success/failure.

RED TODAY:
    ``ModuleNotFoundError: No module named 'worldmonitor.llm.egress_audit'``
    (``egress_audit.py`` does not exist yet.)

──────────────────────────────────────────────────────────────────────────────────────────
BUILDER CONTRACTS — the implementation MUST match these names exactly (spec §2.2-2.5):

    worldmonitor.llm.egress_audit
        fingerprint_messages(messages: list[dict]) -> str
            # canonical = json.dumps(messages, sort_keys=True, separators=(",", ":"),
            #                        ensure_ascii=False, default=str)
            # return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        build_attempt_row(call_id, record, fingerprint, entity_ids) -> LlmEgressRecord
            # phase="attempt"; content_fingerprint=fingerprint; entity_manifest=entity_ids;
            # token columns None; fresh uuid4 id; context columns copied from `record`.
        build_completed_row(call_id, record) -> LlmEgressRecord
            # phase="completed"; content_fingerprint/entity_manifest None; token columns from
            # record.usage via getattr (defensive); fresh uuid4 id.
        write_row(session_factory, row) -> None
            # session = session_factory(); try: add+commit; finally: close(). Propagates a
            # commit failure.

    worldmonitor.settings.Settings
        llm_egress_durable_enabled: bool = False
──────────────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# ── Imports that MUST fail until the builder creates llm/egress_audit.py + wires the
# session_factory ctor seam + the durable flag (collection-time ModuleNotFoundError).
from worldmonitor.llm import egress_audit
from worldmonitor.llm.egress_log import EgressRecord
from worldmonitor.llm.gateway import LLMGateway, LLMGatewayError
from worldmonitor.llm.modes import REGISTRY, LLMMode
from worldmonitor.settings import Settings

_HYP_SETTINGS = hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ── Helpers ─────────────────────────────────────────────────────────────────────────────


def _make_test_settings(
    *,
    llm_mode: str = "local",
    llm_egress_log_enabled: bool = True,
    llm_egress_durable_enabled: bool = False,
    **extra: Any,
) -> Settings:
    """Minimal Settings for durable-audit tests — no .env read (CI-safe).

    Defaults ``llm_egress_durable_enabled=False`` in THIS helper (unlike the property file's
    default of True) because most callers in this file are exercising P-DORMANT / the
    default-off posture; individual tests override where needed.
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
        llm_egress_log_enabled=llm_egress_log_enabled,
        llm_egress_durable_enabled=llm_egress_durable_enabled,
        _env_file=None,
        **extra,
    )


def _make_fake_response(
    prompt_tokens: int = 10, completion_tokens: int = 20, total_tokens: int = 30
) -> MagicMock:
    resp = MagicMock()
    resp.model = "fake-model"
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "unit-test durable response"
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.usage.total_tokens = total_tokens
    return resp


def _make_record(*, usage: object | None = None) -> EgressRecord:
    return EgressRecord(
        mode=LLMMode.OPENROUTER,
        confidentiality=REGISTRY[LLMMode.OPENROUTER].confidentiality,
        target_host="openrouter.ai",
        data_left_perimeter=True,
        model="openrouter/openai/gpt-4o",
        timestamp=datetime.now(tz=UTC),
        caller_tag="unit-test-caller",
        usage=usage,
    )


# ── fingerprint_messages — golden cases ────────────────────────────────────────────────


def test_fingerprint_known_input_matches_golden_digest() -> None:
    """Golden case: a fixed input, hashed via the SPECIFIED canonicalization
    (``json.dumps(messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    default=str)`` -> sha256 hex), must match a pre-computed digest independent of the
    implementation under test.
    """
    messages = [{"role": "user", "content": "hello"}]
    expected = "d98167dd28f22e330824942ba4d4ce217c2411a0d1141d60b40fe4cb8dc0d232"
    assert egress_audit.fingerprint_messages(messages) == expected, (
        f"fingerprint_messages({messages!r}) must equal the golden digest {expected!r} "
        "(sha256 of canonical-JSON: sort_keys=True, separators=(',', ':'), "
        "ensure_ascii=False)."
    )


def test_fingerprint_empty_messages_matches_golden_digest() -> None:
    """Golden case: an empty message list -> sha256('[]')."""
    expected = "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    assert egress_audit.fingerprint_messages([]) == expected, (
        f"fingerprint_messages([]) must equal the golden digest {expected!r}."
    )


def test_fingerprint_unicode_content_matches_golden_digest() -> None:
    """Golden case: non-ASCII content must be UTF-8 encoded (``ensure_ascii=False``), not
    escaped, before hashing.
    """
    messages = [{"role": "user", "content": "héllo wörld — 日本語 emoji 😀"}]
    expected = "a66898d121a49b8c3c03602987d9aa3593872ff9002201fff7a2fb0c85a1d341"
    assert egress_audit.fingerprint_messages(messages) == expected, (
        f"fingerprint_messages({messages!r}) must equal the golden digest {expected!r} "
        "(ensure_ascii=False -> raw UTF-8 bytes, not \\uXXXX escapes)."
    )


def test_fingerprint_hostile_non_serializable_value_does_not_raise() -> None:
    """A message value that is not natively JSON-serializable (a custom object) must be
    handled via ``default=str`` and must NOT raise.
    """

    class _Hostile:
        def __str__(self) -> str:
            return "<hostile-value>"

    messages = [{"role": "user", "content": _Hostile()}]
    expected = "7dbb2c7295047bad0e66e02cf03c179425d100a1148661bd769f458c961756b5"
    digest = egress_audit.fingerprint_messages(messages)
    assert digest == expected, (
        f"fingerprint_messages on a hostile non-serializable value must equal the golden "
        f"digest {expected!r} (str(value) fed through the same canonicalization); "
        f"got {digest!r}."
    )
    assert re.fullmatch(r"[0-9a-f]{64}", digest), f"digest must be 64-char hex; got {digest!r}"


# ── build_attempt_row / build_completed_row — column mapping ───────────────────────────


def test_build_attempt_row_column_mapping() -> None:
    """build_attempt_row: phase='attempt'; fingerprint + manifest set; token columns None."""
    record = _make_record(usage=None)
    call_id = str(uuid.uuid4())
    fingerprint = "a" * 64
    entity_ids = ["Q42", "opensanctions:abcd1234"]

    row = egress_audit.build_attempt_row(call_id, record, fingerprint, entity_ids)

    assert row.phase == "attempt", f"expected phase='attempt'; got {row.phase!r}"
    assert row.call_id == call_id, f"expected call_id={call_id!r}; got {row.call_id!r}"
    assert row.content_fingerprint == fingerprint, (
        f"expected content_fingerprint={fingerprint!r}; got {row.content_fingerprint!r}"
    )
    assert row.entity_manifest == entity_ids, (
        f"expected entity_manifest={entity_ids!r}; got {row.entity_manifest!r}"
    )
    assert row.prompt_tokens is None, "attempt row must have prompt_tokens=None"
    assert row.completion_tokens is None, "attempt row must have completion_tokens=None"
    assert row.total_tokens is None, "attempt row must have total_tokens=None"
    assert row.mode == record.mode.value, f"expected mode={record.mode.value!r}"
    assert row.confidentiality == record.confidentiality
    assert row.target_host == record.target_host
    assert row.data_left_perimeter == record.data_left_perimeter
    assert row.model == record.model
    assert row.caller_tag == record.caller_tag
    assert row.id, "row must carry a non-empty fresh id"


def test_build_attempt_row_entity_ids_none_when_not_declared() -> None:
    """build_attempt_row: entity_ids=None (the /v1 wire-caller default) -> entity_manifest
    is honestly recorded as None, not faked as an empty list.
    """
    record = _make_record(usage=None)
    row = egress_audit.build_attempt_row(str(uuid.uuid4()), record, "b" * 64, None)
    assert row.entity_manifest is None, (
        f"undeclared entity_ids must produce entity_manifest=None (SF-2, not []); "
        f"got {row.entity_manifest!r}"
    )


def test_build_completed_row_column_mapping() -> None:
    """build_completed_row: phase='completed'; fingerprint/manifest None; tokens from usage."""
    usage = MagicMock()
    usage.prompt_tokens = 11
    usage.completion_tokens = 22
    usage.total_tokens = 33
    record = _make_record(usage=usage)
    call_id = str(uuid.uuid4())

    row = egress_audit.build_completed_row(call_id, record)

    assert row.phase == "completed", f"expected phase='completed'; got {row.phase!r}"
    assert row.call_id == call_id, f"expected call_id={call_id!r}; got {row.call_id!r}"
    assert row.content_fingerprint is None, "completed row must have content_fingerprint=None"
    assert row.entity_manifest is None, "completed row must have entity_manifest=None"
    assert row.prompt_tokens == 11, f"expected prompt_tokens=11; got {row.prompt_tokens!r}"
    assert row.completion_tokens == 22, (
        f"expected completion_tokens=22; got {row.completion_tokens!r}"
    )
    assert row.total_tokens == 33, f"expected total_tokens=33; got {row.total_tokens!r}"


def test_attempt_and_completed_rows_share_call_id_but_have_distinct_ids() -> None:
    """The two row kinds of ONE crossing share ``call_id`` but each gets its OWN fresh
    primary-key ``id`` (a fresh uuid4 per ROW, per spec §2.2).
    """
    record = _make_record(usage=None)
    call_id = str(uuid.uuid4())
    attempt = egress_audit.build_attempt_row(call_id, record, "c" * 64, None)

    usage = MagicMock()
    usage.prompt_tokens = 1
    usage.completion_tokens = 2
    usage.total_tokens = 3
    record.usage = usage
    completed = egress_audit.build_completed_row(call_id, record)

    assert attempt.call_id == completed.call_id == call_id
    assert attempt.id != completed.id, (
        f"attempt.id ({attempt.id!r}) and completed.id ({completed.id!r}) must be distinct "
        "fresh ids — each row is its own INSERT."
    )


def test_build_completed_row_handles_usage_missing_attributes_defensively() -> None:
    """build_completed_row must use getattr-style defensive extraction: a usage object
    missing an attribute must yield None for that column, not raise (mirrors
    ``egress_log._extract_usage_tokens``).
    """

    class _PartialUsage:
        prompt_tokens = 5
        # completion_tokens / total_tokens deliberately absent

    record = _make_record(usage=_PartialUsage())
    row = egress_audit.build_completed_row(str(uuid.uuid4()), record)

    assert row.prompt_tokens == 5
    assert row.completion_tokens is None, (
        f"missing usage.completion_tokens must map to None, not raise; got "
        f"{row.completion_tokens!r}"
    )
    assert row.total_tokens is None, (
        f"missing usage.total_tokens must map to None, not raise; got {row.total_tokens!r}"
    )


# ── write_row — add + commit + close; commit-error propagation ────────────────────────


class _SpySession:
    def __init__(self, events: list[tuple[str, Any]], *, raise_on_commit: bool = False) -> None:
        self._events = events
        self._raise_on_commit = raise_on_commit

    def add(self, row: Any) -> None:
        self._events.append(("add", row))

    def commit(self) -> None:
        self._events.append(("commit", None))
        if self._raise_on_commit:
            raise RuntimeError("fake-commit-failure-unit-test")

    def close(self) -> None:
        self._events.append(("close", None))


def test_write_row_calls_add_commit_close_in_order() -> None:
    events: list[tuple[str, Any]] = []
    row = object()

    def factory() -> _SpySession:
        return _SpySession(events)

    egress_audit.write_row(factory, row)

    assert events == [("add", row), ("commit", None), ("close", None)], (
        f"write_row must call add(row), then commit(), then close(), in that exact order; "
        f"got {events!r}"
    )


def test_write_row_propagates_commit_error_but_still_closes_session() -> None:
    events: list[tuple[str, Any]] = []
    row = object()

    def factory() -> _SpySession:
        return _SpySession(events, raise_on_commit=True)

    with pytest.raises(RuntimeError, match="fake-commit-failure-unit-test"):
        egress_audit.write_row(factory, row)

    assert events[0] == ("add", row), "add() must still be called before the failing commit"
    assert ("commit", None) in events, "commit() must have been attempted"
    assert events[-1] == ("close", None), (
        "session.close() must run even when commit() raises (finally-block discipline); "
        f"got {events!r}"
    )


# ── P-DORMANT (metamorphic) — default-off flag ⇒ durable writer never invoked ──────────


@given(mode=st.sampled_from(list(LLMMode)), provider_raises=st.booleans())
@_HYP_SETTINGS
def test_dormant_default_never_invokes_durable_session_factory(
    mode: LLMMode, provider_raises: bool
) -> None:
    """P-DORMANT: with ``llm_egress_durable_enabled=False`` (the default), across every mode
    and success/failure, the durable session factory is NEVER invoked, and the L1 behaviour
    (stdlib emits, fail-closed-on-``llm_egress_log_enabled``) is unchanged.

    Non-vacuity: an impl that writes durably regardless of the flag fails the assertion below
    (the factory-invocation flag flips to True).
    """
    factory_invoked = {"flag": False}

    def _durable_factory() -> Any:
        factory_invoked["flag"] = True
        return MagicMock()

    settings = _make_test_settings(llm_mode=mode.value, llm_egress_durable_enabled=False)
    gateway = LLMGateway(settings, session_factory=_durable_factory)

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        if provider_raises:
            raise RuntimeError("fake-provider-failure-dormant")
        return _make_fake_response()

    with patch("litellm.completion", side_effect=_provider_spy):
        if provider_raises:
            with pytest.raises(LLMGatewayError):
                gateway.chat(messages=[{"role": "user", "content": "dormant-probe"}], mode=mode)
        else:
            gateway.chat(messages=[{"role": "user", "content": "dormant-probe"}], mode=mode)

    assert factory_invoked["flag"] is False, (
        f"mode={mode!r} provider_raises={provider_raises!r}: the durable session factory was "
        f"invoked even though llm_egress_durable_enabled=False (the default). INV-DORMANT "
        f"requires byte-identical L1 behaviour when the flag is off."
    )


def test_settings_default_durable_flag_is_false() -> None:
    """P-DORMANT (structural): the default Settings value is False (dormant by construction,
    no operator opt-in required to get L1's byte-identical behaviour).
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_egress_durable_enabled is False, (
        f"Settings.llm_egress_durable_enabled must default to False; got "
        f"{settings.llm_egress_durable_enabled!r}."
    )
