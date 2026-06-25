"""Gate B-front (ADR 0044) — anchor-preferred durable ids through the LIVE graph write path.

The Docker-free oracle (``tests/test_stable_id.py``) proves ``pick_anchor`` + the ledger helpers;
this integration test proves the END-TO-END wiring the oracle leaves to the builder (its module
docstring §66): an ANCHORED cluster is materialised in Neo4j under its DURABLE canonical id (not a
``wmc-`` hash), a re-ingest converges on the SAME node (no churn), and ``graph/writer``'s
alias-on-read (``resolve_node_id`` / ``get_entity_by_alias``) resolves a SUPERSEDED member id to the
surviving node via the ledger.

The merge is forced through ``resolve_pending``/``_resolve_batch`` by seeding durable
``ResolverJudgement`` positives (the same channel a human sign-off uses, mirroring
``test_b6_resolve_incompat``) so the cluster forms deterministically regardless of Splink scoring.

Real ER pipeline against ephemeral Neo4j + Postgres (testcontainers); ``integration``-marked so it
runs on the dedicated CI job (no Docker in the default run).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import CanonicalIdLedger, ErQueueItem, ResolverJudgement
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import get_entity_by_alias, resolve_node_id
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration

Q_ACME = "Q42"
_DURABLE = f"qid:{Q_ACME}"


def _company(entity_id: str) -> dict[str, object]:
    """A Company carrying its QID anchor as the FtM ``wikidataId`` identifier property."""
    return {
        "id": entity_id,
        "schema": "Company",
        "properties": {
            "name": ["Acme Corporation Ltd"],
            "jurisdiction": ["us"],
            "wikidataId": [Q_ACME],
        },
        "datasets": ["t"],
    }


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-25T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )
    entity = stamp(make_entity(data), provenance)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        entity_id=entity.id,
        raw_entity=entity.to_dict(),
        source_record=provenance.source_record,
        status="pending",
    )


def _judgement(left: str, right: str) -> ResolverJudgement:
    low, high = sorted((left, right))
    return ResolverJudgement(
        id=str(uuid.uuid4()), left_id=low, right_id=high, judgement="positive", source="signoff"
    )


def _node_ids(neo4j: Neo4jClient) -> list[str]:
    return [row["id"] for row in neo4j.execute_read("MATCH (n:Company) RETURN n.id AS id")]


def _ingest_anchored_pair(
    sessions: sessionmaker[Session], neo4j: Neo4jClient, left: str, right: str
) -> None:
    """Seed two anchored members + a positive judgement, then resolve them through the pipeline."""
    with sessions() as session:
        session.add(_queue_item(_company(left), source=left))
        session.add(_queue_item(_company(right), source=right))
        session.add(_judgement(left, right))
        session.commit()
    with sessions() as session:
        resolve_pending(session=session, neo4j=neo4j)


def test_anchored_cluster_written_under_durable_id_and_reingest_converges(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """(a) An anchored merge's node is keyed on the durable QID (not ``wmc-``); (c) a re-ingest
    with FRESH member ids writes to the SAME node — no churn, no second node."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    # First ingest — member ids ing1-*.
    _ingest_anchored_pair(sessions, clean_graph, "ing1-a", "ing1-b")

    node_ids = _node_ids(clean_graph)
    assert node_ids == [_DURABLE], "the anchored merge must be keyed on the durable QID"
    assert not node_ids[0].startswith("wmc-"), "an anchored merge must NOT be keyed on a wmc- hash"

    # Re-ingest the SAME real entity with FRESH per-collect member ids — must converge on the
    # SAME durable node (the crux: no id churn, no duplicate node).
    _ingest_anchored_pair(sessions, clean_graph, "ing2-a", "ing2-b")
    assert _node_ids(clean_graph) == [_DURABLE], "re-ingest must converge on the same durable node"

    # The ledger holds exactly ONE durable canonical for the QID across both ingests (adopt).
    with sessions() as session:
        canon_self = list(
            session.execute(
                select(CanonicalIdLedger.canonical_id).where(
                    CanonicalIdLedger.canonical_id == _DURABLE,
                    CanonicalIdLedger.canonical_alias == _DURABLE,
                )
            ).scalars()
        )
        assert canon_self == [_DURABLE], "exactly one durable canonical row for the QID (no churn)"
    engine.dispose()


def test_alias_on_read_resolves_superseded_member_id_to_surviving_node(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """(b) Alias-on-read: after the merge records the collapsed member ids as aliases, a lookup by
    a SUPERSEDED member id resolves — via the ledger — to the surviving durable node, not a miss."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    _ingest_anchored_pair(sessions, clean_graph, "ing1-a", "ing1-b")

    with sessions() as session:
        # The superseded member ids resolve to the surviving durable id (the alias-on-read map).
        assert resolve_node_id(session, "ing1-a") == _DURABLE
        assert resolve_node_id(session, "ing1-b") == _DURABLE
        # An unknown / unaliased id resolves to itself (the call is always safe).
        assert resolve_node_id(session, "never-seen") == "never-seen"

        # A read BY a superseded member id lands on the surviving node, not a dangling miss.
        via_alias = get_entity_by_alias(clean_graph, session, entity_id="ing1-a")
        assert via_alias is not None, "alias-on-read must land on the surviving node, not miss"
        assert via_alias["id"] == _DURABLE
        # The same node is returned whether queried by the durable id or a superseded alias.
        via_durable = get_entity_by_alias(clean_graph, session, entity_id=_DURABLE)
        assert via_durable is not None
        assert via_durable["id"] == via_alias["id"] == _DURABLE
    engine.dispose()
