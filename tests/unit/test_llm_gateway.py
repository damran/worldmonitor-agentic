"""Unit tests — Phase-3 Gate S2: LLM gateway (INV-S2-EGRESS / INV-S2-DEFAULT invariants).

Covers spec §4b:
- default Settings → LOCAL mode (Ollama loopback, data_left_perimeter=False);
- a successful call writes exactly TWO egress records (pre-call, no usage; post-call, usage
  attached) — updated for Gate L1-a / ADR 0104 item 2 (§2.5 of
  docs/reviews/GATE_L1_LLM_EGRESS_HARDENING_SPEC.md);
- a provider exception still leaves a record AND surfaces a typed LLMGatewayError;
- per-call mode= override routes to that mode and records its flags;
- no-secret-leak: openrouter_api_key bytes and message content absent from log;
- claude shim: invoked with argv list (not shell string), with timeout, stdout untrusted;
- claude shim: only registered/invoked when CLAUDE_HEADLESS mode is active.

RED TODAY:
    ``ModuleNotFoundError: No module named 'worldmonitor.llm.gateway'``

BUILDER CONTRACT (names the implementation MUST match):

    worldmonitor.llm.gateway
        LLMGateway(settings: Settings)
            chat(messages: list[dict], *, mode: LLMMode | None = None) -> Any
                CONTRACT: calls worldmonitor.llm.egress_log.emit(record) BEFORE
                          litellm.completion(...).
                CONTRACT: import litellm (whole module) so `litellm.completion` patch works.
                CONTRACT: raises LLMGatewayError (not a raw litellm exception) on failure.
                CONTRACT (ADR 0104 item 2): on a SUCCESSFUL call, emit() is called TWICE —
                          once BEFORE the provider call (usage=None) and once AFTER (usage
                          populated), enriching the SAME EgressRecord object in place.

        LLMGatewayError(Exception)

    worldmonitor.llm.egress_log
        EgressRecord  (mutable dataclass; .usage enriched in-place after success)
        emit(record: EgressRecord) -> None  (stdlib logging with extra=)

    worldmonitor.llm.modes
        LLMMode  (LOCAL, CLAUDE_HEADLESS, OPENROUTER)

    worldmonitor.settings
        Settings (additive llm_* fields, ADR 0091):
            llm_mode: Literal["local","claude_headless","openrouter"] = "local"
            llm_ollama_base_url: str  = "http://localhost:11434"
            llm_ollama_model: str
            llm_openrouter_api_key: SecretStr
            llm_openrouter_model: str
            llm_claude_binary: str = "claude"
            llm_claude_model_label: str
            llm_claude_timeout_seconds: int | float (gt=0)
            llm_egress_log_enabled: bool = True
"""

from __future__ import annotations

import copy
import dataclasses
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Imports that MUST fail until builder creates the llm/ modules ──────────────────────
from worldmonitor.llm.egress_log import EgressRecord  # noqa: F401
from worldmonitor.llm.gateway import LLMGateway, LLMGatewayError  # noqa: F401
from worldmonitor.llm.modes import LLMMode  # noqa: F401
from worldmonitor.settings import Settings

# ── Helpers ─────────────────────────────────────────────────────────────────────────────

_FAKE_API_KEY = "or-secret-test-key-do-not-leak"
_FAKE_MESSAGE_CONTENT = "private-test-message-content-xyz"


def _make_fake_response(
    model: str = "ollama_chat/llama3.2",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    total_tokens: int = 30,
) -> MagicMock:
    """Fake litellm.ModelResponse — OpenAI-shaped (spec §7)."""
    resp = MagicMock()
    resp.model = model
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "unit-test response content"
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.usage.total_tokens = total_tokens
    return resp


def _local_settings(**extra: Any) -> Settings:
    """Minimal LOCAL-mode Settings (reads no .env — CI safe)."""
    return Settings(  # type: ignore[call-arg]
        llm_mode="local",
        llm_ollama_model="llama3.2",
        llm_ollama_base_url="http://localhost:11434",
        llm_openrouter_api_key=_FAKE_API_KEY,
        llm_openrouter_model="openai/gpt-4o",
        llm_claude_binary="claude",
        llm_claude_model_label="claude-test",
        llm_claude_timeout_seconds=30,
        llm_egress_log_enabled=True,
        _env_file=None,
        **extra,
    )


def _or_settings() -> Settings:
    """Minimal OPENROUTER-mode Settings (reads no .env)."""
    return Settings(  # type: ignore[call-arg]
        llm_mode="openrouter",
        llm_ollama_model="llama3.2",
        llm_ollama_base_url="http://localhost:11434",
        llm_openrouter_api_key=_FAKE_API_KEY,
        llm_openrouter_model="openai/gpt-4o",
        llm_claude_binary="claude",
        llm_claude_model_label="claude-test",
        llm_claude_timeout_seconds=30,
        llm_egress_log_enabled=True,
        _env_file=None,
    )


def _claude_settings() -> Settings:
    """Minimal CLAUDE_HEADLESS-mode Settings (reads no .env)."""
    return Settings(  # type: ignore[call-arg]
        llm_mode="claude_headless",
        llm_ollama_model="llama3.2",
        llm_ollama_base_url="http://localhost:11434",
        llm_openrouter_api_key=_FAKE_API_KEY,
        llm_openrouter_model="openai/gpt-4o",
        llm_claude_binary="claude",
        llm_claude_model_label="claude-test",
        llm_claude_timeout_seconds=30,
        llm_egress_log_enabled=True,
        _env_file=None,
    )


# ── INV-S2-DEFAULT: default settings route to LOCAL / Ollama loopback ─────────────────


def test_default_mode_is_local() -> None:
    """INV-S2-DEFAULT: LLMGateway built from default Settings (no llm_mode set) uses LOCAL.

    The default selector is 'local' (ADR 0091 §4).  With no operator override, the active
    mode is LOCAL: confidential, no egress, Ollama loopback.
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    # The default llm_mode must be "local" (additive field, ADR 0091).
    assert settings.llm_mode == "local", (
        f"Settings.llm_mode must default to 'local'; got {settings.llm_mode!r}.  "
        f"INV-S2-DEFAULT: with no operator override, the active mode is LOCAL "
        f"(confidential / no egress, ADR 0091 §4)."
    )


def test_default_local_mode_uses_ollama_loopback_and_no_egress(caplog: Any) -> None:
    """INV-S2-DEFAULT: default gateway routes to Ollama loopback; egress record says no-egress.

    Verifies that:
    1. litellm.completion is called with an 'ollama_chat/...' model prefix.
    2. The api_base in the litellm call is the loopback address.
    3. The egress record captures data_left_perimeter=False.
    """
    captured_records: list[Any] = []

    def _emit_spy(record: Any) -> None:
        captured_records.append(record)

    fake_response = _make_fake_response()

    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_emit_spy),
        patch("litellm.completion", return_value=fake_response) as mock_litellm,
    ):
        gw = LLMGateway(_local_settings())
        gw.chat(messages=[{"role": "user", "content": "test"}])

    # The litellm call must use the ollama_chat/ model prefix.
    assert mock_litellm.call_count == 1, "exactly one litellm.completion call expected"
    call_kwargs = mock_litellm.call_args
    model_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("model", "")
    assert model_arg.startswith("ollama_chat/"), (
        f"LOCAL mode must use model='ollama_chat/...'; got {model_arg!r}.  "
        f"Spec §7: Local (Ollama): model='ollama_chat/<name>'."
    )

    # The api_base must be the loopback address.
    api_base = call_kwargs.kwargs.get("api_base", "")
    loopback = ("localhost", "127.0.0.1", "::1")
    assert any(h in str(api_base) for h in loopback), (
        f"LOCAL mode must use loopback api_base; got {api_base!r}.  "
        f"Spec §7: api_base='http://localhost:11434'."
    )

    # The egress record must say data_left_perimeter=False. One emitted record is
    # sufficient for THIS assertion (data_left_perimeter is set identically on both the
    # pre-call and post-call record) — the exact emit COUNT is covered by the dedicated
    # two-record test below (ADR 0104 item 2).
    assert len(captured_records) >= 1, (
        f"expected at least 1 egress record for one chat() call; got {len(captured_records)}"
    )
    rec = captured_records[0]
    assert rec.data_left_perimeter is False, (
        f"LOCAL mode egress record must have data_left_perimeter=False; "
        f"got {rec.data_left_perimeter!r}.  "
        "INV-S2-DEFAULT: LOCAL never sends data off-perimeter."
    )


# ── INV-USAGE (ADR 0104 item 2): success emits TWO records — pre-call (no usage), then
#    post-call (usage attached), the SAME mutated EgressRecord object ────────────────────


def test_successful_call_writes_two_egress_records_pre_and_post_with_token_usage() -> None:
    """INV-USAGE (ADR 0104 item 2): a successful chat() emits exactly TWO records — the
    pre-call completeness record (usage=None) and a post-call record enriched with token
    usage. The gateway mutates ONE EgressRecord in place (so the FROZEN completeness test
    stays green), so the spy snapshots each record AT EMIT TIME (``copy.copy``) to capture
    the state the audit actually observed at each emit — inspecting the live reference after
    chat() returns would see only the final, enriched state (a shared-object artifact, not
    what was logged).

    Verifies that:
    1. emit() is called exactly TWICE per successful chat(): BEFORE the provider call
       (INV-S2-EGRESS completeness — usage None) and AFTER (INV-USAGE — usage set).
    2. The post-call record is the pre-call record ENRICHED WITH USAGE and nothing else —
       ``replace(pre, usage=post.usage) == post`` (i.e. they are the same logical crossing,
       not two independently-constructed records; field-agnostic, snapshot-safe).
    3. The enriched record's usage carries the mocked ModelResponse's token counts.

    Supersedes the previous "exactly ONE record" assertion (pre-L1-a): fixing the
    ADR-0104-item-2 token-usage audit bug requires a second, post-call emit so a call's token
    spend actually lands in the audit (it previously never did — the record was enriched
    after the log line had already been written and was never re-emitted).
    """
    captured_records: list[Any] = []

    def _emit_spy(record: Any) -> None:
        # Snapshot the record's state AT EMIT TIME (the gateway mutates one record in place).
        captured_records.append(copy.copy(record))

    fake_response = _make_fake_response(prompt_tokens=11, completion_tokens=22, total_tokens=33)

    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_emit_spy),
        patch("litellm.completion", return_value=fake_response),
    ):
        gw = LLMGateway(_local_settings())
        gw.chat(messages=[{"role": "user", "content": "hello"}])

    assert len(captured_records) == 2, (
        f"expected exactly 2 egress records for a successful call "
        f"(pre-call completeness record + post-call usage record, ADR 0104 item 2); "
        f"got {len(captured_records)}."
    )

    pre_record, post_record = captured_records[0], captured_records[1]

    assert pre_record.usage is None, (
        "the PRE-call record (captured_records[0]) must have usage=None — the provider "
        f"has not responded yet at that point; got {pre_record.usage!r}."
    )
    assert post_record.usage is not None, (
        "the POST-call record (captured_records[1]) must have usage populated with the "
        "response's token counts after a successful provider call."
    )
    assert dataclasses.replace(pre_record, usage=post_record.usage) == post_record, (
        "the post-call record must be the pre-call record enriched with usage and NOTHING "
        "else (same logical crossing — same mode/target/model/caller/timestamp), not a "
        f"distinct independently-built record — pre={pre_record!r} vs post={post_record!r}."
    )

    # Check specific counts match the mocked response, on the final (enriched) record.
    usage = post_record.usage
    assert usage.total_tokens == 33, (
        f"EgressRecord.usage.total_tokens must match the ModelResponse; "
        f"expected 33, got {usage.total_tokens!r}."
    )
    assert usage.prompt_tokens == 11, (
        f"EgressRecord.usage.prompt_tokens expected 11, got {usage.prompt_tokens!r}."
    )
    assert usage.completion_tokens == 22, (
        f"EgressRecord.usage.completion_tokens expected 22, got {usage.completion_tokens!r}."
    )


# ── INV-S2-EGRESS: provider exception → record exists + typed gateway error ───────────


def test_provider_exception_surfaces_as_gateway_error_and_record_still_exists() -> None:
    """INV-S2-EGRESS: on provider failure, record is emitted AND LLMGatewayError is raised.

    A provider failure (network error, timeout, invalid response) MUST:
    1. Still result in an egress record (the attempt is audited before contact).
    2. Surface as a TYPED LLMGatewayError — not the raw litellm exception.

    Leaking provider internals (e.g. ``litellm.exceptions.ServiceUnavailableError``) would
    expose implementation details to callers and break the abstraction boundary.

    UNCHANGED by ADR 0104 item 2: a failing call still emits exactly the ONE pre-call
    record (no post-call emit happens because the provider call never succeeded) — the
    ``>= 1`` assertion below deliberately stays as-is, not weakened.
    """
    captured_records: list[Any] = []

    def _emit_spy(record: Any) -> None:
        captured_records.append(record)

    class _FakeProviderError(RuntimeError):
        """A fake exception simulating a litellm provider error."""

    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_emit_spy),
        patch("litellm.completion", side_effect=_FakeProviderError("fake-provider-down")),
    ):
        gw = LLMGateway(_local_settings())
        with pytest.raises(LLMGatewayError) as exc_info:
            gw.chat(messages=[{"role": "user", "content": "test"}])

    # The raw provider exception must NOT propagate unchanged.
    assert not isinstance(exc_info.value, _FakeProviderError), (
        "The gateway must wrap provider exceptions in LLMGatewayError — "
        "leaking the raw litellm exception breaks the abstraction boundary."
    )

    # The egress record MUST be present (the attempt was audited before the provider failed).
    assert len(captured_records) >= 1, (
        f"expected at least 1 egress record even on provider failure; "
        f"got {len(captured_records)}.  "
        "INV-S2-EGRESS: the record must be emitted BEFORE the provider call so a "
        "failing/timing-out call is still audited (ADR 0091 §1)."
    )


# ── Per-call mode= override routes to the specified mode ──────────────────────────────


def test_per_call_mode_override_routes_to_specified_mode_and_records_flags() -> None:
    """Per-call override: mode=LLMMode.OPENROUTER routes to OR model + data_left_perimeter=True.

    The gateway supports a per-call mode= override (the hook S5 will drive at runtime).
    When passed explicitly, it overrides the Settings default AND the egress record
    reflects the overridden mode's confidentiality / egress flags.
    """
    captured_records: list[Any] = []

    def _emit_spy(record: Any) -> None:
        captured_records.append(record)

    fake_response = _make_fake_response(model="openrouter/openai/gpt-4o")

    # Gateway is built with LOCAL settings but the per-call override selects OPENROUTER.
    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_emit_spy),
        patch("litellm.completion", return_value=fake_response) as mock_litellm,
    ):
        gw = LLMGateway(_local_settings())
        gw.chat(
            messages=[{"role": "user", "content": "override test"}],
            mode=LLMMode.OPENROUTER,
        )

    # The litellm call must use an openrouter/ model prefix.
    call_kwargs = mock_litellm.call_args
    model_arg = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("model", "")
    assert model_arg.startswith("openrouter/"), (
        f"per-call OPENROUTER override must route to 'openrouter/...' model; got {model_arg!r}."
    )

    # The egress record must reflect the overridden mode's flags. At least one record is
    # emitted (exact count is covered by the dedicated two-record test above).
    assert len(captured_records) >= 1
    rec = captured_records[0]
    assert rec.data_left_perimeter is True, (
        f"OPENROUTER override: data_left_perimeter must be True in the egress record; "
        f"got {rec.data_left_perimeter!r}.  "
        "INV-S2-DEFAULT: external modes always set data_left_perimeter=True."
    )
    assert rec.mode == LLMMode.OPENROUTER, (
        f"egress record must name the overridden mode; "
        f"expected LLMMode.OPENROUTER, got {rec.mode!r}."
    )


def test_absent_mode_override_falls_back_to_settings_default() -> None:
    """When no per-call mode= is passed, the gateway uses the Settings default.

    Settings(llm_mode='local') → LOCAL mode; the egress record must say data_left_perimeter=False.
    This is the inverse of the per-call-override test.
    """
    captured_records: list[Any] = []

    def _emit_spy(record: Any) -> None:
        captured_records.append(record)

    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_emit_spy),
        patch("litellm.completion", return_value=_make_fake_response()),
    ):
        gw = LLMGateway(_local_settings())
        gw.chat(messages=[{"role": "user", "content": "no override"}])  # no mode= kwarg

    assert len(captured_records) >= 1
    rec = captured_records[0]
    assert rec.data_left_perimeter is False, (
        f"absent mode= override must use LOCAL (Settings default); "
        f"data_left_perimeter must be False, got {rec.data_left_perimeter!r}."
    )
    assert rec.mode == LLMMode.LOCAL, (
        f"absent override must produce LLMMode.LOCAL in the record; got {rec.mode!r}."
    )


# ── No-secret-leak: api key and message content absent from egress log ────────────────


def test_no_api_key_or_message_content_in_egress_log(caplog: Any) -> None:
    """No-secret-leak: llm_openrouter_api_key and message content NEVER appear in any log line.

    The egress record names mode / host / flags / usage — NEVER the API key or message content
    (ADR 0091 §3: 'names mode/host/flags/usage — never the API key or the message content').

    Captures ALL log records emitted during a gateway call and asserts neither the key bytes
    nor the message content appear in any formatted log line.
    """
    import logging

    sensitive_key = "or-top-secret-key-must-not-appear-9f3a2b"
    sensitive_msg = "private-entity-data-do-not-log-xyz"

    settings = Settings(  # type: ignore[call-arg]
        llm_mode="openrouter",
        llm_ollama_model="llama3.2",
        llm_ollama_base_url="http://localhost:11434",
        llm_openrouter_api_key=sensitive_key,
        llm_openrouter_model="openai/gpt-4o",
        llm_claude_binary="claude",
        llm_claude_model_label="claude-test",
        llm_claude_timeout_seconds=30,
        llm_egress_log_enabled=True,
        _env_file=None,
    )

    with (
        caplog.at_level(logging.DEBUG),
        patch("litellm.completion", return_value=_make_fake_response()),
    ):
        gw = LLMGateway(settings)
        gw.chat(
            messages=[{"role": "user", "content": sensitive_msg}],
            mode=LLMMode.OPENROUTER,
        )

    all_log_text = "\n".join(r.getMessage() for r in caplog.records)

    assert sensitive_key not in all_log_text, (
        f"API key leaked into log output.  "
        f"The egress record must NEVER log the API key (ADR 0091 §3).  "
        f"Found key in log: {caplog.records!r}"
    )
    assert sensitive_msg not in all_log_text, (
        f"Message content leaked into log output.  "
        f"The egress record must NEVER log message content (ADR 0091 §3).  "
        f"Found message in log: {caplog.records!r}"
    )


def test_api_key_is_not_logged_in_egress_record_extra() -> None:
    """No-secret-leak: the EgressRecord captured by the emit spy contains no key bytes.

    Checks the record object directly (not the formatted log line) to catch cases where
    the key might be embedded in a record field even if not formatted into the log text.
    """
    sensitive_key = "or-secret-embedded-test-do-not-store-8a1c"
    captured_records: list[Any] = []

    def _emit_spy(record: Any) -> None:
        captured_records.append(record)

    settings = Settings(  # type: ignore[call-arg]
        llm_mode="openrouter",
        llm_ollama_model="llama3.2",
        llm_ollama_base_url="http://localhost:11434",
        llm_openrouter_api_key=sensitive_key,
        llm_openrouter_model="openai/gpt-4o",
        llm_claude_binary="claude",
        llm_claude_model_label="claude-test",
        llm_claude_timeout_seconds=30,
        llm_egress_log_enabled=True,
        _env_file=None,
    )

    with (
        patch("worldmonitor.llm.egress_log.emit", side_effect=_emit_spy),
        patch("litellm.completion", return_value=_make_fake_response()),
    ):
        gw = LLMGateway(settings)
        gw.chat(
            messages=[{"role": "user", "content": "test content"}],
            mode=LLMMode.OPENROUTER,
        )

    assert len(captured_records) >= 1
    rec = captured_records[0]
    # Serialize the record to a string and check for key leakage.
    record_str = str(vars(rec)) if hasattr(rec, "__dict__") else str(rec)
    assert sensitive_key not in record_str, (
        f"API key leaked into EgressRecord fields: {record_str!r}.  "
        "The record must log mode/host/flags/usage — never the API key (ADR 0091 §3)."
    )


# ── Gateway exposes only chat()/completion() as public egress surface ─────────────────


def test_gateway_has_no_public_egress_bypass_other_than_chat() -> None:
    """INV-S2-EGRESS: LLMGateway has no public method other than chat()/completion().

    The gateway is the ONLY egress surface (ADR 0091 §1: 'There is no other public surface
    that reaches a provider').  Any additional public method could bypass the egress-logging
    invariant.  This structural test asserts that only the intended entry points are public.

    Uses vars(LLMGateway) to inspect ONLY the class's own attributes (not inherited object
    methods), filtered to exclude private/dunder names.
    """
    public_own_attrs = {
        name
        for name in vars(LLMGateway)
        if not name.startswith("_") and callable(vars(LLMGateway).get(name))
    }
    # The spec allows either chat() or completion() or both (§4b says 'chat()/completion()').
    allowed = {"chat", "completion"}
    unexpected = public_own_attrs - allowed
    assert not unexpected, (
        f"LLMGateway exposes unexpected public callable attribute(s): {unexpected!r}.  "
        "Only chat()/completion() should be public (INV-S2-EGRESS: no bypass path, "
        "ADR 0091 §1).  Every other method must be private (prefixed with _)."
    )


# ── Claude shim: argv list, timeout, untrusted stdout (CLAUDE.md hostile-data rule) ───


def test_claude_shim_uses_argv_list_not_shell_string() -> None:
    """The claude -p shim invokes subprocess with an ARGV LIST, never a shell string.

    CLAUDE.md hostile-data rule: 'Heavy CLI tools in containers with constrained egress.
    No eval, no shell interpolation; treat all external/tool/scraped data as hostile.'
    Spec §7: 'Run claude -p as subprocess.run([binary, "-p", ...], ..., never shell=True,
    never string interpolation of untrusted data).'

    The test patches subprocess.run to intercept the call and verifies:
    1. The first argument is a list (argv list), not a string.
    2. shell=False (never shell=True).
    3. A timeout kwarg is present (spec §7: 'with a timeout').
    """
    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stdout = "mocked claude output"
    completed.stderr = ""

    with patch("subprocess.run", return_value=completed) as mock_run:
        gw = LLMGateway(_claude_settings())
        gw.chat(messages=[{"role": "user", "content": "hello from test"}])

    mock_run.assert_called_once()
    run_call = mock_run.call_args

    # argv must be a list (not a string joined for shell execution).
    argv = run_call.args[0] if run_call.args else run_call.kwargs.get("args")
    assert isinstance(argv, list), (
        f"subprocess.run must be called with an argv LIST; got {type(argv).__name__!r}: {argv!r}.  "
        "CLAUDE.md: 'no shell interpolation' — a string would require shell=True which "
        "enables injection via untrusted message content."
    )

    # shell must not be True.
    shell = run_call.kwargs.get("shell", False)
    assert shell is not True, (
        f"subprocess.run must NEVER be called with shell=True; got shell={shell!r}.  "
        "CLAUDE.md hostile-data rule: shell=True + any untrusted content = injection risk."
    )

    # A timeout must be present (subprocess deadline, spec §7).
    timeout = run_call.kwargs.get("timeout")
    assert timeout is not None and timeout > 0, (
        f"subprocess.run must be called with a positive timeout; got timeout={timeout!r}.  "
        "Spec §7: 'with a timeout' — an unbounded subprocess can hang the gateway."
    )


def test_claude_shim_stdout_treated_as_untrusted_no_secondary_subprocess() -> None:
    """The shim treats claude stdout as untrusted string content — no eval, no re-execution.

    A hostile stdout value (shell injection payload) must be passed through as the
    response content string WITHOUT triggering any additional subprocess calls.
    If the gateway or shim evaluated or shell-executed the stdout, mock_run.call_count
    would exceed 1 (or the eval would raise, which is also a failure mode).

    CLAUDE.md: 'treat all external/tool/scraped data as hostile: no eval, no shell
    interpolation'.
    """
    hostile_stdout = "__import__('os').system('id'); $(id); `id`"

    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stdout = hostile_stdout
    completed.stderr = ""

    with patch("subprocess.run", return_value=completed) as mock_run:
        gw = LLMGateway(_claude_settings())
        # Must not raise even with hostile content.
        gw.chat(messages=[{"role": "user", "content": "safe input"}])

    # Exactly one subprocess call: the gateway must not execute the hostile stdout.
    assert mock_run.call_count == 1, (
        f"subprocess.run called {mock_run.call_count} times; expected exactly 1.  "
        "Extra calls indicate the hostile stdout was evaluated or re-executed.  "
        "CLAUDE.md: no eval, no shell interpolation of external data."
    )


def test_claude_shim_only_invoked_for_claude_headless_mode() -> None:
    """The claude shim subprocess is NOT invoked when mode is LOCAL or OPENROUTER.

    The shim (and subprocess.run calls to claude -p) MUST only happen when
    CLAUDE_HEADLESS mode is explicitly selected.  For LOCAL mode (Ollama HTTP) and
    OPENROUTER mode (HTTP to openrouter.ai), no subprocess should be spawned.

    Spec §2 S2c: 'Off by default (only constructed/registered when llm_mode==claude_headless
    or explicitly selected).'
    """
    completed = MagicMock(spec=subprocess.CompletedProcess)
    completed.returncode = 0
    completed.stdout = "should not be reached"
    completed.stderr = ""

    # LOCAL mode: no subprocess.
    with (
        patch("subprocess.run", return_value=completed) as mock_run_local,
        patch("litellm.completion", return_value=_make_fake_response()),
    ):
        gw_local = LLMGateway(_local_settings())
        gw_local.chat(messages=[{"role": "user", "content": "local call"}])

    assert mock_run_local.call_count == 0, (
        f"subprocess.run should NOT be called for LOCAL (Ollama) mode; "
        f"got {mock_run_local.call_count} call(s).  "
        "The claude shim is ONLY for CLAUDE_HEADLESS mode."
    )

    # OPENROUTER mode: no subprocess.
    with (
        patch("subprocess.run", return_value=completed) as mock_run_or,
        patch("litellm.completion", return_value=_make_fake_response()),
    ):
        gw_or = LLMGateway(_or_settings())
        gw_or.chat(messages=[{"role": "user", "content": "openrouter call"}])

    assert mock_run_or.call_count == 0, (
        f"subprocess.run should NOT be called for OPENROUTER mode; "
        f"got {mock_run_or.call_count} call(s).  "
        "The claude shim is ONLY for CLAUDE_HEADLESS mode."
    )


# ================================================================================================
# Per-call request timeout (ADR 0115 hardening) — a wedged provider fails fast.
# ================================================================================================
def test_gateway_passes_request_timeout_to_litellm() -> None:
    """The configured ``llm_request_timeout_seconds`` is forwarded to ``litellm.completion``."""
    with patch("litellm.completion", return_value=_make_fake_response()) as mock_litellm:
        gateway = LLMGateway(_local_settings(llm_request_timeout_seconds=45.0))
        gateway.chat(messages=[{"role": "user", "content": "hi"}])
    assert mock_litellm.call_args.kwargs.get("timeout") == 45.0


def test_gateway_omits_timeout_when_zero() -> None:
    """``0`` opts out — no ``timeout`` kwarg, so litellm's own default applies."""
    with patch("litellm.completion", return_value=_make_fake_response()) as mock_litellm:
        gateway = LLMGateway(_local_settings(llm_request_timeout_seconds=0))
        gateway.chat(messages=[{"role": "user", "content": "hi"}])
    assert "timeout" not in mock_litellm.call_args.kwargs
