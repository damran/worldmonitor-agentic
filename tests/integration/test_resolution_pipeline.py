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
from worldmonitor.db.models import ErQueueItem, MergeAudit
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


def _queue_item(tenant_id: str, entity: dict[str, object]) -> ErQueueItem:
    return ErQueueItem(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        connector_id="opensanctions",
        raw_entity=entity,
        source_record=f"s3://landing/{entity['id']}.json",
        status="pending",
    )


def test_resolve_pending_pipeline(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    # Dedicated tenant so this test is isolated from other tests' ER-queue rows in
    # the shared (session-scoped) Postgres.
    tenant_id = "er-pipeline-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    candidates: list[dict[str, object]] = [
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
    with sessions() as session:
        for candidate in candidates:
            session.add(_queue_item(tenant_id, candidate))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_id)

    # 3 clusters: {c1,c2} merged, {c3} singleton, {p1,p2} sanctioned -> review.
    assert stats.pending == 5
    assert stats.clusters == 3
    assert stats.promoted == 2
    assert stats.review == 1

    # The duplicate companies collapsed to ONE node; distinct company stands alone;
    # no sanctioned person was written to the graph.
    company_nodes = clean_graph.execute_read(
        "MATCH (n:Company) RETURN n.id AS id, n.tenant_id AS tenant"
    )
    assert len(company_nodes) == 2
    assert all(row["tenant"] == tenant_id for row in company_nodes)
    person_nodes = clean_graph.execute_read("MATCH (n:Person) RETURN count(n) AS n")
    assert person_nodes[0]["n"] == 0

    with sessions() as session:
        merged = session.execute(
            select(func.count())
            .select_from(MergeAudit)
            .where(MergeAudit.tenant_id == tenant_id, MergeAudit.decision == "merged")
        ).scalar_one()
        review = session.execute(
            select(MergeAudit).where(
                MergeAudit.tenant_id == tenant_id, MergeAudit.decision == "pending_review"
            )
        ).scalar_one()
        assert merged == 2  # the {c1,c2} merge + the {c3} singleton
        assert sorted(review.source_ids) == ["p1", "p2"]
        assert "sensitive" in review.reason.lower()

        # Queue statuses reflect the decisions.
        resolved = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "resolved")
        ).scalar_one()
        in_review = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending_review")
        ).scalar_one()
        assert resolved == 3
        assert in_review == 2

    engine.dispose()
