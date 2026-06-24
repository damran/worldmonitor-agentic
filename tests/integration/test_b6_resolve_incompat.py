"""B-6 slice-1 (H-2, ADR 0041) — INV-4 through the LIVE resolve pipeline.

A transitive cluster ``Person a1 ~ Person a2`` (compatible) and ``Person a2 ~ Company z1``
(incompatible) is forced through ``resolve_pending``/``_resolve_batch`` by seeding durable
``ResolverJudgement`` positives (the same channel a human sign-off uses) — Splink's
``score_pairs`` could never assemble it because it never compares a1 vs z1. FtM ``merge()``
then raises on the cross-schema member z1.

Pre-fix the H-2 chain corrupts the graph: z1 is swallowed into the Person merge (its own
Company node is never written), it is audited inside the merge's ``MergeAudit.source_ids``,
its row is marked ``resolved`` as part of the merge, and the skip is only logged — never
durably auditable. After the fix:

* INV-4 — z1's queue row ends ``status 'resolved'`` with its OWN correct-schema ``:Company``
  node; the merged ``{a1,a2}`` cluster's ``MergeAudit.source_ids`` EXCLUDES z1; an
  ``IngestDeadLetter`` row exists at stage ``'resolve-incompat'`` carrying z1's source_record.
* INV-5 (convergence) — a re-run converges on the same nodes (no duplicate / orphan).

Real ER pipeline against ephemeral Neo4j + Postgres (testcontainers); ``integration``-marked
so it runs on the dedicated CI job (no Docker in the default run).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, IngestDeadLetter, MergeAudit, ResolverJudgement
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration

# ids chosen so the FtM merge base (sorted member_ids[0]) is a PERSON: a1~a2 merge (KEPT),
# the Company z1 is the lone schema-incompatible drop.
_KEPT_IDS = ("a1", "a2")
_DROPPED_ID = "z1"


def _queue_item(tenant_id: str, data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-23T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )
    entity = stamp(make_entity(data), provenance)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        connector_id="opensanctions",
        entity_id=entity.id,  # real ingest stamps this (ingest.py); the pipeline maps rows by it
        raw_entity=entity.to_dict(),
        source_record=provenance.source_record,
        status="pending",
    )


def _judgement(tenant_id: str, left: str, right: str, verdict: str) -> ResolverJudgement:
    low, high = sorted((left, right))
    return ResolverJudgement(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        left_id=low,
        right_id=high,
        judgement=verdict,
        source="signoff",
    )


def _person(entity_id: str) -> dict[str, object]:
    return {
        "id": entity_id,
        "schema": "Person",
        "properties": {"name": ["Ivan Petrov"], "nationality": ["ru"]},
        "datasets": ["t"],
    }


def _company(entity_id: str) -> dict[str, object]:
    return {
        "id": entity_id,
        "schema": "Company",
        "properties": {"name": ["Petrov Holdings Ltd"], "jurisdiction": ["cy"]},
        "datasets": ["t"],
    }


def _seed_transitive(sessions: sessionmaker[Session], tenant_id: str) -> None:
    """Seed two Persons + one Company and force a transitive cluster via positive judgements."""
    with sessions() as session:
        session.add(_queue_item(tenant_id, _person("a1"), source="a1"))
        session.add(_queue_item(tenant_id, _person("a2"), source="a2"))
        session.add(_queue_item(tenant_id, _company("z1"), source="z1"))
        # a1~a2 (Person~Person) AND a2~z1 (Person~Company): a transitive chain that gathers a
        # cross-schema member. score_pairs alone never compares a1 vs z1, so positives force it.
        session.add(_judgement(tenant_id, "a1", "a2", "positive"))
        session.add(_judgement(tenant_id, "a2", "z1", "positive"))
        session.commit()


def _status_of(sessions: sessionmaker[Session], tenant_id: str, entity_id: str) -> str:
    with sessions() as session:
        return session.execute(
            select(ErQueueItem.status).where(
                ErQueueItem.tenant_id == tenant_id, ErQueueItem.entity_id == entity_id
            )
        ).scalar_one()


def _node_ids(neo4j: Neo4jClient, tenant_id: str, label: str) -> list[str]:
    return [
        row["id"]
        for row in neo4j.execute_read(
            f"MATCH (n:{label} {{tenant_id: $t}}) RETURN n.id AS id ORDER BY n.id", t=tenant_id
        )
    ]


def test_dropped_member_resolves_to_its_own_node_excluded_from_merge_and_dead_lettered(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """INV-4: z1 gets its own Company node, excluded from the merge audit, and dead-lettered."""
    tenant_id = "b6-resolve-incompat"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)
    _seed_transitive(sessions, tenant_id)

    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_id)

    # The dropped member z1 keeps its OWN correct-schema Company node — not swallowed.
    company_ids = _node_ids(clean_graph, tenant_id, "Company")
    assert company_ids == [_DROPPED_ID], (
        "the schema-incompatible member must be written as its own correct-schema Company node"
    )
    # The kept members a1, a2 collapse to exactly ONE Person canonical node.
    person_ids = _node_ids(clean_graph, tenant_id, "Person")
    assert len(person_ids) == 1, "the kept Person members merge into one canonical node"
    kept_canonical = person_ids[0]
    assert kept_canonical not in _KEPT_IDS, "the kept node is a merged canonical, not a member id"
    assert kept_canonical != _DROPPED_ID

    # The dropped member's queue row ends 'resolved' (its own node was written), NOT swallowed.
    assert _status_of(sessions, tenant_id, _DROPPED_ID) == "resolved"
    for member_id in _KEPT_IDS:
        assert _status_of(sessions, tenant_id, member_id) == "resolved"

    with sessions() as session:
        # The merged cluster's MergeAudit must EXCLUDE the dropped id from source_ids.
        merge_audits = list(
            session.execute(
                select(MergeAudit).where(
                    MergeAudit.tenant_id == tenant_id, MergeAudit.decision == "merged"
                )
            ).scalars()
        )
        kept_audit = next(a for a in merge_audits if set(a.source_ids) == set(_KEPT_IDS))
        assert _DROPPED_ID not in kept_audit.source_ids, (
            "the dropped member must be excluded from the merge's MergeAudit.source_ids"
        )
        assert kept_audit.canonical_id == kept_canonical

        # The skip is durably recorded: an IngestDeadLetter at stage 'resolve-incompat' carrying
        # the dropped member's source_record (replayable, not only a log).
        dead_letters = list(
            session.execute(
                select(IngestDeadLetter).where(
                    IngestDeadLetter.tenant_id == tenant_id,
                    IngestDeadLetter.stage == "resolve-incompat",
                )
            ).scalars()
        )
        assert len(dead_letters) == 1, (
            "exactly one 'resolve-incompat' dead-letter for the dropped member"
        )
        assert dead_letters[0].source_record == "s3://landing/z1.json", (
            "the dead-letter carries the dropped member's source_record"
        )
        assert len(dead_letters[0].stage) <= 16, "stage must fit String(16)"

    engine.dispose()


def test_rerun_converges_on_the_same_nodes(clean_graph: Neo4jClient, postgres_dsn: str) -> None:
    """INV-5 (integration convergence): re-resolving the same batch is idempotent.

    The kept canonical id is the content-address of the ACTUAL merged set, so a re-run never
    mints a duplicate/orphan node for the kept merge or the dropped singleton.
    """
    tenant_id = "b6-resolve-incompat-rerun"
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)
    _seed_transitive(sessions, tenant_id)

    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_id)
    companies_first = _node_ids(clean_graph, tenant_id, "Company")
    persons_first = _node_ids(clean_graph, tenant_id, "Person")

    # Re-resolve the SAME records (e.g. a crash that rolled back the Postgres commit AFTER the
    # graph write — the ADR-0036 recovery scenario): reset the rows to pending and resolve again.
    # The durable positive judgements remain, so the transitive cluster reassembles; the
    # content-addressed canonical id must converge on the same nodes — no duplicate / orphan.
    with sessions() as session:
        session.execute(
            update(ErQueueItem).where(ErQueueItem.tenant_id == tenant_id).values(status="pending")
        )
        session.commit()
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, tenant_id=tenant_id)

    companies_second = _node_ids(clean_graph, tenant_id, "Company")
    persons_second = _node_ids(clean_graph, tenant_id, "Person")
    assert companies_second == companies_first == [_DROPPED_ID], (
        "the dropped singleton converges on its own id — no duplicate/orphan"
    )
    assert persons_second == persons_first, (
        "the kept merge converges on the same content-addressed canonical id (ADR 0036)"
    )
    assert len(persons_second) == 1

    with sessions() as session:
        # No row left pending — the drain terminated on both passes.
        pending = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.tenant_id == tenant_id, ErQueueItem.status == "pending")
        ).scalar_one()
        assert pending == 0

    engine.dispose()
