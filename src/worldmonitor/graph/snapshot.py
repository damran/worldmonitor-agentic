"""The one src-side whole-graph reader (Gate 3a-ii-B / ADR 0102 D9).

:func:`read_graph_snapshot` runs exactly two ``execute_read`` Cypher queries — one for every
``id``-bearing node (id/labels/properties) and one for every edge between two ``id``-bearing
nodes (type/endpoints/properties) — and coerces every raw property value to a
``frozenset[str]`` (a scalar becomes ``{str(v)}``; a list becomes ``{str(x) for x in v}``,
mirroring the test-side ``graph_signature`` ``_stable_val`` list handling). It NEVER calls
``execute_write`` — this is the read-only half of the projection rebuild-and-diff guard's
"never write live" invariant (INV-1).
"""

from __future__ import annotations

from typing import Any, cast

from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.resolution.divergence import EdgeSnapshot, GraphSnapshot, NodeSnapshot

_NODE_QUERY = (
    "MATCH (n) WHERE n.id IS NOT NULL RETURN n.id AS nid, labels(n) AS lbls, properties(n) AS props"
)
_EDGE_QUERY = (
    "MATCH (a)-[r]->(b) WHERE a.id IS NOT NULL AND b.id IS NOT NULL "
    "RETURN type(r) AS rtype, a.id AS src, b.id AS dst, properties(r) AS rprops"
)


def _coerce_value(value: Any) -> frozenset[str]:
    """Coerce one raw Neo4j property value to a value-set of strings (ADR 0102 §2.1)."""
    if isinstance(value, list):
        return frozenset(str(item) for item in cast("list[object]", value))
    return frozenset({str(value)})


def _coerce_props(raw: dict[str, Any]) -> dict[str, frozenset[str]]:
    return {key: _coerce_value(value) for key, value in raw.items()}


def read_graph_snapshot(client: Neo4jClient) -> GraphSnapshot:
    """Read the WHOLE graph, read-only, into an in-memory :class:`GraphSnapshot`.

    Uses ``execute_read`` only — never ``execute_write`` — so this is safe to call against
    the LIVE graph (the projection-diff guard's only live-graph touch point).
    """
    node_rows = client.execute_read(_NODE_QUERY)
    nodes = tuple(
        NodeSnapshot(
            id=str(row["nid"]),
            labels=frozenset(row["lbls"]),
            props=_coerce_props(row["props"]),
        )
        for row in node_rows
    )

    edge_rows = client.execute_read(_EDGE_QUERY)
    edges = tuple(
        EdgeSnapshot(
            type=str(row["rtype"]),
            src=str(row["src"]),
            dst=str(row["dst"]),
            props=_coerce_props(row["rprops"]),
        )
        for row in edge_rows
    )

    return GraphSnapshot(nodes=nodes, edges=edges)
