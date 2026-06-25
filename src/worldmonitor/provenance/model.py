"""Provenance — the non-negotiable record of where every fact came from.

Every FtM object a connector emits is stamped with provenance (``source_id``,
``retrieved_at``, ``reliability``, and a pointer to the raw record in the landing
zone). This doubles as the GDPR/audit log. Provenance is stored as **flat scalar
keys** in the entity's FtM context (FtM's ``merge_context`` hashes context values,
so nested dicts are unmergeable) — it travels with the entity through
serialization and is projected onto graph nodes by the writer.

Gate C — value-level provenance (ADR 0045) — adds a **multi-source** read on top of
this single-source surface: :func:`witness_map` returns, per FtM property, the SET of
datasets that witnessed any value of that property on a (possibly fused) entity. A
merged entity carries that map as a flat ``wm_prov_witnesses`` JSON-string context key
(stamped by ``resolution.merge`` from the fused ``StatementEntity``); a single-source
entity with no such key falls back to the singleton ``{source_id}`` per witnessed prop.
The single-source ``Provenance``/``stamp``/``get_provenance``/``provenance_node_properties``
surface is **kept** (G1's ``prov_*`` stays — additive).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass

from worldmonitor.ontology.ftm import FtmEntity

logger = logging.getLogger(__name__)

# Context-key prefix for the flat provenance fields, and the node-property prefix.
_CONTEXT_PREFIX = "wm_prov_"
PROVENANCE_NODE_PREFIX = "prov_"
_FIELDS = ("source_id", "retrieved_at", "reliability", "source_record")

# Tier-1 multi-source witness map: a per-property set of witnessing datasets, stored as a flat
# JSON-string context key (FtM context values are lists; a single-element list survives
# ``merge_context`` and ``to_dict`` like the other ``wm_prov_*`` scalars) so the fused lineage
# travels with the entity through serialization / ``rekey_cluster``. The node-property the writer
# projects this onto (Tier-1, ``graph/writer.py``) — alongside (never replacing) ``prov_*``.
WITNESSES_CONTEXT_KEY = f"{_CONTEXT_PREFIX}witnesses"
WITNESSES_NODE_PROPERTY = f"{PROVENANCE_NODE_PREFIX}witnesses"
# The "id" FtM pseudo-property is never a witnessed value (it is the entity id itself, and the
# fused ``StatementEntity`` stamps it with the entity-construction Dataset, not a source dataset),
# so it is excluded from every witness map (spec §3/§4).
_ID_PSEUDO_PROP = "id"


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


def stamp_witness_map(entity: FtmEntity, witnesses: dict[str, set[str]]) -> FtmEntity:
    """Attach the Tier-1 per-property witness map to ``entity`` (flat context key) and return it.

    The map is serialized as a single JSON string under :data:`WITNESSES_CONTEXT_KEY` (sets become
    sorted lists for determinism), wrapped in a one-element list so it survives FtM
    ``merge_context`` and the ``to_dict`` round-trip exactly like the other ``wm_prov_*`` scalars.
    Called by ``resolution.merge`` after fusing a cluster's ``StatementEntity``; :func:`witness_map`
    reads it back. An empty map writes nothing (keeps a value-less entity's context clean).
    """
    if not witnesses:
        return entity
    encoded = {prop: sorted(datasets) for prop, datasets in witnesses.items()}
    entity.context[WITNESSES_CONTEXT_KEY] = [json.dumps(encoded, sort_keys=True)]
    return entity


def _stamped_witness_map(entity: FtmEntity) -> dict[str, set[str]] | None:
    """Read a previously :func:`stamp_witness_map`-ed witness map back, or ``None`` if absent."""
    raw = entity.context.get(WITNESSES_CONTEXT_KEY)
    payload = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(payload, str):
        return None
    try:
        decoded = json.loads(payload)
    except (ValueError, TypeError):
        logger.warning("provenance: ignoring un-parseable witness map on entity %s", entity.id)
        return None
    if not isinstance(decoded, dict):
        return None
    result: dict[str, set[str]] = {}
    for prop, datasets in decoded.items():
        if isinstance(prop, str) and isinstance(datasets, list):
            result[prop] = {str(dataset) for dataset in datasets}
    return result


def witness_map(entity: FtmEntity) -> dict[str, set[str]]:
    """Tier-1 per-property witness sets for a (possibly fused) entity.

    Returns, for each FtM property name that carries at least one value on ``entity``, the SET of
    datasets (= each contributing member's ``Provenance.source_id``) that witnessed ANY value of
    that property. For a FUSED entity this is read from the ``wm_prov_witnesses`` context key
    ``resolution.merge`` stamped from the fused ``StatementEntity``'s per-(prop, value, dataset)
    statements (spec §4/§5) — so all contributing sources are reflected, not just ``source[0]``. A
    singleton / single-source entity (no stamped map) falls back to the singleton ``{source_id}``
    per witnessed property, derived from its single :func:`get_provenance`. The ``id``
    pseudo-property is NOT included.
    """
    witnessed_props = [prop for prop in entity.properties if prop != _ID_PSEUDO_PROP]
    stamped = _stamped_witness_map(entity)
    if stamped is not None:
        # Restrict to props that actually still carry a value on the entity (a defensive
        # intersection in case the value set and the stamped map came from different states).
        return {prop: stamped[prop] for prop in witnessed_props if prop in stamped}
    # Fallback: a single-source entity legitimately has exactly one source, so every property it
    # carries a value for is witnessed by exactly that one dataset (a singleton set).
    provenance = get_provenance(entity)
    if provenance is None:
        return {}
    return {prop: {provenance.source_id} for prop in witnessed_props}


def witness_node_properties(entity: FtmEntity) -> dict[str, str]:
    """Project the Tier-1 witness map onto a single ``prov_witnesses`` node property (else empty).

    Neo4j stores scalars + homogeneous arrays, not maps, so the per-property witness sets are
    encoded as ONE JSON-string property (:data:`WITNESSES_NODE_PROPERTY` -> a ``{prop: [datasets]}``
    object, sets→sorted lists). This lands alongside (never replacing) ``prov_*`` + the anchors in
    the writer's flat node-property projection (G1 preserved, additive — spec §5 Tier-1). One parse
    on read recovers the map. Empty when the entity witnesses nothing (keeps the node clean).
    """
    witnesses = witness_map(entity)
    if not witnesses:
        return {}
    encoded = {prop: sorted(datasets) for prop, datasets in witnesses.items()}
    return {WITNESSES_NODE_PROPERTY: json.dumps(encoded, sort_keys=True)}
