"""Thin ftmg override — abstract ``Thing``-range entity-link materialization (Gate D).

ftmg 0.1.0 keys every entity-link's target lookup on the **range SCHEMA**
(``config.nodes.schemata.get(prop.range.name)`` — ``ftmg/transform.py:227-229`` for
:func:`generate_entity_links`, ``317-322`` for :func:`generate_edge_entity`). The abstract
base ``Thing`` has NO ``config.nodes.schemata`` entry, because ``ftmg/config.py:67-70``
registers a node schema config only for ``not schema.edge and not schema.abstract`` — and
``config.py:73`` *raises* if you try to register an abstract schema. So EVERY entity-link
whose property range is the abstract ``Thing`` is silently dropped at the range-schema
lookup (the headline OFAC failure: ``Sanction.entity`` → ``Thing``; also ``UnknownLink``).

This module is a **THIN OVERRIDE** (CLAUDE.md: adopt / wrap — never fork as foundation). It
re-implements ONLY the two abstract-range drop sites and imports everything else from
upstream ftmg 0.1.0 unchanged (:class:`QueryBatch`, :class:`QueryBatcher`,
:func:`generate_node_entity`, :func:`generate_topic_labels`, :func:`get_schema_labels`,
``ENTITY_LABEL``). The fix KEEPS the upstream ``prop.type == registry.entity`` filter (the
correct type-level test at upstream line 220) and only changes the post-filter range-schema
drop: it re-keys the target lookup onto ``prop.type == registry.entity`` with the
``ENTITY_LABEL = "Entity"`` MATCH-label fallback when the range is the abstract ``Thing``
(range schema absent from ``config.nodes.schemata``). Every node ftmg writes already carries
the ``:Entity`` base label (``generate_node_entity``), so ``:Entity`` is a sound MATCH/MERGE
target for an abstract range.

``:Ghost`` (spec §6 / ADR 0046): a ``Sanction → target`` whose target id was NEVER ingested
as a concrete entity would MATCH-miss and silently drop the edge again. The override instead
MERGEs the target node and tags it ``:Ghost`` ON CREATE — a typed traversal-only endpoint
that preserves the assertion's edge while being structurally inert to resolution (no anchor
property, minted post-clustering so never a cluster member / anchor source / merge survivor).

Person-NEUTRAL: edge projection runs in ``writer.write_entities`` strictly AFTER
clustering / merge / the guard (``pipeline.resolve_pending``). No ``DEFAULT_MERGE_THRESHOLD``
/ Splink / ``pick_anchor`` / ``cluster_and_merge`` change. A materialized edge / ghost MUST
never lower a merge bar (the corroboration-exclusion fence, ADR 0046 Decision 4).
"""

# ftmg ships no type stubs; relax the boundary's Unknown types for this module alone,
# exactly as the writer does (the public API stays fully typed).
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
from __future__ import annotations

from collections.abc import Generator

from followthemoney import registry
from followthemoney.entity import ValueEntity

# Re-export the upstream symbols UNCHANGED — the override owns only the two drop sites; the
# rest of the ftmg boundary stays in one place (writer.py + this module). Re-implementing any
# of these would be a wholesale fork (DENY D-FORK).
from ftmg.config import Configuration
from ftmg.transform import (
    ENTITY_LABEL,
    QueryBatch,
    QueryBatcher,
    generate_topic_labels,
    get_schema_labels,
)
from ftmg.transform import (
    QueryParams as QueryParams,
)

__all__ = [
    "ENTITY_LABEL",
    "QueryBatch",
    "QueryBatcher",
    "generate_edge_entity",
    "generate_entity_links",
    "generate_node_entity",
    "generate_topic_labels",
    "get_schema_labels",
]


def generate_node_entity(
    config: Configuration,
    proxy: ValueEntity,
) -> Generator[QueryBatch, None, None]:
    """Override of ``ftmg.transform.generate_node_entity`` — additive re-emit (ADR 0060, M-1).

    Replicates upstream ``ftmg/transform.py:91-133`` VERBATIM — the property build, the
    ``get_schema_labels`` multi-label derivation, the ``{id: props.id}`` MERGE key and the
    ``UNWIND $batch AS props`` shape — replacing ONLY the SET clause: upstream's full-replace
    ``SET n = props`` becomes additive ``SET n += props``.

    Upstream ``SET n = props`` is a full node replace: a *thinner* re-emit of the same ``{id}``
    (a sparser source variant, or a B-1 re-resolve) silently ERASES the node's prior anchors /
    ``prov_*`` / ``prov_witnesses`` — a G1 (provenance-on-every-node) + anchor-stability
    regression on any re-ingest. ``SET n += props`` ACCUMULATES, so prior anchors / provenance
    are never lost (a value present earlier but absent in a later emit persists — correct for an
    append-only resolved graph; a genuine retraction is the sign-off-gated ``delete_source`` path,
    ADR 0045/0049, not a silent re-emit). The label is derived from ``get_schema_labels`` exactly
    as upstream (NOT hardcoded), so every node keeps its ``:Entity`` base label + schema labels.
    """
    sconfig = config.nodes.schemata.get(proxy.schema.name)
    if sconfig is None or sconfig.ignore:
        return
    assert proxy.id is not None

    # Build properties dict (upstream 110-114, unchanged).
    properties: QueryParams = {
        "id": proxy.id,
        "caption": proxy.caption,
        "datasets": list(proxy.datasets),
    }

    # Process properties (upstream 117-123, unchanged).
    for prop_name in sconfig.properties:
        prop = proxy.schema.get(prop_name)
        assert prop is not None

        values = proxy.get(prop)
        if len(values):
            properties[prop.name] = values

    # Create node with all labels, using MERGE to make it idempotent (upstream 126-127,
    # unchanged). THE ONLY CHANGE vs upstream: `SET n += props` (additive) replaces the
    # full-replace `SET n = props`, so a thinner re-emit cannot clobber prior anchors / prov_*.
    labels = get_schema_labels(config, proxy.schema)
    label = ":".join(labels)
    create_query = f"""
    UNWIND $batch AS props
    MERGE (n:{label} {{id: props.id}})
    SET n += props
    """
    yield QueryBatch(query=create_query, params=properties)


def _node_match_label(config: Configuration, range_schema_name: str) -> str:
    """The label to MATCH/MERGE an entity-link endpoint on (the re-key, NOT a range gate).

    Upstream keys the lookup on the RANGE SCHEMA — ``config.nodes.schemata.get(range_name)`` —
    and DROPS when that is ``None`` (the abstract-``Thing`` bug: ``config.py:67-70`` never
    registers an abstract schema, and ``config.py:73`` *raises* if you try). The override keeps
    the upstream ``prop.type == registry.entity`` type-level filter and only changes the
    post-filter drop: a concrete range that IS registered keeps its own schema label (so
    ``Person.addressEntity → Address`` still contracts on ``:Address`` — the H3 frozen line);
    an abstract / absent range falls back to ``ENTITY_LABEL = "Entity"``, the base label every
    node carries (``generate_node_entity``). This NEVER re-introduces a range-schema gate
    (D-RANGEKEY): an absent config is a fallback, never a drop.
    """
    srconfig = config.nodes.schemata.get(range_schema_name)
    if srconfig is None or srconfig.ignore:
        return ENTITY_LABEL
    return srconfig.label


def generate_entity_links(
    config: Configuration,
    proxy: ValueEntity,
) -> Generator[QueryBatch, None, None]:
    """Override of ``ftmg.transform.generate_entity_links`` — drop site 1 (line 198).

    Replicates upstream lines 198-250, replacing ONLY the 227-229 range-schema drop with the
    :func:`_node_match_label` fallback. The upstream ``prop.type == registry.entity`` filter
    (line 220) is kept verbatim. Endpoint ids stay ``entity:``-prefixed exactly as upstream
    (``registry.entity.node_id`` / ``prop.type.node_id``), so ``writer._align_entity_link_ids``
    realigns them onto the raw node ids unchanged.

    The target is MERGEd (not MATCHed): a target that was ingested as a concrete entity in
    Pass 1 already carries the MERGE label (every node carries ``:Entity``), so the MERGE
    matches it and ``ON CREATE`` does NOT fire — no ghost, no duplicate. A target id that was
    NEVER ingested has no matching node, so ``ON CREATE`` fires and tags the freshly-MERGEd
    node ``:Ghost`` with NO anchor property — a structurally-inert traversal-only endpoint
    (spec §6). The ``(s)-[r:REL]->(t)`` MERGE is keyed on (source durable id, target durable
    id, rel-type), so re-projection is idempotent (ADR 0036).
    """
    entity_id = registry.entity.node_id_safe(proxy.id)
    if entity_id is None:
        return

    sconfig = config.nodes.schemata.get(proxy.schema.name)
    if sconfig is None or sconfig.ignore:
        return

    for prop in proxy.schema.sorted_properties:
        # KEEP the upstream type-level filter verbatim (transform.py:220) — the rule keys on
        # prop.type == registry.entity, NOT the range schema (D-RANGEKEY).
        if prop.type != registry.entity or prop.range is None:
            continue

        pconfig = config.edges.properties.get(prop.qname)
        if pconfig is None or pconfig.ignore:
            continue

        # THE RE-KEY: resolve the target label off prop.type == registry.entity with the
        # ENTITY_LABEL fallback for an abstract / absent range (replaces upstream 227-229).
        target_label = _node_match_label(config, prop.range.name)

        for value in proxy.get(prop):
            target_id = prop.type.node_id(value)
            if target_id is None:
                continue

            # MERGE the target so a never-ingested id is preserved as a :Ghost endpoint
            # rather than MATCH-missed and dropped. ON CREATE tags :Ghost (no anchor prop) —
            # an already-ingested concrete node matches and is left untouched (no ghost).
            query = f"""
            UNWIND $batch AS item
            MATCH (s:{sconfig.label} {{id: item.source_id}})
            MERGE (t:{target_label} {{id: item.target_id}})
            ON CREATE SET t:Ghost, t.id = item.target_id
            MERGE (s)-[r:{pconfig.label}]->(t)
            ON CREATE SET r = item.props
            """
            yield QueryBatch(
                query=query,
                params={
                    "source_id": entity_id,
                    "target_id": target_id,
                    "props": {},
                },
            )


def generate_edge_entity(
    config: Configuration,
    proxy: ValueEntity,
) -> Generator[QueryBatch, None, None]:
    """Override of ``ftmg.transform.generate_edge_entity`` — drop site 2 (line 291).

    Replicates upstream lines 291-371, replacing ONLY the 317-322 source/target range-schema
    drops with the :func:`_node_match_label` fallback so an edge schema whose ``source_prop`` /
    ``target_prop`` ranges over the abstract ``Thing`` (``UnknownLink.subject/object``)
    materializes on the ``:Entity`` base label. A concrete-range edge schema
    (``Ownership.owner → LegalEntity``, ``Directorship.director → LegalEntity``) keeps its own
    range schema label, so concrete-range contraction is unbroken (D-FROZEN).

    The endpoint MATCH and the idempotency-by-``id`` ``OPTIONAL MATCH … WHERE existing.id =
    item.props.id … CREATE`` form (upstream 347-356) are preserved UNCHANGED — edge schemas
    carry the FtM edge id, and idempotency is by that id. Endpoint ids are the raw FtM ids
    (``proxy.get(source_prop)``), so no ``entity:`` realignment is needed (matching upstream).
    """
    sconfig = config.edges.schemata.get(proxy.schema.name)
    if sconfig is None or sconfig.ignore:
        return

    source_prop = proxy.schema.source_prop
    target_prop = proxy.schema.target_prop

    if source_prop is None or source_prop.range is None:
        return
    if target_prop is None or target_prop.range is None:
        return

    # THE RE-KEY: resolve each endpoint label off the prop range with the ENTITY_LABEL
    # fallback for an abstract / absent range (replaces upstream 317-322).
    source_label = _node_match_label(config, source_prop.range.name)
    target_label = _node_match_label(config, target_prop.range.name)

    assert proxy.id is not None
    sources = proxy.get(source_prop)
    targets = proxy.get(target_prop)

    # Build edge properties (upstream 329-343, unchanged).
    props: dict[str, str | list[str]] = {
        "id": proxy.id,
        "datasets": list(proxy.datasets),
    }
    for prop_name in sconfig.properties:
        prop = proxy.schema.get(prop_name)
        if not prop or prop in (source_prop, target_prop):
            continue
        values = proxy.get(prop)
        if len(values):
            props[prop.name] = values

    # Idempotency-by-id CREATE form (upstream 347-356) preserved verbatim in shape.
    query = f"""
    UNWIND $batch AS item
    MATCH (s:{source_label} {{id: item.source_id}})
    MATCH (t:{target_label} {{id: item.target_id}})
    OPTIONAL MATCH (s)-[existing:{sconfig.label}]->(t)
    WHERE existing.id = item.props.id
    WITH s, t, item, existing
    WHERE existing IS NULL
    CREATE (s)-[r:{sconfig.label}]->(t)
    SET r = item.props
    """

    for source_id in sources:
        for target_id in targets:
            if source_id == target_id:
                continue
            yield QueryBatch(
                query=query,
                params={
                    "source_id": source_id,
                    "target_id": target_id,
                    "props": props,
                },
            )
