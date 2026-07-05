"""PRIMARY property test — Gate 1a review-queue web UI: read-only + XSS invariants (ADR 0103).

The mandatory ``@given`` pair for the read-only review surface (spec
``docs/reviews/GATE_1A_REVIEW_QUEUE_UI_SPEC.md`` §3, ``.claude/gate.scope``):

  INV-READONLY  For ANY generated set of parked (``pending_review``) merges, ``GET /review`` and
                ``GET /review/card?canonical_id=...`` return 200 and issue ZERO writes — neither a
                Neo4j write (a read-only Neo4j spy whose ``execute_write`` raises) nor a Postgres
                write (a real in-memory SQLite session whose ``flush``/``commit`` raises the moment
                it is asked to persist a pending ``new``/``dirty``/``deleted`` object), and the
                relational row-state is byte-identical before and after the GETs.
  INV-XSS       A hostile ``merge_audit.reason`` or a hostile FtM member-property value is
                HTML-escaped by Jinja autoescape (``&lt;script&gt;`` etc. present, the raw tag
                absent) — and NEVER evaluated as a template (a ``{{7*7}}`` payload must survive
                VERBATIM, not become ``49`` — no server-side template injection).

Docker-free: a fresh in-memory SQLite engine (JSONB shim) is built PER Hypothesis example and
disposed in a ``finally`` (no engine leak across examples, per the 3a-ii-A lesson).

RED at collection now: ``worldmonitor.api.review`` does not exist yet, so the module-level
``from worldmonitor.api.review import router`` import below raises ``ModuleNotFoundError`` before
any test body runs — the correct pre-implementation TDD failure.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.api.main import create_app
from worldmonitor.api.review import router as review_router  # noqa: F401  # CONTRACT: RED import
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.db.models import (
    Base,
    ErGoldPair,
    ErQueueItem,
    MergeAudit,
    ResolverJudgement,
    SignOff,
)
from worldmonitor.settings import Settings


# ---------------------------------------------------------------------------
# SQLite JSONB shim (idempotent if another test module already registered it).
# ---------------------------------------------------------------------------
@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


AUTH = {"Authorization": "Bearer good"}

_SNAPSHOT_MODELS = (MergeAudit, ErQueueItem, SignOff, ErGoldPair, ResolverJudgement)


# ---------------------------------------------------------------------------
# Fakes — a bearer verifier, a graph spy that FAILS any write, and a Session subclass that FAILS
# any commit/flush asked to persist a pending write (the two INV-READONLY oracles).
# ---------------------------------------------------------------------------
class _FakeVerifier:
    def verify(self, token: str) -> dict[str, str]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "reviewer-1"}


class _ReadOnlyNeo4jSpy:
    """``execute_read`` answers the ``_any_node_exists``/``_node_exists``-shaped queries
    ``list_parked`` issues with "no node exists"; ``execute_write`` ALWAYS raises."""

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        return [{"n": 0}]

    def execute_write(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        raise AssertionError(f"the review UI must never write to the graph: {query!r}")


class _WriteGuardSession(Session):
    """A real SQLAlchemy session that raises the instant it is asked to persist a pending write.

    ``flush``/``commit`` are allowed to run (and do nothing) when there is nothing pending — normal
    SQLAlchemy autoflush before a SELECT must not spuriously trip this — but raise loudly the moment
    ``new``/``dirty``/``deleted`` is non-empty (spec §3 INV-READONLY oracle (b)).
    """

    def flush(self, objects: Any = None) -> None:
        if self.new or self.dirty or self.deleted:
            raise AssertionError("the review UI issued a write via Session.flush()")
        super().flush(objects)

    def commit(self) -> None:
        if self.new or self.dirty or self.deleted:
            raise AssertionError("the review UI issued a write via Session.commit()")
        super().commit()


def _client(sessions: sessionmaker[Session]) -> TestClient:
    settings = Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-session-key-123",
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app(
        settings=settings,
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=_ReadOnlyNeo4jSpy(),  # type: ignore[arg-type]
        oauth=None,
        db_sessions=sessions,
    )
    return TestClient(app, raise_server_exceptions=False)


def _rows(session: Session, model: type[Any]) -> list[tuple[Any, ...]]:
    """A deterministic, order-independent snapshot of every column of every row of ``model``."""
    cols = [c.name for c in model.__table__.columns]
    return sorted(
        (tuple(getattr(row, c) for c in cols) for row in session.execute(select(model)).scalars()),
        key=repr,
    )


def _snapshot(sessions: sessionmaker[Session]) -> dict[str, list[tuple[Any, ...]]]:
    with sessions() as session:
        return {model.__tablename__: _rows(session, model) for model in _SNAPSHOT_MODELS}


# ---------------------------------------------------------------------------
# Fixture data — parked merges with hostile-including reasons/values (§3 generator).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _MergeSeed:
    member_count: int
    score: float
    reason: str
    member_values: tuple[str, ...]


_STRVALUES = (
    "<script>alert(1)</script>",
    '"><svg onload=alert(1)>',
    "quote\" 'break' here",
    "Iñtërnâtiônàlizætiøn ☃ 漢字",
    "line sep para separators",
    "plain benign value",
)


@st.composite
def _parked_merges(draw: st.DrawFn) -> list[_MergeSeed]:
    n = draw(st.integers(min_value=1, max_value=2))
    seeds: list[_MergeSeed] = []
    for _ in range(n):
        member_count = draw(st.integers(min_value=1, max_value=4))
        score = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False))
        reason = draw(st.sampled_from(_STRVALUES))
        member_values = draw(
            st.lists(st.sampled_from(_STRVALUES), min_size=member_count, max_size=member_count)
        )
        seeds.append(
            _MergeSeed(
                member_count=member_count,
                score=score,
                reason=reason,
                member_values=tuple(member_values),
            )
        )
    return seeds


def _seed(session: Session, seed: _MergeSeed) -> str:
    """Write one ``pending_review`` MergeAudit + its matching ErQueueItem rows.

    Returns the minted canonical_id.
    """
    canonical_id = f"wmc-{uuid.uuid4().hex}"
    member_ids = [f"m-{uuid.uuid4().hex}" for _ in range(seed.member_count)]
    for member_id, value in zip(member_ids, seed.member_values, strict=True):
        session.add(
            ErQueueItem(
                id=str(uuid.uuid4()),
                connector_id="test-connector",
                entity_id=member_id,
                raw_entity={
                    "id": member_id,
                    "schema": "Person",
                    "properties": {"name": [value], "notes": [value]},
                },
                source_record=f"s3://landing/{member_id}.json",
                status="pending_review",
            )
        )
    session.add(
        MergeAudit(
            id=str(uuid.uuid4()),
            canonical_id=canonical_id,
            source_ids=member_ids,
            score=seed.score,
            decision="pending_review",
            reason=seed.reason,
        )
    )
    return canonical_id


def _new_sqlite_engine() -> Any:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


# ===========================================================================
# INV-READONLY
# ===========================================================================
@hyp_settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(seeds=_parked_merges())
def test_p_review_readonly_gets_never_write(seeds: list[_MergeSeed]) -> None:
    engine = _new_sqlite_engine()
    try:
        plain_sessions: sessionmaker[Session] = sessionmaker(bind=engine)
        guard_sessions: sessionmaker[Session] = sessionmaker(bind=engine, class_=_WriteGuardSession)

        canonical_ids: list[str] = []
        with plain_sessions() as session:
            for seed in seeds:
                canonical_ids.append(_seed(session, seed))
            session.commit()

        before = _snapshot(plain_sessions)

        client = _client(guard_sessions)
        list_resp = client.get("/review", headers=AUTH)
        assert list_resp.status_code == 200, (
            f"GET /review over parked merges must be a read-only 200: "
            f"{list_resp.status_code} {list_resp.text}"
        )

        for canonical_id in canonical_ids:
            card_resp = client.get(
                "/review/card", params={"canonical_id": canonical_id}, headers=AUTH
            )
            assert card_resp.status_code == 200, (
                f"GET /review/card for a REAL parked canonical id must be a read-only 200: "
                f"{card_resp.status_code} {card_resp.text}"
            )

        after = _snapshot(plain_sessions)
        assert after == before, (
            "the relational row-state changed after read-only GETs — INV-READONLY violated:\n"
            f"before={before}\nafter={after}"
        )
    finally:
        engine.dispose()


# ===========================================================================
# INV-XSS
# ===========================================================================
_HOSTILE_VARIANTS: tuple[tuple[str, str, str], ...] = (
    ("script_tag", "<script>alert(1)</script>", "&lt;script&gt;"),
    ("img_onerror", "<img src=x onerror=alert(1)>", "&lt;img"),
    ("quote_break", '"><svg onload=alert(1)>', "&lt;svg"),
    # A DISTINCTIVE product so the "was it evaluated?" check below cannot false-positive on a
    # random id/hash substring: 31337*31337 == 982006969 (9 digits, never a uuid-hex run), vs
    # the old 7*7==49 which collided with hex like ...4943... in a generated canonical_id.
    ("jinja_braces", "{{31337*31337}}", ""),
)
_JINJA_EVAL_FORM = "982006969"  # what an SSTI sink would render {{31337*31337}} as


def _assert_hostile_neutralised(body: str, name: str, payload: str, escaped_marker: str) -> None:
    if name == "jinja_braces":
        assert payload in body, (
            f"a hostile Jinja-brace value {payload!r} must survive VERBATIM (never evaluated as a "
            f"template — SSTI); it was not found verbatim in the response"
        )
        assert _JINJA_EVAL_FORM not in body, (
            f"a hostile Jinja-brace value {payload!r} appears to have been EVALUATED "
            f"(found its product {_JINJA_EVAL_FORM!r} — SSTI)"
        )
        return
    assert payload not in body, f"a hostile value was rendered RAW (unescaped): {payload!r}"
    assert escaped_marker in body, (
        f"expected the HTML-escaped form {escaped_marker!r} for hostile value {payload!r}"
    )


@hyp_settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    reason_variant=st.sampled_from(_HOSTILE_VARIANTS),
    member_variant=st.sampled_from(_HOSTILE_VARIANTS),
)
def test_p_review_xss_hostile_values_escaped(
    reason_variant: tuple[str, str, str], member_variant: tuple[str, str, str]
) -> None:
    reason_name, reason_payload, reason_escaped = reason_variant
    member_name, member_payload, member_escaped = member_variant

    engine = _new_sqlite_engine()
    try:
        plain_sessions: sessionmaker[Session] = sessionmaker(bind=engine)
        guard_sessions: sessionmaker[Session] = sessionmaker(bind=engine, class_=_WriteGuardSession)

        seed = _MergeSeed(
            member_count=1, score=0.75, reason=reason_payload, member_values=(member_payload,)
        )
        with plain_sessions() as session:
            canonical_id = _seed(session, seed)
            session.commit()

        client = _client(guard_sessions)

        list_resp = client.get("/review", headers=AUTH)
        assert list_resp.status_code == 200, f"{list_resp.status_code} {list_resp.text}"
        _assert_hostile_neutralised(list_resp.text, reason_name, reason_payload, reason_escaped)

        card_resp = client.get("/review/card", params={"canonical_id": canonical_id}, headers=AUTH)
        assert card_resp.status_code == 200, f"{card_resp.status_code} {card_resp.text}"
        _assert_hostile_neutralised(card_resp.text, member_name, member_payload, member_escaped)
    finally:
        engine.dispose()
