"""Gate 3e — Integrations UI: container-free invariant oracles (ADR 0069).

The DB-free half of the Integrations-UI gate. These inject a FAKE plugin Registry (so the
catalog/form render purely from the registry, no Postgres) over a FAKE empty sessionmaker, and
pin three invariants that need no testcontainer:

  * XSS: a hostile manifest description is HTML-escaped by Jinja2 autoescape (not rendered raw);
  * SECRET HYGIENE: a ``secret`` config field is NEVER pre-filled — not even from a JSON-Schema
    ``default`` — and renders as an empty ``type=password`` input (create-only v1);
  * OPEN-REDIRECT HARDENING (ADR-0068 follow-up): ``auth_web._is_safe_next`` rejects non-ASCII and
    C1 / Unicode line+paragraph separators, falling back to ``"/"``.

RED on the base tree because ``worldmonitor.api.integrations`` does not exist, ``create_app`` has
no ``db_sessions=``/``registry=`` keyword, no templates are mounted, and ``_is_safe_next`` still
admits any char ``> 0x20`` (so non-ASCII / C1 separators sail through).

LOCKED ASSUMPTIONS (must match the builder + the integration oracle):
  * ``create_app(*, ..., db_sessions=None, registry=None)`` — injectable sessionmaker + registry.
  * Routes ``GET /integrations`` (catalog) + ``GET /integrations/new/{plugin_id}`` (schema form),
    both ``get_principal``-gated (bearer ``good`` authenticates).
  * Jinja2 autoescape is ON; no ``| safe`` on untrusted manifest/config data.
  * A ``secret``-flagged property renders ``<input type=password>`` with no pre-filled value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from worldmonitor.api.auth_web import _is_safe_next
from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import (
    Capability,
    Connector,
    Kind,
    Manifest,
    Mode,
    RawRecord,
)
from worldmonitor.plugins.registry import Registry
from worldmonitor.provenance.model import Provenance
from worldmonitor.settings import Settings

AUTH = {"Authorization": "Bearer good"}

XSS_DESCRIPTION = "<script>alert(1)</script>"
SCHEMA_DEFAULT_SECRET = "SCHEMA_DEFAULT_SECRET_XYZ"  # pragma: allowlist secret


# ================================================================================================
# Fakes — a bearer verifier, a placeholder graph client, an empty sessionmaker, and a registry
# holding ONE fake connector (hostile description + a secret field carrying a schema default).
# ================================================================================================
class _FakeVerifier:
    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


class _FakeNeo4j:
    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("the graph client must not be used by the integrations UI")


class _EmptyResult:
    """A read result that yields NO rows for any SQLAlchemy 1.x/2.0 read shape."""

    def scalars(self) -> _EmptyResult:
        return self

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None

    def one_or_none(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None

    def scalar(self) -> None:
        return None

    def filter(self, *a: Any, **k: Any) -> _EmptyResult:
        return self

    def filter_by(self, **k: Any) -> _EmptyResult:
        return self

    def where(self, *a: Any, **k: Any) -> _EmptyResult:
        return self

    def order_by(self, *a: Any, **k: Any) -> _EmptyResult:
        return self

    def limit(self, *a: Any, **k: Any) -> _EmptyResult:
        return self

    def __iter__(self) -> Iterator[Any]:
        return iter(())


class _EmptySession:
    """A no-row, no-op session that is also its own context manager."""

    def execute(self, *a: Any, **k: Any) -> _EmptyResult:
        return _EmptyResult()

    def scalars(self, *a: Any, **k: Any) -> _EmptyResult:
        return _EmptyResult()

    def query(self, *a: Any, **k: Any) -> _EmptyResult:
        return _EmptyResult()

    def get(self, *a: Any, **k: Any) -> None:
        return None

    def add(self, *a: Any, **k: Any) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> _EmptySession:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _FakeSessions:
    """Stands in for a ``sessionmaker``: calling it (or ``.begin()``) yields an empty session."""

    def __call__(self) -> _EmptySession:
        return _EmptySession()

    def begin(self) -> _EmptySession:
        return _EmptySession()


class _FakeConnector(Connector):
    """One fake connector: a hostile (``<script>``) description + a SECRET field that carries a
    JSON-Schema ``default`` (which must NEVER be pre-filled into the rendered password input)."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="fakeplug",
            name="Fake Plugin",
            version="0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description=XSS_DESCRIPTION,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "api_token": {
                    "type": "string",
                    "title": "API token",
                    "secret": True,
                    "default": SCHEMA_DEFAULT_SECRET,
                },
                "q": {"type": "string", "title": "Query"},
            },
            "required": ["api_token", "q"],
            "additionalProperties": False,
        }

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:  # pragma: no cover
        return iter(())

    def map(  # pragma: no cover
        self, record: RawRecord, *, provenance: Provenance
    ) -> Iterable[FtmEntity]:
        return ()


def _fake_registry() -> Registry:
    registry = Registry()
    registry.register(_FakeConnector())
    return registry


def _client(registry: Registry) -> TestClient:
    settings = Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-session-key-123",
        _env_file=None,  # type: ignore[call-arg]
    )
    app = create_app(
        settings=settings,
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
        oauth=None,
        db_sessions=_FakeSessions(),  # type: ignore[call-arg]
        registry=registry,  # type: ignore[call-arg]
    )
    return TestClient(app, raise_server_exceptions=False)


def _input_tag(html: str, name: str) -> str | None:
    for tag in re.findall(r"<input[^>]*>", html, flags=re.IGNORECASE):
        if f'name="{name}"' in tag:
            return tag
    return None


# ================================================================================================
# A. XSS — Jinja2 autoescape escapes a hostile manifest description.
# ================================================================================================
def test_catalog_html_escapes_plugin_description_xss() -> None:
    """A manifest description of ``<script>alert(1)</script>`` is HTML-escaped on the catalog page:
    the raw script tag is absent, the escaped form is present."""
    resp = _client(_fake_registry()).get("/integrations", headers=AUTH)
    assert resp.status_code == 200, f"authed catalog must render: {resp.status_code} {resp.text}"
    body = resp.text
    assert XSS_DESCRIPTION not in body, "an un-escaped <script> manifest description was rendered"
    assert "&lt;script&gt;" in body, "the description must be HTML-escaped (Jinja2 autoescape)"


# ================================================================================================
# B. SECRET HYGIENE — a secret field renders empty, never pre-filled (even from a schema default).
# ================================================================================================
def test_secret_value_never_rendered_back() -> None:
    """The secret ``api_token`` field renders as an EMPTY ``type=password`` input — its JSON-Schema
    ``default`` is NOT echoed into the form (create-only v1; secrets are never rendered back)."""
    resp = _client(_fake_registry()).get("/integrations/new/fakeplug", headers=AUTH)
    assert resp.status_code == 200, f"form must render: {resp.status_code} {resp.text}"
    body = resp.text

    assert SCHEMA_DEFAULT_SECRET not in body, (
        "a secret's schema default leaked into the rendered form"
    )

    token_tag = _input_tag(body, "api_token")
    assert token_tag is not None, "the form must render an input for the secret 'api_token'"
    assert 'type="password"' in token_tag.lower(), (
        f"a 'secret':true field must render as <input type=password>; got {token_tag!r}"
    )
    assert re.search(r'value="[^"]+"', token_tag) is None, (
        f"the secret input must be EMPTY (no pre-filled value); got {token_tag!r}"
    )


# ================================================================================================
# C. OPEN-REDIRECT HARDENING — _is_safe_next rejects non-ASCII / C1 / Unicode separators.
# ================================================================================================
_UNSAFE_NEXT = [
    ("u2028_line_sep", "/\u2028//evil.com"),
    ("u2029_para_sep", "/\u2029//evil.com"),
    ("x85_nel_c1", "/\x85//evil.com"),
    ("nonascii_cafe", "/caf\u00e9"),
    ("raw_space", "/x y"),
]


@pytest.mark.parametrize(
    "unsafe_next",
    [value for _, value in _UNSAFE_NEXT],
    ids=[name for name, _ in _UNSAFE_NEXT],
)
def test_is_safe_next_rejects_nonascii_and_c1(unsafe_next: str) -> None:
    """``_is_safe_next`` must reject any non-ASCII char, any C1 control (e.g. ``\\x85``), and the
    Unicode line/paragraph separators (U+2028/U+2029), falling back to ``"/"``.

    RED now: the guard only requires every char ``> 0x20`` and ``!= 0x7F``, so U+0085 / U+2028 /
    U+2029 / ``é`` all pass and are returned verbatim — an open-redirect / attribute-injection
    vector once ``next`` is rendered into a UI href (ADR 0069 §Folded-in hardening)."""
    result = _is_safe_next(unsafe_next)
    assert result == "/", (
        "a next carrying a non-ASCII / C1 / Unicode-separator char must fall back to the site "
        f"root; got {result!r} for input {unsafe_next!r}"
    )
