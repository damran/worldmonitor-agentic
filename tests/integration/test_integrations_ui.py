"""Gate 3e — Integrations UI: the PRIMARY security/invariant oracle (ADR 0069).

These are the failing-test-first tests for the operator UI (HTMX + Jinja2 catalog +
schema-driven config save). They run over a REAL testcontainer Postgres + a REAL
discovered plugin Registry (connectors + notifiers) + the session-auth bearer test
pattern (``Authorization: Bearer good`` — no live Zitadel/network).

RED on the base tree because:
  * ``worldmonitor.api.integrations`` does not exist (no router);
  * ``create_app`` has no ``db_sessions=`` / ``registry=`` keyword (TypeError today);
  * no Jinja2 templates / StaticFiles are mounted.

LOCKED ASSUMPTIONS the builder MUST match so this oracle stays meaningful:

1. APP FACTORY DI. ``create_app(*, settings, verifier, readiness, neo4j_client, oauth,
   db_sessions=None, registry=None)`` gains a keyword-only Postgres ``db_sessions``
   (a ``sessionmaker``; default ``session_factory(engine_from_settings(settings))``) and a
   plugin ``registry`` (default a discovered ``Registry``). When injected they are used
   verbatim. A ``get_db`` dependency yields a session per request.

2. ROUTES (all behind ``get_principal``):
     - ``GET  /integrations``                          -> HTML catalog + instances
     - ``GET  /integrations/new/{plugin_id}``          -> HTML schema-driven form (404 unknown)
     - ``POST /integrations``                          -> create (303 to /integrations)
     - ``POST /integrations/instances/{id}/enable``    -> 303
     - ``POST /integrations/instances/{id}/disable``   -> 303

3. CSRF. A session-stored synchronizer token. The form embeds a hidden input named
   ``csrf_token`` whose value equals the session token. Every POST MUST carry the matching
   token; absent/wrong -> 403 (no row written / no status change).

4. FORM ENCODING. ``POST /integrations`` is form-encoded and carries a hidden ``plugin_id``
   field naming the plugin, a ``csrf_token`` field, and one field per JSON-Schema property
   keyed by the property name (``api_token``/``q``/``per_page``/``max_pages``). The route
   builds the config from ONLY the schema properties (so ``csrf_token``/``plugin_id`` are
   NOT in the stored config — ``additionalProperties:false`` enforces this), coercing
   ``integer``/``number`` fields, then ``plugin.validate_config`` BEFORE encrypt+store.

5. STATUS values: a freshly created instance is ``status="enabled"``; ``/disable`` flips it
   to ``"disabled"``; ``/enable`` back to ``"enabled"``.
"""

from __future__ import annotations

import importlib
import json
import logging
import pkgutil
import re
from collections.abc import Mapping
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ConnectorInstance
from worldmonitor.plugins.registry import Registry

pytestmark = pytest.mark.integration

AUTH = {"Authorization": "Bearer good"}


# ================================================================================================
# Fakes + builders.
# ================================================================================================
class _FakeVerifier:
    """Accepts the bearer token ``"good"``; rejects everything else (mirrors the auth suite)."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


class _FakeNeo4j:
    """Placeholder graph client stored on ``app.state``; the integrations routes never touch it."""

    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("the graph client must not be used by the integrations UI")


def _settings() -> Any:
    from worldmonitor.settings import Settings

    return Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-session-key-123",
        _env_file=None,  # type: ignore[call-arg]
    )


def _real_registry() -> Registry:
    """A registry discovered from the REAL connector + notifier packages (opencorporates,
    feeds, geonames, opensanctions + the telegram notifier)."""
    registry = Registry()
    for pkg_name in (
        "worldmonitor.plugins.connectors",
        "worldmonitor.plugins.notifiers",
    ):
        package = importlib.import_module(pkg_name)
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package.__name__}."):
            registry.discover_module(importlib.import_module(info.name))
    return registry


def _build(postgres_dsn: str) -> tuple[TestClient, Any, Any]:
    """Wire create_app over the testcontainer Postgres + the real registry; return
    ``(client, settings, sessions)``."""
    settings = _settings()
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    app = create_app(
        settings=settings,
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
        oauth=None,
        db_sessions=sessions,  # type: ignore[call-arg]
        registry=_real_registry(),  # type: ignore[call-arg]
    )
    return TestClient(app, raise_server_exceptions=False), settings, sessions


def _csrf_from(html: str) -> str:
    """Pull the value of the hidden ``csrf_token`` input out of rendered HTML (attribute-order
    tolerant)."""
    for tag in re.findall(r"<input[^>]*>", html, flags=re.IGNORECASE):
        if 'name="csrf_token"' in tag:
            match = re.search(r'value="([^"]*)"', tag)
            if match and match.group(1):
                return match.group(1)
    raise AssertionError(f"no non-empty hidden csrf_token input found in HTML:\n{html[:2000]}")


def _input_tag(html: str, name: str) -> str | None:
    for tag in re.findall(r"<input[^>]*>", html, flags=re.IGNORECASE):
        if f'name="{name}"' in tag:
            return tag
    return None


def _all_instances(sessions: Any) -> list[ConnectorInstance]:
    with sessions() as session:
        return list(session.execute(select(ConnectorInstance)).scalars().all())


def _status_of(sessions: Any, instance_id: str) -> str | None:
    with sessions() as session:
        row = session.execute(
            select(ConnectorInstance).where(ConnectorInstance.id == instance_id)
        ).scalar_one_or_none()
        return None if row is None else row.status


# ================================================================================================
# A. AUTH-GATING + CATALOG.
# ================================================================================================
def test_catalog_lists_plugins_and_requires_auth(postgres_dsn: str) -> None:
    """GET /integrations is auth-gated and, once authenticated, renders the real plugin catalog
    (connectors + notifiers)."""
    client, _settings_, _sessions = _build(postgres_dsn)

    # No auth + a JSON Accept -> 401, NOT served (the API contract; a browser would 302 to /login).
    unauth = client.get(
        "/integrations",
        headers={"Accept": "application/json"},
        follow_redirects=False,
    )
    assert unauth.status_code == 401, (
        f"unauthenticated /integrations must 401 for a JSON request, got {unauth.status_code}"
    )

    resp = client.get("/integrations", headers=AUTH)
    assert resp.status_code == 200, f"authed catalog must render: {resp.status_code} {resp.text}"
    body = resp.text
    # The real discovered plugins surface in the catalog (their ids ride the "Add" links /
    # new-instance hrefs). Connectors AND notifiers are listed uniformly.
    assert "opencorporates" in body, "the opencorporates connector must appear in the catalog"
    assert "feeds" in body, "the feeds connector must appear in the catalog"
    assert "telegram" in body, "the telegram notifier must appear in the catalog (notifiers too)"


# ================================================================================================
# B. SCHEMA-DRIVEN FORM + SECRET RENDERS AS EMPTY PASSWORD.
# ================================================================================================
def test_new_instance_form_renders_schema_with_secret_as_password(postgres_dsn: str) -> None:
    """GET /integrations/new/opencorporates renders one input per schema property; the secret
    ``api_token`` renders as an EMPTY ``type=password`` input. Unknown plugin -> 404."""
    client, _settings_, _sessions = _build(postgres_dsn)

    resp = client.get("/integrations/new/opencorporates", headers=AUTH)
    assert resp.status_code == 200, f"form must render: {resp.status_code} {resp.text}"
    body = resp.text

    # A field for the non-secret "q" property.
    assert 'name="q"' in body, "the schema form must render an input for the 'q' property"

    # The secret "api_token" property -> a password input, rendered EMPTY (never pre-filled).
    token_tag = _input_tag(body, "api_token")
    assert token_tag is not None, "the schema form must render an input for 'api_token'"
    assert 'type="password"' in token_tag.lower(), (
        f"a 'secret':true field must render as <input type=password>; got {token_tag!r}"
    )
    assert re.search(r'value="[^"]+"', token_tag) is None, (
        f"the secret input must be EMPTY (no pre-filled value attribute); got {token_tag!r}"
    )

    missing = client.get("/integrations/new/does-not-exist", headers=AUTH)
    assert missing.status_code == 404, f"unknown plugin_id must 404, got {missing.status_code}"


# ================================================================================================
# C. CREATE: encrypt config + enable + redirect.
# ================================================================================================
def test_create_instance_encrypts_config_and_enables(postgres_dsn: str) -> None:
    """POST /integrations with a valid CSRF token + a valid opencorporates config inserts exactly
    ONE enabled ConnectorInstance whose config_encrypted is NOT plaintext and round-trips through
    ConfigCipher.decrypt (secret intact)."""
    client, settings, sessions = _build(postgres_dsn)

    form_resp = client.get("/integrations/new/opencorporates", headers=AUTH)
    assert form_resp.status_code == 200
    csrf = _csrf_from(form_resp.text)

    resp = client.post(
        "/integrations",
        headers=AUTH,
        data={
            "csrf_token": csrf,
            "plugin_id": "opencorporates",
            "api_token": "SECRET_TOK_42",  # pragma: allowlist secret
            "q": "acme",
            "per_page": "2",
            "max_pages": "2",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, (
        f"create must 303 to /integrations: {resp.status_code} {resp.text}"
    )
    assert resp.headers["location"].endswith("/integrations"), resp.headers.get("location")

    rows = _all_instances(sessions)
    assert len(rows) == 1, f"exactly one ConnectorInstance must be created, got {len(rows)}"
    row = rows[0]
    assert row.connector_id == "opencorporates", row.connector_id
    assert row.status == "enabled", f"a created instance must be enabled, got {row.status!r}"

    # The raw secret is NEVER stored in plaintext.
    assert "SECRET_TOK_42" not in row.config_encrypted, "the secret leaked into the stored blob"

    # The encrypted blob round-trips through ConfigCipher, secret intact + correctly typed.
    cipher = ConfigCipher.from_settings(settings)
    config = json.loads(cipher.decrypt(row.config_encrypted))
    assert config["api_token"] == "SECRET_TOK_42", config  # pragma: allowlist secret
    assert config["q"] == "acme", config
    # integer fields were coerced (a string would have failed validate_config -> no 303).
    assert config["per_page"] == 2, f"per_page must be stored as int 2, got {config['per_page']!r}"
    # The CSRF/plugin_id form fields must NOT bleed into the stored config.
    assert "csrf_token" not in config, "csrf_token must not be stored in the config"
    assert "plugin_id" not in config, "plugin_id must not be stored in the config"


# ================================================================================================
# D. CSRF — absent/wrong token -> 403, NOTHING written.
# ================================================================================================
def test_create_without_csrf_is_403_and_writes_nothing(postgres_dsn: str) -> None:
    """A create POST with no CSRF token, or a wrong one, -> 403 and writes ZERO rows."""
    client, _settings_, sessions = _build(postgres_dsn)

    valid_config = {
        "plugin_id": "opencorporates",
        "api_token": "SECRET_TOK_99",  # pragma: allowlist secret
        "q": "acme",
        "per_page": "1",
        "max_pages": "1",
    }

    # (1) No csrf_token field at all (no session minted) -> 403; an absent token must NOT match an
    # absent session token.
    no_token = client.post("/integrations", headers=AUTH, data=valid_config, follow_redirects=False)
    assert no_token.status_code == 403, f"missing CSRF must 403, got {no_token.status_code}"

    # (2) Establish a real session token, then POST a WRONG token -> 403 (comparison must happen).
    csrf = _csrf_from(client.get("/integrations/new/opencorporates", headers=AUTH).text)
    wrong = client.post(
        "/integrations",
        headers=AUTH,
        data={**valid_config, "csrf_token": csrf + "-tampered"},
        follow_redirects=False,
    )
    assert wrong.status_code == 403, f"a wrong CSRF token must 403, got {wrong.status_code}"

    assert _all_instances(sessions) == [], "a CSRF-rejected create must write NO ConnectorInstance"


# ================================================================================================
# D2. CSRF — a NON-ASCII submitted token -> 403 (fail-closed), NEVER 500.
# ================================================================================================
def test_create_with_non_ascii_csrf_token_is_403_not_500(postgres_dsn: str) -> None:
    """A create POST whose ``csrf_token`` carries a NON-ASCII codepoint (e.g. ``"café"``) must be
    rejected with **403** — the same fail-closed verdict any wrong token gets — and write ZERO rows.

    The threat: ``secrets.compare_digest`` raises ``TypeError: comparing strings with non-ASCII
    characters is not supported`` the instant EITHER operand is a ``str`` holding a non-ASCII
    character. The CSRF guard compares the submitted token against the session token as raw
    ``str`` objects, so an attacker who submits ``"café"`` crashes the comparison and the route
    answers **500** instead of the contractual **403** (a wrong token must be a clean rejection,
    not a server error that leaks a stack trace / DoSes the endpoint). The fix is to compare on
    ``.encode()``d bytes (both sides), which never raises on non-ASCII input.

    This mirrors section D's wrong-token case EXACTLY (same client, same session-cookie carry from
    the form GET, same valid opencorporates fields) — ONLY the ``csrf_token`` value differs: it is
    a non-ASCII string rather than ``csrf + "-tampered"``.
    """
    client, _settings_, sessions = _build(postgres_dsn)

    # Mint a real session CSRF token + set the session cookie (so a real comparison is attempted,
    # not the absent-session short-circuit). The TestClient carries the cookie into the POST.
    form_resp = client.get("/integrations/new/opencorporates", headers=AUTH)
    assert form_resp.status_code == 200
    # Sanity: a non-empty token really was minted into the session/form.
    assert _csrf_from(form_resp.text), "the form GET must mint a session CSRF token"

    non_ascii_token = "café"  # a non-ASCII string — must be rejected, must NOT crash the route
    assert any(ord(ch) > 127 for ch in non_ascii_token), (
        "the token must carry a non-ASCII codepoint"
    )

    resp = client.post(
        "/integrations",
        headers=AUTH,
        data={
            "csrf_token": non_ascii_token,
            "plugin_id": "opencorporates",
            "api_token": "SECRET_TOK_NONASCII",  # pragma: allowlist secret
            "q": "acme",
            "per_page": "1",
            "max_pages": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403, (
        "a non-ASCII CSRF token must be rejected fail-closed with 403 (never a 500 from a "
        f"compare_digest TypeError), got {resp.status_code}: {resp.text}"
    )

    assert _all_instances(sessions) == [], (
        "a CSRF-rejected (non-ASCII token) create must write NO ConnectorInstance"
    )


# ================================================================================================
# E. INPUT VALIDATION — bad config -> 422, unknown plugin -> 404, NOTHING written.
# ================================================================================================
def test_create_invalid_config_is_422(postgres_dsn: str) -> None:
    """A valid-CSRF create with a config missing a required field -> 422 (validate_config); an
    unknown plugin_id -> 404. Neither writes a row."""
    client, _settings_, sessions = _build(postgres_dsn)
    csrf = _csrf_from(client.get("/integrations/new/opencorporates", headers=AUTH).text)

    # Missing the required "api_token" -> validate_config fails -> 422.
    bad = client.post(
        "/integrations",
        headers=AUTH,
        data={"csrf_token": csrf, "plugin_id": "opencorporates", "q": "acme"},
        follow_redirects=False,
    )
    assert bad.status_code == 422, (
        f"a config failing validate_config must 422, got {bad.status_code}"
    )

    # Unknown plugin (CSRF valid so the 404 is the plugin-resolution branch, not CSRF) -> 404.
    unknown = client.post(
        "/integrations",
        headers=AUTH,
        data={"csrf_token": csrf, "plugin_id": "no-such-plugin", "q": "acme"},
        follow_redirects=False,
    )
    assert unknown.status_code == 404, f"an unknown plugin_id must 404, got {unknown.status_code}"

    assert _all_instances(sessions) == [], "neither a 422 nor a 404 create may write a row"


# ================================================================================================
# F. SECRET HYGIENE — never logged.
# ================================================================================================
def test_secret_not_logged_on_create(postgres_dsn: str) -> None:
    """During a create POST the submitted secret appears in NO log record (any logger)."""
    client, _settings_, _sessions = _build(postgres_dsn)
    csrf = _csrf_from(client.get("/integrations/new/opencorporates", headers=AUTH).text)

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__(level=logging.NOTSET)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    handler = _Capture()
    root = logging.getLogger()
    prev_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        resp = client.post(
            "/integrations",
            headers=AUTH,
            data={
                "csrf_token": csrf,
                "plugin_id": "opencorporates",
                "api_token": "LEAKME_SECRET_9",  # pragma: allowlist secret
                "q": "acme",
                "per_page": "1",
                "max_pages": "1",
            },
            follow_redirects=False,
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)

    assert resp.status_code == 303, f"create should have succeeded: {resp.status_code} {resp.text}"
    for record in handler.records:
        rendered = record.getMessage()
        try:
            formatted = handler.format(record)
        except Exception:  # pragma: no cover - defensive
            formatted = ""
        assert "LEAKME_SECRET_9" not in rendered, f"secret leaked into a log message: {rendered!r}"
        assert "LEAKME_SECRET_9" not in formatted, (
            f"secret leaked into a formatted log: {formatted!r}"
        )
    # And never echoed back into the redirect response body.
    assert "LEAKME_SECRET_9" not in resp.text, "secret echoed into the create response"


# ================================================================================================
# G. ENABLE / DISABLE — flips status, CSRF-gated.
# ================================================================================================
def test_enable_disable_flips_status_and_needs_csrf(postgres_dsn: str) -> None:
    """/disable and /enable flip ConnectorInstance.status and require a valid CSRF token (absent/
    wrong -> 403, status unchanged)."""
    client, settings, sessions = _build(postgres_dsn)

    cipher = ConfigCipher.from_settings(settings)
    blob = cipher.encrypt(json.dumps({"api_token": "X", "q": "acme"}))  # pragma: allowlist secret
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id="inst-ed-1",
                connector_id="opencorporates",
                config_encrypted=blob,
                status="enabled",
            )
        )
        session.commit()

    # The catalog page mints the CSRF token used by the enable/disable action forms.
    csrf = _csrf_from(client.get("/integrations", headers=AUTH).text)

    disable = client.post(
        "/integrations/instances/inst-ed-1/disable",
        headers=AUTH,
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert disable.status_code == 303, f"disable must 303: {disable.status_code} {disable.text}"
    assert _status_of(sessions, "inst-ed-1") == "disabled", "disable must flip status to disabled"

    enable = client.post(
        "/integrations/instances/inst-ed-1/enable",
        headers=AUTH,
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert enable.status_code == 303, f"enable must 303: {enable.status_code} {enable.text}"
    assert _status_of(sessions, "inst-ed-1") == "enabled", "enable must flip status to enabled"

    # No CSRF token -> 403, status unchanged.
    no_csrf = client.post(
        "/integrations/instances/inst-ed-1/disable",
        headers=AUTH,
        follow_redirects=False,
    )
    assert no_csrf.status_code == 403, (
        f"a CSRF-less status flip must 403, got {no_csrf.status_code}"
    )
    assert _status_of(sessions, "inst-ed-1") == "enabled", "a 403 flip must not change status"

    # Wrong CSRF token -> 403, status unchanged.
    wrong_csrf = client.post(
        "/integrations/instances/inst-ed-1/disable",
        headers=AUTH,
        data={"csrf_token": csrf + "-tampered"},
        follow_redirects=False,
    )
    assert wrong_csrf.status_code == 403, (
        f"a wrong-CSRF flip must 403, got {wrong_csrf.status_code}"
    )
    assert _status_of(sessions, "inst-ed-1") == "enabled", "a 403 flip must not change status"
