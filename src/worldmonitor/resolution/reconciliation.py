"""Gate 3b reconciliation instruments (ADR 0114 D-5, plan
``docs/fable-review/82_GATE_3B_CUTOVER_PLAN.md`` §4).

Pure, Neo4j-free, DB-free functions mirroring ``worldmonitor.resolution.divergence`` (ADR 0102):
the one-time Gate 3b cutover reconciliation runbook (82 §4/§7-12) needs a small set of executable,
``@given``-tested instruments answering questions the one-directional
:func:`worldmonitor.resolution.divergence.measure_divergence` measure cannot (or deliberately does
not) answer:

- fold-side extras invisible to the live -> fold direction (R7, :func:`enumerate_fold_side_extras`);
- a wrong-but-logged catastrophic merge hidden behind ``survivor_of``-normalisation on BOTH sides
  (R9c, :func:`find_copresent_value_divergence`);
- graph-level erased-source residue, at the node level, for both fold-extra and co-present nodes
  (R9b, :func:`find_erased_source_residue`);
- same-id live-node/edge multiplicity that the alias-collapse arithmetic could otherwise silently
  absorb (R11/R11b, :func:`reconcile_counts`);
- dropped-label loss (§3.1, :func:`compare_labels`).

No import of any Neo4j/SQLAlchemy/DB module here (INV-RECON-PURE), so the mandatory ``@given``
property suite (``tests/property/test_prop_reconciliation.py``) runs entirely in memory,
Docker-free — mirroring how ``divergence.py``'s own purity is relied on (its module docstring,
ADR 0102 D9).

Reuses ``NodeSnapshot``/``EdgeSnapshot``/``GraphSnapshot`` and the ``_excluded`` exclusion
predicate from ``worldmonitor.resolution.divergence`` rather than reimplementing them — a second,
driftable copy of the exclusion predicate is exactly the hazard ``build_survivor_of``/ADR 0102
already avoids once (see ``resolution/erasure_scrub.py`` for the same reuse precedent).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from worldmonitor.resolution.divergence import (
    EdgeSnapshot,
    GraphSnapshot,
    NodeSnapshot,
    _excluded,  # pyright: ignore[reportPrivateUsage]
)


@dataclass(frozen=True)
class FoldSideExtras:
    """Fold nodes/edges with no ``survivor_of``-preimage on the live side (R7).

    ``divergence.measure_divergence`` is deliberately one-directional (live -> fold) and is BLIND
    to a fold node/edge that no live entity resolves to — that blind spot is exactly what this
    instrument enumerates.
    """

    nodes: tuple[NodeSnapshot, ...]
    edges: tuple[EdgeSnapshot, ...]


@dataclass(frozen=True)
class CoPresentValueDivergence:
    """One (co-present node, non-excluded prop, offending fold value) finding (R9c)."""

    node_id: str
    prop: str
    value: str


@dataclass(frozen=True)
class ErasedResidue:
    """One (fold node, erased source_id) finding — the fold node still references it (R9b)."""

    node_id: str
    source_id: str


@dataclass(frozen=True)
class CountReconciliation:
    """Node/edge multiplicity reconciliation counts (R11/R11b).

    See the module docstring's residual formula (also pinned in
    ``tests/property/test_prop_reconciliation.py``'s module docstring): a same-id live
    duplicate's ``survivor_of`` value is IDENTICAL to the original row's, so
    ``distinct_live_node_survivors`` alone is blind to it. ``duplicate_live_node_ids`` is counted
    on RAW (un-normalised) ids and enters the residual as its OWN additive term so it can never be
    silently cancelled by the alias-collapse arithmetic.
    """

    live_nodes: int
    fold_nodes: int
    distinct_live_node_survivors: int
    duplicate_live_node_ids: int
    fold_side_extra_nodes: int
    node_residual: int
    live_edges: int
    fold_edges: int
    distinct_live_edge_survivors: int
    duplicate_live_edge_ids: int
    fold_side_extra_edges: int
    edge_residual: int


@dataclass(frozen=True)
class LabelDivergence:
    """One (node, label) label-parity finding."""

    node_id: str
    label: str


@dataclass(frozen=True)
class LabelParity:
    """Result of :func:`compare_labels` (§3.1) — the LOSS direction first."""

    missing_in_fold: tuple[LabelDivergence, ...]
    extra_in_fold: tuple[LabelDivergence, ...]


def enumerate_fold_side_extras(
    live: GraphSnapshot,
    fold: GraphSnapshot,
    survivor_of: Callable[[str], str],
) -> FoldSideExtras:
    """Fold nodes/edges with no live preimage under ``survivor_of`` (R7).

    A fold node ``F`` is EXTRA iff no live node ``L`` has ``survivor_of(L.id) == F.id``. A fold
    edge is EXTRA iff no live edge normalises (via ``survivor_of`` on both endpoints, mirroring
    ``measure_divergence``'s key-building) to the same ``(type, survivor_of(src),
    survivor_of(dst))`` key. Both result tuples are deterministically sorted.
    """
    live_survivor_ids = {survivor_of(node.id) for node in live.nodes}
    extra_nodes = sorted(
        (node for node in fold.nodes if node.id not in live_survivor_ids),
        key=lambda node: node.id,
    )

    live_edge_keys = {
        (edge.type, survivor_of(edge.src), survivor_of(edge.dst)) for edge in live.edges
    }
    extra_edges = sorted(
        (
            edge
            for edge in fold.edges
            if (edge.type, survivor_of(edge.src), survivor_of(edge.dst)) not in live_edge_keys
        ),
        key=lambda edge: (edge.type, edge.src, edge.dst),
    )
    return FoldSideExtras(nodes=tuple(extra_nodes), edges=tuple(extra_edges))


def find_copresent_value_divergence(
    live: GraphSnapshot,
    fold: GraphSnapshot,
    survivor_of: Callable[[str], str],
) -> list[CoPresentValueDivergence]:
    """Non-excluded fold-only prop values on a CO-PRESENT node, with no live counterpart (R9c).

    A "co-present" node is a fold node ``F`` for which some live node ``L`` has
    ``survivor_of(L.id) == F.id`` (when several live nodes map to the same fold survivor, their
    prop values are unioned before comparison, mirroring :func:`compare_labels`). Both sides are
    normalised via ``survivor_of`` exactly as ``divergence._props_subset`` does, but scanned in
    the fold -> live direction: this catches a wrong-but-logged catastrophic merge that
    ``measure_divergence``'s live -> fold subset direction cannot see (its subset check passes
    whenever the fold props are a strict superset of the live props).
    """
    live_props_by_survivor: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for live_node in live.nodes:
        target = live_props_by_survivor[survivor_of(live_node.id)]
        for prop, values in live_node.props.items():
            if _excluded(prop):
                continue
            target[prop].update(survivor_of(value) for value in values)

    findings: list[CoPresentValueDivergence] = []
    for fold_node in fold.nodes:
        live_props = live_props_by_survivor.get(fold_node.id)
        if live_props is None:
            continue  # not co-present -- enumerate_fold_side_extras's job
        for prop, fold_values in fold_node.props.items():
            if _excluded(prop):
                continue
            live_norm = live_props.get(prop, set())
            for value in fold_values:
                norm_value = survivor_of(value)
                if norm_value not in live_norm:
                    findings.append(
                        CoPresentValueDivergence(node_id=fold_node.id, prop=prop, value=norm_value)
                    )

    findings.sort(key=lambda finding: (finding.node_id, finding.prop, finding.value))
    return findings


def _decode_witnesses(raw: object) -> dict[str, list[str]]:
    """Parse a ``prov_witnesses`` JSON string into ``{prop: [datasets]}`` (mirrors
    ``graph/ops.py::_decode_witnesses`` defensively, without importing the FROZEN module)."""
    if not isinstance(raw, str):
        return {}
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    result: dict[str, list[str]] = {}
    for prop, datasets in cast("dict[Any, Any]", decoded).items():
        if isinstance(prop, str) and isinstance(datasets, list):
            result[prop] = [str(dataset) for dataset in cast("list[Any]", datasets)]
    return result


def find_erased_source_residue(
    fold: GraphSnapshot,
    erased_source_ids: frozenset[str],
) -> list[ErasedResidue]:
    """Fold nodes whose ``datasets``/``prov_witnesses`` still reference an erased source (R9b).

    One row per DISTINCT ``(node_id, source_id)`` pair. Fires when EITHER the node's ``datasets``
    prop value-set contains an erased id, OR the decoded ``prov_witnesses`` JSON map has an erased
    id in any prop's dataset list. Applies uniformly to every fold node -- fold-side-extra AND
    co-present alike (no ``live``/``survivor_of`` needed).
    """
    findings: list[ErasedResidue] = []
    for node in fold.nodes:
        referenced: set[str] = set(node.props.get("datasets", frozenset()))
        for raw in node.props.get("prov_witnesses", frozenset()):
            for datasets in _decode_witnesses(raw).values():
                referenced.update(datasets)
        for source_id in sorted(referenced & erased_source_ids):
            findings.append(ErasedResidue(node_id=node.id, source_id=source_id))

    findings.sort(key=lambda finding: (finding.node_id, finding.source_id))
    return findings


def reconcile_counts(
    live: GraphSnapshot,
    fold: GraphSnapshot,
    survivor_of: Callable[[str], str],
) -> CountReconciliation:
    """Node/edge multiplicity reconciliation (R11/R11b) -- see :class:`CountReconciliation`."""
    extras = enumerate_fold_side_extras(live, fold, survivor_of)

    live_nodes = len(live.nodes)
    fold_nodes = len(fold.nodes)
    distinct_live_node_survivors = len({survivor_of(node.id) for node in live.nodes})
    duplicate_live_node_ids = live_nodes - len({node.id for node in live.nodes})
    fold_side_extra_nodes = len(extras.nodes)
    node_residual = duplicate_live_node_ids + (
        fold_nodes - distinct_live_node_survivors - fold_side_extra_nodes
    )

    live_edges = len(live.edges)
    fold_edges = len(fold.edges)
    distinct_live_edge_survivors = len(
        {(edge.type, survivor_of(edge.src), survivor_of(edge.dst)) for edge in live.edges}
    )
    duplicate_live_edge_ids = live_edges - len(
        {(edge.type, edge.src, edge.dst) for edge in live.edges}
    )
    fold_side_extra_edges = len(extras.edges)
    edge_residual = duplicate_live_edge_ids + (
        fold_edges - distinct_live_edge_survivors - fold_side_extra_edges
    )

    return CountReconciliation(
        live_nodes=live_nodes,
        fold_nodes=fold_nodes,
        distinct_live_node_survivors=distinct_live_node_survivors,
        duplicate_live_node_ids=duplicate_live_node_ids,
        fold_side_extra_nodes=fold_side_extra_nodes,
        node_residual=node_residual,
        live_edges=live_edges,
        fold_edges=fold_edges,
        distinct_live_edge_survivors=distinct_live_edge_survivors,
        duplicate_live_edge_ids=duplicate_live_edge_ids,
        fold_side_extra_edges=fold_side_extra_edges,
        edge_residual=edge_residual,
    )


def compare_labels(
    live: GraphSnapshot,
    fold: GraphSnapshot,
    survivor_of: Callable[[str], str],
) -> LabelParity:
    """Per-co-present-node label parity, LOSS direction first (§3.1).

    ``missing_in_fold`` = a live label absent from the co-present fold node's label set (the
    direction that catches a dropped topic label -- a naive ``fold_labels <= live_labels`` check
    stays true exactly when this bug is present). ``extra_in_fold`` = a fold-invented label absent
    from live. When more than one live node maps to the same fold survivor, their live labels are
    unioned before comparison.
    """
    live_labels_by_survivor: dict[str, set[str]] = defaultdict(set)
    for node in live.nodes:
        live_labels_by_survivor[survivor_of(node.id)].update(node.labels)

    missing: list[LabelDivergence] = []
    extra: list[LabelDivergence] = []
    for fold_node in fold.nodes:
        live_labels = live_labels_by_survivor.get(fold_node.id)
        if live_labels is None:
            continue  # not co-present
        for label in sorted(live_labels - fold_node.labels):
            missing.append(LabelDivergence(node_id=fold_node.id, label=label))
        for label in sorted(fold_node.labels - live_labels):
            extra.append(LabelDivergence(node_id=fold_node.id, label=label))

    missing.sort(key=lambda finding: (finding.node_id, finding.label))
    extra.sort(key=lambda finding: (finding.node_id, finding.label))
    return LabelParity(missing_in_fold=tuple(missing), extra_in_fold=tuple(extra))
