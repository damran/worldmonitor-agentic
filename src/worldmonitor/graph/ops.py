"""Gate B-4a slice-1 — the Tier-1-aware Neo4j prune for GDPR source erasure (ADR 0049).

``erase_source_graph(neo4j, source_id)`` removes ONE source's contribution from the property
graph. B's provenance is **Tier-1 only** (verified in ``graph/writer.py`` + ``provenance/model.py``
+ ``resolution/merge.py``): every node carries ``prov_*`` (G1, single-source = ``source[0]``) and a
(possibly fused) node additionally carries a ``prov_witnesses`` JSON ``{prop: [datasets…]}`` map.
There is no Tier-2 (``:Statement``/``:Source``), so the graph erase works purely on
``prov_witnesses`` + ``prov_*`` + node/edge deletion.

The decision is made by **parsing ``prov_witnesses`` in Python** — a Cypher ``CONTAINS`` /
``prov_source_id =`` is only a cheap candidate pre-filter that may over-match (the substring trap:
``"ofac"`` is a substring of ``"ofac-eu"``), never the decision (spec §4.3, pinned by T5b):

* **sole-source node** (``datasets(n) ⊆ {source_id}``) → ``DETACH DELETE`` — node, its property
  VALUES, and its incident edges all go (value-complete erase, fixing A's lineage-only gap, B-1).
* **multi-source survivor** (``source_id ∈ datasets(n)`` but other datasets remain) → drop
  ``source_id`` from every witness set; ``REMOVE`` any property it was the *sole* witness of (a full
  property replace, so no dynamic-property Cypher is needed); rebuild ``prov_*`` from a surviving
  witness when ``prov_source_id == source_id`` (clearing the now-unrecoverable
  ``prov_retrieved_at``/``prov_reliability``/``prov_source_record`` so no dangling pointer to the
  deleted raw record remains — **G1 preserved**, a ``prov_source_id`` always stays).
* a pure ``CONTAINS`` false positive (``source_id`` only a *substring* of a real dataset) → no-op.

Relationships carry ``prov_*`` only (the witness map is node-only in B), so every relationship whose
``prov_source_id == source_id`` is deleted whole; a sole-source node's edges are already gone via
its ``DETACH DELETE``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, cast

from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.provenance.model import PROVENANCE_NODE_PREFIX, WITNESSES_NODE_PROPERTY

logger = logging.getLogger(__name__)

_PROV_SOURCE_ID = f"{PROVENANCE_NODE_PREFIX}source_id"
_PROV_RETRIEVED_AT = f"{PROVENANCE_NODE_PREFIX}retrieved_at"
_PROV_RELIABILITY = f"{PROVENANCE_NODE_PREFIX}reliability"
_PROV_SOURCE_RECORD = f"{PROVENANCE_NODE_PREFIX}source_record"


@dataclass(frozen=True, slots=True)
class GraphErasureCounts:
    """Per-operation counts from one graph erase (feeds the cross-store audit row)."""

    nodes_deleted: int = 0
    """Sole-source nodes ``DETACH DELETE``d (value + incident edges gone)."""
    nodes_pruned: int = 0
    """Multi-source survivors whose witness map / ``prov_*`` were pruned of the erased source."""
    props_retracted: int = 0
    """Properties ``REMOVE``d from survivors because the erased source was their sole witness."""
    edges_deleted: int = 0
    """Relationships deleted because their ``prov_source_id`` is the erased source."""


def _decode_witnesses(raw: object) -> dict[str, list[str]]:
    """Parse a node's ``prov_witnesses`` JSON into ``{prop: [datasets]}`` (empty if absent)."""
    if not isinstance(raw, str):
        return {}
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("erase: ignoring un-parseable prov_witnesses")
        return {}
    if not isinstance(decoded, dict):
        return {}
    result: dict[str, list[str]] = {}
    for prop, datasets in cast("dict[Any, Any]", decoded).items():
        if isinstance(prop, str) and isinstance(datasets, list):
            result[prop] = [str(dataset) for dataset in cast("list[Any]", datasets)]
    return result


def erase_source_graph(neo4j: Neo4jClient, source_id: str) -> GraphErasureCounts:
    """Remove ``source_id``'s contribution from the Neo4j graph (Tier-1-aware, value-complete).

    See the module docstring for the per-node decision. Returns the per-operation counts.
    Idempotent and source-scoped: a second call finds no sole-source nodes, survivors already pruned
    (the source is absent from every ``datasets(n)`` so each is a precise no-op), and no matching
    relationships — every count is zero. Erasing one source never touches another (a name that is a
    *prefix* of another never collides — the decision is an exact-string test, not ``CONTAINS``).
    """
    candidates = neo4j.execute_read(
        "MATCH (n:Entity) "
        "WHERE n.prov_source_id = $source_id OR n.prov_witnesses CONTAINS $source_id "
        "RETURN n.id AS id, properties(n) AS props",
        source_id=source_id,
    )

    sole_source_ids: list[str] = []
    nodes_pruned = 0
    props_retracted = 0

    for row in candidates:
        node_id = row["id"]
        raw_props = row["props"]
        if not isinstance(node_id, str) or not isinstance(raw_props, dict):
            continue
        props = cast("dict[str, Any]", raw_props)

        witnesses = _decode_witnesses(props.get(WITNESSES_NODE_PROPERTY))
        prov_source_id = props.get(_PROV_SOURCE_ID)

        datasets: set[str] = set()
        for dsets in witnesses.values():
            datasets.update(dsets)
        if isinstance(prov_source_id, str) and prov_source_id:
            datasets.add(prov_source_id)

        # Precise membership decision (never the CONTAINS pre-filter): a pure substring false
        # positive ('ofac' inside 'ofac-eu') leaves the node untouched (T5b).
        if source_id not in datasets:
            continue
        if datasets <= {source_id}:
            sole_source_ids.append(node_id)
            continue

        # Multi-source survivor — prune the erased source precisely; every other source is retained.
        new_props = dict(props)
        pruned_witnesses: dict[str, list[str]] = {}
        retracted = 0
        for prop, dsets in witnesses.items():
            remaining = sorted(dataset for dataset in dsets if dataset != source_id)
            if remaining:
                pruned_witnesses[prop] = remaining
            else:
                # The erased source SOLELY witnessed this property — value-retract it wholesale
                # (no collateral; no surviving source witnessed it).
                new_props.pop(prop, None)
                retracted += 1
        new_props[WITNESSES_NODE_PROPERTY] = json.dumps(pruned_witnesses, sort_keys=True)

        # G1: if the single-source pointer was the erased source, rebuild it onto a surviving
        # dataset and clear the now-unrecoverable / dangling single-source fields.
        if prov_source_id == source_id:
            surviving = sorted(
                {dataset for dsets in pruned_witnesses.values() for dataset in dsets}
            )
            if surviving:
                new_props[_PROV_SOURCE_ID] = surviving[0]
                new_props[_PROV_RETRIEVED_AT] = ""
                new_props[_PROV_RELIABILITY] = ""
                new_props[_PROV_SOURCE_RECORD] = ""

        # Full property replace: removes the retracted props + rewrites the witness map / prov_* in
        # one SET, so no dynamic-property-name Cypher (injection-safe; query stays a LiteralString).
        neo4j.execute_write(
            "MATCH (n:Entity {id: $id}) SET n = $props",
            id=node_id,
            props=new_props,
        )
        nodes_pruned += 1
        props_retracted += retracted

    nodes_deleted = 0
    if sole_source_ids:
        neo4j.execute_write(
            "MATCH (n:Entity) WHERE n.id IN $ids DETACH DELETE n",
            ids=sole_source_ids,
        )
        nodes_deleted = len(sole_source_ids)

    # Relationships carry prov_* only (node-only witness map): an edge IS its source[0] provenance.
    edge_rows = neo4j.execute_write(
        "MATCH ()-[r]->() WHERE r.prov_source_id = $source_id "
        "WITH collect(r) AS rels, count(r) AS deleted "
        "FOREACH (x IN rels | DELETE x) "
        "RETURN deleted",
        source_id=source_id,
    )
    edges_deleted = int(edge_rows[0]["deleted"]) if edge_rows else 0

    return GraphErasureCounts(
        nodes_deleted=nodes_deleted,
        nodes_pruned=nodes_pruned,
        props_retracted=props_retracted,
        edges_deleted=edges_deleted,
    )


def set_node_values(
    neo4j: Neo4jClient,
    node_id: str,
    *,
    compared_props: dict[str, list[str]],
    remove_anchor_keys: list[str],
) -> None:
    """Provenance-preserving value/anchor prune-to-fold writer (Gate P2 / ADR 0107, SF-4).

    Reads the node's FULL current properties FIRST (mirrors :func:`erase_source_graph`'s
    read-current-props-then-merge idiom above) and merges ``compared_props`` /
    ``remove_anchor_keys`` into a COPY, then issues ONE full-property-replace ``SET n = $props``
    — never a bare partial-map ``SET`` built only from the compared/anchor deltas, which would
    silently wipe ``prov_source_id`` / ``prov_witnesses`` / ``id`` / ``caption`` / ``datasets``
    (G1, plan-verify HIGH-1).

    ``compared_props``: ``{prop: new_value_list}`` — a NON-EMPTY list REPLACES the property's
    value set with the fold's row-granular result ("prune to match"); an EMPTY list REMOVEs the
    property entirely (every value the node held for it was erased-source-only).

    ``remove_anchor_keys``: bare ``CANONICAL_ID_FIELDS`` keys to REMOVE. REMOVE-only, by design
    (plan-verify HIGH-2): every anchor carries a Neo4j ``UNIQUE`` constraint
    (``graph/constraints.py``), so this writer never SETs a new/changed anchor value —
    surfacing a previously omit-on-conflict value could collide with another node and abort the
    erasure mid-transaction. A key absent from ``remove_anchor_keys`` is left untouched (no
    gratuitous anchor rebuild).

    A missing node (e.g. already ``DETACH DELETE``d by a sole-source :func:`erase_source_graph`
    prune) is a silent no-op — nothing left to prune.
    """
    rows = neo4j.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN properties(n) AS props", id=node_id
    )
    if not rows:
        return
    current_props = rows[0]["props"]
    if not isinstance(current_props, dict):
        return

    new_props: dict[str, Any] = dict(cast("dict[str, Any]", current_props))
    for prop, values in compared_props.items():
        if values:
            new_props[prop] = list(values)
        else:
            new_props.pop(prop, None)
    for key in remove_anchor_keys:
        new_props.pop(key, None)

    neo4j.execute_write(
        "MATCH (n:Entity {id: $id}) SET n = $props",
        id=node_id,
        props=new_props,
    )
