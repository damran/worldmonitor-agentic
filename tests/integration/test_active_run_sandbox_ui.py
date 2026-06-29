"""Gate 3h — sandbox gate + enforced allowlist + Run-UI: the operator oracle (ADR 0072).

End-to-end over a testcontainer Postgres + a FAKE ``run_command`` (NO real ``dig``/``nmap`` binary,
no network) + an in-memory landing + an injected registry, this file pins the 6b invariants the
slice adds on top of the proven 6a active-gate:

* SANDBOX GATE (ADR 0072 §1) — ``run_connector_once`` on a ``sandbox=="container"`` connector (nmap)
  with ``settings.container_sandbox_enabled`` False raises ``SandboxUnavailableError`` BEFORE any
  runner/landing (the heavy tool NEVER runs; nothing lands); a ``sandbox=="subprocess"`` connector
  (dig) runs fine. With the flag TRUE the gate lets nmap proceed (flag-conditioned, not a blanket
  refusal).
* REST SANDBOX 409 (ADR 0072 §1/§6) — ``POST /integrations/instances/{nmap_id}/run`` (authed +
  valid CSRF + scope) maps the refusal to **409**, records no successful run; the dig run -> 303.
* ALLOWLIST E2E (ADR 0072 §2) — a dig instance configured with ``allowed_targets=["allowed.com"]``
  refuses an out-of-list scope target with **422** (runner never invoked); the in-list target -> 303
  and actually runs the tool.
* UI RUN BUTTONS (ADR 0072 §6) — ``GET /integrations`` renders, for an ACTIVE instance, a Run-active
  ``<form action=".../run">`` with a ``target`` input + a hidden ``csrf_token``; for a PASSIVE
  instance a "Run now" control (no scope target). A ``/run`` POST with NO csrf -> 403.

LOCKED ASSUMPTIONS the builder MUST match so this oracle stays meaningful:

  ``worldmonitor.settings.Settings`` gains ``container_sandbox_enabled: bool = False``.
  ``worldmonitor.runner.operator_run`` gains ``SandboxUnavailableError`` (NOT a ``ValueError``
        subclass — the route maps ``ValueError``->422 but ``SandboxUnavailableError``->409);
        ``run_connector_once`` raises it BEFORE any runner/landing when ``connector.sandbox ==
        "container"`` and ``settings.container_sandbox_enabled`` is False.
  ``api/integrations.py`` ``POST /run`` maps ``SandboxUnavailableError``->409, and the allowlist
        ``ValueError``->422; ``GET /integrations`` annotates each instance with its connector
        capability (+ sandbox) so ``integrations.html`` renders the right Run control.
  Fake-runner registry: ``DigConnector(runner=...)`` (subprocess) + ``NmapConnector(runner=...)``
        (container) + a PASSIVE fake; the ``/run`` route reads landing from ``app.state.landing``.

RED on the base tree: ``worldmonitor.plugins.connectors.dig`` / ``...nmap`` and
``SandboxUnavailableError`` do not exist (ImportError at collection); ``Settings`` has no
``container_sandbox_enabled``; the template has no Run forms.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ConnectorInstance, TaskRun
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.plugins.base import Capability, Connector, Kind, Manifest, Mode, RawRecord
from worldmonitor.plugins.connectors.dig.connector import DigConnector
from worldmonitor.plugins.connectors.nmap.connector import NmapConnector
from worldmonitor.plugins.registry import Registry
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.runner.operator_run import SandboxUnavailableError, run_connector_once
from worldmonitor.runner.subprocess import RunResult

pytestmark = pytest.mark.integration

AUTH = {"Authorization": "Bearer good"}

_DIG_STDOUT = b"93.184.216.34\n"
_NMAP_STDOUT = b'<?xml version="1.0"?><nmaprun></nmaprun>'


# ================================================================================================
# Fakes.
# ================================================================================================
class _FakeRunner:
    """An injectable ``run_command``-compatible async callable; records argv, returns canned stdout
    (no real binary, no subprocess, no network)."""

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
    """A minimal PASSIVE connector — the UI renders it a 'Run now' control (no scope target)."""

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
# Builders / helpers.
# ================================================================================================
def _settings(
    *,
    container_sandbox_enabled: bool = False,
    sandbox_runner_url: str = "",
    sandbox_runner_secret: str = "",
) -> Any:
    from worldmonitor.settings import Settings

    return Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-session-sandbox-ui",
        container_sandbox_enabled=container_sandbox_enabled,  # type: ignore[call-arg]
        sandbox_runner_url=sandbox_runner_url,  # type: ignore[call-arg]
        sandbox_runner_secret=sandbox_runner_secret,  # type: ignore[call-arg]
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


def _csrf_from(html: str) -> str:
    """Pull the hidden ``csrf_token`` value out of rendered HTML (attribute-order tolerant)."""
    for tag in re.findall(r"<input[^>]*>", html, flags=re.IGNORECASE):
        if 'name="csrf_token"' in tag:
            match = re.search(r'value="([^"]*)"', tag)
            if match and match.group(1):
                return match.group(1)
    raise AssertionError(f"no non-empty csrf_token input in HTML:\n{html[:1500]}")


def _form_block(html: str, action: str) -> str | None:
    """Return the ``<form ...>...</form>`` block whose ``action`` equals ``action``, or None."""
    pattern = re.compile(
        r'<form[^>]*action="' + re.escape(action) + r'"[^>]*>.*?</form>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(html)
    return match.group(0) if match else None


def _build_rest(
    postgres_dsn: str, *, container_sandbox_enabled: bool = False
) -> tuple[TestClient, Any, Any, _FakeRunner, _FakeRunner]:
    """Wire ``create_app`` over the testcontainer Postgres + a fake-runner registry (dig +
    nmap + passive); return ``(client, settings, sessions, dig_runner, nmap_runner)``."""
    settings = _settings(container_sandbox_enabled=container_sandbox_enabled)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    dig_runner = _FakeRunner(_DIG_STDOUT)
    nmap_runner = _FakeRunner(_NMAP_STDOUT)
    registry = Registry()
    registry.register(DigConnector(runner=dig_runner))
    registry.register(NmapConnector(runner=nmap_runner))
    registry.register(_PassiveFake())
    app = create_app(
        settings=settings,
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=_FakeNeo4j(),  # type: ignore[arg-type]
        oauth=None,
        db_sessions=sessions,  # type: ignore[call-arg]
        registry=registry,  # type: ignore[call-arg]
    )
    app.state.landing = _FakeLanding()
    return (
        TestClient(app, raise_server_exceptions=False),
        settings,
        sessions,
        dig_runner,
        nmap_runner,
    )


def _add_instance(
    sessions: Any, settings: Any, *, connector_id: str, instance_id: str, config: dict
) -> str:
    cipher = ConfigCipher.from_settings(settings)
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id=instance_id,
                connector_id=connector_id,
                config_encrypted=cipher.encrypt(json.dumps(config)),
                status="enabled",
            )
        )
        session.commit()
    return instance_id


# ================================================================================================
# SANDBOX GATE (direct) — the container connector is refused before any runner/landing.
# ================================================================================================
def test_sandbox_gate_refuses_container_connector(postgres_dsn: str) -> None:
    settings = _settings(container_sandbox_enabled=False)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    nmap_runner = _FakeRunner(_NMAP_STDOUT)
    connector = NmapConnector(runner=nmap_runner)
    landing = _FakeLanding()
    instance = _transient_instance(
        "nmap", settings, instance_id="nmap-gate", config={"dataset": "nmap"}
    )

    with pytest.raises(SandboxUnavailableError):
        run_connector_once(
            instance,
            connector,
            scope={"target": "example.com"},
            operator="op-alice",
            sessions=sessions,
            landing=landing,
            settings=settings,
        )

    # The heavy tool NEVER ran and NOTHING landed — the refusal is before any runner/landing.
    assert nmap_runner.calls == [], "a container-gated tool must NOT reach the runner"
    assert landing.objects == {}, "a refused container run must land nothing"
    oks = [r for r in _operator_rows(sessions, "nmap-gate") if r.status == "ok"]
    assert oks == [], "a refused container run must record no successful run"
    engine.dispose()


def test_sandbox_gate_subprocess_connector_runs(postgres_dsn: str) -> None:
    settings = _settings(container_sandbox_enabled=False)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    dig_runner = _FakeRunner(_DIG_STDOUT)
    connector = DigConnector(runner=dig_runner)
    instance = _transient_instance(
        "dig", settings, instance_id="dig-gate-ok", config={"dataset": "dig"}
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

    # The subprocess tool ran (the container gate does not touch it) and was audited.
    assert len(dig_runner.calls) == 1, dig_runner.calls
    assert dig_runner.calls[0]["argv"][0] == "dig", dig_runner.calls[0]["argv"]
    authorized = [r for r in _operator_rows(sessions, "dig-gate-ok") if r.scope_token]
    assert len(authorized) == 1, "a subprocess ACTIVE run must produce one authorized audit row"
    engine.dispose()


def test_sandbox_gate_is_flag_conditioned(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``container_sandbox_enabled`` TRUE *and* a configured sandbox-runner (ADR 0077 §D3) the
    gate lets the container connector proceed — the refusal is conditioned on the flag (+config),
    not a blanket always-refuse.

    ADR 0077 supersedes the ADR-0072 "flag alone ⇒ proceed on the HOST runner": the flag now needs
    a configured sidecar (``sandbox_runner_url`` + secret), and an enabled+configured run ROUTES
    through the sidecar ``ContainerRunner`` built by ``make_container_runner``. We patch that
    factory to return the recording runner so the connector proceeds to a runner (no real HTTP) and
    the proceed-not-refuse assertion below holds at the same strength."""
    settings = _settings(
        container_sandbox_enabled=True,
        sandbox_runner_url="http://sandbox-runner:9000",
        sandbox_runner_secret="sidecar-shared-secret",
    )
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    nmap_runner = _FakeRunner(_NMAP_STDOUT)
    connector = NmapConnector(runner=nmap_runner)
    monkeypatch.setattr(
        "worldmonitor.runner.operator_run.make_container_runner",
        lambda *args, **kwargs: nmap_runner,
        raising=False,
    )
    instance = _transient_instance(
        "nmap", settings, instance_id="nmap-enabled", config={"dataset": "nmap"}
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

    assert len(nmap_runner.calls) == 1, (
        "with the sandbox flag enabled AND configured the container connector must proceed to the "
        "(sidecar) runner"
    )
    engine.dispose()


# ================================================================================================
# REST — nmap run -> 409 (sandbox refusal); dig run -> 303.
# ================================================================================================
def test_rest_run_nmap_is_409_and_dig_is_303(postgres_dsn: str) -> None:
    client, settings, sessions, dig_runner, nmap_runner = _build_rest(postgres_dsn)
    nmap_id = _add_instance(
        sessions, settings, connector_id="nmap", instance_id="nmap-rest", config={"dataset": "nmap"}
    )
    dig_id = _add_instance(
        sessions, settings, connector_id="dig", instance_id="dig-rest", config={"dataset": "dig"}
    )
    csrf = _csrf_from(client.get("/integrations", headers=AUTH).text)

    nmap_resp = client.post(
        f"/integrations/instances/{nmap_id}/run",
        headers=AUTH,
        data={"csrf_token": csrf, "scope": json.dumps({"target": "example.com"})},
        follow_redirects=False,
    )
    assert nmap_resp.status_code == 409, (
        f"a container-gated nmap run must 409 (sandbox refusal), got {nmap_resp.status_code}: "
        f"{nmap_resp.text}"
    )
    assert nmap_runner.calls == [], "a 409 sandbox refusal must never run the tool"
    nmap_oks = [r for r in _operator_rows(sessions, nmap_id) if r.status == "ok"]
    assert nmap_oks == [], "a refused nmap run must record no successful run"

    dig_resp = client.post(
        f"/integrations/instances/{dig_id}/run",
        headers=AUTH,
        data={"csrf_token": csrf, "scope": json.dumps({"target": "example.com"})},
        follow_redirects=False,
    )
    assert dig_resp.status_code == 303, (
        f"a subprocess dig run must 303, got {dig_resp.status_code}: {dig_resp.text}"
    )
    assert len(dig_runner.calls) == 1, "the dig (subprocess) run must execute the tool once"


# ================================================================================================
# REST — enforced allowlist: an out-of-list target -> 422 (before the runner); in-list -> 303.
# ================================================================================================
def test_rest_allowlist_blocks_out_of_list_target(postgres_dsn: str) -> None:
    client, settings, sessions, dig_runner, _nmap = _build_rest(postgres_dsn)
    dig_id = _add_instance(
        sessions,
        settings,
        connector_id="dig",
        instance_id="dig-allow",
        config={"allowed_targets": ["allowed.com"]},
    )
    csrf = _csrf_from(client.get("/integrations", headers=AUTH).text)

    blocked = client.post(
        f"/integrations/instances/{dig_id}/run",
        headers=AUTH,
        data={"csrf_token": csrf, "scope": json.dumps({"target": "blocked.com"})},
        follow_redirects=False,
    )
    assert blocked.status_code == 422, (
        f"an out-of-allowlist target must be refused with 422, got {blocked.status_code}: "
        f"{blocked.text}"
    )
    assert dig_runner.calls == [], "an out-of-allowlist target must NOT reach the runner"

    allowed = client.post(
        f"/integrations/instances/{dig_id}/run",
        headers=AUTH,
        data={"csrf_token": csrf, "scope": json.dumps({"target": "allowed.com"})},
        follow_redirects=False,
    )
    assert allowed.status_code == 303, (
        f"an in-allowlist target must run (303), got {allowed.status_code}: {allowed.text}"
    )
    assert len(dig_runner.calls) == 1, "the in-allowlist target must actually run the tool"


# ================================================================================================
# UI — ACTIVE renders a Run-active form (target + csrf); PASSIVE renders a 'Run now' control.
# ================================================================================================
def test_ui_renders_run_controls_per_capability(postgres_dsn: str) -> None:
    client, settings, sessions, _dig, _nmap = _build_rest(postgres_dsn)
    dig_id = _add_instance(
        sessions, settings, connector_id="dig", instance_id="dig-ui", config={"dataset": "dig"}
    )
    passive_id = _add_instance(
        sessions, settings, connector_id="passive-fake", instance_id="passive-ui", config={}
    )

    body = client.get("/integrations", headers=AUTH).text

    # ACTIVE (dig): a Run-active form posting to this instance's /run, with a target input + a
    # hidden csrf_token field.
    active_form = _form_block(body, f"/integrations/instances/{dig_id}/run")
    assert active_form is not None, (
        f"an ACTIVE instance must render a Run form posting to /integrations/instances/{dig_id}/run"
    )
    assert 'name="target"' in active_form, (
        f"the ACTIVE Run form must carry a scope target input:\n{active_form}"
    )
    assert 'name="csrf_token"' in active_form, (
        f"the ACTIVE Run form must carry a hidden csrf_token:\n{active_form}"
    )

    # PASSIVE: a 'Run now' control posting to this instance's /run, with NO scope target input.
    passive_form = _form_block(body, f"/integrations/instances/{passive_id}/run")
    assert passive_form is not None, (
        "a PASSIVE instance must render a Run-now form posting to "
        f"/integrations/instances/{passive_id}/run"
    )
    assert "Run now" in passive_form, f"the PASSIVE control must read 'Run now':\n{passive_form}"
    assert 'name="target"' not in passive_form, (
        f"the PASSIVE Run-now control must NOT carry a scope target input:\n{passive_form}"
    )


def test_ui_run_without_csrf_is_403(postgres_dsn: str) -> None:
    client, settings, sessions, dig_runner, _nmap = _build_rest(postgres_dsn)
    dig_id = _add_instance(
        sessions, settings, connector_id="dig", instance_id="dig-nocsrf", config={"dataset": "dig"}
    )

    # A /run POST with NO csrf_token (absent token must not match an absent session token) -> 403.
    resp = client.post(
        f"/integrations/instances/{dig_id}/run",
        headers=AUTH,
        data={"target": "example.com"},
        follow_redirects=False,
    )
    assert resp.status_code == 403, f"a CSRF-less /run must 403, got {resp.status_code}"
    assert dig_runner.calls == [], "a CSRF-rejected /run must never run the tool"
