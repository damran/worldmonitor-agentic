"""followthemoney-graph (ftmg) adapter — writes FtM entities into Neo4j.

ftmg owns the FtM -> property-graph transformation (schema -> labels, edges,
property links, topic labels), and is single-tenant by design. This adapter wraps
ftmg's query generators and its :class:`QueryBatcher` to project provenance
(``prov_*``) + the canonical anchors onto every node AND every relationship —
upholding the non-negotiable "provenance on every node and edge" invariant (G1).
ftmg's native ``{id}`` MERGE/MATCH key is used directly (the platform is
single-tenant, D1 / ADR 0042).

ftmg ships no type stubs, so it is imported only here; the boundary's ``Unknown``
types are relaxed for this module alone while the public API stays fully typed.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from followthemoney import registry
from ftmg.config import Configuration, DatabaseConfig
from ftmg.transform import (
    QueryBatch,
    QueryBatcher,
    generate_edge_entity,
    generate_entity_links,
    generate_node_entity,
    generate_topic_labels,
)
from sqlalchemy.orm import Session

from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.anchors import get_anchors
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.provenance.model import provenance_node_properties
from worldmonitor.resolution.canonical import resolve_durable


def _inject_props(
    params: dict[str, Any],
    node_props_by_id: dict[str, dict[str, str]] | None = None,
    edge_props: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Project the provenance the batch kind needs onto its params (G1).

    Node params (Pass 1) get their anchors + provenance via ``node_props_by_id``.
    Relationship params (Pass 2) carry a nested ``props`` dict that ftmg ``SET``s
    onto the edge (``SET r = item.props``); ``edge_props`` — the asserting entity's
    provenance — is merged there so every relationship is traceable to the
    assertion that created it. Topic-label batches have no ``props`` and are left
    untouched (the labelled node already carries its own provenance).
    """
    stamped = dict(params)
    props = stamped.get("props")
    if isinstance(props, dict):
        # Relationship batch: ftmg sets `r = item.props`, so the asserting entity's
        # provenance must live inside the nested props.
        nested: dict[str, Any] = dict(props)
        if edge_props:
            nested.update(edge_props)
        stamped["props"] = nested
    elif node_props_by_id is not None:
        # Flat node params: project the entity's anchors + provenance onto the node.
        extra = node_props_by_id.get(str(stamped.get("id")))
        if extra:
            stamped.update(extra)
    return stamped


def _with_props(
    batch: Any,
    node_props_by_id: dict[str, dict[str, str]] | None = None,
    edge_props: dict[str, str] | None = None,
) -> Any:
    """Return a copy of an ftmg ``QueryBatch`` with node/edge provenance projected (G1)."""
    return QueryBatch(
        query=batch.query,
        params=_inject_props(batch.params, node_props_by_id, edge_props),
    )


# ftmg's `generate_entity_links` keys each endpoint by `registry.entity.node_id`, which
# PREFIXES the FtM id (e.g. "entity:abc"). But nodes are written with the RAW id
# (`generate_node_entity` -> MERGE {id: props.id}), so the link MATCH `{id: "entity:abc"}`
# misses the raw node and the relationship is silently dropped (review H3). This is the
# prefix to strip so they realign — derived from ftmg, not hardcoded, so a future change
# to ftmg's id scheme surfaces in the regression test rather than silently re-breaking.
_ENTITY_LINK_PREFIX = (registry.entity.node_id_safe("x") or "entity:x").removesuffix("x")


def _align_entity_link_ids(batch: Any) -> Any:
    """Strip the ``entity:`` prefix from an entity-link batch's endpoint ids (H3 fix).

    Realigns ``generate_entity_links``' ``entity:``-prefixed ``source_id`` / ``target_id``
    with the RAW ids the nodes are written under, so the link materializes instead of
    silently MATCH-missing. **Scoped to the entity-link path only:** edge-schema entities
    and topic labels already key on raw ids (untouched), and abstract-``Thing``-range
    links (G3) are skipped inside ftmg before any batch exists — so this never sees them.
    H3 is fixed; G3 stays deferred and unchanged.
    """
    params = dict(batch.params)
    for key in ("source_id", "target_id"):
        value = params.get(key)
        if isinstance(value, str):
            params[key] = value.removeprefix(_ENTITY_LINK_PREFIX)
    return QueryBatch(query=batch.query, params=params)


def _ftmg_config(client: Neo4jClient) -> Any:
    """Build the ftmg :class:`Configuration` (db creds drive label/transform logic)."""
    return Configuration(
        path=Path("."),  # unused: we drive generation directly, not load_entities()
        db=DatabaseConfig(url=client.uri, username=client.user, password=client.password),
    )


def write_entities(client: Neo4jClient, entities: Iterable[FtmEntity]) -> None:
    """Write FtM ``entities`` into Neo4j (nodes, then edges/links).

    Mirrors ftmg's two-pass load — nodes first so relationships can MATCH them,
    keyed by ftmg's native FtM/canonical ``{id}`` (single-tenant, D1 / ADR 0042).
    Provenance (``prov_*``) is projected onto every node *and* every relationship:
    an edge carries the provenance of the assertion that created it — the edge
    entity itself (Ownership/Sanction/…) or, for an entity-reference link, the
    property-holder — **not** either endpoint's. This upholds "provenance on every
    node *and edge*" (the GDPR/audit-log invariant, G1).
    """
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
                batcher.add(_with_props(batch, node_props_by_id))
        batcher.flush()

    # Pass 2 — relationships: edge entities, property links, and topic labels.
    # An edge's provenance is the provenance of the assertion that CREATED it —
    # the edge entity itself (Ownership/Sanction/…) or, for an entity-reference
    # link, the property-holder — never an endpoint node's. In both cases that
    # asserting entity is the current `entity`, so its provenance is what we stamp
    # onto the relationship. (Topic-label batches carry no `props`, so they are
    # untouched: the labelled node already carries its own provenance.)
    with client.session() as session:
        batcher = QueryBatcher(config, session)
        for entity in materialized:
            edge_prov = provenance_node_properties(entity)
            if entity.schema.edge:
                for batch in generate_edge_entity(config, entity):
                    batcher.add(_with_props(batch, edge_props=edge_prov))
            else:
                # Entity-typed property links: realign the `entity:`-prefixed endpoint ids
                # to the raw node ids so the link materializes (H3). Edge-schema and topic
                # batches already key on raw ids, so only this generator is realigned.
                for batch in generate_entity_links(config, entity):
                    batcher.add(_with_props(_align_entity_link_ids(batch), edge_props=edge_prov))
                for batch in generate_topic_labels(config, entity):
                    batcher.add(_with_props(batch, edge_props=edge_prov))
        batcher.flush()


def resolve_node_id(ledger: Session, entity_id: str) -> str:
    """Resolve a (possibly superseded) ``entity_id`` to the surviving DURABLE node id.

    Gate B-front / ADR 0044 alias-on-read: a node is written under its durable canonical id (the
    native ``{id}`` MERGE key, single-tenant, ADR 0042), and the ``canonical_id_ledger`` records
    every superseded/prior id (a collapsed merge member, a prior ``wmc-`` fingerprint, or a
    split-ejected id) as a traceable alias. A lookup by such a superseded id must land on the
    surviving node, not miss — this maps an alias to its survivor via the ledger. An id with no
    alias row (its own durable id, or an unknown id) resolves to itself, so the call is always safe.
    """
    return resolve_durable(ledger, entity_id) or entity_id


def get_entity_by_alias(
    client: Neo4jClient, ledger: Session, *, entity_id: str
) -> dict[str, Any] | None:
    """Read a node by ``entity_id``, honoring ``canonical_alias`` on read (ADR 0044).

    Resolves ``entity_id`` through the ledger (:func:`resolve_node_id`) to the surviving durable
    id, then reads that node's properties — so a lookup by a superseded id (a merged-away member,
    a stale ``wmc-`` fingerprint, a split-ejected id) returns the SURVIVING node rather than a
    dangling miss. Returns ``None`` if no node exists for the resolved id.
    """
    durable_id = resolve_node_id(ledger, entity_id)
    rows = client.execute_read(
        "MATCH (n:Entity {id: $entity_id}) RETURN properties(n) AS props",
        entity_id=durable_id,
    )
    return rows[0]["props"] if rows else None
