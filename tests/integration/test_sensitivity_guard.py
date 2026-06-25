"""Gate E (slice-1) — T4: the stale approved-group exemption fence (the #1 regression fence).

Spec: ``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md`` §5 (the approved-group exemption) + §8 T4.
ADR: ``docs/decisions/0047-fail-closed-sensitivity-guard.md`` Decision 5 — **sensitivity overrides
the exemption** (choice A in the prompt: the prior approval is set aside and the cluster re-parked
ONCE). DENY **E-STALE-EXEMPT** if a previously-parkable cluster slips through a stale exemption.

The hole this closes (``pipeline.py:359-360``, runs AFTER ``needs_review``):

    if flagged and any(members <= group for group in approved_groups):
        flagged = False  # exactly an already-approved merge — promote, never re-park

The inversion now flags clusters the legacy denylist missed (e.g. a ``role.rca`` member — one of
the 18 G6 holes). If such a cluster's members are a SUBSET of a human-APPROVED group recorded
BEFORE that topic was understood to be sensitive, the exemption would silently un-flag it →
auto-merge — the one path where fail-closed could accidentally NOT park.

This is an INTEGRATION test (drives ``resolve_pending`` against ephemeral Neo4j + Postgres,
mirroring ``tests/integration/test_signoff.py``):

  - Seed a positive (APPROVED) judgement on {p1, p2} — a prior human sign-off.
  - Ingest p1, p2 where p1 now carries ``topics:["role.rca"]`` (a newly-understood-sensitive risk
    code the legacy guard MISSED). The cluster {p1, p2} is an EXACT subset of the approved group.
  - Under ``guard_mode="block"`` the cluster must be RE-PARKED (``stats.review == 1``, status
    ``pending_review``, audit decision ``pending_review``, NOTHING written), NOT auto-promoted via
    the stale exemption.

PRE-FIX: ``needs_review`` misses ``role.rca`` → ``flagged`` is already ``False`` before the
exemption even runs → the merge auto-promotes (``stats.review == 0``, ``stats.promoted == 1``, the
canonical person written). The test FAILS. POST-FIX both the programmatic ``RISKS`` flag AND the
sensitivity-overrides-exemption fix must hold for it to pass — it cannot be passed by weakening
only one.

Contrast harness: ``test_t4_legacy_caught_sanction_subset_of_approved_group_*`` proves a member
carrying ``topics:["sanction"]`` (caught by BOTH guards) is the load-bearing discriminator — the
ONLY behaviour difference pre/post-fix is whether the exemption defers to the sensitivity flag.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, MergeAudit, ResolverJudgement
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration


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


def _judgement(left: str, right: str, verdict: str) -> ResolverJudgement:
    low, high = sorted((left, right))
    return ResolverJudgement(
        id=str(uuid.uuid4()),
        left_id=low,
        right_id=high,
        judgement=verdict,
        source="signoff",
    )


def _person(member_id: str, *, topics: list[str] | None = None) -> dict[str, object]:
    """Two of these (identical name+nationality+dob) cluster as a merge under Splink."""
    properties: dict[str, object] = {
        "name": ["Vladimir Example"],
        "nationality": ["ru"],
        "birthDate": ["1960-01-01"],
    }
    if topics:
        properties["topics"] = topics
    return {"id": member_id, "schema": "Person", "properties": properties, "datasets": ["t"]}


def _person_ids(neo4j: Neo4jClient) -> list[str]:
    return [
        row["id"] for row in neo4j.execute_read("MATCH (n:Person) RETURN n.id AS id ORDER BY n.id")
    ]


def test_t4_stale_exemption_does_not_auto_promote_newly_sensitive_cluster(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """T4: a cluster ⊆ a STALE approved group that is NOW sensitive (``role.rca`` — a legacy-missed
    risk code) must RE-PARK, not auto-promote through the exemption.

    The operator approved {p1, p2} before ``role.rca`` was understood sensitive. p1 now carries
    that topic and Splink re-forms the EXACT same cluster {p1, p2}. Per ADR 0047 Decision 5, a
    sensitivity flag is not exemptible by a prior approval: the cluster is set aside and re-parked
    once.

    PRE-FIX: ``needs_review`` never flags ``role.rca`` (one of the 18 G6 misses), so the cluster
    auto-promotes (``stats.review == 0``, ``promoted == 1``, the canonical person written). FAILS.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    # Prior human sign-off: {p1, p2} are the same entity. Recorded BEFORE role.rca was understood
    # sensitive — exactly the stale approval the exemption would honour.
    with sessions() as session:
        session.add(_judgement("p1", "p2", "positive"))
        session.commit()

    # Re-ingest the SAME pair, now with a newly-understood-sensitive risk code on p1.
    with sessions() as session:
        session.add(_queue_item(_person("p1", topics=["role.rca"]), source="p1"))
        session.add(_queue_item(_person("p2"), source="p2"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # The fence: the newly-sensitive cluster must re-park ONCE, not slip through the stale
    # exemption.
    assert stats.review == 1, (
        "a newly-sensitive (role.rca) cluster that is a subset of a STALE approval must re-park — "
        "sensitivity overrides the exemption (ADR 0047 Dec 5); the stale exemption must NOT "
        "auto-promote it"
    )
    assert stats.promoted == 0, "the stale-exempted sensitive cluster must NOT auto-promote"
    assert _person_ids(clean_graph) == [], "a parked cluster writes NOTHING to the graph"

    with sessions() as session:
        parked = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        assert set(parked.source_ids) == {"p1", "p2"}, "re-parked with both members"
        merged_rows = session.execute(
            select(func.count()).select_from(MergeAudit).where(MergeAudit.decision == "merged")
        ).scalar_one()
        assert merged_rows == 0, "no merged audit row — the cluster was parked, not promoted"
        parked_items = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.status == "pending_review")
        ).scalar_one()
        assert parked_items == 2, "both queue rows move to pending_review"
    engine.dispose()


def test_t4_non_sensitive_subset_of_approved_group_still_auto_promotes(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """T4 (no-regression fence): the approve-to-promote path for a NON-sensitive merge is unchanged.

    {p1, p2} approved, re-ingested with NO risk topic → the exemption still un-flags it (the merge
    is not sensitive, only size/anchor would be exemptible and neither fires) → it auto-promotes.
    Proves the fix narrows the exemption to sensitivity ONLY and does not break ordinary approved
    re-merges (spec §9: never weaken the existing approve-to-promote path). This passes pre- AND
    post-fix; it is the contrast that makes the sensitive case's failure load-bearing.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    with sessions() as session:
        session.add(_judgement("p1", "p2", "positive"))
        session.commit()
    with sessions() as session:
        session.add(_queue_item(_person("p1"), source="p1"))
        session.add(_queue_item(_person("p2"), source="p2"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    assert stats.review == 0, "a non-sensitive approved re-merge must NOT re-park"
    assert stats.promoted == 1, "the approved non-sensitive merge auto-promotes (exemption holds)"
    assert len(_person_ids(clean_graph)) == 1, "one canonical person is written"
    engine.dispose()
