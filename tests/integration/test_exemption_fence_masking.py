"""Gate E (slice-3) — T-MASK-khop: the short-circuit MASKING fail-open (the failing-first oracle).

Spec: ``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md`` §15.1 (Finding B — the masking fail-open),
§15.2 (the structured probe ``has_nonexemptible_sensitivity``), §15.6 (the STRUCTURED
NON-EXEMPTIBILITY / NO-MASKING HARD INV), §15.7 (failing-first requirement), §16 (``T-MASK-khop``).
ADR ``docs/decisions/0047-fail-closed-sensitivity-guard.md`` Decision 5 + the slice-3 refinement.
DENY **E-MASK** (a facet of E-STALE-EXEMPT).

THE INVARIANT. A cluster that is SIMULTANEOUSLY [an exemptible-first flag: size>10 OR a
LEGACY-CAUGHT topic like ``sanction`` OR anchor-conflict] AND [a NON-exemptible signal: a member
k-hop-adjacent to a non-ghost risk node, OR Chow-in-band, OR a newly-broadened topic] AND ⊆ a
STALE approved group MUST RE-PARK, never auto-promote (§15.6). Non-exemptibility is computed by the
STRUCTURED probe ``has_nonexemptible_sensitivity(cluster, by_id, neo4j)`` — INDEPENDENT of
``needs_review``'s first-flag short-circuit and of the returned reason STRING.

THE MASKING (why these are RED on slice-2). ``needs_review`` returns only the FIRST flag's reason
(order: size>10 → Stage-1 topic → anchor-conflict → Stage-2 k-hop → Stage-3 Chow). slice-2's fence
un-flagged a cluster ⊆ an approved group unless ``is_newly_broadened_sensitive`` (TOPIC-only) OR
``is_nonexemptible_reason(reason)`` (a SUBSTRING match of the single reason) held. So when an
EXEMPTIBLE flag fires FIRST, the co-occurring Stage-2 k-hop signal is NEVER in the reason →
``is_nonexemptible_reason`` MISSES it → the stale exemption un-flags it → **AUTO-PROMOTE** despite
real risk-adjacency.

These are END-TO-END integration tests driven through the LIVE ``resolve_pending`` pipeline
(Neo4j + Postgres), mirroring the FROZEN ``tests/integration/test_sensitivity_guard.py`` T4
harness. They live in their OWN file so the frozen T4 stays byte-for-byte (spec §15.8 / §16 notes).

Construction (sanction variant — spec §16): seed a PRIOR-batch
``(:Entity:Person:Sanction {id:"risk-1"})-[:LINKED]->(:Entity:Person {id:"p2"})`` (mirrors T5a);
record a STALE positive judgement ``{p1, p2}``; re-ingest ``p1`` with ``topics=["sanction"]``
(legacy-caught — Stage 1 fires FIRST with an EXEMPTIBLE reason; ``is_newly_broadened_sensitive``
of ``p1`` is False) and a topic-clean ``p2`` (graph-resolvable to ``risk-1``). The ONLY
non-exemptible signal is ``p2``'s k-hop adjacency; without the structured probe the ``sanction``
reason hides it.

PRE-FIX (slice-2): the ``sanction`` / size flag short-circuits ``needs_review`` with an EXEMPTIBLE
reason, ``is_nonexemptible_reason`` misses ``p2``'s k-hop signal, the stale exemption un-flags →
**AUTO-PROMOTES** (``stats.review == 0``, ``promoted == 1``, a canonical written) → the test FAILS.
POST-FIX: ``has_nonexemptible_sensitivity`` evaluates ``p2``'s k-hop adjacency independently →
RE-PARKS (``stats.review == 1``, ``promoted == 0``, nothing written) → PASSES.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, MergeAudit, ResolverJudgement
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.settings import get_settings

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
    """Two+ of these (identical name+nationality+dob) cluster as one merge under Splink."""
    properties: dict[str, object] = {
        "name": ["Vladimir Example"],
        "nationality": ["ru"],
        "birthDate": ["1960-01-01"],
    }
    if topics:
        properties["topics"] = topics
    return {"id": member_id, "schema": "Person", "properties": properties, "datasets": ["t"]}


def _canonical_ids(neo4j: Neo4jClient) -> list[str]:
    """Ids of any MERGED canonical (``wmc-``) Person node written by the pipeline.

    The seeded ``risk-1`` / ``p2`` / ``m00`` nodes are NOT ``wmc-``, so this isolates exactly what
    an auto-promote (pre-fix) WOULD write and a re-park (post-fix) must NOT: an empty list proves
    nothing was written for the merge.
    """
    return [
        row["id"]
        for row in neo4j.execute_read(
            "MATCH (n:Person) WHERE n.id STARTS WITH 'wmc-' RETURN n.id AS id ORDER BY n.id"
        )
    ]


@pytest.fixture
def guard_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the Stage-2 k-hop depth ON (k=1) and the Chow band OFF, clearing the settings cache.

    ``resolve_pending`` reads ``settings.sensitivity_khop_depth`` (default 1) and the abstain band
    via the cached ``get_settings``. Pinning ``k=1`` makes ``p2``'s one-hop adjacency the
    load-bearing NON-exemptible signal; clearing the abstain env keeps the band OFF so the
    EXEMPTIBLE first flag (``sanction`` / size) is provably the only thing masking it — the
    re-park, post-fix, can ONLY come from the structured k-hop probe.
    """
    monkeypatch.setenv("SENSITIVITY_KHOP_DEPTH", "1")
    monkeypatch.delenv("SENSITIVITY_ABSTAIN_LOW", raising=False)
    monkeypatch.delenv("SENSITIVITY_ABSTAIN_HIGH", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------------------------
# T-MASK-khop (sanction variant, simplest) — a LEGACY-CAUGHT topic flag masks a k-hop signal.
# --------------------------------------------------------------------------------------------


def test_mask_legacy_caught_sanction_first_flag_does_not_unmask_khop(
    clean_graph: Neo4jClient, postgres_dsn: str, guard_env: None
) -> None:
    """A cluster whose FIRST (Stage-1) flag is a LEGACY-CAUGHT ``sanction`` topic — but whose
    topic-clean member ``p2`` is ONE hop from a seeded non-ghost ``:Sanction`` node — ⊆ a STALE
    approval must RE-PARK, not auto-promote (spec §16 ``T-MASK-khop``; DENY E-MASK).

    The operator approved ``{p1, p2}`` before any graph awareness. On re-ingest ``p1`` carries
    ``topics=["sanction"]`` (legacy-caught → Stage-1 fires FIRST with an EXEMPTIBLE reason;
    ``is_newly_broadened_sensitive(p1)`` is False) and ``p2`` is topic-clean but graph-resolvable
    to the seeded ``risk-1`` (``:Sanction``) one hop away. The ONLY non-exemptible signal is
    ``p2``'s k-hop adjacency, which the ``sanction`` reason hides from a reason-string fence.

    PRE-FIX (slice-2): ``needs_review`` short-circuits on the ``sanction`` topic,
    ``is_newly_broadened`` is False, ``is_nonexemptible_reason`` (over the topic reason) is False,
    so the stale exemption un-flags the cluster → AUTO-PROMOTES (``review == 0``, ``promoted == 1``,
    a ``wmc-`` canonical written) → **FAILS**. POST-FIX: ``has_nonexemptible_sensitivity`` evaluates
    ``p2``'s k-hop adjacency independently → re-parks.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    # PRIOR-batch graph state: a non-ghost risk node one hop from p2 (the graph-resolvable member).
    clean_graph.execute_write(
        "CREATE (risk:Entity:Person:Sanction {id: $risk}) "
        "CREATE (p2:Entity:Person {id: $p2}) "
        "CREATE (risk)-[:LINKED]->(p2)",
        risk="risk-1",
        p2="p2",
    )
    # Fixture pin (non-vacuity): risk-1 IS exactly one hop from p2 and IS :Sanction-labelled, so
    # the k-hop signal the probe must catch genuinely exists.
    adjacent = clean_graph.execute_read(
        "MATCH (:Sanction {id: 'risk-1'})-[]-(:Person {id: 'p2'}) RETURN count(*) AS n"
    )[0]["n"]
    assert adjacent == 1, "fixture: risk-1 (:Sanction) must be exactly one hop from p2"

    # STALE prior sign-off: {p1, p2} are the same entity (no graph/sanction awareness at the time).
    with sessions() as session:
        session.add(_judgement("p1", "p2", "positive"))
        session.commit()

    # Re-ingest the SAME pair: p1 legacy-caught (sanction, exemptible-FIRST), p2 the k-hop carrier.
    with sessions() as session:
        session.add(_queue_item(_person("p1", topics=["sanction"]), source="p1"))
        session.add(_queue_item(_person("p2"), source="p2"))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # The masking-proof fence: the k-hop signal survives the exemptible first flag → re-park.
    assert stats.review == 1, (
        "an exemptible-first (sanction) flag must NOT mask p2's k-hop adjacency: the cluster ⊆ a "
        "STALE approval must RE-PARK (spec §15.6 / §16 T-MASK-khop; DENY E-MASK). PRE-FIX it "
        "auto-promotes because is_nonexemptible_reason never sees the masked k-hop signal."
    )
    assert stats.promoted == 0, "the masked, k-hop-adjacent stale-exempt cluster must NOT promote"
    assert _canonical_ids(clean_graph) == [], "a re-parked cluster writes NO canonical to the graph"

    with sessions() as session:
        parked = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        assert set(parked.source_ids) == {"p1", "p2"}, (
            "non-vacuity: the re-parked audit must cover EXACTLY the approved pair {p1, p2} — "
            "proving the merge actually formed over the stale-approved members"
        )
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


# --------------------------------------------------------------------------------------------
# T-MASK-khop (size variant) — the size>10 flag is the PUREST exemptible-masker (no legacy-topic
# confound: every member is topic-clean, so the masking flag is unambiguously the size cap).
# --------------------------------------------------------------------------------------------


def test_mask_size_over_10_first_flag_does_not_unmask_khop(
    clean_graph: Neo4jClient, postgres_dsn: str, guard_env: None
) -> None:
    """An 11-member, fully TOPIC-CLEAN cluster (FIRST flag unambiguously size>10) whose member
    ``m00`` is ONE hop from a seeded non-ghost ``:Sanction`` node, ⊆ a STALE approval, must RE-PARK
    (spec §16 ``T-MASK-khop`` size parametrization; DENY E-MASK).

    The size cap is the purest exemptible-masker: with NO topic on any member, the ONLY flag
    ``needs_review`` can short-circuit on is the size reason, and the ONLY non-exemptible signal is
    ``m00``'s k-hop adjacency. The 11 identical records ⊆ a star of positive judgements form one
    approved merge of 11.

    PRE-FIX (slice-2): ``needs_review`` returns the SIZE reason first (before Stage-2 k-hop),
    ``is_newly_broadened`` is False (topic-clean), ``is_nonexemptible_reason`` (over the size
    reason) is False, so the stale exemption un-flags → AUTO-PROMOTES (``review == 0``,
    ``promoted == 1``) → **FAILS**. POST-FIX: ``has_nonexemptible_sensitivity`` sees ``m00``'s
    k-hop adjacency → re-parks.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    ensure_constraints(clean_graph)

    member_ids = [f"m{i:02d}" for i in range(11)]
    star = member_ids[0]  # m00 — the star centre AND the k-hop carrier

    # PRIOR-batch graph state: a non-ghost risk node one hop from m00 (a cluster member).
    clean_graph.execute_write(
        "CREATE (risk:Entity:Person:Sanction {id: $risk}) "
        "CREATE (m:Entity:Person {id: $m}) "
        "CREATE (risk)-[:LINKED]->(m)",
        risk="risk-1",
        m=star,
    )
    adjacent = clean_graph.execute_read(
        "MATCH (:Sanction {id: 'risk-1'})-[]-(:Person {id: $m}) RETURN count(*) AS n", m=star
    )[0]["n"]
    assert adjacent == 1, "fixture: risk-1 (:Sanction) must be exactly one hop from m00"

    # STALE sign-off: a star of positive judgements puts all 11 members in ONE approved group.
    with sessions() as session:
        for other in member_ids[1:]:
            session.add(_judgement(star, other, "positive"))
        session.commit()

    # Re-ingest all 11 topic-clean members (one batch — default batch size 1000).
    with sessions() as session:
        for mid in member_ids:
            session.add(_queue_item(_person(mid), source=mid))
        session.commit()
    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    assert stats.review == 1, (
        "a size>10 EXEMPTIBLE first flag must NOT mask m00's k-hop adjacency: the cluster ⊆ a "
        "STALE approval must RE-PARK (spec §15.6 / §16; DENY E-MASK). PRE-FIX the size reason "
        "hides the k-hop signal from is_nonexemptible_reason and the cluster auto-promotes."
    )
    assert stats.promoted == 0, (
        "the masked, k-hop-adjacent oversized stale-exempt cluster must NOT promote"
    )
    assert _canonical_ids(clean_graph) == [], "a re-parked cluster writes NO canonical to the graph"

    with sessions() as session:
        parked = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        assert set(parked.source_ids) == set(member_ids), (
            "non-vacuity: the re-parked audit must cover EXACTLY the 11 approved members — proving "
            "the oversized merge actually formed over the stale-approved group"
        )
        merged_rows = session.execute(
            select(func.count()).select_from(MergeAudit).where(MergeAudit.decision == "merged")
        ).scalar_one()
        assert merged_rows == 0, "no merged audit row — the cluster was parked, not promoted"
        parked_items = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.status == "pending_review")
        ).scalar_one()
        assert parked_items == 11, "all 11 queue rows move to pending_review"
    engine.dispose()
