"""Clustering + FtM-native merge.

Splink scores pairs; nomenklatura's :class:`Resolver` (the OpenSanctions
canonical ledger) turns high-confidence pairs into POSITIVE judgements and
computes canonical clusters. Members are then combined with FtM's own
``merge()`` — the same primitive the nomenklatura/FtM stack uses — into one
canonical entity. nomenklatura ships no type stubs, so it is imported only here.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingTypeArgument=false
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import nomenklatura as nk
from followthemoney.exc import InvalidData
from nomenklatura import Judgement
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.splink_model import ScoredPair

logger = logging.getLogger(__name__)

DEFAULT_MERGE_THRESHOLD = 0.92

# A MERGED cluster's canonical id is content-addressed: a deterministic function of its
# sorted member ids (B-1, ADR 0036). nomenklatura still computes the CLUSTERING (transitive
# positive judgements); only the *final* id is derived here, instead of nomenklatura's random
# ``NK-<shortuuid>``. This makes a crash+retry idempotent: re-resolving the same member set
# re-derives the SAME id, so the tenant-scoped graph MERGE converges on one node rather than
# minting a duplicate/orphan. The id is intentionally NOT globally unique — every node and row
# is keyed by ``(tenant_id, id)`` (the writer's composite MERGE key), so an identical member
# set in two tenants still yields two distinct nodes (G4 preserved). SHA-256 makes an accidental
# collision between genuinely distinct member sets infeasible; a singleton keeps its own id so
# its node id and inbound edges are unchanged.
_CANONICAL_ID_PREFIX = "wmc-"


def _canonical_id(member_ids: tuple[str, ...]) -> str:
    """Deterministic canonical id for a cluster (stable under the same membership).

    A singleton keeps its own id; a real merge is content-addressed by the SHA-256 of its
    sorted member ids (order-independent), so distinct clusters get distinct ids and a retry
    of the same cluster re-derives the same id.
    """
    if len(member_ids) == 1:
        return member_ids[0]
    digest = hashlib.sha256("\x00".join(sorted(member_ids)).encode("utf-8")).hexdigest()
    return f"{_CANONICAL_ID_PREFIX}{digest[:40]}"


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


@dataclass(frozen=True, slots=True)
class StoredJudgement:
    """A persisted, tenant-scoped human sign-off judgement on a pair (ADR 0031)."""

    left_id: str
    right_id: str
    judgement: str  # "positive" | "negative"


def _ephemeral_resolver() -> nk.Resolver:
    """Return a private, in-memory nomenklatura resolver scoped to ONE batch.

    Batch-first resolution (ADR 0026) resolves each batch in isolation: it must not
    read or write any cross-batch / cross-tenant state. ``Resolver.make_default()``
    binds to a shared, persistent, **non-tenant-scoped** SQLite ledger
    (``NOMENKLATURA_DB_URL``); judgements accumulate there across every batch, tenant
    and run, so one tenant's merge can canonicalize another tenant's entities — a G4
    isolation violation (proven: a record shared between two tenants fuses their
    independent merges). A throwaway in-memory engine (one shared connection via
    ``StaticPool``) makes the resolver a pure function of *this* batch's pairs.
    Persistent, per-tenant resolution is the deferred incremental-ER work (ADR 0019b).
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    return nk.Resolver.make_default(engine)


def cluster_and_merge(
    entities: Sequence[FtmEntity],
    pairs: Sequence[ScoredPair],
    *,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    judgements: Sequence[StoredJudgement] = (),
) -> list[ResolvedCluster]:
    """Cluster ``entities`` by their high-confidence ``pairs`` and merge each cluster.

    Persisted human sign-off ``judgements`` (ADR 0031, already filtered to this
    tenant by the caller) are seeded into the ephemeral resolver FIRST and take
    precedence over Splink: a Splink pair that a judgement already decided is skipped
    (the human decision wins), so a rejected cluster never re-merges and an approved
    one always does — neither re-parks on a later batch.
    """
    by_id = {entity.id: entity for entity in entities if entity.id is not None}
    resolver = _ephemeral_resolver()

    pair_scores: dict[frozenset[str], float] = {}
    groups: dict[str, list[str]] = defaultdict(list)
    judged_ids: set[str] = set()

    # The resolver mutates inside an explicit transaction (begin-once style).
    resolver.begin()
    try:
        # Seed persisted sign-off judgements first — authoritative over Splink. Only a
        # judgement whose BOTH ids are in this batch can affect its clustering.
        decided_pairs: set[frozenset[str]] = set()
        for judgement in judgements:
            if judgement.left_id in by_id and judgement.right_id in by_id:
                verdict = (
                    Judgement.NEGATIVE if judgement.judgement == "negative" else Judgement.POSITIVE
                )
                resolver.decide(judgement.left_id, judgement.right_id, verdict, user="signoff")
                decided_pairs.add(frozenset((judgement.left_id, judgement.right_id)))
                judged_ids.update((judgement.left_id, judgement.right_id))
        for pair in pairs:
            key = frozenset((pair.left_id, pair.right_id))
            if key in decided_pairs:
                continue  # a human sign-off already decided this pair — never override it
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
                pair_scores[key] = pair.probability
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
    for members in groups.values():
        member_ids = tuple(sorted(members))
        # B-1 (ADR 0036): derive the canonical id deterministically from the member set,
        # not from nomenklatura's random mint (the grouping key above is discarded), so a
        # crash+retry re-resolves to the SAME id and the graph MERGE converges.
        canonical_id = _canonical_id(member_ids)
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
        try:
            merged.merge(by_id[member_id])
        except InvalidData:
            # Defence-in-depth: score_pairs already drops schema-incompatible candidate
            # pairs, but a TRANSITIVE cluster (A~B and B~C compatible, A~C not) could still
            # gather members with no common schema. FtM merge raises InvalidData on those —
            # skip the offending member (logged for audit) rather than abort the whole batch.
            logger.warning(
                "merge: skipped schema-incompatible member %s (%s) in cluster %s (%s)",
                member_id,
                by_id[member_id].schema.name,
                canonical_id,
                merged.schema.name,
            )
    return merged


def _cluster_score(member_ids: tuple[str, ...], pair_scores: dict[frozenset[str], float]) -> float:
    """Weakest-link score among the cluster's member pairs (1.0 for a singleton)."""
    relevant = [score for key, score in pair_scores.items() if key <= set(member_ids)]
    return min(relevant) if relevant else 1.0
