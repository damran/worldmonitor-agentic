"""Integration — Gate 1a review-queue UI against REAL Postgres + Neo4j (ADR 0103).

Seeds a REAL parked (sensitive) merge exactly the way ``tests/integration/test_signoff.py`` does —
a sanctioned duplicate Person pair goes through ``resolve_pending(..., guard_mode="block")`` and
parks as ``pending_review`` (never written to the graph) — then wires the FastAPI app to the REAL
Postgres session factory + the REAL (ephemeral) Neo4j client and asserts:

  * ``GET /review`` lists the parked merge: 200, its canonical id present, the prominent sensitive
    badge present (a sanctioned member), the audit stays ``pending_review`` afterward.
  * ``GET /review/card?canonical_id=...`` renders BOTH members (their raw ids + a shared prop).

RED now: ``worldmonitor.api.review`` does not exist, so ``create_app`` never registers a
``/review`` router — both routes 404 instead of the expected 200. (No module-level CONTRACT
import here — this file is ``integration``-marked, and an eager import failure would break
collection even for a ``-m "not integration"`` run, since marker-based deselection happens
AFTER import; the 404 at runtime is RED enough and keeps the module import-safe.)
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, MergeAudit
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.settings import Settings

pytestmark = pytest.mark.integration

AUTH = {"Authorization": "Bearer good"}


class _FakeVerifier:
    def verify(self, token: str) -> dict[str, str]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "reviewer-1"}


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-21T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )
    entity = stamp(make_entity(data), provenance)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity=entity.to_dict(),
        source_record=provenance.source_record,
        status="pending",
    )


def _sanctioned(member_id: str, *, flag: bool = True) -> dict[str, object]:
    """A Person record; ``flag`` puts a sanction topic on it so the merge trips the guard."""
    properties: dict[str, object] = {
        "name": ["Vladimir Example"],
        "nationality": ["ru"],
        "birthDate": ["1960-01-01"],
    }
    if flag:
        properties["topics"] = ["sanction"]
    return {"id": member_id, "schema": "Person", "properties": properties, "datasets": ["t"]}


def _client(sessions: object, neo4j: Neo4jClient) -> TestClient:
    settings = Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-session-key-123",
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app(
        settings=settings,
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=neo4j,
        oauth=None,
        db_sessions=sessions,  # type: ignore[arg-type]
    )
    return TestClient(app, raise_server_exceptions=False)


def test_review_lists_and_renders_a_real_parked_sensitive_merge(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)
        ensure_constraints(clean_graph)

        with sessions() as session:
            session.add(_queue_item(_sanctioned("p1"), source="p1"))
            session.add(_queue_item(_sanctioned("p2", flag=False), source="p2"))
            session.commit()
        with sessions() as session:
            stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
        assert stats.review == 1, "the sanctioned duplicate pair must PARK, not merge"

        with sessions() as session:
            canonical_id = session.execute(
                select(MergeAudit.canonical_id).where(MergeAudit.decision == "pending_review")
            ).scalar_one()

        client = _client(sessions, clean_graph)

        list_resp = client.get("/review", headers=AUTH)
        assert list_resp.status_code == 200, (
            f"GET /review must list the real parked merge: {list_resp.status_code} {list_resp.text}"
        )
        assert canonical_id in list_resp.text, (
            "the parked merge's canonical id must appear in the rendered queue"
        )
        assert "wm-badge-sensitive" in list_resp.text, (
            "a merge with a sanctioned member must render the prominent sensitive badge"
        )

        card_resp = client.get("/review/card", params={"canonical_id": canonical_id}, headers=AUTH)
        assert card_resp.status_code == 200, (
            f"GET /review/card must render the parked merge's members: "
            f"{card_resp.status_code} {card_resp.text}"
        )
        assert "p1" in card_resp.text, "member p1's card must render"
        assert "p2" in card_resp.text, "member p2's card must render"
        assert "Vladimir Example" in card_resp.text, "shared member prop (name) must render"

        with sessions() as session:
            still_pending = session.execute(
                select(MergeAudit.decision).where(MergeAudit.canonical_id == canonical_id)
            ).scalar_one()
        assert still_pending == "pending_review", (
            "the read-only routes must never advance the audit decision"
        )
    finally:
        engine.dispose()
