"""Graph Data Science — one degree-centrality run over the resolved graph.

Projects a tenant's resolved subgraph and ranks nodes by degree centrality to
surface the most-connected entities (flagging sanctioned ones). The projection is
tenant-scoped via a Cypher projection and always dropped afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass

from worldmonitor.graph.neo4j_client import Neo4jClient

_DEFAULT_GRAPH = "wm-degree"


@dataclass(frozen=True, slots=True)
class DegreeResult:
    """A node's degree-centrality score within the projected graph."""

    entity_id: str
    score: float
    labels: tuple[str, ...]

    @property
    def is_sanctioned(self) -> bool:
        """True if the entity carries the sanctions topic label (ftmg encodes topics as labels)."""
        return "Sanction" in self.labels


def degree_centrality(
    client: Neo4jClient,
    *,
    tenant_id: str,
    top: int = 10,
    graph_name: str = _DEFAULT_GRAPH,
) -> list[DegreeResult]:
    """Project ``tenant_id``'s resolved graph and return the top nodes by degree."""
    client.execute_write(
        "MATCH (s:Entity {tenant_id: $tenant_id}) "
        "OPTIONAL MATCH (s)-[]->(tgt:Entity {tenant_id: $tenant_id}) "
        "WITH gds.graph.project($graph, s, tgt) AS g RETURN g.graphName AS name",
        tenant_id=tenant_id,
        graph=graph_name,
    )
    try:
        rows = client.execute_read(
            "CALL gds.degree.stream($graph) YIELD nodeId, score "
            "WITH gds.util.asNode(nodeId) AS n, score "
            "RETURN n.id AS id, labels(n) AS labels, score "
            "ORDER BY score DESC, id LIMIT $top",
            graph=graph_name,
            top=top,
        )
    finally:
        client.execute_write(
            "CALL gds.graph.drop($graph, false) YIELD graphName RETURN graphName",
            graph=graph_name,
        )
    return [
        DegreeResult(
            entity_id=row["id"],
            score=float(row["score"]),
            labels=tuple(row["labels"]),
        )
        for row in rows
    ]
