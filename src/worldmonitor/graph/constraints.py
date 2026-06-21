"""Schema constraints + indexes for the resolved graph.

Per-tenant uniqueness on the canonical anchor IDs (Wikidata / GeoNames / LEI /
OpenCorporates) so the same real-world identifier can't land twice *inside* a
tenant, plus a ``tenant_id`` index because every read is tenant-scoped. The
anchors are populated later (entity resolution + reference anchors); creating the
constraints now is harmless and makes the invariant enforceable from the first
write. Uniqueness is composite (``tenant_id`` + the ID) so two tenants may each
hold the same canonical entity, and nodes without an anchor are unconstrained.
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
    """Idempotently create canonical-ID uniqueness constraints + the tenant index."""
    for prop in CANONICAL_ID_PROPERTIES:
        client.execute_write(
            f"CREATE CONSTRAINT entity_tenant_{prop} IF NOT EXISTS "
            f"FOR (n:{ENTITY_LABEL}) REQUIRE (n.tenant_id, n.{prop}) IS UNIQUE"
        )
    client.execute_write(
        f"CREATE INDEX entity_tenant_id IF NOT EXISTS FOR (n:{ENTITY_LABEL}) ON (n.tenant_id)"
    )
