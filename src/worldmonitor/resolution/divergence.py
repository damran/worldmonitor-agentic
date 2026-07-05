"""Projection divergence measure (Gate 3a-ii-B / ADR 0102 D6/D7/D9).

Pure, Neo4j-free, DB-free graph-snapshot dataclasses + the one-directional "explained"
divergence measure. No import of any Neo4j/DB/SQLAlchemy module here, so the mandatory
``@given`` property suite (P-DIV-1/2, ``tests/property/test_prop_projection_divergence.py``)
runs entirely in memory, Docker-free.

The measure answers: *how much of the LIVE graph can the FOLD (the whole statement log,
re-projected via ``resolution.projector.project(full_rebuild=True)``) NOT explain?* It is
deliberately **one-directional** (live -> fold only) — the fold is a resolved superset of
the live graph (ADR 0100 D2), so a naive symmetric-difference measure would false-alarm on
every legitimate cross-batch consolidation (E1). See ADR 0102 D6 for the full rationale.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NodeSnapshot:
    """One graph node, read-only: id + labels + properties (each value a string-set)."""

    id: str
    labels: frozenset[str]
    props: dict[str, frozenset[str]]


@dataclass(frozen=True)
class EdgeSnapshot:
    """One graph edge, read-only: relationship type + endpoints + properties."""

    type: str
    src: str
    dst: str
    props: dict[str, frozenset[str]]


@dataclass(frozen=True)
class GraphSnapshot:
    """An immutable, in-memory snapshot of a whole graph (nodes + edges)."""

    nodes: tuple[NodeSnapshot, ...]
    edges: tuple[EdgeSnapshot, ...]


@dataclass(frozen=True)
class ProjectionDivergence:
    """The result of one :func:`measure_divergence` run (ADR 0102 D7).

    ``computed_at`` is fed by the caller (the driver's ``now``) — the measure itself takes
    no clock, keeping it pure.
    """

    unexplained_nodes: int
    unexplained_edges: int
    live_nodes: int
    live_edges: int
    computed_at: datetime

    @property
    def total(self) -> int:
        """The headline divergence count — the Prometheus gauge value (ADR 0102 D7)."""
        return self.unexplained_nodes + self.unexplained_edges


def _excluded(prop: str) -> bool:
    """The compared-property exclusion predicate (ADR 0102 D6).

    Excluded: the ``id`` join key (legitimately differs under E1 alias-collapse),
    ``wm_anchor_*`` (E2 — anchors live in entity context, never in the log), ``datasets``
    (E4 — reconstructed but batch-dependent), ``prov_*`` (scalars + ``prov_witnesses`` —
    representative-shift under E1, D6-ii), and ``caption`` (D6-iii — a single FtM *pick*
    over the name values, not a union-monotone value set: a live node's caption reflects its
    last write's pick while the fold's caption is picked over the WHOLE-log union, so a
    routine cross-batch name update legitimately diverges and would false-alarm forever. The
    caption's INPUTS — the name values themselves — remain fully compared).
    """
    return (
        prop == "id"
        or prop == "caption"
        or prop.startswith("wm_anchor_")
        or prop == "datasets"
        or prop.startswith("prov_")
    )


def _props_subset(
    live_props: dict[str, frozenset[str]],
    fold_props: dict[str, frozenset[str]],
    survivor_of: Callable[[str], str],
) -> bool:
    """Per-prop value-set subset test after ``survivor_of`` normalisation (ADR 0102 D6).

    For every non-excluded property ``p`` present on the live side, every
    ``survivor_of``-normalised live value must appear in the ``survivor_of``-normalised set
    of fold values for that prop (a missing fold prop is treated as an empty set, so any
    non-empty live value-set on that prop fails). Returns ``False`` on the first failure.
    """
    for prop, live_values in live_props.items():
        if _excluded(prop):
            continue
        fold_values = fold_props.get(prop, frozenset())
        live_norm = {survivor_of(v) for v in live_values}
        fold_norm = {survivor_of(v) for v in fold_values}
        if not live_norm.issubset(fold_norm):
            return False
    return True


def measure_divergence(
    live: GraphSnapshot,
    fold: GraphSnapshot,
    survivor_of: Callable[[str], str],
    *,
    computed_at: datetime,
) -> ProjectionDivergence:
    """The one-directional "explained" divergence measure (ADR 0102 D6).

    A live node ``L`` is EXPLAINED iff a fold node ``F`` exists with
    ``F.id == survivor_of(L.id)`` and ``_props_subset(L.props, F.props, survivor_of)``. A live
    edge ``L`` is EXPLAINED iff some fold edge under the key
    ``(L.type, survivor_of(L.src), survivor_of(L.dst))`` satisfies the same prop-subset rule.
    Labels are never compared (D6-i). ``divergence = unexplained_nodes + unexplained_edges``.
    """
    fold_nodes_by_id: dict[str, NodeSnapshot] = {node.id: node for node in fold.nodes}

    unexplained_nodes = 0
    for live_node in live.nodes:
        fold_node = fold_nodes_by_id.get(survivor_of(live_node.id))
        if fold_node is None or not _props_subset(live_node.props, fold_node.props, survivor_of):
            unexplained_nodes += 1

    fold_edges_by_key: dict[tuple[str, str, str], list[EdgeSnapshot]] = defaultdict(list)
    for fold_edge in fold.edges:
        key = (fold_edge.type, survivor_of(fold_edge.src), survivor_of(fold_edge.dst))
        fold_edges_by_key[key].append(fold_edge)

    unexplained_edges = 0
    for live_edge in live.edges:
        key = (live_edge.type, survivor_of(live_edge.src), survivor_of(live_edge.dst))
        candidates = fold_edges_by_key.get(key, ())
        if not any(
            _props_subset(live_edge.props, candidate.props, survivor_of) for candidate in candidates
        ):
            unexplained_edges += 1

    return ProjectionDivergence(
        unexplained_nodes=unexplained_nodes,
        unexplained_edges=unexplained_edges,
        live_nodes=len(live.nodes),
        live_edges=len(live.edges),
        computed_at=computed_at,
    )
