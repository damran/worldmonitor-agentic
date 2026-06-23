"""H-1 (ADR 0037) — a human NEGATIVE judgement is enforced TRANSITIVELY.

A reject of pair A–C must hold even when a later batch contains a bridging record B with
strong A~B and B~C links: without enforcement, the transitive chain re-fuses A and C into
one canonical node, silently overriding the human "these are distinct" (graph corruption by
a different route than a direct re-merge). These unit tests drive `cluster_and_merge`
directly (pure — no Splink, no DB) with explicit `ScoredPair`s and `StoredJudgement`s,
mirroring the audit's reproduction.
"""

from __future__ import annotations

import logging

import pytest

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import ResolvedCluster, StoredJudgement, cluster_and_merge
from worldmonitor.resolution.splink_model import ScoredPair


def _person(entity_id: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Person",
            "properties": {"name": ["Ivan Petrov"], "nationality": ["ru"]},
            "datasets": ["t"],
        }
    )


def _cluster_of(clusters: list[ResolvedCluster], member_id: str) -> ResolvedCluster:
    return next(c for c in clusters if member_id in c.member_ids)


def test_negative_judgement_suppresses_transitive_refusion() -> None:
    """The H-1 bug: reject A–C, then a bridging B (A~B, B~C) must NOT re-fuse A and C."""
    entities = [_person("a"), _person("b"), _person("c")]
    pairs = [ScoredPair("a", "b", 0.99), ScoredPair("b", "c", 0.95)]
    clusters = cluster_and_merge(
        entities, pairs, judgements=[StoredJudgement("a", "c", "negative")]
    )
    a_cluster = _cluster_of(clusters, "a")
    c_cluster = _cluster_of(clusters, "c")
    assert "c" not in a_cluster.member_ids, "a rejected pair must not be re-fused transitively"
    assert a_cluster is not c_cluster
    # b joins its higher-scored side (a~b 0.99 > b~c 0.95); c is left on its own.
    assert set(a_cluster.member_ids) == {"a", "b"}
    assert set(c_cluster.member_ids) == {"c"}


def test_bridging_record_joins_its_higher_scored_side() -> None:
    """Auto-resolve-by-score: when the chain breaks, B lands on its strongest link."""
    entities = [_person("a"), _person("b"), _person("c")]
    pairs = [ScoredPair("a", "b", 0.93), ScoredPair("b", "c", 0.99)]  # b~c is stronger now
    clusters = cluster_and_merge(
        entities, pairs, judgements=[StoredJudgement("a", "c", "negative")]
    )
    assert set(_cluster_of(clusters, "c").member_ids) == {"b", "c"}, "b joins its stronger side"
    assert set(_cluster_of(clusters, "a").member_ids) == {"a"}


def test_direct_rejected_pair_never_re_merges() -> None:
    """Regression (Legion-style): a rejected DIRECT pair stays split."""
    entities = [_person("a"), _person("c")]
    pairs = [ScoredPair("a", "c", 0.99)]
    clusters = cluster_and_merge(
        entities, pairs, judgements=[StoredJudgement("a", "c", "negative")]
    )
    assert all(not c.is_merge for c in clusters), "the rejected direct pair must not merge"
    assert {c.member_ids for c in clusters} == {("a",), ("c",)}


def test_reject_is_reversible_by_a_later_approve() -> None:
    """The reject holds only while the negative exists — an approve of A–C reverses it."""
    pairs = [ScoredPair("a", "b", 0.99), ScoredPair("b", "c", 0.95)]
    # Negative A–C → suppressed: a and c are NOT co-clustered.
    rejected = cluster_and_merge(
        [_person("a"), _person("b"), _person("c")],
        pairs,
        judgements=[StoredJudgement("a", "c", "negative")],
    )
    assert "c" not in _cluster_of(rejected, "a").member_ids

    # Flip the human decision to POSITIVE (an approve): A, B, C now co-cluster cleanly —
    # a positive connection is reported before the negative is consulted, so it is not
    # permanent. (Same entities + same Splink pairs; only the judgement changed.)
    approved = cluster_and_merge(
        [_person("a"), _person("b"), _person("c")],
        pairs,
        judgements=[StoredJudgement("a", "c", "positive")],
    )
    assert len(approved) == 1, "an approve of the pair reverses the suppression"
    assert set(approved[0].member_ids) == {"a", "b", "c"}


def test_clean_chain_without_a_negative_still_merges_fully() -> None:
    """No false suppression: a chain with no negative judgement merges into one cluster."""
    entities = [_person("a"), _person("b"), _person("c")]
    pairs = [ScoredPair("a", "b", 0.99), ScoredPair("b", "c", 0.95)]
    clusters = cluster_and_merge(entities, pairs)
    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"a", "b", "c"}


def test_suppression_is_observable_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """Enforcement must be observable (this fix removes a previously-silent override)."""
    pairs = [ScoredPair("a", "b", 0.99), ScoredPair("b", "c", 0.95)]
    with caplog.at_level(logging.WARNING, logger="worldmonitor.resolution.merge"):
        cluster_and_merge(
            [_person("a"), _person("b"), _person("c")],
            pairs,
            judgements=[StoredJudgement("a", "c", "negative")],
        )
    assert "suppressed Splink merge" in caplog.text, "the suppression must be logged"
    assert "b~c" in caplog.text, "the log names the suppressed (reject-crossing) link"
