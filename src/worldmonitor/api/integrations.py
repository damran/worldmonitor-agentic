"""Integrations UI — server-rendered plugin catalog + schema-driven config (ADR 0069).

The operator UI: an HTMX/Jinja2 catalog of available plugins (``Registry.all_manifests`` —
connectors AND notifiers) plus a config form generated from each plugin's
``config.schema.json``, saving an **encrypted** :class:`ConnectorInstance` (the first instance
write path) and flipping its status enabled/disabled. Every route is ``get_principal``-gated;
an unauthenticated browser is 302'd to ``/login`` by the dual-path middleware (ADR 0068).

Security (the threats a browser form UI introduces, ADR 0069 §Security):

* **CSRF** — a session synchronizer token, minted on a form GET, embedded as a hidden
  ``csrf_token`` field, and required + constant-time-compared on every POST (absent/wrong → 403).
* **XSS** — Jinja2 autoescaping (on for ``.html``) escapes all rendered manifest/config/instance
  data; no ``| safe`` on untrusted data.
* **Secrets** — config secrets are ``ConfigCipher``-encrypted before storage, never logged, and
  never rendered back (create-only v1; a ``"secret": true`` field renders as an EMPTY password).
* **Input validation** — unknown ``plugin_id`` → 404; the config is validated via
  ``plugin.validate_config`` (its JSON Schema) BEFORE encrypt+store; the instance id is a
  server-minted uuid. The config is built from ONLY the schema property names, so ``csrf_token`` /
  ``plugin_id`` can never bleed into the stored blob.

No graph write, no resolution — the UI only reads the registry + writes a ``ConnectorInstance``
row. Single-tenant (D1, ADR 0042).
"""

from __future__ import annotations

import json
import secrets
from typing import Annotated, Any
from uuid import uuid4

import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse, Response

from worldmonitor.api.deps import get_db, get_principal
from worldmonitor.authz.oidc import Principal
from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.models import ConnectorInstance
from worldmonitor.plugins.registry import (
    Registry,
    UnknownConnectorError,
    UnknownNotifierError,
)

router = APIRouter(tags=["integrations"])


# ------------------------------------------------------------------------------------------------
# CSRF — a session-stored synchronizer token (mint-on-read; constant-time compare).
# ------------------------------------------------------------------------------------------------
def _csrf_token(request: Request) -> str:
    """Return the session CSRF token, minting one on first read (mint-on-form-GET)."""
    token: str = request.session.setdefault("csrf_token", secrets.token_urlsafe(32))
    return token


def _check_csrf(request: Request, submitted: str | None) -> None:
    """403 unless ``submitted`` is present AND matches the session token (constant-time).

    An ABSENT submitted token must NOT match an absent session token — both must be present and
    equal, or the POST is rejected (ADR 0069).
    """
    expected = request.session.get("csrf_token")
    if (
        not submitted
        or not expected
        or not secrets.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(status_code=403, detail="CSRF token mismatch")


# ------------------------------------------------------------------------------------------------
# Plugin resolution + the schema → form-field projection.
# ------------------------------------------------------------------------------------------------
def _resolve_plugin(registry: Registry, plugin_id: str) -> Any:
    """Return the connector OR notifier for ``plugin_id``; 404 if neither knows it.

    Connectors and notifiers live in separate registry namespaces; the catalog lists both, so the
    form/create routes resolve across both. The returned plugin exposes ``.manifest`` /
    ``.config_schema`` / ``.validate_config`` (the :class:`Connector` ∪ :class:`Notifier` surface).
    """
    try:
        return registry.get(plugin_id)
    except UnknownConnectorError:
        pass
    try:
        return registry.get_notifier(plugin_id)
    except UnknownNotifierError as exc:
        raise HTTPException(status_code=404, detail="Unknown plugin") from exc


def _form_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a config JSON-Schema into render-ready field descriptors.

    Each field carries its property ``name``, a ``label`` (schema ``title`` or the name), whether it
    is ``required``, an input ``kind`` (``password`` for ``"secret": true`` — takes precedence so a
    secret never renders its value — else ``select`` for an enum, ``number`` for integer/number,
    ``checkbox`` for boolean, else ``text``), and the enum ``options``. A schema ``default`` is
    DELIBERATELY NOT carried through: secrets must never be pre-filled (ADR 0069).
    """
    properties: dict[str, Any] = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: list[dict[str, Any]] = []
    for name, prop in properties.items():
        if prop.get("secret"):
            kind = "password"
        elif "enum" in prop:
            kind = "select"
        elif prop.get("type") in {"integer", "number"}:
            kind = "number"
        elif prop.get("type") == "boolean":
            kind = "checkbox"
        else:
            kind = "text"
        fields.append(
            {
                "name": name,
                "label": prop.get("title", name),
                "required": name in required,
                "kind": kind,
                "options": list(prop.get("enum", [])),
            }
        )
    return fields


def _build_config(schema: dict[str, Any], form: Any) -> dict[str, Any]:
    """Build the instance config from ONLY the schema's property names (typed coercion).

    Reads exclusively the keys declared in ``schema.properties`` (so ``csrf_token`` / ``plugin_id``
    / any extra form field can never enter the stored config — ``additionalProperties: false`` would
    reject them anyway). Coercion: ``integer`` → ``int``, ``number`` → ``float`` (a non-numeric
    string is left as-is so ``validate_config`` rejects it → 422, never a 500); ``boolean`` →
    presence of the checkbox; ``string``/enum → the value, omitting an empty optional string (so a
    blank field — including a blank secret — is simply not stored).
    """
    properties: dict[str, Any] = schema.get("properties", {})
    config: dict[str, Any] = {}
    for name, prop in properties.items():
        ptype = prop.get("type")
        if ptype == "boolean":
            # A checkbox sends its value only when checked; absence means False/unset.
            if name in form:
                config[name] = True
            continue
        if name not in form:
            continue
        raw = form[name]
        if not isinstance(raw, str):
            continue
        if ptype == "integer":
            try:
                config[name] = int(raw)
            except ValueError:
                config[name] = raw
        elif ptype == "number":
            try:
                config[name] = float(raw)
            except ValueError:
                config[name] = raw
        elif raw == "":
            # Omit an empty optional string (never store "" for a left-blank field).
            continue
        else:
            config[name] = raw
    return config


# ------------------------------------------------------------------------------------------------
# Routes (all behind get_principal).
# ------------------------------------------------------------------------------------------------
@router.get("/integrations", include_in_schema=False)
def catalog(
    request: Request,
    _principal: Annotated[Principal, Depends(get_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Render the plugin catalog (connectors + notifiers) + the configured instances."""
    registry: Registry = request.app.state.registry
    instances = list(db.execute(select(ConnectorInstance)).scalars().all())
    context = {
        "manifests": registry.all_manifests(),
        "instances": instances,
        "csrf_token": _csrf_token(request),
    }
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "integrations.html", context)


@router.get("/integrations/new/{plugin_id}", include_in_schema=False)
def new_instance(
    request: Request,
    plugin_id: str,
    _principal: Annotated[Principal, Depends(get_principal)],
) -> Response:
    """Render a schema-driven config form for ``plugin_id`` (404 if unknown)."""
    registry: Registry = request.app.state.registry
    plugin = _resolve_plugin(registry, plugin_id)
    context = {
        "plugin_id": plugin_id,
        "manifest": plugin.manifest,
        "fields": _form_fields(plugin.config_schema),
        "csrf_token": _csrf_token(request),
    }
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "new_instance.html", context)


@router.post("/integrations", include_in_schema=False)
async def create_instance(
    request: Request,
    _principal: Annotated[Principal, Depends(get_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Create an enabled, encrypted ``ConnectorInstance`` from a validated config (303)."""
    form = await request.form()
    _check_csrf(request, _as_str(form.get("csrf_token")))

    registry: Registry = request.app.state.registry
    plugin_id = _as_str(form.get("plugin_id")) or ""
    plugin = _resolve_plugin(registry, plugin_id)

    config = _build_config(plugin.config_schema, form)
    try:
        plugin.validate_config(config)
    except jsonschema.ValidationError as exc:
        # Never echo the config/value in the detail — it may carry the secret (ADR 0069).
        raise HTTPException(status_code=422, detail="Invalid configuration") from exc

    settings = request.app.state.settings
    blob = ConfigCipher.from_settings(settings).encrypt(json.dumps(config))
    db.add(
        ConnectorInstance(
            id=str(uuid4()),
            connector_id=plugin_id,
            config_encrypted=blob,
            status="enabled",
        )
    )
    db.commit()
    return RedirectResponse("/integrations", status_code=303)


@router.post("/integrations/instances/{instance_id}/enable", include_in_schema=False)
async def enable_instance(
    request: Request,
    instance_id: str,
    _principal: Annotated[Principal, Depends(get_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Flip an instance to ``status="enabled"`` (CSRF-gated; 303)."""
    return await _set_status(request, instance_id, "enabled", db)


@router.post("/integrations/instances/{instance_id}/disable", include_in_schema=False)
async def disable_instance(
    request: Request,
    instance_id: str,
    _principal: Annotated[Principal, Depends(get_principal)],
    db: Annotated[Session, Depends(get_db)],
) -> Response:
    """Flip an instance to ``status="disabled"`` (CSRF-gated; 303)."""
    return await _set_status(request, instance_id, "disabled", db)


async def _set_status(request: Request, instance_id: str, status: str, db: Session) -> Response:
    """Shared enable/disable: validate CSRF, load the instance (404), flip status, 303."""
    form = await request.form()
    _check_csrf(request, _as_str(form.get("csrf_token")))
    instance = db.get(ConnectorInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    instance.status = status
    db.commit()
    return RedirectResponse("/integrations", status_code=303)


def _as_str(value: Any) -> str | None:
    """Narrow a form value to ``str`` (ignore file uploads / absent fields)."""
    return value if isinstance(value, str) else None
