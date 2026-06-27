"""Property: a NEGATIVE human judgement is enforced TRANSITIVELY (H-1, ADR 0037).

A human reject of (A, B) must hold even when high-confidence Splink positives A~C and C~B would
bridge them through a third record — otherwise a rejected identity merge is silently re-fused via a
back door, with no human sign-off. This must hold under any permutation of the bridging pairs and
under either bridge orientation.
"""

from __future__ import annotations

import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.ftm import make_entity
from worldmonitor.resolution.merge import StoredJudgement, cluster_and_merge
from worldmonitor.resolution.splink_model import ScoredPair

_SETTINGS = settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])


def _company(entity_id: str, name: str):  # noqa: ANN202 - FtmEntity
    return make_entity({"id": entity_id, "schema": "Company", "properties": {"name": [name]}})


@given(
    p_ac=st.floats(min_value=0.92, max_value=1.0),
    p_cb=st.floats(min_value=0.92, max_value=1.0),
    seed=st.integers(min_value=0, max_value=10_000),
)
@_SETTINGS
def test_negative_judgement_blocks_transitive_bridge(p_ac: float, p_cb: float, seed: int) -> None:
    """NEGATIVE(a, b) keeps a and b in DIFFERENT clusters even though a~c and c~b are both above
    threshold and would otherwise bridge them. Asserted under a shuffled pair list."""
    a, b, c = _company("a", "Acme"), _company("b", "Beta"), _company("c", "Cee")
    pairs = [ScoredPair("a", "c", p_ac), ScoredPair("c", "b", p_cb)]
    random.Random(seed).shuffle(pairs)

    clusters = cluster_and_merge(
        [a, b, c], pairs, judgements=[StoredJudgement("a", "b", "negative")]
    )

    co_clustered = any(
        "a" in cluster.member_ids and "b" in cluster.member_ids for cluster in clusters
    )
    assert not co_clustered, (
        "TRANSITIVE RE-FUSION of a human-rejected pair: a and b landed in the same cluster via the "
        f"c bridge despite NEGATIVE(a,b).\n  clusters={[cl.member_ids for cl in clusters]}"
    )


@given(seed=st.integers(min_value=0, max_value=10_000))
@_SETTINGS
def test_negative_judgement_still_lets_valid_link_merge(seed: int) -> None:
    """The reject drops ONLY the reject-crossing link: a and c (or c and b) may still legitimately
    merge — the negative on (a,b) must not nuke the whole component."""
    a, b, c = _company("a", "Acme"), _company("b", "Beta"), _company("c", "Cee")
    pairs = [ScoredPair("a", "c", 0.99), ScoredPair("c", "b", 0.99)]
    random.Random(seed).shuffle(pairs)
    clusters = cluster_and_merge(
        [a, b, c], pairs, judgements=[StoredJudgement("a", "b", "negative")]
    )
    # Exactly one of the valid links survives into a 2-member merge; a and b are never together.
    merges = [cl for cl in clusters if cl.is_merge]
    assert merges, "the negative judgement wrongly suppressed ALL merges in the component"
    for cluster in merges:
        assert not ("a" in cluster.member_ids and "b" in cluster.member_ids)
