"""Integration tests — Gate F2: durable, append-only LLM-egress audit (ADR 0105), §4 of
``docs/reviews/GATE_F2_DURABLE_EGRESS_AUDIT_SPEC.md``.

Real Postgres (testcontainer, ``postgres_dsn`` fixture — mirrors
``tests/integration/test_statement_spine.py``): applies the ORM schema (``create_all``, the
repo's integration idiom for exercising the head schema without a full Alembic run — the
migration ``0011`` drift itself is proven by the UNCHANGED ``test_migrations.py`` guard), drives
a REAL ``LLMGateway.chat()`` bound to a REAL sessionmaker with a patched ``litellm.completion``,
then SELECTs the durable rows back and asserts on their real, persisted, JSONB/NULL-typed
column values.

Covers spec §4:
    (a) two rows on success with a shared ``call_id``, correct ``phase``, hex fingerprint,
        tokens on the completed row (and NULL on the attempt row).
    (b) column fidelity — JSONB ``entity_manifest`` round-trips; NULL placement matches
        the table spec (attempt: tokens NULL; completed: fingerprint/manifest NULL).
    (c) no column of ANY persisted row contains the message text or the api key.
    (d) an EXTERNAL crossing whose durable sink is unreachable (a genuinely broken Postgres
        connection — connection refused, not a mock) refuses with ``LLMGatewayError`` and
        the provider is never contacted; the REAL, reachable ``llm_egress`` table stays
        empty.

``tests/integration/test_migrations.py`` stays green UNCHANGED — its drift guard exercises the
new ``llm_egress`` table automatically once model + migration agree (not re-verified here).

RED TODAY:
    ``ImportError: cannot import name 'LlmEgressRecord' from 'worldmonitor.db.models'``
    (the model does not exist yet; even once it does, ``ModuleNotFoundError`` for
    ``worldmonitor.llm.egress_audit`` and ``LLMGateway.__init__`` rejecting
    ``session_factory=`` block every test below.)

──────────────────────────────────────────────────────────────────────────────────────────
BUILDER CONTRACTS (spec §2.2, §2.4-2.6):

    worldmonitor.db.models.LlmEgressRecord   # new model, table "llm_egress"
        id, call_id, phase, mode, confidentiality, target_host, data_left_perimeter,
        model, caller_tag, content_fingerprint, entity_manifest (JSONB), prompt_tokens,
        completion_tokens, total_tokens, created_at

    worldmonitor.llm.gateway.LLMGateway(settings, session_factory=None)
        chat(messages, *, mode=None, caller_tag="gateway", entity_ids=None) -> Any
──────────────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, make_url, select
from sqlalchemy.orm import sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import LlmEgressRecord
from worldmonitor.llm.gateway import LLMGateway, LLMGatewayError
from worldmonitor.llm.modes import LLMMode
from worldmonitor.settings import Settings

pytestmark = pytest.mark.integration


# ── Helpers ─────────────────────────────────────────────────────────────────────────────


def _make_test_settings(
    *,
    llm_mode: str = "openrouter",
    llm_egress_durable_enabled: bool = True,
    **extra: Any,
) -> Settings:
    """Minimal Settings for the durable-audit integration path — no .env read (CI-safe).

    ``extra.setdefault`` (not a hardcoded kwarg) for ``llm_openrouter_api_key`` so a caller
    overriding it via ``**extra`` (e.g. the no-content-leak test's sensitive-key probe) does
    not collide with a duplicate keyword argument to ``Settings(...)``.
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
        llm_egress_log_enabled=True,
        llm_egress_durable_enabled=llm_egress_durable_enabled,
        _env_file=None,
        **extra,
    )


def _make_fake_response(
    prompt_tokens: int = 101, completion_tokens: int = 202, total_tokens: int = 303
) -> MagicMock:
    resp = MagicMock()
    resp.model = "fake-model"
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "integration-test durable response"
    resp.usage = MagicMock()
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    resp.usage.total_tokens = total_tokens
    return resp


# ---------------------------------------------------------------------------
# (a) + (b): success -> two correlated rows, JSONB manifest round-trip, NULL placement
# ---------------------------------------------------------------------------


def test_successful_external_call_writes_two_correlated_rows_with_column_fidelity(
    postgres_dsn: str,
) -> None:
    """A successful EXTERNAL call writes exactly two ``llm_egress`` rows sharing one
    ``call_id``: an "attempt" row (hex fingerprint, declared entity manifest, NULL tokens)
    and a "completed" row (NULL fingerprint/manifest, the response's real token counts).

    RED today: ImportError — LlmEgressRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    entity_ids = ["Q42", "opensanctions:abcd1234"]
    settings = _make_test_settings()
    gateway = LLMGateway(settings, session_factory=sessions)

    fake_response = _make_fake_response(prompt_tokens=101, completion_tokens=202, total_tokens=303)

    with patch("litellm.completion", return_value=fake_response):
        gateway.chat(
            messages=[{"role": "user", "content": "F2 integration probe"}],
            mode=LLMMode.OPENROUTER,
            entity_ids=entity_ids,
        )

    with sessions() as session:
        rows = list(session.execute(select(LlmEgressRecord)).scalars())

    assert len(rows) == 2, (
        f"expected exactly 2 durable 'llm_egress' rows for one successful external call; "
        f"got {len(rows)}: {[(r.id, r.phase) for r in rows]!r}"
    )

    by_phase = {row.phase: row for row in rows}
    assert set(by_phase) == {"attempt", "completed"}, (
        f"expected phases {{'attempt', 'completed'}}; got {set(by_phase)!r}"
    )
    attempt, completed = by_phase["attempt"], by_phase["completed"]

    assert attempt.call_id == completed.call_id, (
        f"attempt.call_id={attempt.call_id!r} != completed.call_id={completed.call_id!r}: "
        "the two rows of one crossing must share call_id."
    )
    assert attempt.call_id, "call_id must be non-empty"
    assert attempt.id != completed.id, "each row must carry its OWN fresh primary-key id"

    # (a) attempt row: hex fingerprint, declared manifest, NULL tokens.
    assert re.fullmatch(r"[0-9a-f]{64}", attempt.content_fingerprint or ""), (
        f"attempt.content_fingerprint must be a 64-char lowercase hex digest; "
        f"got {attempt.content_fingerprint!r}"
    )
    assert attempt.prompt_tokens is None
    assert attempt.completion_tokens is None
    assert attempt.total_tokens is None

    # (b) JSONB round-trip: the declared entity_ids list survives Postgres storage exactly.
    assert attempt.entity_manifest == entity_ids, (
        f"entity_manifest must round-trip through JSONB unchanged; "
        f"expected {entity_ids!r}, got {attempt.entity_manifest!r}"
    )

    # (a) + (b) completed row: NULL fingerprint/manifest, real token counts.
    assert completed.content_fingerprint is None, (
        f"completed row's content_fingerprint must be NULL; got {completed.content_fingerprint!r}"
    )
    assert completed.entity_manifest is None, (
        f"completed row's entity_manifest must be NULL; got {completed.entity_manifest!r}"
    )
    assert completed.prompt_tokens == 101, (
        f"completed.prompt_tokens expected 101; got {completed.prompt_tokens!r}"
    )
    assert completed.completion_tokens == 202, (
        f"completed.completion_tokens expected 202; got {completed.completion_tokens!r}"
    )
    assert completed.total_tokens == 303, (
        f"completed.total_tokens expected 303; got {completed.total_tokens!r}"
    )

    # Context columns copied onto both rows.
    assert attempt.mode == "openrouter" == completed.mode
    assert attempt.data_left_perimeter is True
    assert completed.data_left_perimeter is True

    engine.dispose()


def test_call_without_entity_ids_writes_null_manifest(postgres_dsn: str) -> None:
    """A call that does NOT declare ``entity_ids`` (the ``/v1`` wire-caller default) writes
    ``entity_manifest=NULL`` — honestly recorded absence, never a faked empty list.

    RED today: ImportError — LlmEgressRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    settings = _make_test_settings()
    gateway = LLMGateway(settings, session_factory=sessions)

    with patch("litellm.completion", return_value=_make_fake_response()):
        gateway.chat(
            messages=[{"role": "user", "content": "no-manifest probe"}],
            mode=LLMMode.OPENROUTER,
        )  # entity_ids omitted -> default None

    with sessions() as session:
        attempt = session.execute(
            select(LlmEgressRecord).where(LlmEgressRecord.phase == "attempt")
        ).scalar_one()

    assert attempt.entity_manifest is None, (
        f"undeclared entity_ids must persist as NULL, not []; got {attempt.entity_manifest!r}"
    )

    engine.dispose()


# ---------------------------------------------------------------------------
# (c) no column of any persisted row contains the message text or the api key
# ---------------------------------------------------------------------------


def test_no_persisted_column_contains_message_text_or_api_key(postgres_dsn: str) -> None:
    """No column of ANY row actually persisted to Postgres contains the raw message
    content or the configured api key — checked against the REAL round-tripped row values
    (not the in-memory object before insert).

    RED today: ImportError — LlmEgressRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    sensitive_key = "or-integration-secret-do-not-store-9f2a"
    sensitive_msg = "integration-private-payload-do-not-store-77x"

    settings = _make_test_settings(llm_openrouter_api_key=sensitive_key)
    gateway = LLMGateway(settings, session_factory=sessions)

    with patch("litellm.completion", return_value=_make_fake_response()):
        gateway.chat(
            messages=[{"role": "user", "content": sensitive_msg}],
            mode=LLMMode.OPENROUTER,
        )

    with sessions() as session:
        rows = list(session.execute(select(LlmEgressRecord)).scalars())

    assert rows, "expected at least one persisted row to inspect"
    for row in rows:
        for column in LlmEgressRecord.__table__.columns:
            value_str = str(getattr(row, column.name))
            assert sensitive_key not in value_str, (
                f"persisted column {column.name!r} of a {row.phase!r} row leaked the api "
                f"key: {value_str!r}"
            )
            assert sensitive_msg not in value_str, (
                f"persisted column {column.name!r} of a {row.phase!r} row leaked the "
                f"message content: {value_str!r}"
            )

    engine.dispose()


# ---------------------------------------------------------------------------
# (d) external crossing + unreachable durable sink -> refuse, write nothing
# ---------------------------------------------------------------------------


def test_external_call_with_unreachable_db_refuses_and_writes_nothing(postgres_dsn: str) -> None:
    """An EXTERNAL crossing whose durable sink points at a genuinely unreachable Postgres
    (connection refused — a real network failure, not a mocked exception) refuses with
    ``LLMGatewayError``; the provider is NEVER contacted; and the REAL, reachable
    ``llm_egress`` table (a separate, working database) stays completely empty.

    RED today: ImportError — LlmEgressRecord does not exist yet.
    """
    # The REAL, working database — proves nothing durable landed anywhere reachable.
    engine = make_engine(postgres_dsn)
    create_all(engine)
    working_sessions = session_factory(engine)

    # A genuinely unreachable database: same credentials, host/port rewritten to a closed
    # port on loopback -> immediate "connection refused" (bounded via connect_timeout, so a
    # misconfigured network can't hang the test).
    bad_url = make_url(postgres_dsn).set(host="127.0.0.1", port=1)
    bad_engine = create_engine(bad_url, connect_args={"connect_timeout": 3})
    bad_sessions = sessionmaker(bind=bad_engine)

    settings = _make_test_settings()
    gateway = LLMGateway(settings, session_factory=bad_sessions)

    provider_calls: list[Any] = []

    def _provider_spy(*args: Any, **kwargs: Any) -> Any:
        provider_calls.append((args, kwargs))
        return _make_fake_response()

    with (
        patch("litellm.completion", side_effect=_provider_spy),
        pytest.raises(LLMGatewayError) as exc_info,
    ):
        gateway.chat(
            messages=[{"role": "user", "content": "unreachable-db probe"}],
            mode=LLMMode.OPENROUTER,
        )

    assert not provider_calls, (
        f"the provider must NEVER be contacted when the durable sink is unreachable; "
        f"got {len(provider_calls)} call(s)."
    )
    assert "durable audit" in str(exc_info.value).lower(), (
        f"the refusal must be attributable to the durable-audit write failure, not some "
        f"unrelated error; got: {exc_info.value!r}"
    )

    with working_sessions() as session:
        row_count = session.execute(select(LlmEgressRecord)).scalars().all()
    assert row_count == [], (
        f"the REAL, reachable llm_egress table must stay empty — no row landed anywhere "
        f"queryable; got {len(row_count)} row(s)."
    )

    bad_engine.dispose()
    engine.dispose()
