"""Provenance — the non-negotiable record of where every fact came from.

Every FtM object a connector emits is stamped with provenance (``source_id``,
``retrieved_at``, ``reliability``, and a pointer to the raw record in the landing
zone). This doubles as the GDPR/audit log. Provenance is stored as **flat scalar
keys** in the entity's FtM context (FtM's ``merge_context`` hashes context values,
so nested dicts are unmergeable) — it travels with the entity through
serialization and is projected onto graph nodes by the writer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from worldmonitor.ontology.ftm import FtmEntity

# Context-key prefix for the flat provenance fields, and the node-property prefix.
_CONTEXT_PREFIX = "wm_prov_"
PROVENANCE_NODE_PREFIX = "prov_"
_FIELDS = ("source_id", "retrieved_at", "reliability", "source_record")


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a fact came from. Required on every mapped entity."""

    source_id: str
    """Connector-instance / dataset identifier the fact was collected by."""
    retrieved_at: str
    """ISO-8601 timestamp of collection."""
    reliability: str
    """Source reliability grade (e.g. NATO admiralty ``A``..``F``)."""
    source_record: str
    """Pointer to the raw record in the landing zone (e.g. an S3 URI/key)."""

    def as_dict(self) -> dict[str, str]:
        """Return the provenance as a plain serializable mapping."""
        return asdict(self)


def _context_scalar(entity: FtmEntity, key: str) -> str | None:
    """Read a context value as a scalar (merge wraps context values in lists)."""
    value = entity.context.get(key)
    if isinstance(value, list):
        return str(value[0]) if value else None
    return str(value) if isinstance(value, str) else None


def stamp(entity: FtmEntity, provenance: Provenance) -> FtmEntity:
    """Attach ``provenance`` to ``entity`` (flat context keys) and return it."""
    for field in _FIELDS:
        # FtM context values are lists; a single-element list also survives merge_context.
        entity.context[f"{_CONTEXT_PREFIX}{field}"] = [getattr(provenance, field)]
    return entity


def get_provenance(entity: FtmEntity) -> Provenance | None:
    """Read provenance back off an entity, or ``None`` if it was never stamped."""
    values = {field: _context_scalar(entity, f"{_CONTEXT_PREFIX}{field}") for field in _FIELDS}
    if values["source_id"] is None:
        return None
    return Provenance(
        source_id=values["source_id"],
        retrieved_at=values["retrieved_at"] or "",
        reliability=values["reliability"] or "",
        source_record=values["source_record"] or "",
    )


def provenance_node_properties(entity: FtmEntity) -> dict[str, str]:
    """Flatten an entity's provenance into ``prov_*`` node properties (empty if none)."""
    provenance = get_provenance(entity)
    if provenance is None:
        return {}
    return {f"{PROVENANCE_NODE_PREFIX}{key}": value for key, value in provenance.as_dict().items()}
