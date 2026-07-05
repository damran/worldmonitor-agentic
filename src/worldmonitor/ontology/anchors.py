"""Canonical reference anchors on entities (Wikidata / GeoNames / LEI / OpenCorporates).

Anchors are the canonical identifiers entities resolve to (CLAUDE.md invariant:
*resolve to canonical IDs*). They are stored as **flat scalar keys** in the FtM
entity context (FtM's ``merge_context`` hashes context values, so nested dicts are
unmergeable) — they survive serialization through the ER queue and are projected
onto graph node properties by the writer, where the uniqueness
constraints enforce them.

Each ``CANONICAL_ID_FIELDS`` anchor is **single-valued and authoritative**: a real
entity has at most one Wikidata Q-number, one LEI, one GeoNames id, one
OpenCorporates id. When a cluster's ``merge_context`` unions two members carrying
DISTINCT values for the same field, the result (``['Q1', 'Q2']``) is, by definition,
two different real-world entities fused into one node — a catastrophic merge
(Gate B-5 / ADR 0040, Finding 1). :func:`get_anchors` therefore **omits** a
conflicting field (rather than silently projecting an arbitrary ``[0]`` winner onto
the node), and :func:`get_anchor_conflicts` surfaces the conflict so the
catastrophic-merge guard can park the cluster for human review.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from worldmonitor.ontology.ftm import FtmEntity

logger = logging.getLogger(__name__)

# Canonical identifier fields (the node properties PR1's constraints enforce).
CANONICAL_ID_FIELDS = ("wikidata_id", "geonames_id", "lei", "opencorporates_id")

_CONTEXT_PREFIX = "wm_anchor_"


def set_anchor(entity: FtmEntity, field: str, value: str) -> None:
    """Attach a canonical anchor (e.g. ``wikidata_id`` -> ``Q1065``) to an entity."""
    if field not in CANONICAL_ID_FIELDS:
        raise ValueError(f"unknown anchor field: {field!r}")
    # FtM context values are lists; a single-element list also survives merge_context.
    entity.context[f"{_CONTEXT_PREFIX}{field}"] = [value]


def _anchor_values(entity: FtmEntity, field: str) -> list[str]:
    """Distinct, non-empty string values held for ``field`` in the entity context.

    Accepts either a scalar (a freshly :func:`set_anchor`-ed value) or a list (the
    shape FtM's ``merge_context`` produces when it unions two members' contexts). The
    order is preserved so ``[0]`` is the FIRST-seen value for the clean single-value path.
    """
    raw = entity.context.get(f"{_CONTEXT_PREFIX}{field}")
    if raw is None:
        return []
    candidates = raw if isinstance(raw, list) else [raw]
    values: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate and candidate not in values:
            values.append(candidate)
    return values


def get_anchor_conflicts(entity: FtmEntity) -> dict[str, list[str]]:
    """Return canonical-anchor fields that carry MORE THAN ONE distinct value.

    Each canonical anchor is single-valued and authoritative, so ``> 1`` distinct
    non-empty value for one field means the entity's context fuses ``> 1`` real-world
    entity (Gate B-5 / ADR 0040, Finding 1). Maps each conflicting field to its sorted
    distinct values (a deterministic, human-readable lead for the review-queue park);
    the dict is empty for a conflict-free entity.
    """
    conflicts: dict[str, list[str]] = {}
    for field in CANONICAL_ID_FIELDS:
        values = _anchor_values(entity, field)
        if len(values) > 1:
            conflicts[field] = sorted(values)
    return conflicts


def anchor_conflicts_across(entities: Iterable[FtmEntity]) -> dict[str, list[str]]:
    """Conflicting canonical anchors over a COLLECTION of (source) entities.

    Unions each ``CANONICAL_ID_FIELDS`` field's values across all ``entities`` and returns the
    fields whose union holds ``> 1`` distinct non-empty value, mapped to the sorted distinct
    values. This is the catastrophic-merge guard's view (Gate B-5 / ADR 0040, fork (C)): it is
    computed over a cluster's SOURCE members — NOT the merged ``cluster.entity``, whose
    ``merge_context`` already unions the conflicting values and which :func:`get_anchors` would
    mask (that masking is Finding 1). It therefore also catches the TRANSITIVE conflict that
    pairwise scoring cannot see (A~M~Z assembled from clean bridges). Empty if conflict-free.
    """
    per_field: dict[str, list[str]] = {field: [] for field in CANONICAL_ID_FIELDS}
    for entity in entities:
        for field in CANONICAL_ID_FIELDS:
            for value in _anchor_values(entity, field):
                if value not in per_field[field]:
                    per_field[field].append(value)
    return {field: sorted(values) for field, values in per_field.items() if len(values) > 1}


def set_anchor_claims(entity: FtmEntity, field: str, values: Iterable[str]) -> None:
    """Set the RAW multi-value anchor context for a field (fold reinstatement, Gate P1).

    Mirrors the shape FtM's ``merge_context`` produces when it unions two members' anchor
    contexts (a sorted, deduped list of distinct non-empty string values) so
    :func:`get_anchors` applies the IDENTICAL omit-on-conflict rule to a fold-reconstructed
    entity as it does to a live merged entity (:mod:`worldmonitor.resolution.projector`,
    ADR 0106 §2). An empty/all-filtered ``values`` is a no-op (leaves the context key unset,
    same as a field nobody ever claimed).
    """
    if field not in CANONICAL_ID_FIELDS:
        raise ValueError(f"unknown anchor field: {field!r}")
    # ``values`` is typed Iterable[str] (the public contract); the truthy filter drops empty
    # strings the same way _anchor_values' isinstance-and-truthy filter does for its Any-typed
    # (untyped context) input.
    vals = sorted({v for v in values if v})
    if vals:
        entity.context[f"{_CONTEXT_PREFIX}{field}"] = vals


def get_anchors(entity: FtmEntity) -> dict[str, str]:
    """Return the canonical anchors set on an entity (empty if none).

    A field carrying a single value is projected as ``{field: value}`` (``dict[str, str]``
    — the writer contract, ``graph/writer.py``). A field whose context holds ``> 1`` distinct
    value (a fused anchor conflict) is **OMITTED** — never collapsed to an arbitrary ``[0]``
    winner — and logged, so the node carries no anchor for that field rather than a silently
    wrong one (Gate B-5 / ADR 0040, Finding 1). The conflict itself is surfaced for the guard
    via :func:`get_anchor_conflicts`.
    """
    anchors: dict[str, str] = {}
    for field in CANONICAL_ID_FIELDS:
        values = _anchor_values(entity, field)
        if not values:
            continue
        if len(values) > 1:
            logger.warning(
                "anchors: omitting conflicting %s anchor (distinct values: %s) — refusing to "
                "project an arbitrary winner onto the node (Gate B-5 / ADR 0040, Finding 1)",
                field,
                ", ".join(sorted(values)),
            )
            continue
        anchors[field] = values[0]
    return anchors
