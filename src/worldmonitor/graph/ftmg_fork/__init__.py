"""Thin ftmg override package — abstract ``Thing``-range edge materialization (Gate D).

A wrapper over upstream ftmg 0.1.0 (CLAUDE.md: adopt / wrap — never fork as foundation). It
overrides ONLY the two abstract-range drop sites — :func:`generate_entity_links` (drop site 1,
the ``Sanction.entity`` crux) and :func:`generate_edge_entity` (drop site 2, ``UnknownLink``)
— and re-exports everything else (:class:`QueryBatch`, :class:`QueryBatcher`,
:func:`generate_node_entity`, :func:`generate_topic_labels`, :func:`get_schema_labels`,
``ENTITY_LABEL``) straight from upstream so the boundary stays in one place. See
``ftmg_fork.transform`` and ``docs/reviews/GATE_D_ABSTRACT_EDGES_SPEC.md``.
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
