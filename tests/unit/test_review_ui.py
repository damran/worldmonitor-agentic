"""Gate 1a — review-queue web UI: container-free example invariants (ADR 0103).

The DB-free-but-real half of the review-queue gate: a real in-memory SQLite session factory
(JSONB shim, mirrors ``tests/unit/test_driver_projection_diff.py``) + a fake bearer verifier +
a configurable Neo4j fake, injected via ``create_app(..., db_sessions=, neo4j_client=)`` (the exact
seam ``tests/unit/test_integrations_ui.py`` uses for the sibling Integrations UI gate, ADR 0069).

Covers (spec ``docs/reviews/GATE_1A_REVIEW_QUEUE_UI_SPEC.md`` §3 / ``.claude/gate.scope``):
  INV-QUEUE          GET /review lists EVERY pending_review merge (canonical id, guard reason
                     verbatim, and one ``.wm-confidence-band``/``.wm-badge-blocked`` PER row).
  INV-SENSITIVE      The prominent sensitive badge (``.wm-badge-sensitive``) is driven by
                     ``is_sensitive`` over the REAL members, never by substring-matching the
                     free-text reason — a benign merge whose reason contains the word "sensitive"
                     must NOT get the badge (exactly one badge across the two seeded merges).
  INV-BAND-NOT-VERDICT  The score renders in ``.wm-confidence-band`` with its numeric value; no
                     MATCH/PASS/FAIL/VERDICT word appears anywhere in the response.
  INV-CARD-DIFF      GET /review/card renders one card per member from raw_entity (schema, props,
                     a source chip: source_id/reliability/retrieved_at/raw pointer), marks an
                     agreeing property and a contradicting one, degrades an unparseable member to
                     an "unparseable" card WITHOUT writing (write-spy + a post-hoc dead-letter-count
                     check), and 404s on an unknown canonical_id.
  INV-RECOVER        A Neo4j existence hit for a member/canonical id surfaces the graph_written
                     recovery flag (mirrors the CLI's "[GRAPH-WRITTEN...]" note) — present iff true.
  INV-AUTH           No Authorization + Accept text/html -> 302 /login; tokenless JSON -> 401; a
                     bad bearer -> 401 (both routes).

RED at collection now: ``worldmonitor.api.review`` does not exist yet, so the module-level
``from worldmonitor.api.review import router`` import below raises ``ModuleNotFoundError``.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.api.main import create_app
from worldmonitor.api.review import router as review_router  # noqa: F401  # CONTRACT: RED import
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.db.models import Base, ErQueueItem, IngestDeadLetter, MergeAudit
from worldmonitor.settings import Settings

AUTH = {"Authorization": "Bearer good"}
_VERDICT_WORDS = ("MATCH", "PASS", "FAIL", "VERDICT")


# ---------------------------------------------------------------------------
# SQLite JSONB shim (idempotent if another test module already registered it).
# ---------------------------------------------------------------------------
@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------
class _FakeVerifier:
    def verify(self, token: str) -> dict[str, str]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "reviewer-1"}


class _FakeNeo4j:
    """A configurable read-only graph fake: ``existing_ids`` answers ``list_parked``'s
    ``_any_node_exists``/``_node_exists``-shaped read queries; ``execute_write`` ALWAYS raises."""

    def __init__(self, existing_ids: frozenset[str] = frozenset()) -> None:
        self._existing = existing_ids

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        ids = params.get("ids")
        if ids is not None:
            return [{"n": sum(1 for i in ids if i in self._existing)}]
        node_id = params.get("id")
        if node_id is not None:
            return [{"n": 1 if node_id in self._existing else 0}]
        return [{"n": 0}]

    def execute_write(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        raise AssertionError(f"the review UI must never write to the graph: {query!r}")


class _WriteGuardSession(Session):
    """Raises the instant it is asked to persist a pending write (INV-CARD-DIFF's no-write half)."""

    def flush(self, objects: Any = None) -> None:
        if self.new or self.dirty or self.deleted:
            raise AssertionError("the review UI issued a write via Session.flush()")
        super().flush(objects)

    def commit(self) -> None:
        if self.new or self.dirty or self.deleted:
            raise AssertionError("the review UI issued a write via Session.commit()")
        super().commit()


@dataclass(frozen=True)
class _Sessions:
    plain: sessionmaker[Session]
    guard: sessionmaker[Session]


@pytest.fixture
def sessions() -> Iterator[_Sessions]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    try:
        yield _Sessions(
            plain=sessionmaker(bind=engine),
            guard=sessionmaker(bind=engine, class_=_WriteGuardSession),
        )
    finally:
        engine.dispose()


def _client(db_sessions: sessionmaker[Session], neo4j: Any = None) -> TestClient:
    settings = Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-session-key-123",
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app(
        settings=settings,
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=neo4j if neo4j is not None else _FakeNeo4j(),  # type: ignore[arg-type]
        oauth=None,
        db_sessions=db_sessions,
    )
    return TestClient(app, raise_server_exceptions=False)


def _member(
    member_id: str,
    *,
    name: str = "Real Person",
    schema: str = "Person",
    extra_props: dict[str, list[str]] | None = None,
) -> ErQueueItem:
    props: dict[str, list[str]] = {"name": [name]}
    if extra_props:
        props.update(extra_props)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="test-connector",
        entity_id=member_id,
        raw_entity={"id": member_id, "schema": schema, "properties": props},
        source_record=f"s3://landing/{member_id}.json",
        status="pending_review",
    )


def _bad_member(member_id: str) -> ErQueueItem:
    """A queue row whose ``raw_entity`` an FtM entity parser (``make_entity``) cannot build
    (unknown schema) — the source of an "unparseable" member card."""
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="test-connector",
        entity_id=member_id,
        raw_entity={"id": member_id, "schema": "NotARealFtmSchema", "properties": {}},
        source_record=f"s3://landing/{member_id}.json",
        status="pending_review",
    )


def _merge(canonical_id: str, member_ids: list[str], *, score: float, reason: str) -> MergeAudit:
    return MergeAudit(
        id=str(uuid.uuid4()),
        canonical_id=canonical_id,
        source_ids=member_ids,
        score=score,
        decision="pending_review",
        reason=reason,
    )


# ===========================================================================
# INV-QUEUE
# ===========================================================================
def test_inv_queue_lists_every_pending_review_merge_with_counts(sessions: _Sessions) -> None:
    cid_a, cid_b = "wmc-queue-a", "wmc-queue-b"
    with sessions.plain() as session:
        session.add(_member("qa-1"))
        session.add(_member("qa-2"))
        session.add(_member("qb-1"))
        session.add(_merge(cid_a, ["qa-1", "qa-2"], score=0.91, reason="two similar companies"))
        session.add(_merge(cid_b, ["qb-1"], score=0.55, reason="single low-score record"))
        session.commit()

    client = _client(sessions.guard)
    resp = client.get("/review", headers=AUTH)
    assert resp.status_code == 200, (
        f"GET /review must list parked merges: {resp.status_code} {resp.text}"
    )
    body = resp.text

    assert cid_a in body, "every pending_review canonical id must be listed"
    assert cid_b in body, "every pending_review canonical id must be listed"
    assert "two similar companies" in body, "the guard reason must be shown verbatim"
    assert "single low-score record" in body, "the guard reason must be shown verbatim"
    assert body.count("wm-confidence-band") == 2, (
        "the queue must render exactly one confidence band PER parked merge (proves the LIST, "
        f"not just one row): got {body.count('wm-confidence-band')}"
    )
    assert body.count("wm-badge-blocked") == 2, (
        "every parked row must show the base 'blocked pending human sign-off' state: "
        f"got {body.count('wm-badge-blocked')}"
    )


# ===========================================================================
# INV-SENSITIVE
# ===========================================================================
def test_inv_sensitive_badge_is_driven_by_is_sensitive_not_reason_text(sessions: _Sessions) -> None:
    sensitive_cid, benign_cid = "wmc-sensitive", "wmc-benign"
    with sessions.plain() as session:
        # A REAL sensitive member (FtM risk topic) with a totally benign reason.
        session.add(
            _member("sens-1", name="Vladimir Example", extra_props={"topics": ["sanction"]})
        )
        session.add(_merge(sensitive_cid, ["sens-1"], score=0.8, reason="two similar companies"))
        # A benign member whose REASON happens to contain the word "sensitive" — must NOT badge.
        session.add(_member("benign-1", name="Acme Holdings"))
        session.add(
            _merge(
                benign_cid,
                ["benign-1"],
                score=0.6,
                reason="flagged as sensitive due to a score outlier",
            )
        )
        session.commit()

    client = _client(sessions.guard)
    resp = client.get("/review", headers=AUTH)
    assert resp.status_code == 200
    body = resp.text

    assert sensitive_cid in body
    assert benign_cid in body
    assert body.count("wm-badge-sensitive") == 1, (
        "EXACTLY one merge (the one with a real risk-topic member) may show the sensitive badge — "
        "a reason merely containing the word 'sensitive' must NOT trigger it: "
        f"got {body.count('wm-badge-sensitive')} occurrences"
    )


def test_inv_sensitive_badge_fails_closed_on_unparseable_member(sessions: _Sessions) -> None:
    """FAIL-CLOSED: a member whose ``raw_entity`` does not parse cannot be *cleared* as
    non-sensitive, so its merge MUST still show the sensitive badge — a sensitivity warning
    must fail toward MORE caution, never render a sanction-tagged-but-unparseable member as
    silently un-flagged (defence-in-depth beyond the pipeline's pre-cluster quarantine)."""
    cid = "wmc-unparseable-sensitive"
    with sessions.plain() as session:
        session.add(_bad_member("poison-1"))  # unknown schema ⇒ make_entity cannot parse it
        session.add(
            _merge(cid, ["poison-1"], score=0.7, reason="a merge of one unparseable member")
        )
        session.commit()

    body = _client(sessions.guard).get("/review", headers=AUTH).text
    assert cid in body
    assert body.count("wm-badge-sensitive") == 1, (
        "an unparseable member must fail CLOSED — its merge must still show the sensitive caution "
        f"badge (unknown sensitivity ⇒ not cleared); got {body.count('wm-badge-sensitive')}"
    )


# ===========================================================================
# INV-BAND-NOT-VERDICT
# ===========================================================================
def test_inv_band_not_verdict_score_has_no_verdict_styling(sessions: _Sessions) -> None:
    cid = "wmc-band"
    score = 0.732
    with sessions.plain() as session:
        session.add(_member("band-1"))
        session.add(_merge(cid, ["band-1"], score=score, reason="borderline duplicate"))
        session.commit()

    client = _client(sessions.guard)
    resp = client.get("/review", headers=AUTH)
    assert resp.status_code == 200
    body = resp.text

    assert "wm-confidence-band" in body, (
        "the score must render inside a .wm-confidence-band element"
    )
    numeric_forms = (f"{score:.2f}", f"{score:.3f}", str(round(score * 100)), f"{score}")
    assert any(form in body for form in numeric_forms), (
        f"the numeric score value must be rendered; tried {numeric_forms!r} against the body"
    )
    for word in _VERDICT_WORDS:
        assert re.search(rf"\b{word}\b", body, re.IGNORECASE) is None, (
            f"the confidence band must NEVER carry a verdict word — found {word!r} in the response"
        )


# ===========================================================================
# INV-CARD-DIFF
# ===========================================================================
def test_inv_card_diff_renders_members_agreement_and_contradiction(sessions: _Sessions) -> None:
    cid = "wmc-card-diff"
    with sessions.plain() as session:
        session.add(
            _member(
                "card-1",
                name="Acme Holdings Ltd",
                extra_props={"jurisdiction": ["cy"]},
            )
        )
        session.add(
            _member(
                "card-2",
                name="Acme Holdings Limited",
                extra_props={"jurisdiction": ["cy"]},
            )
        )
        session.add(_merge(cid, ["card-1", "card-2"], score=0.93, reason="near-duplicate names"))
        session.commit()

    client = _client(sessions.guard)
    resp = client.get("/review/card", params={"canonical_id": cid}, headers=AUTH)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    body = resp.text

    assert "Acme Holdings Ltd" in body, "member 1's raw_entity value must render"
    assert "Acme Holdings Limited" in body, "member 2's raw_entity value must render"
    assert "cy" in body, "the AGREEING jurisdiction value must render"
    assert "agree" in body.lower(), "an agreeing property must be marked as such in the diff"
    assert "contradict" in body.lower(), (
        "a differing property (name) must be marked a contradiction"
    )


def test_inv_card_diff_unparseable_member_degrades_without_write(sessions: _Sessions) -> None:
    cid = "wmc-card-poison"
    with sessions.plain() as session:
        session.add(_member("good-member", name="Real Person"))
        session.add(_bad_member("bad-member"))
        session.add(
            _merge(cid, ["good-member", "bad-member"], score=0.7, reason="mixed-quality cluster")
        )
        session.commit()

    client = _client(sessions.guard)  # write-guard: any write during the GET raises
    resp = client.get("/review/card", params={"canonical_id": cid}, headers=AUTH)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
    body = resp.text

    assert "unparseable" in body.lower(), (
        "a member whose raw_entity make_entity cannot parse must degrade to an 'unparseable' card"
    )
    assert "Real Person" in body, "the OTHER (parseable) member must still render its own card"

    with sessions.plain() as session:
        dead_letters = session.execute(
            select(func.count()).select_from(IngestDeadLetter)
        ).scalar_one()
    assert dead_letters == 0, (
        "the read-only card loader must NOT dead-letter a poison row (that is a WRITE via "
        "signoff._dead_letter_poison) — it must have its OWN read-only loader"
    )


def test_inv_card_diff_unknown_canonical_id_is_404(sessions: _Sessions) -> None:
    client = _client(sessions.guard)
    resp = client.get("/review/card", params={"canonical_id": "wmc-does-not-exist"}, headers=AUTH)
    assert resp.status_code == 404, (
        f"an unknown / non-pending_review canonical_id must 404: got {resp.status_code}"
    )


# ===========================================================================
# INV-RECOVER
# ===========================================================================
def test_inv_recover_graph_written_flag_surfaces_from_neo4j(sessions: _Sessions) -> None:
    cid = "wmc-recover"
    with sessions.plain() as session:
        session.add(_member("rec-1"))
        session.add(_merge(cid, ["rec-1"], score=0.85, reason="half-committed sign-off"))
        session.commit()

    written_resp = _client(sessions.guard, _FakeNeo4j(existing_ids=frozenset({cid}))).get(
        "/review", headers=AUTH
    )
    assert written_resp.status_code == 200
    assert "graph-written" in written_resp.text.lower(), (
        "when the graph already holds a node for the parked canonical id, the queue row must "
        "surface the graph_written recovery flag (mirrors the CLI's '[GRAPH-WRITTEN...]' note)"
    )

    not_written_resp = _client(sessions.guard, _FakeNeo4j(existing_ids=frozenset())).get(
        "/review", headers=AUTH
    )
    assert not_written_resp.status_code == 200
    assert "graph-written" not in not_written_resp.text.lower(), (
        "the SAME merge must NOT surface the recovery flag when the graph holds no matching node"
    )


# ===========================================================================
# INV-AUTH
# ===========================================================================
@pytest.mark.parametrize("path", ["/review", "/review/card?canonical_id=whatever"])
def test_inv_auth_no_header_html_redirects_to_login(sessions: _Sessions, path: str) -> None:
    client = _client(sessions.guard)
    resp = client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 302, (
        f"an unauthenticated browser GET must redirect: {resp.status_code}"
    )
    assert resp.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", ["/review", "/review/card?canonical_id=whatever"])
def test_inv_auth_no_header_json_is_401(sessions: _Sessions, path: str) -> None:
    client = _client(sessions.guard)
    resp = client.get(path, headers={"Accept": "application/json"}, follow_redirects=False)
    assert resp.status_code == 401, f"a tokenless API caller must 401: {resp.status_code}"


@pytest.mark.parametrize("path", ["/review", "/review/card?canonical_id=whatever"])
def test_inv_auth_bad_bearer_is_401(sessions: _Sessions, path: str) -> None:
    client = _client(sessions.guard)
    resp = client.get(path, headers={"Authorization": "Bearer nope"}, follow_redirects=False)
    assert resp.status_code == 401, f"an invalid bearer must 401: {resp.status_code}"
