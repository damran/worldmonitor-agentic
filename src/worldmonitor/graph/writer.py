"""followthemoney-graph (ftmg) adapter — writes FtM entities into Neo4j.

ftmg owns the FtM -> property-graph transformation (schema -> labels, edges,
property links, topic labels). It is single-tenant by design, so this adapter
wraps ftmg's query generators and its :class:`QueryBatcher` to (a) inject
``tenant_id`` into every parameter set and (b) tenant-scope every MERGE/MATCH
key — upholding the non-negotiable "tenant_id on every node and edge" invariant.

ftmg ships no type stubs, so it is imported only here; the boundary's ``Unknown``
types are relaxed for this module alone while the public API stays fully typed.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ftmg.config import Configuration, DatabaseConfig
from ftmg.transform import (
    QueryBatch,
    QueryBatcher,
    generate_edge_entity,
    generate_entity_links,
    generate_node_entity,
    generate_topic_labels,
)

from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.anchors import get_anchors
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.provenance.model import provenance_node_properties


class WriterError(RuntimeError):
    """Raised when ftmg emits a query this adapter cannot tenant-scope."""


# ftmg's generated node-key patterns -> tenant-scoped replacements. If a future
# ftmg release changes these, `_tenantize_query` fails loudly rather than
# silently writing tenant-leaky data.
_KEY_REWRITES: dict[str, str] = {
    "{id: props.id}": "{id: props.id, tenant_id: props.tenant_id}",
    "{id: item.source_id}": "{id: item.source_id, tenant_id: item.tenant_id}",
    "{id: item.target_id}": "{id: item.target_id, tenant_id: item.tenant_id}",
    "{id: item.id}": "{id: item.id, tenant_id: item.tenant_id}",
}


def _tenantize_query(query: str) -> str:
    """Rewrite ftmg's node-key matches to be tenant-scoped, or raise."""
    rewritten = query
    for old, new in _KEY_REWRITES.items():
        rewritten = rewritten.replace(old, new)
    if "tenant_id" not in rewritten:
        raise WriterError(f"ftmg query could not be tenant-scoped:\n{query}")
    return rewritten


def _inject_tenant(
    params: dict[str, Any],
    tenant_id: str,
    node_props_by_id: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Stamp ``tenant_id`` (and, for node params, anchors + provenance) onto params."""
    stamped = dict(params)
    stamped["tenant_id"] = tenant_id
    props = stamped.get("props")
    if isinstance(props, dict):
        nested: dict[str, Any] = dict(props)
        nested["tenant_id"] = tenant_id
        stamped["props"] = nested
    elif node_props_by_id is not None:
        # Flat node params: project the entity's anchors + provenance onto the node.
        extra = node_props_by_id.get(str(stamped.get("id")))
        if extra:
            stamped.update(extra)
    return stamped


def _tenantize(
    batch: Any, tenant_id: str, node_props_by_id: dict[str, dict[str, str]] | None = None
) -> Any:
    """Return a copy of an ftmg ``QueryBatch`` that is tenant-scoped (+ node props)."""
    return QueryBatch(
        query=_tenantize_query(batch.query),
        params=_inject_tenant(batch.params, tenant_id, node_props_by_id),
    )


def _ftmg_config(client: Neo4jClient) -> Any:
    """Build the ftmg :class:`Configuration` (db creds drive label/transform logic)."""
    return Configuration(
        path=Path("."),  # unused: we drive generation directly, not load_entities()
        db=DatabaseConfig(url=client.uri, username=client.user, password=client.password),
    )


def write_entities(client: Neo4jClient, entities: Iterable[FtmEntity], *, tenant_id: str) -> None:
    """Write FtM ``entities`` into Neo4j for ``tenant_id`` (nodes, then edges/links).

    Mirrors ftmg's two-pass load — nodes first so relationships can MATCH them —
    but every node and edge carries ``tenant_id`` and every key match is
    tenant-scoped, so tenants can never collide on a shared FtM id.
    """
    if not tenant_id:
        raise WriterError("tenant_id is required")
    materialized = list(entities)
    config = _ftmg_config(client)
    node_props_by_id = {
        entity.id: extra
        for entity in materialized
        if entity.id is not None
        and (extra := {**get_anchors(entity), **provenance_node_properties(entity)})
    }

    # Pass 1 — entity nodes (skip edge schemata; they become relationships).
    with client.session() as session:
        batcher = QueryBatcher(config, session)
        for entity in materialized:
            if entity.schema.edge:
                continue
            for batch in generate_node_entity(config, entity):
                batcher.add(_tenantize(batch, tenant_id, node_props_by_id))
        batcher.flush()

    # Pass 2 — relationships: edge entities, property links, and topic labels.
    with client.session() as session:
        batcher = QueryBatcher(config, session)
        for entity in materialized:
            if entity.schema.edge:
                generators = (generate_edge_entity(config, entity),)
            else:
                generators = (
                    generate_entity_links(config, entity),
                    generate_topic_labels(config, entity),
                )
            for generator in generators:
                for batch in generator:
                    batcher.add(_tenantize(batch, tenant_id))
        batcher.flush()
