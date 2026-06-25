"""Schema constraints + indexes for the resolved graph.

Uniqueness on the canonical anchor IDs (Wikidata / GeoNames / LEI /
OpenCorporates) so the same real-world identifier can't land twice. The anchors
are populated later (entity resolution + reference anchors); creating the
constraints now is harmless and makes the invariant enforceable from the first
write. The platform is single-tenant (D1, ADR 0042), so uniqueness is on the ID
alone; nodes without an anchor are unconstrained.
"""

from __future__ import annotations

from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS

# The base label followthemoney-graph stamps on every entity node.
ENTITY_LABEL = "Entity"

# Canonical identifier properties resolved entities are anchored on (single
# source of truth shared with the anchor enrichers).
CANONICAL_ID_PROPERTIES = CANONICAL_ID_FIELDS


def ensure_constraints(client: Neo4jClient) -> None:
    """Idempotently create the canonical-ID uniqueness constraints."""
    for prop in CANONICAL_ID_PROPERTIES:
        client.execute_write(
            f"CREATE CONSTRAINT entity_{prop} IF NOT EXISTS "
            f"FOR (n:{ENTITY_LABEL}) REQUIRE n.{prop} IS UNIQUE"
        )
