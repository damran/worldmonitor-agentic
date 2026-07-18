"""Typed adapter over FollowTheMoney (FtM) — the L2 ontology contract.

Everything above L2 builds on FtM through this module. FtM 4.x ships type
information (``py.typed``), so the surface here is genuinely typed; we still
funnel all FtM use through one place so the rest of the codebase depends on a
small, stable shape (:data:`FtmEntity`, :func:`make_entity`, :func:`get_model`)
rather than on FtM internals.

This module is also the injection point for **``wm:`` schema extensions**
(CLAUDE.md: *"wm: extensions only where FtM can't reach"*) — see
:func:`register_wm_schemata`, called once at the bottom of this module so every
consumer (writer, resolver, validation, tests) sees the extended model with
zero env-var plumbing (ADR 0118).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml
from followthemoney import Model, ValueEntity, model
from followthemoney.schema import Schema, SchemaSpec

# The canonical FtM entity type used across WorldMonitor. ``ValueEntity`` is the
# value-based proxy that followthemoney-graph consumes when writing to Neo4j.
FtmEntity = ValueEntity

# Vendored, reviewable `wm:` schema YAMLs (ADR 0098 philosophy) — one file per schema, each
# top-level key a FtM schema name mapped to its `SchemaSpec`-shaped body (the exact shape FtM's
# own `Model._load` reads from its own `schema/*.yaml` directory).
_WM_SCHEMA_DIR = Path(__file__).resolve().parent / "schema" / "wm"


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


def register_wm_schemata() -> None:
    """Inject every ``wm:`` schema YAML under ``ontology/schema/wm/`` into the GLOBAL FtM model.

    Mirrors the probed injection recipe (ADR 0118 "Verified facts"): each schema is built with
    ``Schema(model, name, spec)``, assigned onto ``model.schemata[name]``, then the model is
    ``generate()``-d once so the new schema's inheritance/properties/reverse links resolve
    exactly like a schema FtM loaded from its own ``schema/`` directory.

    **Idempotent by construction**: a schema ``name`` already present in ``model.schemata`` is
    left completely untouched — the loop never re-creates or reassigns an existing entry, and
    ``model.generate()`` is skipped entirely when nothing new was added. This keeps every wm:
    ``Schema`` object identity-stable across repeated calls (module import + any later explicit
    call), which is load-bearing for callers that cache a schema reference (P-IND-3, ADR 0118).
    """
    added = False
    for yaml_path in sorted(_WM_SCHEMA_DIR.glob("*.yaml")):
        raw: Any = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        file_schemata = cast("dict[str, Any]", raw)
        for name, spec in file_schemata.items():
            if name in model.schemata:
                continue
            model.schemata[name] = Schema(model, name, cast("SchemaSpec", spec))
            added = True
    if added:
        model.generate()


# Invoked at import time (after every definition above) so `Indicator` — and any future wm:
# schema — exists for every consumer that imports this module, with zero env-var plumbing
# (ADR 0118 D1). Safe to import-order-fragile callers: this call only ever ADDS schemata to the
# shared FtM singleton and is itself idempotent (see docstring), so re-importing this module
# (Python's module cache) or calling `register_wm_schemata()` again explicitly is always a no-op.
register_wm_schemata()
