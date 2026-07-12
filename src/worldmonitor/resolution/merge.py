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
from followthemoney import Dataset, Statement, StatementEntity
from followthemoney.exc import InvalidData
from nomenklatura import Judgement
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import get_provenance, stamp_witness_map
from worldmonitor.resolution.splink_model import ScoredPair

logger = logging.getLogger(__name__)

DEFAULT_MERGE_THRESHOLD = 0.92

# Gate C (ADR 0045): the entity-construction Dataset ``StatementEntity.from_statements`` requires.
# This is the FtM ``Dataset`` whose slug-safe ``name`` stamps the ``id`` pseudo-statement — it is
# DISTINCT from the per-``Statement`` ``dataset`` field, which carries each member's actual
# ``Provenance.source_id`` (the real source lineage the witness map is derived from). The two MUST
# NOT be conflated (VERIFIED_API.md / spec §2 Dataset-name collision note): the per-statement
# ``dataset`` is a free-form string (hyphens allowed, e.g. ``src-A``), while this construction
# Dataset's ``name`` must satisfy FtM's slug rule, so a fixed slug is used and excluded from every
# witness map via the ``id`` pseudo-property carve-out (provenance.model).
_FUSION_DATASET = Dataset({"name": "worldmonitor", "title": "WorldMonitor"})

# A MERGED cluster's canonical id is content-addressed: a deterministic function of its
# sorted member ids (B-1, ADR 0036). nomenklatura still computes the CLUSTERING (transitive
# positive judgements); only the *final* id is derived here, instead of nomenklatura's random
# ``NK-<shortuuid>``. This makes a crash+retry idempotent: re-resolving the same member set
# re-derives the SAME id, so the graph MERGE (keyed by ftmg's native ``{id}``) converges on one
# node rather than minting a duplicate/orphan. SHA-256 makes an accidental collision between
# genuinely distinct member sets infeasible; a singleton keeps its own id so its node id and
# inbound edges are unchanged.
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
    merge_incompatible: bool = False
    """True for a singleton re-emitted because it was schema-incompatible with its transitive
    cluster (H-2, ADR 0041); the pipeline dead-letters the skip while still materialising its
    own correct-schema node. An ordinary (genuinely-merged or unjudged) cluster keeps it False."""

    @property
    def is_merge(self) -> bool:
        """True if this canonical entity collapses more than one source entity."""
        return len(self.member_ids) > 1


@dataclass(frozen=True, slots=True)
class StoredJudgement:
    """A persisted human sign-off judgement on a pair (ADR 0031)."""

    left_id: str
    right_id: str
    judgement: str  # "positive" | "negative"


def _ephemeral_resolver() -> nk.Resolver:
    """Return a private, in-memory nomenklatura resolver scoped to ONE batch.

    Batch-first resolution (ADR 0026) resolves each batch in isolation: it must not
    read or write any cross-batch state. ``Resolver.make_default()`` binds to a shared,
    persistent SQLite ledger (``NOMENKLATURA_DB_URL``); judgements accumulate there
    across every batch and run, so one batch's merge can canonicalize a later batch's
    entities — a batch-purity violation (and, before single-tenancy, the ADR-0028
    cross-batch leak). A throwaway in-memory engine (one shared connection via
    ``StaticPool``) makes the resolver a pure function of *this* batch's pairs, which
    is also the B-1 crash-recovery guarantee (a re-run re-derives the same clusters).
    Persistent / incremental resolution is the deferred incremental-ER work (ADR 0019b).
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

    Persisted human sign-off ``judgements`` (ADR 0031) are seeded into the ephemeral
    resolver FIRST and take precedence over Splink: a Splink pair that a judgement
    already decided is skipped
    (the human decision wins), so a rejected cluster never re-merges and an approved
    one always does — neither re-parks on a later batch.

    A negative judgement is enforced **transitively** (H-1, ADR 0037): a Splink positive
    that would connect two entities across a stored negative (directly, or via a bridging
    record that joins their components) is suppressed, so a rejected pair can never be
    silently re-fused through a third record. Positives are applied strongest-first so the
    bridging record lands on its highest-confidence side. The reject holds only while the
    negative judgement exists — a later approve (positive) of the pair reverses it (a
    positive connection is reported before the negative is consulted), so it is not permanent.
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
        # Apply Splink positives strongest-first so that when a negative judgement breaks
        # a chain (H-1), the bridging record joins its HIGHEST-confidence side deterministically.
        for pair in sorted(pairs, key=lambda p: p.probability, reverse=True):
            key = frozenset((pair.left_id, pair.right_id))
            if key in decided_pairs:
                continue  # a human sign-off already decided this pair — never override it
            if (
                pair.probability >= merge_threshold
                and pair.left_id in by_id
                and pair.right_id in by_id
            ):
                # H-1 (ADR 0037): a human NEGATIVE judgement forbids co-clustering its pair —
                # even TRANSITIVELY. nomenklatura's get_judgement is component-aware (it reports
                # NEGATIVE when a negative edge exists between the two ids' positive-connected
                # components), so skipping a positive it flags suppresses a bridging link that
                # would otherwise re-fuse a rejected pair via a third record. Only the
                # reject-crossing link is dropped; the other (valid) links still merge. Logged
                # so this enforcement is observable (it removes a previously-silent override).
                if resolver.get_judgement(pair.left_id, pair.right_id) == Judgement.NEGATIVE:
                    logger.warning(
                        "resolution: suppressed Splink merge %s~%s (score %.3f) — a human "
                        "negative judgement forbids co-clustering these entities, directly or "
                        "transitively (H-1, ADR 0037)",
                        pair.left_id,
                        pair.right_id,
                        pair.probability,
                    )
                    continue
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
        merged, dropped = _merge_entities(canonical_id, member_ids, by_id)
        if not dropped:
            clusters.append(
                ResolvedCluster(
                    canonical_id=canonical_id,
                    member_ids=member_ids,
                    entity=merged,
                    score=_cluster_score(member_ids, pair_scores),
                )
            )
            continue
        # H-2 (ADR 0041): one or more members had no common FtM schema with the merge base and
        # could NOT be merged in. Rebuild the KEPT (genuinely-merged) cluster with a canonical id
        # RE-DERIVED from only the kept set (ADR 0036 — the content-address must reflect what was
        # actually merged, or a crash+retry would diverge), then re-emit EACH dropped member as
        # its own correct-schema singleton so no cross-schema member ever enters a merged node and
        # every member keeps its own node.
        dropped_set = set(dropped)
        kept = tuple(m for m in member_ids if m not in dropped_set)
        kept_canon = _canonical_id(kept)
        kept_entity, _ = _merge_entities(kept_canon, kept, by_id)
        clusters.append(
            ResolvedCluster(
                canonical_id=kept_canon,
                member_ids=kept,
                entity=kept_entity,
                score=_cluster_score(kept, pair_scores),
            )
        )
        for member_id in sorted(dropped):
            clusters.append(
                ResolvedCluster(
                    canonical_id=member_id,
                    member_ids=(member_id,),
                    entity=by_id[member_id],
                    score=1.0,
                    merge_incompatible=True,
                )
            )
    return clusters


def rekey_cluster(cluster: ResolvedCluster, durable_id: str) -> ResolvedCluster:
    """Return a copy of ``cluster`` re-keyed under ``durable_id`` (the anchor-preferred id).

    Gate B-front (ADR 0044): a merged cluster's ``canonical_id`` is the DURABLE id derived from its
    anchor (``resolution/canonical.resolve_durable_id``), not the ``wmc-`` idempotency fingerprint.
    This re-keys the cluster's merged FtM node so it is written under the durable id (the graph
    MERGE key, still native ``{id}``, ADR 0042) and ``build_referent_map`` maps members onto the
    durable id. A no-op when ``durable_id`` already equals the cluster's id (the unanchored
    fallback, where ``wmc-``/singleton id IS the durable id). Pure: the node's properties,
    provenance/context and every other field are untouched — only its ``id`` changes.
    """
    if durable_id == cluster.canonical_id:
        return cluster
    entity = make_entity({**cluster.entity.to_dict(), "id": durable_id})
    return ResolvedCluster(
        canonical_id=durable_id,
        member_ids=cluster.member_ids,
        entity=entity,
        score=cluster.score,
        merge_incompatible=cluster.merge_incompatible,
    )


def _member_statements(canonical_id: str, member: FtmEntity, source_id: str) -> list[Statement]:
    """Build the per-``(prop, value)`` :class:`Statement`s for one cluster member (Gate C).

    Each statement carries that member's lineage: ``dataset`` = the member's
    ``Provenance.source_id`` (the real source — VERIFIED_API.md / spec §2), ``canonical_id`` = the
    survivor id so every member's statements aggregate under one node, and ``origin``/``first_seen``
    = the raw-record pointer / retrieval timestamp from the same single-source ``Provenance``. A
    member with no stamped provenance falls back to its own id as the dataset so it is never
    witness-less.
    """
    schema_name = member.schema.name
    provenance = get_provenance(member)
    origin = provenance.source_record if provenance is not None else None
    first_seen = provenance.retrieved_at if provenance is not None else None
    statements: list[Statement] = []
    for prop in member.properties:
        for value in member.get(prop):
            statements.append(
                Statement(
                    entity_id=member.id or canonical_id,
                    prop=prop,
                    schema=schema_name,
                    value=value,
                    dataset=source_id,
                    canonical_id=canonical_id,
                    origin=origin,
                    first_seen=first_seen,
                )
            )
    return statements


def fuse_statement_entity(
    canonical_id: str, kept_ids: list[str], by_id: dict[str, FtmEntity]
) -> StatementEntity | None:
    """Fuse the KEPT cluster members into one :class:`StatementEntity` under ``canonical_id``.

    Gate C (ADR 0045): feeds ``StatementEntity.merge`` a ``StatementEntity`` (NOT a
    ``ValueEntity``), so it re-canonicalizes each member's per-``(prop, value, dataset)`` statements
    to the survivor id and ``add_statement``s them — all sources' lineage aggregates under one node
    (VERIFIED_API.md). ``add_statement``'s per-prop SET union makes the fused VALUE set identical to
    ``ValueEntity.merge`` (the §9 fence is derived independently from the kept ``ValueEntity``
    merge; this entity exists only to derive the witness map). Returns ``None`` if nothing to fuse.

    Renamed from ``_fuse_statement_entity`` to the public ``fuse_statement_entity`` (Gate 2a /
    ADR 0099): makes one authoritative fusion feed both the witness map and the statement log. Pure
    identifier rename — ZERO logic, threshold, score, or value change.

    Gate WPI-1 (ADR 0112): a member with ZERO FtM properties yields ZERO
    :func:`_member_statements` (not even the ``id`` pseudo-statement, which FtM synthesises only
    when >= 1 real statement exists), so calling ``StatementEntity.from_statements(dataset, [])``
    for it RAISES ``InvalidData: No valid schema for entity: None``. A member whose statement list
    is empty is therefore SKIPPED here (never fed to ``from_statements``); ``fused`` is seeded from
    the first NON-empty member and every subsequent non-empty member is merged in. Returns ``None``
    when EVERY member is propertyless (nothing to fuse) — the existing sentinel both callers
    already handle. This is byte-behaviour-preserving for every currently-fusing input: any member
    with >= 1 property still contributes every statement, and ``StatementEntity.merge`` unions
    statement SETS, so the fused statement set (and the witness map derived from it) is identical
    regardless of skip/merge order.
    """
    if not kept_ids:
        return None
    fused: StatementEntity | None = None
    for member_id in kept_ids:
        member = by_id[member_id]
        stmts = _member_statements(canonical_id, member, _member_source(member))
        if not stmts:
            continue  # zero-prop member: no statements to fuse (ADR 0112 crash->None hardening)
        member_entity = StatementEntity.from_statements(_FUSION_DATASET, stmts)
        if fused is None:
            fused = member_entity
        else:
            fused.merge(member_entity)
    return fused


def _member_source(member: FtmEntity) -> str:
    """The dataset id (= ``Provenance.source_id``) a member's statements are witnessed by.

    A source entity legitimately has exactly one source, so its single-source ``get_provenance`` is
    the correct per-MEMBER read. Falls back to the member's own id when unstamped, so a value is
    never assigned an empty dataset (which would make its witness set un-prunable later).
    """
    provenance = get_provenance(member)
    if provenance is not None and provenance.source_id:
        return provenance.source_id
    return member.id or ""


def _witness_map_from_statements(fused: StatementEntity) -> dict[str, set[str]]:
    """Derive the Tier-1 per-property witness map from a fused ``StatementEntity`` (spec §4/§5)."""
    witnesses: dict[str, set[str]] = defaultdict(set)
    for statement in fused.statements:
        if statement.prop == "id":
            continue  # the id pseudo-statement carries the construction Dataset, not a source
        witnesses[statement.prop].add(statement.dataset)
    return dict(witnesses)


def _merge_entities(
    canonical_id: str, member_ids: tuple[str, ...], by_id: dict[str, FtmEntity]
) -> tuple[FtmEntity, tuple[str, ...]]:
    """Combine member entities into one canonical FtM entity under ``canonical_id``.

    Returns ``(merged, dropped)`` where ``dropped`` is the set of member ids that had no
    common FtM schema with the merge base and so could NOT be merged in (H-2, ADR 0041).
    The base ``member_ids[0]`` is always mergeable into itself, so it is never in ``dropped``.
    Surfacing the dropped set (instead of only logging it) lets ``cluster_and_merge`` re-emit
    each dropped member as its own correct-schema singleton, so a cross-schema member never
    enters a merged node and is never silently swallowed into the wrong-schema canonical.

    Gate C (ADR 0045): the canonical VALUE set + FtM context (anchors, single-source ``prov_*``)
    are produced by the established ``ValueEntity.merge`` path — UNCHANGED, so the value set is
    byte-for-byte identical to the legacy fusion (the §9 value-set-invariance fence) and the
    ``(merged, dropped)`` H-2 contract is preserved exactly. The SAME kept members are then fused
    a second time as a ``StatementEntity`` purely to derive the multi-source Tier-1 witness map
    (each member's statements carry its ``Provenance.source_id`` as the per-statement ``dataset``),
    which is stamped onto the returned entity's context (``wm_prov_witnesses``). ``StatementEntity``
    has no FtM ``context``, so the value entity — not the statement entity — is what carries the
    anchors/provenance the writer projects; the statement entity contributes lineage only.
    """
    base = by_id[member_ids[0]]
    merged = make_entity({**base.to_dict(), "id": canonical_id})
    dropped: list[str] = []
    kept: list[str] = []
    for member_id in member_ids:
        try:
            merged.merge(by_id[member_id])
        except InvalidData:
            # Defence-in-depth: score_pairs already drops schema-incompatible candidate
            # pairs, but a TRANSITIVE cluster (A~B and B~C compatible, A~C not) could still
            # gather members with no common schema. FtM merge raises InvalidData on those —
            # skip the offending member (logged for audit) AND surface it to the caller (H-2,
            # ADR 0041) rather than abort the whole batch or swallow it silently.
            logger.warning(
                "merge: skipped schema-incompatible member %s (%s) in cluster %s (%s)",
                member_id,
                by_id[member_id].schema.name,
                canonical_id,
                merged.schema.name,
            )
            dropped.append(member_id)
        else:
            kept.append(member_id)
    # Gate C: derive + stamp the Tier-1 witness map from the SAME kept members (a no-op for an
    # entity with no values). The fused StatementEntity is lineage-only; the value set above is
    # authoritative and the fence proves the two agree.
    fused = fuse_statement_entity(canonical_id, kept, by_id)
    if fused is not None:
        stamp_witness_map(merged, _witness_map_from_statements(fused))
    return merged, tuple(dropped)


def _cluster_score(member_ids: tuple[str, ...], pair_scores: dict[frozenset[str], float]) -> float:
    """Weakest-link score among the cluster's member pairs (1.0 for a singleton)."""
    relevant = [score for key, score in pair_scores.items() if key <= set(member_ids)]
    return min(relevant) if relevant else 1.0
