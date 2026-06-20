"""Provenance — the non-negotiable record of where every fact came from.

Every FtM object a connector emits is stamped with provenance (``source_id``,
``retrieved_at``, ``reliability``, and a pointer to the raw record in the landing
zone). This doubles as the GDPR/audit log. Stamping attaches the provenance to
the entity's FtM ``context`` under :data:`PROVENANCE_KEY`, so it travels with the
entity through serialization (raw landing) and can be projected onto graph
nodes/edges by the writer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from worldmonitor.ontology.ftm import FtmEntity

# Key under which provenance lives in an entity's FtM context.
PROVENANCE_KEY = "wm_provenance"


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


def stamp(entity: FtmEntity, provenance: Provenance) -> FtmEntity:
    """Attach ``provenance`` to ``entity`` (via FtM context) and return it."""
    entity.context[PROVENANCE_KEY] = provenance.as_dict()
    return entity


def get_provenance(entity: FtmEntity) -> Provenance | None:
    """Read provenance back off an entity, or ``None`` if it was never stamped."""
    raw = entity.context.get(PROVENANCE_KEY)
    if not isinstance(raw, dict):
        return None
    return Provenance(
        source_id=raw["source_id"],
        retrieved_at=raw["retrieved_at"],
        reliability=raw["reliability"],
        source_record=raw["source_record"],
    )
