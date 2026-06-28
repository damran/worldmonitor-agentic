"""Gate 3g — ACTIVE-capability gating: the operator-run security-boundary oracle (ADR 0071).

The active-execution boundary, end to end, over a testcontainer Postgres + a FAKE run_command (no
real ``whois`` binary, no network) + an in-memory landing. The invariants this file pins (the
failing-test-first list, ADR 0071 §Invariant gate note):

* (a) CADENCE STILL REFUSES — ``driver._ingest_instance`` on an ACTIVE connector still records a
      ``task_run`` error with ``ActiveConnectorRefused`` and NEVER executes the tool (frozen);
* (b) OPERATOR RUN — ``run_connector_once`` on an ACTIVE connector with NO scope refuses (raises)
      and does not run the tool; WITH a scope it mints+verifies a token, runs, and writes a
      ``task_run`` with ``run_mode="operator"``, ``triggered_by=<operator>``, ``scope_token`` set
      (and that token verifies); a PASSIVE run-now needs no token (``scope_token`` None);
* (e/f) REST ``POST /integrations/instances/{id}/run`` is ``get_principal``-gated (no auth -> 401),
      CSRF-protected (absent token -> 403, no run), 422 for an ACTIVE run with no scope, and 303 +
      an operator ``task_run`` whose ``triggered_by`` is the principal subject on success;
* SEPARATE LOGGING — an ACTIVE run emits a distinct log line (connector/instance/operator) and never
      logs the scope-token plaintext nor any config secret;
* (g) MIGRATION — ``migrate_to_head`` adds ``task_run.run_mode/triggered_by/scope_token``.

LOCKED ASSUMPTIONS the builder MUST match so this oracle stays meaningful:

  ``runner/operator_run.py::run_connector_once(instance, connector, *, scope, operator, sessions,
        landing, settings)`` — ``instance`` is the ``ConnectorInstance`` (id + encrypted config),
        ``connector`` the resolved plugin; it decrypts config (ConfigCipher from settings), injects
        ``config["_scope"] = scope`` for ACTIVE, runs via ``run_ingest`` and writes the audit
        ``task_run``. ACTIVE-without-scope raises ``ValueError`` (the route maps it to 422). It does
        NOT call ``validate_config`` (the injected ``_scope`` key would fail a strict schema), so
        extra config keys are ignored.
  ``TaskRun`` gains ``run_mode`` (default "cadence"), ``triggered_by``, ``scope_token``; migration
        ``0008_task_run_audit`` (down_revision ``0007_stream_cursor``).
  ``POST /integrations/instances/{id}/run`` — CSRF via the form field ``csrf_token`` (minted on a
        GET, exactly like the other integrations POSTs); the scope arrives as a form field ``scope``
        carrying a JSON object (absent/empty -> no scope); ``operator = principal.subject``. The
        route resolves its ``LandingStore`` from ``app.state.landing`` when present (the test seam,
        since ``create_app`` is out of this gate's blast radius and gains no ``landing=`` kwarg).
  The runner seam is an async ``run_command``-compatible callable; tests inject a fake.

RED on the base tree: ``worldmonitor.plugins.scope_token`` / ``worldmonitor.plugins.cli_tool`` /
``worldmonitor.plugins.connectors.whois`` / ``worldmonitor.runner.operator_run`` do not exist, and
``TaskRun`` has no audit columns + there is no ``/run`` route (ModuleNotFoundError at collection).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import uuid
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, make_url, select, text

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import create_all, make_engine, migrate_to_head, session_factory
from worldmonitor.db.models import ConnectorInstance, TaskRun
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.plugins.base import Capability, Connector, Kind, Manifest, Mode, RawRecord
from worldmonitor.plugins.connectors.whois.connector import WhoisConnector
from worldmonitor.plugins.registry import Registry
from worldmonitor.plugins.scope_token import verify as verify_scope_token
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.runner.driver import ActiveConnectorRefused, IngestDriver
from worldmonitor.runner.operator_run import run_connector_once
from worldmonitor.runner.subprocess import RunResult

pytestmark = pytest.mark.integration

AUTH = {"Authorization": "Bearer good"}
_NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)

_WHOIS_STDOUT = (
    b"Domain Name: EXAMPLE.COM\n"
    b"Registrar: Example Registrar, Inc.\n"
    b"Registrant Organization: Example Holdings LLC\n"
    b"Registrant Country: US\n"
)


# ================================================================================================
# Fakes.
# ================================================================================================
class _FakeRunner:
    """An injectable ``run_command``-compatible async callable; records argv, returns canned stdout
    (no real ``whois`` binary, no subprocess, no network)."""

    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv: Any, *, timeout: float, **kwargs: Any) -> RunResult:
        self.calls.append({"argv": argv, "timeout": timeout})
        return RunResult(
            returncode=0, stdout=self._stdout, stderr=b"", timed_out=False, duration=0.0
        )


class _FakeLanding:
    """In-memory landing zone (no MinIO/S3)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        self.objects[key] = data
        return f"s3://landing/{key}"


class _FakeNeo4j:
    """A graph client placeholder; the gated paths under test never touch the graph."""

    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("the graph client must not be touched on the gated paths")

    def execute_write(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("the graph client must not be touched on the gated paths")


class _FakeVerifier:
    """Accepts the bearer token ``"good"`` (subject ``user-123``); rejects everything else."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


class _PassiveFake(Connector):
    """A minimal PASSIVE connector for the operator-run-now-without-a-token case."""

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="passive-fake",
            name="Passive Fake",
            version="0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        yield RawRecord(key="p0", data=b'{"n":1}', retrieved_at="2026-06-28T00:00:00Z")

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        entity = make_entity(
            {"id": record.key, "schema": "Company", "properties": {"name": ["Passive Co"]}}
        )
        return [stamp(entity, provenance)]


# ================================================================================================
# Builders.
# ================================================================================================
def _settings() -> Any:
    from worldmonitor.settings import Settings

    return Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-session-key-active",
        _env_file=None,  # type: ignore[call-arg]
    )


def _transient_instance(connector_id: str, settings: Any, *, instance_id: str, config: dict) -> Any:
    """A ConnectorInstance carrying an encrypted config — a plain (un-persisted) object so attribute
    reads never hit a detached-session error."""
    cipher = ConfigCipher.from_settings(settings)
    return ConnectorInstance(
        id=instance_id,
        connector_id=connector_id,
        config_encrypted=cipher.encrypt(json.dumps(config)),
        status="enabled",
    )


def _csrf_from(html: str) -> str:
    """Pull the hidden ``csrf_token`` value out of rendered HTML (attribute-order tolerant)."""
    for tag in re.findall(r"<input[^>]*>", html, flags=re.IGNORECASE):
        if 'name="csrf_token"' in tag:
            match = re.search(r'value="([^"]*)"', tag)
            if match and match.group(1):
                return match.group(1)
    raise AssertionError(f"no non-empty csrf_token input in HTML:\n{html[:1500]}")


def _fresh_database(postgres_dsn: str) -> str:
    """Create a uniquely-named empty database on the test server; return its DSN (mirrors
    tests/integration/test_migrations.py)."""
    url = make_url(postgres_dsn)
    name = f"active_gate_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


def _operator_rows(sessions: Any, instance_id: str) -> list[TaskRun]:
    with sessions() as session:
        return list(
            session.execute(
                select(TaskRun).where(
                    TaskRun.connector_instance_id == instance_id,
                    TaskRun.run_mode == "operator",
                )
            ).scalars()
        )


# ================================================================================================
# (a) CADENCE STILL REFUSES — frozen behaviour, the active tool is NEVER executed on the cadence.
# ================================================================================================
def test_cadence_still_refuses_active_whois(postgres_dsn: str) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    runner = _FakeRunner(_WHOIS_STDOUT)
    registry = Registry()
    registry.register(WhoisConnector(runner=runner))
    cipher = ConfigCipher(ConfigCipher.generate_key())
    driver = IngestDriver(
        sessions=sessions,
        landing=_FakeLanding(),  # type: ignore[arg-type]
        neo4j=_FakeNeo4j(),  # type: ignore[arg-type]
        registry=registry,
        cipher=cipher,
    )

    instance_id = str(uuid.uuid4())
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id=instance_id,
                connector_id="whois",
                config_encrypted=cipher.encrypt(json.dumps({"dataset": "d"})),
                status="enabled",
            )
        )
        session.commit()

    driver._ingest_instance(instance_id, now=_NOW)

    with sessions() as session:
        task = session.execute(
            select(TaskRun).where(TaskRun.connector_instance_id == instance_id)
        ).scalar_one()
        assert task.status == "error", "the cadence must record an error for an ACTIVE connector"
        assert "ACTIVE" in task.error, f"the refusal reason must be recorded: {task.error!r}"
        # The cadence MUST stay on the default run mode (it is never an operator-triggered run).
        assert task.run_mode == "cadence", (
            f"a cadence task_run must be run_mode=cadence, got {task.run_mode!r}"
        )

    # The headline: the cadence NEVER executes the active tool.
    assert runner.calls == [], "the active tool must never be executed on the cadence path"

    # Sanity: the refusal class is the one the driver still uses (frozen contract).
    assert issubclass(ActiveConnectorRefused, RuntimeError)
    engine.dispose()


# ================================================================================================
# (b) OPERATOR RUN — ACTIVE requires a scope; refuses without one and never runs the tool.
# ================================================================================================
def test_operator_run_active_without_scope_refuses(postgres_dsn: str) -> None:
    settings = _settings()
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    runner = _FakeRunner(_WHOIS_STDOUT)
    connector = WhoisConnector(runner=runner)
    instance = _transient_instance(
        "whois", settings, instance_id="whois-op-noscope", config={"dataset": "whois"}
    )

    with pytest.raises(ValueError):
        run_connector_once(
            instance,
            connector,
            scope=None,
            operator="op-alice",
            sessions=sessions,
            landing=_FakeLanding(),
            settings=settings,
        )

    assert runner.calls == [], "an ACTIVE run with no scope must NOT execute the tool"
    # No AUTHORIZED operator run was recorded (no minted scope token on any operator row).
    authorized = [r for r in _operator_rows(sessions, "whois-op-noscope") if r.scope_token]
    assert authorized == [], (
        "a refused ACTIVE run must not produce an authorized (token-bearing) audit row"
    )
    engine.dispose()


def test_operator_run_active_with_scope_mints_token_and_audits(postgres_dsn: str) -> None:
    settings = _settings()
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    runner = _FakeRunner(_WHOIS_STDOUT)
    connector = WhoisConnector(runner=runner)
    instance = _transient_instance(
        "whois", settings, instance_id="whois-op-ok", config={"dataset": "whois"}
    )

    run_connector_once(
        instance,
        connector,
        scope={"target": "example.com"},
        operator="op-alice",
        sessions=sessions,
        landing=_FakeLanding(),
        settings=settings,
    )

    # The tool ran with the EXACT, list-form argv (the validated target after the '--' terminator).
    assert len(runner.calls) == 1, runner.calls
    assert runner.calls[0]["argv"] == ["whois", "--", "example.com"], runner.calls[0]["argv"]

    rows = [r for r in _operator_rows(sessions, "whois-op-ok") if r.scope_token]
    assert len(rows) == 1, f"exactly one authorized operator run must be audited, got {len(rows)}"
    row = rows[0]
    assert row.run_mode == "operator"
    assert row.triggered_by == "op-alice"
    assert row.scope_token, "an ACTIVE operator run must store the minted scope token"

    # The stored token is a VALID, verifiable authorization bound to this connector + instance.
    claims = verify_scope_token(
        row.scope_token,
        expected_connector_id="whois",
        expected_instance_id="whois-op-ok",
        settings=settings,
    )
    assert claims["scope"] == {"target": "example.com"}, claims
    assert claims["operator"] == "op-alice", claims
    engine.dispose()


def test_operator_run_passive_needs_no_token(postgres_dsn: str) -> None:
    settings = _settings()
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    connector = _PassiveFake()
    instance = _transient_instance(
        "passive-fake", settings, instance_id="passive-op-ok", config={"dataset": "p"}
    )

    run_connector_once(
        instance,
        connector,
        scope=None,
        operator="op-bob",
        sessions=sessions,
        landing=_FakeLanding(),
        settings=settings,
    )

    with sessions() as session:
        row = session.execute(
            select(TaskRun).where(TaskRun.connector_instance_id == "passive-op-ok")
        ).scalar_one()
    assert row.run_mode == "operator", "an operator run-now must be run_mode=operator"
    assert row.triggered_by == "op-bob"
    assert row.scope_token is None, "a PASSIVE run-now must NOT mint/store a scope token"
    engine.dispose()


# ================================================================================================
# (f) REST /run — get_principal-gated + CSRF-protected; 422 for ACTIVE-no-scope; 303 + audit.
# ================================================================================================
def _build_rest(postgres_dsn: str) -> tuple[TestClient, Any, Any, _FakeRunner]:
    settings = _settings()
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    runner = _FakeRunner(_WHOIS_STDOUT)
    registry = Registry()
    registry.register(WhoisConnector(runner=runner))
    registry.register(_PassiveFake())
    app = create_app(
        settings=settings,
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
        oauth=None,
        db_sessions=sessions,  # type: ignore[call-arg]
        registry=registry,  # type: ignore[call-arg]
    )
    # The /run route reads its landing store from app.state.landing (the injectable test seam,
    # since create_app is out of this gate's blast radius and cannot gain a landing= kwarg).
    app.state.landing = _FakeLanding()
    return TestClient(app, raise_server_exceptions=False), settings, sessions, runner


def _add_whois_instance(sessions: Any, settings: Any, instance_id: str = "whois-rest") -> str:
    cipher = ConfigCipher.from_settings(settings)
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id=instance_id,
                connector_id="whois",
                config_encrypted=cipher.encrypt(json.dumps({"dataset": "whois"})),
                status="enabled",
            )
        )
        session.commit()
    return instance_id


def test_rest_run_requires_auth(postgres_dsn: str) -> None:
    client, settings, sessions, runner = _build_rest(postgres_dsn)
    iid = _add_whois_instance(sessions, settings)

    resp = client.post(
        f"/integrations/instances/{iid}/run",
        headers={"Accept": "application/json"},
        data={"scope": json.dumps({"target": "example.com"})},
        follow_redirects=False,
    )
    assert resp.status_code == 401, f"an unauthenticated /run must 401, got {resp.status_code}"
    assert runner.calls == [], "an unauthenticated request must never run the tool"
    assert _operator_rows(sessions, iid) == [], "no operator run may be recorded"


def test_rest_run_requires_csrf(postgres_dsn: str) -> None:
    client, settings, sessions, runner = _build_rest(postgres_dsn)
    iid = _add_whois_instance(sessions, settings)

    # Authed but NO csrf token -> 403 (an absent token must not match an absent session token).
    resp = client.post(
        f"/integrations/instances/{iid}/run",
        headers=AUTH,
        data={"scope": json.dumps({"target": "example.com"})},
        follow_redirects=False,
    )
    assert resp.status_code == 403, f"a CSRF-less /run must 403, got {resp.status_code}"
    assert runner.calls == [], "a CSRF-rejected request must never run the tool"
    assert _operator_rows(sessions, iid) == [], (
        "a CSRF-rejected request must record no operator run"
    )


def test_rest_run_active_without_scope_is_422(postgres_dsn: str) -> None:
    client, settings, sessions, runner = _build_rest(postgres_dsn)
    iid = _add_whois_instance(sessions, settings)
    csrf = _csrf_from(client.get("/integrations", headers=AUTH).text)

    resp = client.post(
        f"/integrations/instances/{iid}/run",
        headers=AUTH,
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 422, f"an ACTIVE run with no scope must 422, got {resp.status_code}"
    assert runner.calls == [], "a 422 (no-scope) ACTIVE run must never run the tool"


def test_rest_run_active_with_scope_is_303_and_audits_operator(postgres_dsn: str) -> None:
    client, settings, sessions, runner = _build_rest(postgres_dsn)
    iid = _add_whois_instance(sessions, settings)
    csrf = _csrf_from(client.get("/integrations", headers=AUTH).text)

    resp = client.post(
        f"/integrations/instances/{iid}/run",
        headers=AUTH,
        data={"csrf_token": csrf, "scope": json.dumps({"target": "example.com"})},
        follow_redirects=False,
    )
    assert resp.status_code == 303, (
        f"a valid authed+csrf+scope /run must 303, got {resp.status_code}: {resp.text}"
    )

    # The tool ran once with the exact list-form argv.
    assert len(runner.calls) == 1, runner.calls
    assert runner.calls[0]["argv"] == ["whois", "--", "example.com"], runner.calls[0]["argv"]

    rows = [r for r in _operator_rows(sessions, iid) if r.scope_token]
    assert len(rows) == 1, f"exactly one authorized operator run must be audited, got {len(rows)}"
    row = rows[0]
    # triggered_by is the AUTHENTICATED principal subject (the _FakeVerifier's "sub").
    assert row.triggered_by == "user-123", (
        f"triggered_by must be the principal subject, got {row.triggered_by!r}"
    )
    assert row.scope_token, "the audit row must carry the minted scope token"
    claims = verify_scope_token(
        row.scope_token,
        expected_connector_id="whois",
        expected_instance_id=iid,
        settings=settings,
    )
    assert claims["operator"] == "user-123", claims
    assert claims["scope"] == {"target": "example.com"}, claims


# ================================================================================================
# SEPARATE LOGGING — the scope-token plaintext + any config secret appear in NO log record; the
# distinct ACTIVE marker line carries connector/instance/operator.
# ================================================================================================
def test_active_operator_run_never_logs_token_or_secret(postgres_dsn: str) -> None:
    settings = _settings()
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    runner = _FakeRunner(_WHOIS_STDOUT)
    connector = WhoisConnector(runner=runner)
    cfg_secret = "LEAK_CFG_SECRET_8675309"  # pragma: allowlist secret
    instance = _transient_instance(
        "whois",
        settings,
        instance_id="whois-log",
        config={"dataset": "d", "api_token": cfg_secret},
    )
    operator = "operator-zaphod-RECOGNIZABLE"

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
    root.setLevel(logging.DEBUG)
    try:
        run_connector_once(
            instance,
            connector,
            scope={"target": "example.com"},
            operator=operator,
            sessions=sessions,
            landing=_FakeLanding(),
            settings=settings,
        )
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)

    # Recover the minted token from the audit column (the ONLY place it is allowed to live).
    with sessions() as session:
        row = session.execute(
            select(TaskRun).where(
                TaskRun.connector_instance_id == "whois-log",
                TaskRun.run_mode == "operator",
            )
        ).scalar_one()
    token = row.scope_token
    assert token, "the ACTIVE run must have stored a scope token to check against the logs"

    rendered: list[str] = []
    for record in handler.records:
        rendered.append(record.getMessage())
        with contextlib.suppress(Exception):  # defensive: a bad %-format must not abort the check
            rendered.append(handler.format(record))
    blob = "\n".join(rendered)

    assert token not in blob, "the scope-token plaintext leaked into a log record"
    assert cfg_secret not in blob, "a config secret leaked into a log record"

    # The distinct ACTIVE marker line is emitted and names the connector + operator (the audit
    # signal), without the token.
    marker_lines = [m for m in rendered if operator in m and "whois" in m]
    assert marker_lines, (
        "an ACTIVE run must emit a distinct log line naming the connector + operator"
    )
    engine.dispose()


# ================================================================================================
# (g) MIGRATION — 0008 adds the three audit columns; drift guard stays green (run separately).
# ================================================================================================
def test_migration_0008_adds_task_run_audit_columns(postgres_dsn: str) -> None:
    engine = make_engine(_fresh_database(postgres_dsn))
    migrate_to_head(engine)

    columns = {c["name"] for c in inspect(engine).get_columns("task_run")}
    assert {"run_mode", "triggered_by", "scope_token"} <= columns, (
        f"migration 0008 must add the task_run audit columns; have {sorted(columns)}"
    )
    engine.dispose()
