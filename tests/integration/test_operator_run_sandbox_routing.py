"""Integration RED tests for operator-run → sandbox-runner ROUTING — ADR 0077 Slice 1.

End-to-end over a testcontainer Postgres + an in-memory landing + FAKE runners (no real
``dig``/``nmap`` binary, no HTTP, no container), this file pins the ``gate.scope`` routing
invariants
the slice adds on top of the proven ADR-0072 sandbox refusal. It is the sibling of
``test_active_run_sandbox_ui.py`` (whose nmap-refused-when-flag-off assertion stays GREEN and is NOT
touched) — these are ADDED, refusal-or-route tests:

* INV-1 (flag off ⇒ refuse, UNCHANGED): a ``sandbox=="container"`` connector (nmap) with
  ``container_sandbox_enabled=False`` raises ``SandboxUnavailableError`` BEFORE any runner/landing.
* INV-2 (enabled but unconfigured ⇒ STILL refuse): the flag is True but ``sandbox_runner_url`` (or
  the secret) is empty ⇒ ``SandboxUnavailableError`` — nmap NEVER runs un-sandboxed, and the
  ContainerRunner is NEVER built (``make_container_runner`` is a boom-factory that would fail if
  called before the refusal).
* INV-3 (enabled + configured ⇒ ROUTE): nmap executes via the sidecar ``ContainerRunner`` (the
  monkeypatched ``make_container_runner`` returns a recording fake runner), NOT the host runner /
  ``run_command``; the run lands a raw record + records an ``ok`` task_run. A subprocess tool
  (dig) is UNAFFECTED — it keeps its host ``run_command`` path and never touches the container
  runner.

ASSUMED SEAM (the builder must match): the routing in ``run_connector_once`` obtains the runner via
``make_container_runner(settings)`` and injects it onto the connector so ``collect()`` uses it. We
monkeypatch ``make_container_runner`` at BOTH import sites it could be referenced from
(``worldmonitor.sandbox.container_runner.make_container_runner`` and
``worldmonitor.runner.operator_run.make_container_runner``) so either import style is intercepted.

RED today: ``worldmonitor.sandbox.container_runner`` does not exist (ModuleNotFoundError at the
top-level import ⇒ collection error) AND the routing does not exist (INV-2/INV-3 would
refuse/host-run
even once the module lands). GREEN once the builder lands the settings + module + routing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ConnectorInstance, TaskRun
from worldmonitor.plugins.connectors.dig.connector import DigConnector
from worldmonitor.plugins.connectors.nmap.connector import NmapConnector
from worldmonitor.runner.operator_run import SandboxUnavailableError, run_connector_once
from worldmonitor.runner.subprocess import RunResult

# Top-level import of the not-yet-built module — ModuleNotFoundError today (correct RED). Imported
# (not just monkeypatched by string) so the RED is a clean collection error, not a silent skip.
from worldmonitor.sandbox.container_runner import make_container_runner  # noqa: F401
from worldmonitor.settings import Settings

pytestmark = pytest.mark.integration

_NMAP_STDOUT = b'<?xml version="1.0"?><nmaprun></nmaprun>'
_DIG_STDOUT = b"93.184.216.34\n"
_URL = "http://sandbox-runner:9000"
_SECRET = "sidecar-shared-secret"


# ================================================================================================
# Fakes / helpers.
# ================================================================================================
class _RecordingRunner:
    """A fake ``run_command``-compatible async runner: records argv+timeout, returns canned stdout
    (no real binary, no subprocess, no HTTP)."""

    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, argv: Any, *, timeout: float, **kwargs: Any) -> RunResult:
        self.calls.append({"argv": argv, "timeout": timeout})
        return RunResult(
            returncode=0, stdout=self._stdout, stderr=b"", timed_out=False, duration=0.0
        )


class _RecordingFactory:
    """A fake ``make_container_runner``: records the settings it was handed, returns a fixed
    recording Runner (so the test can assert the sidecar path actually ran)."""

    def __init__(self, runner: _RecordingRunner) -> None:
        self.runner = runner
        self.settings_seen: list[Any] = []

    def __call__(self, settings: Any, *args: Any, **kwargs: Any) -> _RecordingRunner:
        self.settings_seen.append(settings)
        return self.runner


def _boom_factory(*args: Any, **kwargs: Any) -> Any:
    """A ``make_container_runner`` stand-in that MUST never be called on its path."""
    raise AssertionError("make_container_runner must NOT be called on this path")


class _FakeLanding:
    """In-memory landing zone (no MinIO/S3)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        self.objects[key] = data
        return f"s3://landing/{key}"


def _settings(*, container_sandbox_enabled: bool, url: str = "", secret: str = "") -> Settings:
    return Settings(
        environment="test",
        config_encryption_key=Fernet.generate_key().decode(),
        session_secret_key="test-sandbox-routing",
        container_sandbox_enabled=container_sandbox_enabled,
        sandbox_runner_url=url,
        sandbox_runner_secret=secret,
        _env_file=None,
    )  # type: ignore[call-arg]


def _transient_instance(
    connector_id: str, settings: Settings, *, instance_id: str, config: dict[str, Any]
) -> ConnectorInstance:
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


def _patch_factory(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    """Patch ``make_container_runner`` wherever the builder may reference it (both styles)."""
    monkeypatch.setattr(
        "worldmonitor.sandbox.container_runner.make_container_runner", factory, raising=False
    )
    monkeypatch.setattr(
        "worldmonitor.runner.operator_run.make_container_runner", factory, raising=False
    )


# ================================================================================================
# INV-1 — flag off ⇒ refuse (UNCHANGED; mirrors the frozen ADR-0072 assertion).
# ================================================================================================
def test_inv1_container_connector_refused_when_flag_off(postgres_dsn: str) -> None:
    settings = _settings(container_sandbox_enabled=False)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    host_runner = _RecordingRunner(_NMAP_STDOUT)
    connector = NmapConnector(runner=host_runner)
    landing = _FakeLanding()
    instance = _transient_instance(
        "nmap", settings, instance_id="nmap-inv1", config={"dataset": "nmap"}
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

    assert host_runner.calls == [], "flag off ⇒ the heavy tool must NOT run (INV-1)"
    assert landing.objects == {}, "a refused container run lands nothing (INV-1)"
    assert [r for r in _operator_rows(sessions, "nmap-inv1") if r.status == "ok"] == []
    engine.dispose()


# ================================================================================================
# INV-2 — enabled but unconfigured ⇒ STILL refuse (url OR secret empty); never builds a runner.
# ================================================================================================
def test_inv2_refused_when_enabled_but_url_empty(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(container_sandbox_enabled=True, url="", secret=_SECRET)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    host_runner = _RecordingRunner(_NMAP_STDOUT)
    connector = NmapConnector(runner=host_runner)
    landing = _FakeLanding()
    # If routing tried to build the runner before checking config, this would raise AssertionError
    # (NOT SandboxUnavailableError) and fail the test — proving the refusal precedes construction.
    _patch_factory(monkeypatch, _boom_factory)
    instance = _transient_instance(
        "nmap", settings, instance_id="nmap-inv2a", config={"dataset": "nmap"}
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

    assert host_runner.calls == [], "enabled-but-no-url ⇒ nmap must NEVER run un-sandboxed (INV-2)"
    assert landing.objects == {}
    engine.dispose()


def test_inv2_refused_when_enabled_but_secret_empty(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(container_sandbox_enabled=True, url=_URL, secret="")
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    host_runner = _RecordingRunner(_NMAP_STDOUT)
    connector = NmapConnector(runner=host_runner)
    landing = _FakeLanding()
    _patch_factory(monkeypatch, _boom_factory)
    instance = _transient_instance(
        "nmap", settings, instance_id="nmap-inv2b", config={"dataset": "nmap"}
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

    assert host_runner.calls == [], "enabled-but-no-secret ⇒ nmap must NEVER run un-sandboxed"
    assert landing.objects == {}
    engine.dispose()


# ================================================================================================
# INV-3 — enabled + configured ⇒ ROUTE the container connector through the sidecar ContainerRunner.
# ================================================================================================
def test_inv3_container_connector_routes_through_sidecar(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(container_sandbox_enabled=True, url=_URL, secret=_SECRET)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    # The ORIGINALLY-injected runner is the HOST path; routing must REPLACE it with the sidecar one.
    host_runner = _RecordingRunner(_NMAP_STDOUT)
    connector = NmapConnector(runner=host_runner)
    landing = _FakeLanding()
    sidecar_runner = _RecordingRunner(_NMAP_STDOUT)
    factory = _RecordingFactory(sidecar_runner)
    _patch_factory(monkeypatch, factory)
    instance = _transient_instance(
        "nmap", settings, instance_id="nmap-inv3", config={"dataset": "nmap"}
    )

    task_id = run_connector_once(
        instance,
        connector,
        scope={"target": "example.com"},
        operator="op-alice",
        sessions=sessions,
        landing=landing,
        settings=settings,
    )

    # Routed through the sidecar ContainerRunner — NOT the host runner / run_command (INV-3).
    assert len(sidecar_runner.calls) == 1, (
        "a configured container connector must execute via the sidecar ContainerRunner (INV-3)"
    )
    assert sidecar_runner.calls[0]["argv"] == ["nmap", "-oX", "-", "--", "example.com"], (
        "argv must stay a LIST end-to-end (INV-6)"
    )
    assert host_runner.calls == [], (
        "the sidecar path must REPLACE the host runner (no run_command on the host) (INV-3)"
    )
    assert factory.settings_seen, (
        "the ContainerRunner must be built via make_container_runner(settings)"
    )
    # A real run landed the raw record and recorded an ok task_run.
    assert landing.objects != {}, "a routed container run must land the raw record"
    oks = [r for r in _operator_rows(sessions, "nmap-inv3") if r.status == "ok"]
    assert len(oks) == 1, "a routed container run must record exactly one ok task_run"
    assert task_id
    engine.dispose()


def test_inv3_subprocess_tool_is_unaffected(
    postgres_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(container_sandbox_enabled=True, url=_URL, secret=_SECRET)
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    dig_runner = _RecordingRunner(_DIG_STDOUT)
    connector = DigConnector(runner=dig_runner)
    landing = _FakeLanding()
    # A subprocess tool must NEVER route through the container runner — boom if it does.
    _patch_factory(monkeypatch, _boom_factory)
    instance = _transient_instance(
        "dig", settings, instance_id="dig-inv3", config={"dataset": "dig"}
    )

    run_connector_once(
        instance,
        connector,
        scope={"target": "example.com"},
        operator="op-alice",
        sessions=sessions,
        landing=landing,
        settings=settings,
    )

    assert len(dig_runner.calls) == 1, "a subprocess tool keeps its host run_command path (INV-3)"
    assert dig_runner.calls[0]["argv"][0] == "dig"
    assert landing.objects != {}, "the subprocess run still lands its raw record"
    engine.dispose()
