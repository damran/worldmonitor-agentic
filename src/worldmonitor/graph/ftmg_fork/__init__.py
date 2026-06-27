"""Thin ftmg override package — abstract ``Thing``-range edge materialization (Gate D).

A wrapper over upstream ftmg 0.1.0 (CLAUDE.md: adopt / wrap — never fork as foundation). It
overrides the two abstract-range drop sites — :func:`generate_entity_links` (drop site 1,
the ``Sanction.entity`` crux) and :func:`generate_edge_entity` (drop site 2, ``UnknownLink``)
— and :func:`generate_node_entity`, whose SET clause is made additive (``SET n += props``) so a
thinner re-emit cannot clobber a node's prior anchors / ``prov_*`` (ADR 0060, M-1). Everything
else (:class:`QueryBatch`, :class:`QueryBatcher`, :func:`generate_topic_labels`,
:func:`get_schema_labels`, ``ENTITY_LABEL``) is re-exported straight from upstream so the
boundary stays in one place. See ``ftmg_fork.transform``,
``docs/reviews/GATE_D_ABSTRACT_EDGES_SPEC.md`` and
``docs/decisions/0060-node-provenance-integrity.md``.
"""

from __future__ import annotations

from worldmonitor.graph.ftmg_fork.transform import (
    ENTITY_LABEL,
    QueryBatch,
    QueryBatcher,
    generate_edge_entity,
    generate_entity_links,
    generate_node_entity,
    generate_topic_labels,
    get_schema_labels,
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
