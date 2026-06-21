"""Clustering + FtM-native merge.

Splink scores pairs; nomenklatura's :class:`Resolver` (the OpenSanctions
canonical ledger) turns high-confidence pairs into POSITIVE judgements and
computes canonical clusters. Members are then combined with FtM's own
``merge()`` — the same primitive the nomenklatura/FtM stack uses — into one
canonical entity. nomenklatura ships no type stubs, so it is imported only here.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import nomenklatura as nk
from nomenklatura import Judgement

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.splink_model import ScoredPair

DEFAULT_MERGE_THRESHOLD = 0.92


@dataclass(frozen=True, slots=True)
class ResolvedCluster:
    """A canonical entity merged from one or more source entities."""

    canonical_id: str
    member_ids: tuple[str, ...]
    entity: FtmEntity
    score: float
    """Weakest-link match probability within the cluster (1.0 for a singleton)."""

    @property
    def is_merge(self) -> bool:
        """True if this canonical entity collapses more than one source entity."""
        return len(self.member_ids) > 1


def cluster_and_merge(
    entities: Sequence[FtmEntity],
    pairs: Sequence[ScoredPair],
    *,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
) -> list[ResolvedCluster]:
    """Cluster ``entities`` by their high-confidence ``pairs`` and merge each cluster."""
    by_id = {entity.id: entity for entity in entities if entity.id is not None}
    resolver = nk.Resolver.make_default()

    pair_scores: dict[frozenset[str], float] = {}
    groups: dict[str, list[str]] = defaultdict(list)
    judged_ids: set[str] = set()

    # The resolver mutates inside an explicit transaction (begin-once style).
    resolver.begin()
    try:
        for pair in pairs:
            if (
                pair.probability >= merge_threshold
                and pair.left_id in by_id
                and pair.right_id in by_id
            ):
                resolver.decide(
                    pair.left_id,
                    pair.right_id,
                    Judgement.POSITIVE,
                    user="splink",
                    score=pair.probability,
                )
                pair_scores[frozenset((pair.left_id, pair.right_id))] = pair.probability
                judged_ids.update((pair.left_id, pair.right_id))
        # Only resolve ids that took part in a judgement; the rest are singletons
        # keyed by their own id (get_canonical is unreliable for unjudged ids).
        for entity_id in by_id:
            canonical = resolver.get_canonical(entity_id) if entity_id in judged_ids else entity_id
            groups[canonical].append(entity_id)
        resolver.commit()
    except Exception:
        resolver.rollback()
        raise

    clusters: list[ResolvedCluster] = []
    for canonical_id, members in groups.items():
        member_ids = tuple(sorted(members))
        clusters.append(
            ResolvedCluster(
                canonical_id=canonical_id,
                member_ids=member_ids,
                entity=_merge_entities(canonical_id, member_ids, by_id),
                score=_cluster_score(member_ids, pair_scores),
            )
        )
    return clusters


def _merge_entities(
    canonical_id: str, member_ids: tuple[str, ...], by_id: dict[str, FtmEntity]
) -> FtmEntity:
    """Combine member entities into one canonical FtM entity under ``canonical_id``."""
    base = by_id[member_ids[0]]
    merged = make_entity({**base.to_dict(), "id": canonical_id})
    for member_id in member_ids:
        merged.merge(by_id[member_id])
    return merged


def _cluster_score(member_ids: tuple[str, ...], pair_scores: dict[frozenset[str], float]) -> float:
    """Weakest-link score among the cluster's member pairs (1.0 for a singleton)."""
    relevant = [score for key, score in pair_scores.items() if key <= set(member_ids)]
    return min(relevant) if relevant else 1.0
