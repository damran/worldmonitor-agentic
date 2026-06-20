"""Schema validation — the gate that keeps invalid data out of the pipeline.

Every FtM object a connector produces must validate against the FtM schema
before anything downstream touches it (CLAUDE.md: *validate every object against
the FtM schema; never invent a parallel model*). Failures raise
:class:`InvalidEntity` loudly rather than silently dropping data — FtM's own
parser is permissive (it discards unknown properties and tolerates a missing
id), which would let unusable records slip through.
"""

from __future__ import annotations

from typing import Any

from followthemoney.exc import FollowTheMoneyException

from worldmonitor.ontology.ftm import FtmEntity, make_entity


class InvalidEntity(ValueError):
    """Raised when input data is not a usable FtM entity."""


def validate_or_raise(data: dict[str, Any]) -> FtmEntity:
    """Validate ``data`` as an FtM entity, returning a typed proxy or raising.

    Beyond FtM's permissive parse, the entity must carry a non-empty string
    ``id`` and a resolvable ``schema`` — the minimum needed to write it to the
    graph and trace it back to its source.
    """
    entity_id = data.get("id")
    if not entity_id or not isinstance(entity_id, str):
        raise InvalidEntity("entity is missing a non-empty string 'id'")
    if not data.get("schema"):
        raise InvalidEntity(f"entity {entity_id!r} is missing 'schema'")
    try:
        entity = make_entity(data)
    except FollowTheMoneyException as exc:
        raise InvalidEntity(f"entity {entity_id!r} failed FtM validation: {exc}") from exc
    if entity.id is None:
        raise InvalidEntity(f"entity {entity_id!r} produced no id after parsing")
    return entity
