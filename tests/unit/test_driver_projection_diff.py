"""Unit tests for Gate 3a-ii-B — the driver's projection rebuild-and-diff guard (ADR 0102).

Docker-free: an ``IngestDriver`` wired with a real in-memory SQLite session factory (portable
ORM queries) + inert stand-ins for the live Neo4j client / landing store / config cipher (never
exercised on the paths under test here — dormancy, the misconfig fence, error isolation, and
cadence all resolve or fail BEFORE the live client, landing store, or cipher would ever be
touched).

Covers (spec §4 UNIT — driver guard dormancy / fence / error-isolation / cadence):
  * DORMANCY — disabled (default), and enabled-with-empty-URI, are both runtime no-ops: no
    ``Neo4jClient.connect``, no wipe, no fold; ``_latest_projection_divergence`` stays ``None``.
  * MISCONFIG FENCE — ``projection_diff_neo4j_uri == settings.neo4j_uri`` -> NO connect, NO wipe,
    an ERROR is logged, the stat stays ``None``; and the underlying ``_run_projection_diff`` call
    raises ``ProjectionDiffMisconfiguredError`` directly (D3).
  * ERROR ISOLATION — a diff-path failure (``Neo4jClient.connect`` raising) never aborts
    ``run_maintenance`` and never propagates; the stat stays ``None``; a maintenance effect that
    runs BEFORE the guard block (``prune_task_runs``) still executed (D10).
  * CADENCE — with the guard enabled + a benign stubbed diff path, a second ``run_maintenance``
    call inside the cadence window does NOT re-invoke it (D8).

RED at collection time: ``worldmonitor.runner.driver`` does not yet export
``_same_neo4j_target`` / ``ProjectionDiffMisconfiguredError``, and
``worldmonitor.resolution.divergence.ProjectionDivergence`` does not exist yet — the module-level
imports fail with ``ImportError``. That is the correct, intended TDD failure mode.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import worldmonitor.runner.driver as driver_module
from worldmonitor.db.models import Base, TaskRun
from worldmonitor.plugins.registry import Registry
from worldmonitor.resolution.divergence import ProjectionDivergence  # gate import: RED
from worldmonitor.runner.driver import IngestDriver
from worldmonitor.settings import Settings

# ---------------------------------------------------------------------------
# SQLite JSONB shim (idempotent if already registered by another test module)
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


_NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Inert stand-ins — never exercised on the paths under test in this file.
# ---------------------------------------------------------------------------


class _InertLanding:
    """Stand-in for ``LandingStore`` — never touched (``landing_gc_enabled`` defaults False, and
    ``run_maintenance``'s GC branch is the only caller)."""


class _InertCipher:
    """Stand-in for ``ConfigCipher`` — never touched by ``run_maintenance``."""


class _FailIfTouchedNeo4j:
    """Stand-in for the LIVE ``Neo4jClient`` — fails loudly if read/written.

    None of the scenarios in this file ever reach ``read_graph_snapshot(self._neo4j)`` (dormancy
    and the fence both short-circuit before it; error-isolation fails at ``Neo4jClient.connect``
    for the DIFF target before the live graph is ever read; cadence stubs
    ``_run_projection_diff`` wholesale) — so the live client must never be touched here.
    """

    def execute_read(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("the LIVE neo4j client must not be read in this test")

    def execute_write(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("the LIVE neo4j client must NEVER be written (INV-1)")

    def close(self) -> None:
        pass


def _sqlite_sessions() -> sessionmaker[Session]:
    """A real session factory over a single shared in-memory SQLite DB (Docker-free)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _build_driver(settings: Settings) -> IngestDriver:
    return IngestDriver(
        sessions=_sqlite_sessions(),
        landing=_InertLanding(),  # type: ignore[arg-type]
        neo4j=_FailIfTouchedNeo4j(),  # type: ignore[arg-type]
        registry=Registry(),
        cipher=_InertCipher(),  # type: ignore[arg-type]
        settings=settings,
    )


def _fail_if_called_connect(**_kwargs: Any) -> Any:
    raise AssertionError(
        "Neo4jClient.connect must not be called — the guard is dormant or the misconfig fence "
        "must have blocked it BEFORE any client construction (ADR 0102 D3)"
    )


# ===========================================================================
# DORMANCY (INV-4)
# ===========================================================================


def test_dormancy_default_settings_is_a_runtime_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver_module.Neo4jClient, "connect", _fail_if_called_connect)
    settings = Settings(neo4j_uri="bolt://live:7687")  # projection_diff_enabled defaults False
    driver = _build_driver(settings)

    driver.run_maintenance(now=_NOW)  # must not raise, must not touch Neo4jClient.connect

    assert driver._latest_projection_divergence is None


def test_dormancy_enabled_but_empty_diff_uri_is_a_runtime_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(driver_module.Neo4jClient, "connect", _fail_if_called_connect)
    settings = Settings(
        neo4j_uri="bolt://live:7687",
        projection_diff_enabled=True,
        projection_diff_neo4j_uri="",  # empty -> dormant no-op (D2), NOT a boot failure
    )
    driver = _build_driver(settings)

    driver.run_maintenance(now=_NOW)

    assert driver._latest_projection_divergence is None


# ===========================================================================
# MISCONFIG FENCE (INV-2 / D3 — the single most dangerous line)
# ===========================================================================


def test_misconfig_fence_blocks_wipe_when_diff_uri_equals_live_uri(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(driver_module.Neo4jClient, "connect", _fail_if_called_connect)
    settings = Settings(
        neo4j_uri="bolt://live:7687",
        projection_diff_enabled=True,
        projection_diff_neo4j_uri="bolt://live:7687",  # EXACT match -> must be fenced
        projection_diff_neo4j_user="neo4j",
        projection_diff_neo4j_password=SecretStr("x"),
    )
    assert (
        driver_module._same_neo4j_target(settings.neo4j_uri, settings.projection_diff_neo4j_uri)
        is True
    ), "test precondition: this configured pair must be recognised as the same target"

    driver = _build_driver(settings)

    # Direct call: the fence must raise its OWN exception type BEFORE any client/wipe/fold.
    with pytest.raises(driver_module.ProjectionDiffMisconfiguredError):
        driver._run_projection_diff(now=_NOW)

    # Through run_maintenance: the guard's own try/except (D10) swallows it; an ERROR is logged;
    # no stat is cached; Neo4jClient.connect is never reached.
    with caplog.at_level(logging.ERROR, logger="worldmonitor.runner.driver"):
        driver.run_maintenance(now=_NOW)  # must not raise

    assert driver._latest_projection_divergence is None
    assert any(r.levelno >= logging.ERROR for r in caplog.records), (
        "the misconfig fence must log an ERROR (ADR 0102 D3) when diff_uri == live_uri"
    )


# ===========================================================================
# ERROR ISOLATION (INV-7 / D10)
# ===========================================================================


def test_diff_target_failure_is_isolated_and_does_not_abort_maintenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_connect(**_kwargs: Any) -> Any:
        raise RuntimeError("diff target unreachable")

    monkeypatch.setattr(driver_module.Neo4jClient, "connect", _raise_connect)

    settings = Settings(
        neo4j_uri="bolt://live:7687",
        projection_diff_enabled=True,
        projection_diff_neo4j_uri="bolt://diff-host:7687",  # DISTINCT — the fence passes
        projection_diff_neo4j_user="neo4j",
        projection_diff_neo4j_password=SecretStr("x"),
    )
    driver = _build_driver(settings)

    # Seed one stale, FINISHED task_run row so a maintenance effect that runs BEFORE the guard
    # block (prune_task_runs) is observable — proving the guard failure did not abort the tick.
    stale_at = _NOW - timedelta(days=settings.task_run_retention_days + 1)
    with driver._sessions() as session:
        session.add(
            TaskRun(
                id="stale-1", kind="ingest", status="ok", started_at=stale_at, finished_at=stale_at
            )
        )
        session.commit()

    driver.run_maintenance(now=_NOW)  # must NOT raise, despite the diff-target failure

    assert driver._latest_projection_divergence is None, (
        "a failed diff run must NOT cache a divergence stat (D10: cache only on success)"
    )
    with driver._sessions() as session:
        remaining = session.get(TaskRun, "stale-1")
    assert remaining is None, (
        "prune_task_runs (which runs BEFORE the guard block) must still have executed — the diff "
        "target failure must be isolated to its own try/except and never abort the tick (D10)"
    )


# ===========================================================================
# IDENTITY HANDSHAKE (D3 gate 2 — the AUTHORITATIVE anti-alias check)
# ===========================================================================


class _CannedDbIdNeo4j:
    """A Neo4j stand-in that answers ``CALL db.info()`` with a canned id and records writes.

    Stands in for BOTH sides of the D3 identity handshake: the textual fence cannot see a
    DNS-alias/port-forward that reaches the live instance, but the database id is the same
    object on both connections — the handshake must refuse BEFORE any wipe.
    """

    def __init__(self, db_id: str | None, uri: str = "bolt://live:7687") -> None:
        self._db_id = db_id
        self.uri = uri
        self.writes: list[str] = []

    def execute_read(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        if "db.info" in query:
            if self._db_id is None:
                raise RuntimeError("db.info unavailable")
            return [{"id": self._db_id}]
        return []

    def execute_write(self, query: str, /, **_params: Any) -> list[dict[str, Any]]:
        self.writes.append(query)
        return []

    def close(self) -> None:
        pass


def _handshake_driver(
    monkeypatch: pytest.MonkeyPatch, *, live_id: str | None, diff_id: str | None
) -> tuple[IngestDriver, _CannedDbIdNeo4j]:
    """An enabled, fence-passing driver whose live/diff clients answer db.info with canned ids."""
    settings = Settings(
        neo4j_uri="bolt://live:7687",
        projection_diff_enabled=True,
        projection_diff_neo4j_uri="bolt://live-alias.internal:7687",  # textual fence PASSES
        projection_diff_neo4j_user="neo4j",
        projection_diff_neo4j_password=SecretStr("x"),
    )
    live = _CannedDbIdNeo4j(live_id, uri=settings.neo4j_uri)
    diff = _CannedDbIdNeo4j(diff_id, uri=settings.projection_diff_neo4j_uri)
    monkeypatch.setattr(driver_module.Neo4jClient, "connect", lambda **_kw: diff)
    driver = IngestDriver(
        sessions=_sqlite_sessions(),
        landing=_InertLanding(),  # type: ignore[arg-type]
        neo4j=live,  # type: ignore[arg-type]
        registry=Registry(),
        cipher=_InertCipher(),  # type: ignore[arg-type]
        settings=settings,
    )
    return driver, diff


def test_identity_handshake_refuses_equal_db_ids_before_any_wipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equal database ids = the diff URI reaches the LIVE database through an alias the textual
    fence cannot see (the CRITICAL adversarial-verify finding) — refuse BEFORE any wipe."""
    driver, diff = _handshake_driver(monkeypatch, live_id="db-uuid-LIVE", diff_id="db-uuid-LIVE")

    with pytest.raises(driver_module.ProjectionDiffMisconfiguredError):
        driver._run_projection_diff(now=_NOW)

    assert diff.writes == [], (
        "the identity handshake must refuse BEFORE the wipe — no execute_write may ever reach "
        "a target whose database id equals the live one (ADR 0102 D3 gate 2)"
    )
    driver.run_maintenance(now=_NOW)  # via the tick: swallowed by D10, no stat cached
    assert driver._latest_projection_divergence is None


def test_identity_handshake_fails_closed_when_id_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If EITHER side's database id cannot be read, distinctness is UNPROVEN — refuse (no wipe)."""
    driver, diff = _handshake_driver(monkeypatch, live_id=None, diff_id="db-uuid-DIFF")

    with pytest.raises(driver_module.ProjectionDiffMisconfiguredError):
        driver._run_projection_diff(now=_NOW)

    assert diff.writes == [], "an unproven identity must refuse the wipe (fail-closed, D3)"


# ===========================================================================
# CADENCE (D8)
# ===========================================================================


def test_guard_runs_at_most_once_per_cadence_window(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        neo4j_uri="bolt://live:7687",
        projection_diff_enabled=True,
        projection_diff_neo4j_uri="bolt://diff-host:7687",
        projection_diff_neo4j_user="neo4j",
        projection_diff_neo4j_password=SecretStr("x"),
        projection_diff_cadence_seconds=86400.0,  # default; far beyond this test's time deltas
    )
    driver = _build_driver(settings)

    canned = ProjectionDivergence(
        unexplained_nodes=0, unexplained_edges=0, live_nodes=3, live_edges=2, computed_at=_NOW
    )
    call_count = 0

    def _stub_run_projection_diff(*, now: datetime) -> ProjectionDivergence:
        nonlocal call_count
        call_count += 1
        return canned

    monkeypatch.setattr(driver, "_run_projection_diff", _stub_run_projection_diff)

    driver.run_maintenance(now=_NOW)
    assert call_count == 1
    assert driver._latest_projection_divergence == canned

    # A second tick well within the (86400s) cadence window must NOT re-invoke the diff.
    driver.run_maintenance(now=_NOW + timedelta(seconds=10))
    assert call_count == 1, (
        "a second run_maintenance call inside the cadence window must NOT re-run the diff (D8)"
    )
    assert driver._latest_projection_divergence == canned
