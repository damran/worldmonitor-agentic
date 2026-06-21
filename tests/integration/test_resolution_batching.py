"""Integration tests: bounded-batch resolution (ADR 0026).

``resolve_pending`` drains the ER queue in windows of ``batch_size`` rows,
committing per batch. These prove the queue is fully drained across batches, that
within-batch duplicates still merge, that cross-batch duplicates do NOT merge
(the accepted v0 limitation — incremental dedup is deferred to the ER-streaming
gate), and that a drained re-run is a no-op.

Run against an ephemeral Neo4j + Postgres (testcontainers); marked
``integration`` so they are excluded from the default quality run.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.constraints import ensure_constraints
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


def _company(entity_id: str, name: str, jurisdiction: str) -> dict[str, object]:
    return {
        "id": entity_id,
        "schema": "Company",
        "properties": {"name": [name], "jurisdiction": [jurisdiction]},
        "datasets": ["t"],
    }


def _petrov(entity_id: str) -> dict[str, object]:
    """One of two deliberate duplicates (same name + jurisdiction)."""
    return _company(entity_id, "Petrov Holdings Ltd", "cy")


def _seed(sessions, tenant_id: str, entities: list[dict[str, object]]) -> None:
    with sessions() as session:
        for entity in entities:
            session.add(_queue_item(tenant_id, entity))
        session.commit()


def test_drains_whole_queue_in_bounded_batches(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    """Five distinct candidates with batch_size=2 drain across 3 batches, all written."""
    tenant_id = "batch-drain-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    distinct = [
        _company("d1", "Acme Corporation", "us"),
        _company("d2", "Globex", "gb"),
        _company("d3", "Initech", "de"),
        _company("d4", "Umbrella", "fr"),
        _company("d5", "Stark Industries", "jp"),
    ]
    _seed(sessions, tenant_id, distinct)

    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, batch_size=2
        )

    # 5 rows / window of 2 -> batches of [2, 2, 1] = 3 batches; all promoted.
    assert stats.batches == 3
    assert stats.pending == 5
    assert stats.clusters == 5
    assert stats.promoted == 5
    assert stats.review == 0

    nodes = clean_graph.execute_read(
        "MATCH (n:Company {tenant_id: $t}) RETURN count(n) AS n", t=tenant_id
    )[0]["n"]
    assert nodes == 5, "every distinct candidate must be written, across all batches"

    # The queue is fully drained — nothing left pending.
    with sessions() as session:
        pending = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending")
        ).scalar_one()
        assert pending == 0

    engine.dispose()


def test_within_batch_duplicates_merge(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    """Two duplicates in the SAME batch still collapse to one canonical node."""
    tenant_id = "batch-within-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    _seed(sessions, tenant_id, [_petrov("petrov-a"), _petrov("petrov-b")])

    with sessions() as session:
        # batch_size comfortably holds both, so they are scored together and merge.
        stats = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, batch_size=10
        )

    assert stats.batches == 1
    assert stats.clusters == 1
    assert stats.promoted == 1
    nodes = clean_graph.execute_read(
        "MATCH (n:Company {tenant_id: $t}) RETURN count(n) AS n", t=tenant_id
    )[0]["n"]
    assert nodes == 1, "duplicates in one batch must merge"

    engine.dispose()


def test_cross_batch_duplicates_are_not_merged_v0_limitation(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """Duplicates split across batches do NOT merge — the accepted ADR 0026 limitation.

    With batch_size=1 each candidate is its own batch, so the two duplicates are
    never scored against each other. They land as two separate nodes. Closing this
    gap is incremental ER, deferred to the ER-streaming gate (ADR 0019).
    """
    tenant_id = "batch-cross-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    _seed(sessions, tenant_id, [_petrov("petrov-a"), _petrov("petrov-b")])

    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, batch_size=1
        )

    # Two single-item batches; each is a singleton cluster, promoted separately.
    assert stats.batches == 2
    assert stats.clusters == 2
    assert stats.promoted == 2
    nodes = clean_graph.execute_read(
        "MATCH (n:Company {tenant_id: $t}) RETURN count(n) AS n", t=tenant_id
    )[0]["n"]
    assert nodes == 2, "cross-batch duplicates are not merged in v0 (incremental ER deferred)"

    engine.dispose()


def test_batched_resolution_rerun_is_idempotent_noop(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """After a batched drain, re-running resolves nothing and writes nothing."""
    tenant_id = "batch-idem-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    _seed(
        sessions,
        tenant_id,
        [_company("d1", "Acme Corporation", "us"), _company("d2", "Globex", "gb"), _petrov("p-a")],
    )

    with sessions() as session:
        first = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, batch_size=2
        )
    assert first.pending == 3
    assert first.batches == 2  # [2, 1]

    def _node_count() -> int:
        return clean_graph.execute_read(
            "MATCH (n:Entity {tenant_id: $t}) RETURN count(n) AS n", t=tenant_id
        )[0]["n"]

    before = _node_count()

    with sessions() as session:
        second = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, batch_size=2
        )

    # Nothing pending the second time: no batches run, graph unchanged.
    assert second.pending == 0
    assert second.batches == 0
    assert second.promoted == 0
    assert _node_count() == before

    engine.dispose()


def test_unresolvable_id_row_is_quarantined_not_looped_forever(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A row with an unusable FtM id is quarantined as 'invalid', never re-loaded.

    cluster_and_merge drops an entity with a None/missing id, so its queue row gets
    no resolved/review status — the bounded-drain loop would otherwise reload it
    forever. The safety sweep marks it 'invalid' so the drain terminates; the valid
    row alongside it still resolves.
    """
    tenant_id = "batch-invalid-tenant"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    valid = _company("ok", "Acme Corporation", "us")
    unusable: dict[str, object] = {
        "id": None,
        "schema": "Company",
        "properties": {"name": ["No Id Co"], "jurisdiction": ["gb"]},
        "datasets": ["t"],
    }
    _seed(sessions, tenant_id, [valid, unusable])

    # If the sweep regressed, this call would never return (infinite drain loop).
    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, tenant_id=tenant_id, batch_size=10
        )

    assert stats.promoted == 1  # only the valid row resolves

    with sessions() as session:
        invalid = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "invalid")
        ).scalar_one()
        pending = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending")
        ).scalar_one()
        assert invalid == 1
        assert pending == 0  # queue fully drained, nothing left to loop on

    engine.dispose()
