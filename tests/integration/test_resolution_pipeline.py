"""Integration test: the ER pipeline resolves the queue into the graph.

Seeds the ER queue (Postgres) with a clear duplicate pair, a distinct entity, and
a sanctioned duplicate pair; runs the pipeline; asserts the duplicate collapses
to one canonical node in Neo4j, the distinct entity stands alone, and the
sanctioned merge is held for review (never written to the graph).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, MergeAlert, MergeAudit
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


def _queue_item(entity: dict[str, object]) -> ErQueueItem:
    # ADR 0060 (fail-closed node provenance): real ingest stamps every entity before queueing
    # (run_ingest -> connector.map(provenance=...)), so the queued raw_entity must carry prov_* —
    # else resolve_pending writes an unprovenanced node and the writer fails closed (node G1).
    # Stamping only; the merge/review/alert assertions are unaffected by provenance.
    source_record = f"s3://landing/{entity['id']}.json"
    stamped = stamp(
        make_entity(entity),
        Provenance(
            source_id="src:pipeline-test",
            retrieved_at="2026-06-21T00:00:00Z",
            reliability="A",
            source_record=source_record,
        ),
    )
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity=stamped.to_dict(),
        source_record=source_record,
        status="pending",
    )


def _candidates() -> list[dict[str, object]]:
    """A clear duplicate pair, a distinct entity, and a sanctioned duplicate pair."""
    return [
        {
            "id": "c1",
            "schema": "Company",
            "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "c2",
            "schema": "Company",
            "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "c3",
            "schema": "Company",
            "properties": {"name": ["Globex Incorporated"], "jurisdiction": ["gb"]},
            "datasets": ["t"],
        },
        {
            "id": "p1",
            "schema": "Person",
            "properties": {
                "name": ["Vladimir Example"],
                "nationality": ["ru"],
                "birthDate": ["1960-01-01"],
                "topics": ["sanction"],
            },
            "datasets": ["t"],
        },
        {
            "id": "p2",
            "schema": "Person",
            "properties": {
                "name": ["Vladimir Example"],
                "nationality": ["ru"],
                "birthDate": ["1960-01-01"],
            },
            "datasets": ["t"],
        },
    ]


def test_resolve_pending_pipeline(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        # mode="block" (current/pre-ADR-0024 behavior): sanctioned merge is parked.
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # 3 clusters: {c1,c2} merged, {c3} singleton, {p1,p2} sanctioned -> review.
    assert stats.pending == 5
    assert stats.clusters == 3
    assert stats.promoted == 2
    assert stats.review == 1
    assert stats.alerts == 0

    # The duplicate companies collapsed to ONE node; distinct company stands alone;
    # no sanctioned person was written to the graph.
    company_nodes = clean_graph.execute_read("MATCH (n:Company) RETURN n.id AS id")
    assert len(company_nodes) == 2
    person_nodes = clean_graph.execute_read("MATCH (n:Person) RETURN count(n) AS n")
    assert person_nodes[0]["n"] == 0

    with sessions() as session:
        merged = session.execute(
            select(func.count()).select_from(MergeAudit).where(MergeAudit.decision == "merged")
        ).scalar_one()
        review = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        assert merged == 2  # the {c1,c2} merge + the {c3} singleton
        assert sorted(review.source_ids) == ["p1", "p2"]
        assert "sensitive" in review.reason.lower()

        # Queue statuses reflect the decisions.
        resolved = session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.status == "resolved")
        ).scalar_one()
        in_review = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.status == "pending_review")
        ).scalar_one()
        assert resolved == 3
        assert in_review == 2

    engine.dispose()


def test_resolve_pending_alert_mode_writes_and_records(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """mode="alert" (ADR 0024): the sanctioned merge IS written AND a merge_alerts row is recorded.

    Same fixture as the block test; only the guard ACTION differs. The flagged
    {p1,p2} cluster is no longer parked — it merges, lands in Neo4j, and leaves a
    durable, auditable trail.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="alert")

    # All 3 clusters proceed; the flagged one is promoted-with-alert, none parked.
    assert stats.pending == 5
    assert stats.clusters == 3
    assert stats.promoted == 3
    assert stats.review == 0
    assert stats.alerts == 1

    # The sanctioned person WAS written to the graph (unlike block mode).
    person_nodes = clean_graph.execute_read("MATCH (n:Person) RETURN n.id AS id")
    assert len(person_nodes) == 1, "the flagged sanctioned merge must be written in alert mode"

    with sessions() as session:
        # No cluster parked for review; every flagged-but-merged decision is "merged".
        in_review = session.execute(
            select(func.count())
            .select_from(MergeAudit)
            .where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        assert in_review == 0

        # The durable alert trail: exactly one row, for the sanctioned {p1,p2} merge.
        alert = session.execute(select(MergeAlert)).scalar_one()
        assert sorted(alert.source_ids) == ["p1", "p2"]
        assert "sensitive" in alert.reason.lower()

        # Queue: all items resolved, none left in review.
        resolved = session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.status == "resolved")
        ).scalar_one()
        in_review_items = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.status == "pending_review")
        ).scalar_one()
        assert resolved == 5
        assert in_review_items == 0

    engine.dispose()
