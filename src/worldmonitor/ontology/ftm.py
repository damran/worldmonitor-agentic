"""Typed adapter over FollowTheMoney (FtM) — the L2 ontology contract.

Everything above L2 builds on FtM through this module. FtM 4.x ships type
information (``py.typed``), so the surface here is genuinely typed; we still
funnel all FtM use through one place so the rest of the codebase depends on a
small, stable shape (:data:`FtmEntity`, :func:`make_entity`, :func:`get_model`)
rather than on FtM internals.
"""

from __future__ import annotations

from typing import Any

from followthemoney import Model, ValueEntity, model

# The canonical FtM entity type used across WorldMonitor. ``ValueEntity`` is the
# value-based proxy that followthemoney-graph consumes when writing to Neo4j.
FtmEntity = ValueEntity


def get_model() -> Model:
    """Return the shared, process-wide FtM model (schemata + property types)."""
    return model


def make_entity(data: dict[str, Any]) -> ValueEntity:
    """Build a typed FtM entity from a plain mapping.

    ``data`` is the FtM entity shape (``{"id", "schema", "properties", ...}``).
    Raises :class:`followthemoney.exc.InvalidData` if the schema is unknown;
    callers wanting a friendly, uniform error should go through
    :func:`worldmonitor.ontology.validation.validate_or_raise` instead.
    """
    return ValueEntity.from_dict(data)
