"""B-2 (ADR 0038) — a poison input is contained, never wedges the tenant's drain.

The recurring failure mode (id-less rows, the eponymous over-merge crash, the all-no-name
SplinkException) is: one bad input at some stage of ``_resolve_batch`` raises, the batch
never commits, the rows stay ``pending``, and the bounded drain re-loads + re-fails them
FOREVER. These tests prove **containment by PROGRESS**, not merely "exception caught": after
a poison input, the offending rows are quarantined (``invalid`` + dead-letter) and
``queue_pending`` reaches 0 — the drain terminates and a re-run is a clean no-op. Good rows in
the same batch still resolve. Both granularities are covered: a single bad ROW (construction)
and a whole BATCH that cannot be scored (the all-no-name window).

Pre-fix these wedge: ``make_entity`` (bad schema → ``InvalidData``) and ``score_pairs`` (an
all-no-name window → ``SplinkException``) both raise unguarded, so ``resolve_pending`` itself
raises and the rows are never drained. Real ER pipeline against ephemeral Neo4j + Postgres;
``integration``-gated (CI).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, IngestDeadLetter
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


def _good_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    """A well-formed queue row (mapped + provenance-stamped, as a connector would emit)."""
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


def _raw_item(raw_entity: dict[str, object], *, source: str) -> ErQueueItem:
    """A queue row whose raw_entity is stored verbatim — used to seed a POISON row that
    ``make_entity`` cannot parse (so it must not be built at seed time)."""
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity=raw_entity,
        source_record=f"s3://landing/{source}.json",
        status="pending",
    )


def _company(entity_id: str) -> dict[str, object]:
    return {
        "id": entity_id,
        "schema": "Company",
        "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
        "datasets": ["t"],
    }


def _pending(sessions: sessionmaker[Session]) -> int:
    with sessions() as session:
        return session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.status == "pending")
        ).scalar_one()


def _dead_letters(sessions: sessionmaker[Session]) -> list[IngestDeadLetter]:
    with sessions() as session:
        return list(session.execute(select(IngestDeadLetter)).scalars())


def test_poison_row_is_quarantined_and_the_drain_terminates(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """Row-level: one un-parseable row is dead-lettered; the good rows still resolve; the
    queue drains to 0 (pre-fix the un-guarded make_entity raises and wedges the drain)."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    poison = _raw_item(
        {
            "id": "poison",
            "schema": "NotARealSchema",
            "properties": {"name": ["x"]},
            "datasets": ["t"],
        },
        source="poison",
    )
    with sessions() as session:
        session.add(poison)
        session.add(_good_item(_company("c1"), source="c1"))
        session.add(_good_item(_company("c2"), source="c2"))
        session.commit()

    # Must NOT raise and must terminate (pre-fix: make_entity raises → resolve_pending raises).
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    # PROGRESS/CONTAINMENT: nothing left pending — the drain terminated, not wedged.
    assert _pending(sessions) == 0, "queue must drain to 0 (no row stuck pending)"

    # The good duplicates still resolved into one canonical node.
    companies = clean_graph.execute_read("MATCH (n:Company) RETURN n.id AS id")
    assert len(companies) == 1, "good rows resolve despite the poison row"

    # The poison row is quarantined: invalid + dead-letter carrying its source_record.
    dls = _dead_letters(sessions)
    assert len(dls) == 1
    assert dls[0].stage == "resolve-row"
    assert dls[0].source_record == "s3://landing/poison.json"
    with sessions() as session:
        invalid = (
            session.execute(select(ErQueueItem.raw_entity).where(ErQueueItem.status == "invalid"))
            .scalars()
            .all()
        )
    assert [r["id"] for r in invalid] == ["poison"]

    # Re-run is a clean no-op — the poison row is NOT re-loaded (no re-wedge).
    with sessions() as session:
        again = resolve_pending(session=session, neo4j=clean_graph)
    assert again.pending == 0 and again.promoted == 0


def test_unscoreable_all_no_name_batch_is_quarantined_and_the_drain_terminates(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """Batch-level: a window of only no-name entities (which trips Splink's name blocking) is
    quarantined as a set; the queue drains to 0 (pre-fix score_pairs raises and wedges)."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    # Two valid but no-name Sanction entities — OpenSanctions emits these; their fingerprint is
    # null, so the whole batch is unscoreable (SplinkException) rather than a single bad row.
    for sid in ("sanc-1", "sanc-2"):
        data: dict[str, object] = {
            "id": sid,
            "schema": "Sanction",
            "properties": {"program": ["RUSSIA-EO14024"]},
            "datasets": ["t"],
        }
        with sessions() as session:
            session.add(_good_item(data, source=sid))
            session.commit()

    # Must NOT raise and must terminate (pre-fix: score_pairs raises SplinkException → wedge).
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    # PROGRESS/CONTAINMENT: the unscoreable set is quarantined, queue drains to 0.
    assert _pending(sessions) == 0, "unscoreable batch must drain to 0, not wedge"

    dls = _dead_letters(sessions)
    assert {d.stage for d in dls} == {"resolve-batch"}, "quarantined at the batch stage"
    assert {d.source_record for d in dls} == {
        "s3://landing/sanc-1.json",
        "s3://landing/sanc-2.json",
    }, "each row dead-lettered with its source_record (replayable)"

    # Nothing was written for the quarantined batch.
    nodes = clean_graph.execute_read("MATCH (n:Entity) RETURN count(n) AS n")[0]["n"]
    assert nodes == 0

    # Re-run is a clean no-op — the quarantined rows are NOT re-loaded.
    with sessions() as session:
        again = resolve_pending(session=session, neo4j=clean_graph)
    assert again.pending == 0
