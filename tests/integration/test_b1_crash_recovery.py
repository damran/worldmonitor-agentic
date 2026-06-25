"""B-1 crash-window regression: a crash between the Neo4j write and the Postgres commit
must NOT, on restart, leave a duplicate/orphan canonical node (ADR 0036, Part 1).

The audit's lesson is that happy-path fixtures hid every prior bug, so this test SIMULATES
THE CRASH WINDOW rather than the success path: it lets ``write_entities`` commit the graph,
then fails the very next Postgres ``session.commit()`` (the one at ``pipeline.py``), exactly
as a process death in that window would. It then "restarts" by re-running ``resolve_pending``
on the still-pending rows and asserts the retry converges on the SAME canonical node — which
holds only because the canonical id is now a deterministic function of the cluster membership
(without that, the retry mints a fresh ``NK-`` id and a SECOND canonical node would appear).

Runs the real ER pipeline against an ephemeral Neo4j + Postgres (testcontainers); marked
``integration`` so it is gated by the dedicated CI job (no Docker in the default run).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, MergeAudit
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-23T00:00:00Z",
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


def _petrov(member_id: str) -> dict[str, object]:
    """One of two deliberate duplicate companies (merge into one canonical node)."""
    return {
        "id": member_id,
        "schema": "Company",
        "properties": {"name": ["Petrov Holdings Ltd"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }


def _ownership(edge_id: str, owner: str, asset: str) -> dict[str, object]:
    return {
        "id": edge_id,
        "schema": "Ownership",
        "properties": {"owner": [owner], "asset": [asset]},
        "datasets": ["t"],
    }


def _companies(graph: Neo4jClient) -> list[str]:
    return [row["id"] for row in graph.execute_read("MATCH (n:Company) RETURN n.id AS id")]


def test_crash_between_graph_write_and_postgres_commit_does_not_duplicate_canonical(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    # Real-data-shaped: two duplicate companies that merge, an owner, and ownership edges
    # that name the merged-away members (so we also prove referent rewriting survives).
    ivan: dict[str, object] = {
        "id": "ivan",
        "schema": "Person",
        "properties": {"name": ["Ivan Owner"]},
        "datasets": ["t"],
    }
    rows: list[tuple[dict[str, object], str]] = [
        (_petrov("petrov-a"), "petrov-a"),
        (_petrov("petrov-b"), "petrov-b"),
        (ivan, "ivan"),
        (_ownership("own-a", "ivan", "petrov-a"), "own-a"),
        (_ownership("own-b", "ivan", "petrov-b"), "own-b"),
    ]
    with sessions() as session:
        for data, source in rows:
            session.add(_queue_item(data, source=source))
        session.commit()

    # --- CRASH WINDOW: let write_entities commit the graph, then fail the FIRST Postgres
    #     commit (pipeline.py) — i.e. the process dies after the graph write, before the
    #     queue-status/audit commit. (The crash-hook pattern from the Gate A driver test:
    #     inject a raise at the exact failure point instead of testing only the happy path.)
    with sessions() as session:
        calls = {"n": 0}
        real_commit = session.commit

        def crashing_commit() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("WM_CRASH_AFTER: graph write committed, before postgres commit")
            real_commit()

        session.commit = crashing_commit  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="WM_CRASH_AFTER"):
            resolve_pending(session=session, neo4j=clean_graph)

    # The graph write DID land (Neo4j commits independently of Postgres): one canonical company.
    after_crash = _companies(clean_graph)
    assert len(after_crash) == 1, "the canonical node was committed to the graph before the crash"
    canonical_id = after_crash[0]
    assert canonical_id not in {"petrov-a", "petrov-b"}, "canonical is a minted id, not a member"

    # Postgres rolled back: the cross-store gap is real — rows still pending, no audit committed.
    with sessions() as session:
        pending = session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.status == "pending")
        ).scalar_one()
        audits = session.execute(select(func.count()).select_from(MergeAudit)).scalar_one()
    assert pending == len(rows), "Postgres rolled back: every row is still pending"
    assert audits == 0, "no merge audit was committed in the crashed run"

    # --- RESTART: re-resolve the still-pending rows. With a deterministic canonical id the
    #     retry re-derives the SAME id, so the graph MERGE converges instead of duplicating.
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    # THE B-1 ASSERTION: still exactly ONE canonical company, and it is the SAME node — no
    # duplicate, no orphan. (Pre-fix, the random NK- mint would make this 2.)
    final = _companies(clean_graph)
    assert final == [canonical_id], (
        "the retry must converge on the same canonical node, not duplicate it"
    )
    for member in ("petrov-a", "petrov-b"):
        orphan = clean_graph.execute_read("MATCH (n {id: $id}) RETURN count(n) AS n", id=member)[0][
            "n"
        ]
        assert orphan == 0, f"merged-away id {member} must not survive as a node"

    # Postgres is now consistent: the queue drained, the merge is audited exactly once.
    with sessions() as session:
        pending = session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.status == "pending")
        ).scalar_one()
        merged = (
            session.execute(select(MergeAudit).where(MergeAudit.decision == "merged"))
            .scalars()
            .all()
        )
    assert pending == 0, "the restart drained the queue"
    assert [sorted(a.source_ids) for a in merged].count(["petrov-a", "petrov-b"]) == 1, (
        "the petrov merge is audited exactly once after recovery"
    )

    # Referent rewriting still correct: both ownership edges point at the SAME canonical node.
    owns = clean_graph.execute_read(
        "MATCH (:Entity {id: 'ivan'})-[r:OWNS]->(m:Entity) RETURN m.id AS target",
    )
    assert {row["target"] for row in owns} == {canonical_id}, (
        "edges rewrite onto the canonical node"
    )

    engine.dispose()
