"""PRIMARY property test — Gate F2: durable, append-only LLM-egress audit (ADR 0105).

This is the ``@given`` oracle for P-AUDIT-1..6
(``docs/reviews/GATE_F2_DURABLE_EGRESS_AUDIT_SPEC.md`` §3). It mirrors the spy/monkeypatch
idiom from ``tests/property/test_llm_egress_completeness.py`` and
``tests/property/test_prop_llm_egress_hardening.py`` (both stay byte-unchanged) but adds a
**spy SQLAlchemy Session/session_factory** double for the new durable-write seam — no Docker,
no real DB (the spec mandates pure spies here; real-DB fidelity lives in
``tests/integration/test_llm_egress_durable.py``).

  P-AUDIT-1  external-call completeness — the durable "attempt" row commits BEFORE the
             provider is contacted, even when the provider raises.
  P-AUDIT-2  append-only — across arbitrary call sequences, the writer only INSERTs.
  P-AUDIT-3  no-content-leak — no durable row column ever holds message text or the api key;
             ``content_fingerprint`` is a 64-hex sha256 digest.
  P-AUDIT-4  fingerprint determinism + sensitivity — key-order-insensitive, content-sensitive,
             always 64-hex, never raises on hostile content.
  P-AUDIT-5  durable fail-closed asymmetry — external fails closed on a durable-write failure
             or an unwired sink; LOCAL proceeds best-effort regardless.
  P-AUDIT-6  two-row usage correlation — a successful call writes an "attempt" row then a
             "completed" row sharing one ``call_id``; a provider failure leaves exactly one row.

RED TODAY:
    ``ModuleNotFoundError: No module named 'worldmonitor.llm.egress_audit'``
    (``egress_audit.py`` does not exist yet; even once it exists, ``LLMGateway.__init__`` does
    not accept ``session_factory=`` and ``Settings`` has no ``llm_egress_durable_enabled``
    field, so every test below would still fail at gateway construction time.)

──────────────────────────────────────────────────────────────────────────────────────────
BUILDER CONTRACTS — the implementation MUST match these names exactly (spec §2.3-2.5):

    worldmonitor.llm.egress_audit
        fingerprint_messages(messages: list[dict]) -> str        # 64-char lowercase hex sha256
        build_attempt_row(call_id, record, fingerprint, entity_ids) -> LlmEgressRecord
        build_completed_row(call_id, record) -> LlmEgressRecord
        write_row(session_factory, row) -> None                  # add + commit + close

    worldmonitor.llm.gateway
        LLMGateway(settings, session_factory: Callable[[], Session] | None = None)
        chat(messages, *, mode=None, caller_tag="gateway", entity_ids=None) -> Any

    worldmonitor.settings.Settings
        llm_egress_durable_enabled: bool = False
──────────────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import contextlib
import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# ── Imports that MUST fail until the builder delivers llm/egress_audit.py + the
# session_factory ctor seam + the durable flag — a collection-time ModuleNotFoundError in
# THIS file only is the acceptable RED signal. Do NOT stub these.
from worldmonitor.llm import egress_audit  # noqa: F401
from worldmonitor.llm.gateway import LLMGateway, LLMGatewayError
from worldmonitor.llm.modes import REGISTRY, LLMMode
from worldmonitor.settings import Settings

_SETTINGS = hyp_settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ── Helpers ─────────────────────────────────────────────────────────────────────────────


def _make_test_settings(
    *,
    llm_mode: str = "local",
    llm_egress_log_enabled: bool = True,
    llm_egress_durable_enabled: bool = True,
    **extra: Any,
) -> Settings:
    """Minimal Settings for durable-audit gateway tests — no .env read (CI-safe).

    ``extra.setdefault`` (not a hardcoded kwarg) for ``llm_openrouter_api_key`` so a caller
    overriding it via ``**extra`` (e.g. P-AUDIT-3's api-key-leak probe) does not collide with
    a duplicate keyword argument to ``Settings(...)``.
    """
    extra.setdefault("llm_openrouter_api_key", "test-fake-or-key")
    return Settings(  # type: ignore[call-arg]
        llm_mode=llm_mode,
        llm_ollama_model="llama3.2",
        llm_ollama_base_url="http://localhost:11434",
        llm_openrouter_model="openai/gpt-4o",
        llm_claude_binary="claude",
        llm_claude_model_label="claude-test",
        llm_claude_timeout_seconds=30,
        llm_egress_log_enabled=llm_egress_log_enabled,
        llm_egress_durable_enabled=llm_egress_durable_enabled,
        _env_file=None,
        **extra,
    )


def _make_fake_response(
    prompt_tokens: int = 7, completion_tokens: int = 13, total_tokens: int = 20
) -> MagicMock:
    """Fake litellm.ModelResponse — OpenAI-shaped (mirrors the L1 property test files)."""
    resp = MagicMock()
    resp.model = "fake-model"
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "fake-durable-property-response"
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.usage.total_tokens = total_tokens
    return resp


class _SpySession:
    """A spy SQLAlchemy Session double — records every method call on the shared bus."""

    def __init__(self, bus: _SpyBus) -> None:
        self._bus = bus
        self._pending: list[Any] = []

    def add(self, row: Any) -> None:
        self._bus.calls.append(("add", row))
        self._bus.rows.append(row)
        self._pending.append(row)

    def commit(self) -> None:
        self._bus.calls.append(("commit", None))
        if self._bus.raise_on_commit:
            raise RuntimeError("fake-durable-commit-failure")
        self._bus.events.append("durable_commit")
        # Oracle tie (adversarial-verify hardening): snapshot the rows THIS commit carried,
        # so P-AUDIT-1 can assert the pre-provider commit contains the attempt row itself —
        # a contrived impl committing an EMPTY session pre-provider and adding the row
        # afterwards would otherwise satisfy the decomposed assertions.
        self._bus.commit_snapshots.append(list(self._pending))
        self._pending.clear()

    def close(self) -> None:
        self._bus.calls.append(("close", None))

    def delete(self, row: Any) -> None:
        self._bus.calls.append(("delete", row))

    def flush(self) -> None:
        self._bus.calls.append(("flush", None))

    def execute(self, statement: Any) -> Any:
        self._bus.calls.append(("execute", statement))
        return None


class _SpyBus:
    """Shared mutable state a spy ``session_factory`` writes into across ``chat()`` calls.

    ``events`` is a shared ordered tape both the DB spy (``"durable_commit"``) and the
    ``litellm.completion`` spy (``"provider"``) append to, so ordering assertions compare
    positions in ONE timeline (mirrors the FROZEN completeness test's ``events`` idiom).
    """

    def __init__(self, *, raise_on_commit: bool = False) -> None:
        self.events: list[str] = []
        self.calls: list[tuple[str, Any]] = []
        self.rows: list[Any] = []
        self.commit_snapshots: list[list[Any]] = []
        self.raise_on_commit = raise_on_commit
        self.factory_invoked = False

    def factory(self) -> _SpySession:
        self.factory_invoked = True
        return _SpySession(self)


_ALL_MODES = st.sampled_from(list(LLMMode))
_EXTERNAL_MODES = st.sampled_from([LLMMode.OPENROUTER, LLMMode.CLAUDE_HEADLESS])
_MESSAGE_DICT = st.fixed_dictionaries(
    {
        "role": st.sampled_from(["user", "system", "assistant"]),
        "content": st.text(max_size=40),
    }
)
_MESSAGES = st.lists(_MESSAGE_DICT, min_size=1, max_size=4)


# ── P-AUDIT-1 — external-call completeness ─────────────────────────────────────────────


@given(mode=_EXTERNAL_MODES, provider_raises=st.booleans(), messages=_MESSAGES)
@_SETTINGS
def test_external_attempt_row_committed_before_provider_call(
    mode: LLMMode, provider_raises: bool, messages: list[dict[str, Any]]
) -> None:
    """P-AUDIT-1: durable_on=True + EXTERNAL ⇒ the "attempt" row commits BEFORE
    ``litellm.completion`` is contacted, even when the provider raises.

    Non-vacuity: an impl that writes AFTER the provider call fails the ordering assertion
    below (or, on ``provider_raises``, never gets a chance to write at all); today's
    no-durable-write code fails the very first assertion ("durable_commit" never appears).
    """
    bus = _SpyBus()

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        bus.events.append("provider")
        if provider_raises:
            raise RuntimeError("fake-provider-failure-P-AUDIT-1")
        return _make_fake_response()

    settings = _make_test_settings(llm_mode=mode.value)
    gateway = LLMGateway(settings, session_factory=bus.factory)

    with patch("litellm.completion", side_effect=_provider_spy):
        if provider_raises:
            with pytest.raises(LLMGatewayError):
                gateway.chat(messages=messages, mode=mode)
        else:
            gateway.chat(messages=messages, mode=mode)

    assert "durable_commit" in bus.events, (
        f"mode={mode!r} provider_raises={provider_raises!r}: no durable attempt-row commit "
        f"observed; events={bus.events!r}. INV-DURABLE-COMPLETE requires a committed attempt "
        f"row for every external crossing."
    )
    if "provider" in bus.events:
        commit_idx = bus.events.index("durable_commit")
        provider_idx = bus.events.index("provider")
        assert commit_idx < provider_idx, (
            f"mode={mode!r}: durable commit happened AFTER the provider call "
            f"(commit@{commit_idx} >= provider@{provider_idx}); events={bus.events!r}."
        )

    attempt_rows = [row for row in bus.rows if row.phase == "attempt"]
    assert len(attempt_rows) >= 1, (
        f"mode={mode!r} provider_raises={provider_raises!r}: expected >=1 committed "
        f"'attempt' row (present even on provider failure); rows={bus.rows!r}"
    )

    # Oracle tie (adversarial-verify hardening): the FIRST commit — the one proven above to
    # precede the provider — must itself carry the attempt row, not an empty session.
    assert bus.commit_snapshots, "a durable_commit event exists but no commit snapshot"
    assert any(getattr(r, "phase", None) == "attempt" for r in bus.commit_snapshots[0]), (
        f"mode={mode!r}: the pre-provider commit carried no 'attempt' row "
        f"(snapshot={bus.commit_snapshots[0]!r}) — the attempt row must be IN the commit "
        f"that precedes the provider call, not a later one."
    )


# ── P-AUDIT-2 — append-only (no UPDATE/DELETE) ─────────────────────────────────────────

_CALL_SPEC = st.fixed_dictionaries({"mode": _ALL_MODES, "provider_raises": st.booleans()})
_CALL_SEQ = st.lists(_CALL_SPEC, min_size=1, max_size=5)


@given(call_specs=_CALL_SEQ)
@_SETTINGS
def test_writer_issues_only_inserts_never_update_or_delete(
    call_specs: list[dict[str, Any]],
) -> None:
    """P-AUDIT-2: across an arbitrary sequence of ``chat()`` calls (mixed modes, mixed
    success), the writer issues only INSERTs — never UPDATE, never DELETE, never
    ``session.delete``.

    Non-vacuity: an impl that "enriches" the attempt row in place (an UPDATE) or issues a
    ``session.delete`` cleanup fails the subset / delete-absence assertions below.
    """
    bus = _SpyBus()
    current: dict[str, bool] = {"raises": False}

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        if current["raises"]:
            raise RuntimeError("fake-provider-failure-P-AUDIT-2")
        return _make_fake_response()

    gateway = LLMGateway(_make_test_settings(llm_mode="local"), session_factory=bus.factory)

    with patch("litellm.completion", side_effect=_provider_spy):
        for spec in call_specs:
            current["raises"] = spec["provider_raises"]
            with contextlib.suppress(LLMGatewayError):
                gateway.chat(
                    messages=[{"role": "user", "content": "append-only-probe"}],
                    mode=spec["mode"],
                )

    call_names = {name for name, _ in bus.calls}
    allowed = {"add", "commit", "close", "flush"}
    assert call_names <= allowed, (
        f"writer issued call(s) outside {allowed}: {call_names - allowed!r}; calls={bus.calls!r}"
    )
    assert "delete" not in call_names, (
        f"writer called session.delete at least once; calls={bus.calls!r}. "
        f"INV-DURABLE-APPENDONLY: the writer must NEVER delete a row."
    )
    for name, arg in bus.calls:
        if name == "execute":
            statement_text = str(arg).upper()
            assert "UPDATE" not in statement_text and "DELETE" not in statement_text, (
                f"writer issued an execute() carrying an UPDATE/DELETE statement: {arg!r}"
            )


# ── P-AUDIT-3 — no-content-leak (through the REAL gateway.chat() path) ─────────────────

_SECRET_PREFIX = "sk-test-secret-property-"
_API_KEY_PREFIX = "or-test-api-key-property-"


@st.composite
def _messages_with_embedded_secret(draw: st.DrawFn) -> tuple[list[dict[str, Any]], str]:
    suffix = draw(st.text(min_size=6, max_size=16))
    secret_value = _SECRET_PREFIX + suffix
    filler = draw(st.lists(_MESSAGE_DICT, max_size=3))
    messages = [*filler, {"role": "user", "content": secret_value}]
    return messages, secret_value


@given(
    payload=_messages_with_embedded_secret(),
    entity_ids=st.one_of(st.none(), st.lists(st.text(min_size=1, max_size=12), max_size=4)),
    api_key_suffix=st.text(min_size=6, max_size=16),
)
@_SETTINGS
def test_no_row_column_ever_contains_message_text_or_api_key(
    payload: tuple[list[dict[str, Any]], str],
    entity_ids: list[str] | None,
    api_key_suffix: str,
) -> None:
    """P-AUDIT-3: for arbitrary message payloads (incl. an embedded api-key-looking secret)
    and an arbitrary declared manifest, no serialized column of ANY durably-committed row
    contains the raw message text or the api key; ``content_fingerprint`` is a 64-hex digest.

    Drives the REAL ``gateway.chat()`` path (not the row builders directly) so a regression
    that threads the api key or message content into a row anywhere in the control flow is
    caught, not just a bug isolated to the builder functions.

    Non-vacuity: a row that stored ``messages`` (or a preview) fails; a truncated-but-present
    content column fails.
    """
    messages, secret_value = payload
    api_key_value = _API_KEY_PREFIX + api_key_suffix

    bus = _SpyBus()
    settings = _make_test_settings(llm_mode="openrouter", llm_openrouter_api_key=api_key_value)
    gateway = LLMGateway(settings, session_factory=bus.factory)

    with patch("litellm.completion", return_value=_make_fake_response()):
        gateway.chat(messages=messages, mode=LLMMode.OPENROUTER, entity_ids=entity_ids)

    assert bus.rows, "expected at least one durable row for a successful external call"
    for row in bus.rows:
        for column in row.__table__.columns:
            value_str = str(getattr(row, column.name))
            assert secret_value not in value_str, (
                f"column {column.name!r} of a {row.phase!r} row leaked the message secret: "
                f"{value_str!r}"
            )
            assert api_key_value not in value_str, (
                f"column {column.name!r} of a {row.phase!r} row leaked the api key: {value_str!r}"
            )

    attempt_rows = [row for row in bus.rows if row.phase == "attempt"]
    assert len(attempt_rows) == 1, f"expected exactly 1 attempt row; got {len(attempt_rows)}"
    fingerprint = attempt_rows[0].content_fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint or ""), (
        f"content_fingerprint must be a 64-char lowercase hex digest; got {fingerprint!r}"
    )


# ── P-AUDIT-4 — fingerprint determinism + sensitivity (pure function) ─────────────────


def _mutate_messages(messages: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Return a GENUINELY different copy of ``messages`` (never a no-op)."""
    mutated = [dict(m) for m in messages]
    if kind == "change" and mutated:
        mutated[0]["content"] = str(mutated[0].get("content", "")) + "-P-AUDIT-4-mutated"
    elif kind == "remove" and len(mutated) > 1:
        mutated.pop()
    else:
        mutated.append({"role": "user", "content": "P-AUDIT-4-extra-message"})
    return mutated


@given(messages=_MESSAGES, mutation_kind=st.sampled_from(["change", "add", "remove"]))
@_SETTINGS
def test_fingerprint_deterministic_key_order_insensitive_and_sensitive(
    messages: list[dict[str, Any]], mutation_kind: str
) -> None:
    """P-AUDIT-4: ``fingerprint_messages`` is deterministic (key-order-insensitive) and
    content-sensitive; output is always 64-char lowercase hex.

    Non-vacuity: a constant / ``repr()``-based / key-order-sensitive digest fails; a digest
    that ignores the mutated field fails.
    """
    reordered = [dict(reversed(list(m.items()))) for m in messages]
    fp_original = egress_audit.fingerprint_messages(messages)
    fp_reordered = egress_audit.fingerprint_messages(reordered)

    assert re.fullmatch(r"[0-9a-f]{64}", fp_original), (
        f"fingerprint must be 64-char lowercase hex; got {fp_original!r}"
    )
    assert fp_original == fp_reordered, (
        f"fingerprint must be key-order-insensitive: {messages!r} (fp={fp_original!r}) vs "
        f"{reordered!r} (fp={fp_reordered!r})"
    )

    mutated = _mutate_messages(messages, mutation_kind)
    assert mutated != messages, "test bug: the mutation strategy produced a no-op"
    fp_mutated = egress_audit.fingerprint_messages(mutated)
    assert fp_mutated != fp_original, (
        f"fingerprint must change when content changes: {messages!r} -> {mutated!r} both "
        f"hashed to {fp_original!r}"
    )


class _HostileValue:
    """A non-JSON-serializable object — must serialize via ``default=str``, never raise."""

    def __init__(self, tag: str) -> None:
        self._tag = tag

    def __str__(self) -> str:
        return f"<hostile:{self._tag}>"


@given(tag=st.text(max_size=20))
@_SETTINGS
def test_fingerprint_never_raises_on_hostile_content(tag: str) -> None:
    """P-AUDIT-4 (hostile-content clause): a non-JSON-serializable message value must NOT
    raise; the digest is still 64-hex.
    """
    messages = [{"role": "user", "content": _HostileValue(tag)}]
    fingerprint = egress_audit.fingerprint_messages(messages)
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint), (
        f"fingerprint on hostile content must still be 64-char hex; got {fingerprint!r}"
    )


# ── P-AUDIT-4 totality clause (adversarial-verify fix round) ───────────────────────────
# Four EXECUTED raise classes refuted the original "default=str never raises" claim:
# a lone UTF-16 surrogate (WIRE-REACHABLE via stdlib json.loads escape handling + pydantic
# `content: str` pass-through → was an HTTP 500 on /v1), a circular structure, a leaf whose
# __str__ raises, and mixed-type dict keys under sort_keys. The fingerprint must be TOTAL.

_LONE_SURROGATE_CONTENT = "hi \ud800 there"


class _RaisingStr:
    """A leaf whose ``__str__``/``__repr__`` raise — ``default=str`` propagates the raise."""

    def __str__(self) -> str:
        raise RuntimeError("hostile __str__")

    __repr__ = __str__  # type: ignore[assignment]


def _hostile_payload(tag: str) -> list[dict[Any, Any]]:
    if tag == "lone-surrogate":
        return [{"role": "user", "content": _LONE_SURROGATE_CONTENT}]
    if tag == "circular":
        msg: dict[Any, Any] = {"role": "user"}
        msg["content"] = msg
        return [msg]
    if tag == "raising-str":
        return [{"role": "user", "content": _RaisingStr()}]
    return [{1: "a", "role": "user", "content": "x"}]  # mixed-type-keys


@pytest.mark.parametrize("tag", ["lone-surrogate", "circular", "raising-str", "mixed-type-keys"])
def test_fingerprint_total_on_executed_hostile_classes(tag: str) -> None:
    """P-AUDIT-4 (totality): every executed raise class yields a 64-hex digest, never a
    raise, and equal hostile input ⇒ equal digest (deterministic within its domain).
    """
    digest = egress_audit.fingerprint_messages(_hostile_payload(tag))
    assert re.fullmatch(r"[0-9a-f]{64}", digest), (
        f"tag={tag!r}: fingerprint must be total (64-hex), got {digest!r}"
    )
    assert digest == egress_audit.fingerprint_messages(_hostile_payload(tag)), (
        f"tag={tag!r}: fingerprint must be deterministic for equal hostile input"
    )


@pytest.mark.parametrize("mode", [LLMMode.LOCAL, LLMMode.OPENROUTER])
def test_hostile_payload_never_escapes_chat_untyped(mode: LLMMode) -> None:
    """Adversarial-verify fix round: with the durable flag ON, a hostile (lone-surrogate)
    payload must NOT raise out of ``chat()`` — the provider is still called exactly once and
    the committed attempt row carries a 64-hex fingerprint. Refutes the executed /v1 HTTP-500
    (raw ``UnicodeEncodeError`` escaping the typed-error contract) and proves LOCAL
    best-effort (SF-5) is not broken by the audit side.
    """
    bus = _SpyBus()
    provider_calls: list[Any] = []

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        provider_calls.append((args, kwargs))
        return _make_fake_response()

    settings = _make_test_settings(llm_mode=mode.value)
    gateway = LLMGateway(settings, session_factory=bus.factory)
    messages = [{"role": "user", "content": _LONE_SURROGATE_CONTENT}]

    with patch("litellm.completion", side_effect=_provider_spy):
        gateway.chat(messages=messages, mode=mode)  # must NOT raise

    assert len(provider_calls) == 1, f"mode={mode!r}: provider must be called exactly once"
    attempt_rows = [row for row in bus.rows if row.phase == "attempt"]
    assert len(attempt_rows) == 1
    assert re.fullmatch(r"[0-9a-f]{64}", attempt_rows[0].content_fingerprint)


# ── P-AUDIT-5 — durable fail-closed asymmetry ──────────────────────────────────────────


@given(mode=_ALL_MODES, sink_state=st.sampled_from(["raises", "none", "ok"]))
@_SETTINGS
def test_durable_write_failure_fail_closed_asymmetry(mode: LLMMode, sink_state: str) -> None:
    """P-AUDIT-5: durable_on=True + EXTERNAL + (write raises OR unwired factory) ⇒
    ``LLMGatewayError``, provider NEVER contacted; LOCAL proceeds best-effort regardless.

    Non-vacuity: an always-raise impl fails the LOCAL/ok cases; a never-raise impl (today's
    behaviour) fails the external-raises/none cases.
    """
    bus = _SpyBus(raise_on_commit=(sink_state == "raises"))
    factory = None if sink_state == "none" else bus.factory

    provider_calls: list[Any] = []

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        provider_calls.append((args, kwargs))
        return _make_fake_response()

    settings = _make_test_settings(llm_mode=mode.value)
    gateway = LLMGateway(settings, session_factory=factory)
    external = REGISTRY[mode].data_left_perimeter

    with patch("litellm.completion", side_effect=_provider_spy):
        if external and sink_state in ("raises", "none"):
            with pytest.raises(LLMGatewayError):
                gateway.chat(messages=[{"role": "user", "content": "probe"}], mode=mode)
            assert len(provider_calls) == 0, (
                f"mode={mode!r} sink_state={sink_state!r}: provider must NEVER be contacted "
                f"when the durable write cannot succeed; got {len(provider_calls)} call(s)."
            )
        else:
            gateway.chat(messages=[{"role": "user", "content": "probe"}], mode=mode)
            assert len(provider_calls) == 1, (
                f"mode={mode!r} sink_state={sink_state!r}: expected exactly 1 provider call "
                f"(this combination must proceed); got {len(provider_calls)}."
            )


# ── P-AUDIT-6 — two-row usage correlation ──────────────────────────────────────────────


@given(
    mode=_ALL_MODES,
    prompt_tokens=st.integers(min_value=0, max_value=999_999),
    completion_tokens=st.integers(min_value=0, max_value=999_999),
    total_tokens=st.integers(min_value=0, max_value=999_999),
    provider_raises=st.booleans(),
)
@_SETTINGS
def test_two_row_usage_correlation_on_success_one_row_on_failure(
    mode: LLMMode,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    provider_raises: bool,
) -> None:
    """P-AUDIT-6: a successful call writes exactly two durable rows sharing one ``call_id``
    (an "attempt" row then a "completed" row); a provider failure leaves exactly one row.

    Non-vacuity: a single-row impl fails the success-case row count; an in-place-update impl
    fails the two-distinct-rows / call_id-sharing checks; a mismatched call_id fails
    correlation.
    """
    bus = _SpyBus()

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        if provider_raises:
            raise RuntimeError("fake-provider-failure-P-AUDIT-6")
        return _make_fake_response(prompt_tokens, completion_tokens, total_tokens)

    settings = _make_test_settings(llm_mode=mode.value)
    gateway = LLMGateway(settings, session_factory=bus.factory)

    with patch("litellm.completion", side_effect=_provider_spy):
        if provider_raises:
            with pytest.raises(LLMGatewayError):
                gateway.chat(messages=[{"role": "user", "content": "usage-probe"}], mode=mode)
        else:
            gateway.chat(messages=[{"role": "user", "content": "usage-probe"}], mode=mode)

    if provider_raises:
        assert len(bus.rows) == 1, (
            f"mode={mode!r}: provider failure after the attempt row must leave EXACTLY one "
            f"durable row; got {len(bus.rows)}: {bus.rows!r}"
        )
        assert bus.rows[0].phase == "attempt", (
            f"the sole surviving row must be the 'attempt' row; got phase={bus.rows[0].phase!r}"
        )
        return

    assert len(bus.rows) == 2, (
        f"mode={mode!r}: a successful call must write exactly 2 durable rows; "
        f"got {len(bus.rows)}: {bus.rows!r}"
    )
    attempt, completed = bus.rows
    assert attempt.phase == "attempt" and completed.phase == "completed", (
        f"expected phase order ['attempt', 'completed']; got "
        f"[{attempt.phase!r}, {completed.phase!r}]"
    )
    assert attempt.call_id == completed.call_id and attempt.call_id, (
        f"attempt.call_id={attempt.call_id!r} must equal completed.call_id="
        f"{completed.call_id!r} and be non-empty (correlates one crossing)."
    )
    assert re.fullmatch(r"[0-9a-f]{64}", attempt.content_fingerprint or ""), (
        f"attempt.content_fingerprint must be 64-hex; got {attempt.content_fingerprint!r}"
    )
    assert attempt.prompt_tokens is None, "attempt row must have NULL token columns"
    assert attempt.completion_tokens is None, "attempt row must have NULL token columns"
    assert attempt.total_tokens is None, "attempt row must have NULL token columns"
    assert completed.content_fingerprint is None, "completed row's fingerprint must be NULL"
    assert completed.prompt_tokens == prompt_tokens, (
        f"completed.prompt_tokens expected {prompt_tokens}, got {completed.prompt_tokens!r}"
    )
    assert completed.completion_tokens == completion_tokens, (
        f"completed.completion_tokens expected {completion_tokens}, got "
        f"{completed.completion_tokens!r}"
    )
    assert completed.total_tokens == total_tokens, (
        f"completed.total_tokens expected {total_tokens}, got {completed.total_tokens!r}"
    )
