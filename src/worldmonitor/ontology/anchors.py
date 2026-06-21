"""Canonical reference anchors on entities (Wikidata / GeoNames / LEI / OpenCorporates).

Anchors are the canonical identifiers entities resolve to (CLAUDE.md invariant:
*resolve to canonical IDs*). They are stored as **flat scalar keys** in the FtM
entity context (FtM's ``merge_context`` hashes context values, so nested dicts are
unmergeable) — they survive serialization through the ER queue and are projected
onto graph node properties by the writer, where the per-tenant uniqueness
constraints enforce them.
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import FtmEntity

# Canonical identifier fields (the node properties PR1's constraints enforce).
CANONICAL_ID_FIELDS = ("wikidata_id", "geonames_id", "lei", "opencorporates_id")

_CONTEXT_PREFIX = "wm_anchor_"


def set_anchor(entity: FtmEntity, field: str, value: str) -> None:
    """Attach a canonical anchor (e.g. ``wikidata_id`` -> ``Q1065``) to an entity."""
    if field not in CANONICAL_ID_FIELDS:
        raise ValueError(f"unknown anchor field: {field!r}")
    # FtM context values are lists; a single-element list also survives merge_context.
    entity.context[f"{_CONTEXT_PREFIX}{field}"] = [value]


def get_anchors(entity: FtmEntity) -> dict[str, str]:
    """Return the canonical anchors set on an entity (empty if none)."""
    anchors: dict[str, str] = {}
    for field in CANONICAL_ID_FIELDS:
        value = entity.context.get(f"{_CONTEXT_PREFIX}{field}")
        # FtM merge wraps context values in lists; accept scalar or list.
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, str) and value:
            anchors[field] = value
    return anchors
