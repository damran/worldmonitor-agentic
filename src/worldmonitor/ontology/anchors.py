"""Canonical reference anchors on entities (Wikidata / GeoNames / LEI / OpenCorporates).

Anchors are the canonical identifiers entities resolve to (CLAUDE.md invariant:
*resolve to canonical IDs*). They live in the FtM entity context under
:data:`ANCHOR_KEY` (so they survive serialization through the ER queue) and are
projected onto graph node properties by the writer — where the per-tenant
uniqueness constraints enforce them.
"""

from __future__ import annotations

from typing import Any

from worldmonitor.ontology.ftm import FtmEntity

# Key under which anchors live in an entity's FtM context.
ANCHOR_KEY = "wm_anchors"

# Canonical identifier fields (the node properties PR1's constraints enforce).
CANONICAL_ID_FIELDS = ("wikidata_id", "geonames_id", "lei", "opencorporates_id")


def set_anchor(entity: FtmEntity, field: str, value: str) -> None:
    """Attach a canonical anchor (e.g. ``wikidata_id`` -> ``Q1065``) to an entity."""
    if field not in CANONICAL_ID_FIELDS:
        raise ValueError(f"unknown anchor field: {field!r}")
    anchors: dict[str, Any] = entity.context.setdefault(ANCHOR_KEY, {})
    anchors[field] = value


def get_anchors(entity: FtmEntity) -> dict[str, str]:
    """Return the canonical anchors set on an entity (empty if none)."""
    raw = entity.context.get(ANCHOR_KEY)
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if k in CANONICAL_ID_FIELDS}
