"""GET /sources/freshness — the REST surface (Gate F-1 slice 1, ADR 0123 D3).

Spec `docs/reviews/GATE_F1_FRESHNESS_SURFACE_SPEC.md` §4.2/§6.2/AC-4/AC-6. Auth-gated
(`get_principal`) + DB-backed (`get_db`), read-only, Postgres-only (never touches Neo4j — freshness
lives entirely in `ConnectorInstance` + `task_run`, spec §3.6). Uses a REAL in-memory SQLite
session (mirrors `tests/unit/test_review_ui.py` / `tests/unit/test_integrations_ui.py`'s
`create_app(..., db_sessions=)` injection seam) and an injected FAKE Neo4j client that raises on
ANY call, proving the route never reads/writes the graph.

RED at collection is NOT expected here (no top-level import of the not-yet-existing
`worldmonitor.api.freshness` module — the route is exercised purely over HTTP, mirroring
`tests/unit/test_api_graph.py`'s dossier-route convention, so a missing route surfaces as a
per-test 404/response-shape failure, not a collection error). Every 200-shape assertion below
pins the route's OWN body (envelope keys + values), which a bare "route not found" 404 cannot
produce — so a vacuous pass is not possible. The one exception is auth-gating without a bearer:
`AuthMiddleware` 401s ANY non-public path before routing even resolves whether the route exists,
so `test_freshness_requires_auth_401` is expected to ALREADY be green pre-gate (see its docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.db.models import Base, ConnectorInstance, TaskRun
from worldmonitor.settings import Settings

AUTH = {"Authorization": "Bearer good"}

# Forbidden top-level/nested JSON keys — INV-7 opaque-ids-only (spec §3.3): no connector
# config/secret/URL/dataset/name/person field may ever appear in the response.
_FORBIDDEN_KEYS = {
    "config",
    "config_encrypted",
    "secret",
    "secrets",
    "url",
    "api_token",
    "api_key",
    "token",
    "password",
    "name",
    "dataset",
    "description",
}

_LEAKED_SECRET = "SUPER_SECRET_CONNECTOR_CONFIG_BLOB_MUST_NOT_LEAK"  # pragma: allowlist secret


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _sqlite_sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


class _FakeVerifier:
    def verify(self, token: str) -> dict[str, str]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123"}


class _RaisingNeo4j:
    """Freshness is Postgres-only (spec §3.6) — ANY graph call from this route is a bug."""

    def execute_read(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("GET /sources/freshness must never read from Neo4j")

    def execute_write(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:  # pragma: no cover
        raise AssertionError("GET /sources/freshness must never write (read-only, AC-6)")


def _client(sessions: sessionmaker[Session]) -> TestClient:
    app = create_app(
        settings=Settings(environment="test"),
        verifier=_FakeVerifier(),  # type: ignore[arg-type]
        neo4j_client=_RaisingNeo4j(),  # type: ignore[arg-type]
        db_sessions=sessions,
    )
    return TestClient(app, raise_server_exceptions=False)


def _collect_keys(obj: Any) -> set[str]:
    """Recursively collect every dict key in a decoded JSON structure."""
    keys: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _collect_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _collect_keys(item)
    return keys


# ================================================================================================
# Auth gating.
# ================================================================================================
def test_freshness_requires_auth_401() -> None:
    """No bearer -> 401. NOTE: `AuthMiddleware` gates every non-public path BEFORE the router
    even resolves whether `/sources/freshness` exists, so this assertion already holds on the
    base tree today (a route-not-found path is never reached without auth either) — it is listed
    as a named AC (§6.2) and kept as a permanent regression pin, not a RED-until-implemented case.
    """
    sessions = _sqlite_sessions()
    resp = _client(sessions).get("/sources/freshness")
    assert resp.status_code == 401


def test_freshness_rejects_bad_bearer_401() -> None:
    sessions = _sqlite_sessions()
    resp = _client(sessions).get("/sources/freshness", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


# ================================================================================================
# 200 shape + derived values (AC-4).
# ================================================================================================
def test_freshness_returns_sources_and_summary() -> None:
    sessions = _sqlite_sessions()
    now_ref = datetime.now(UTC)
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-fresh", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-stale", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-off", connector_id="feeds", config_encrypted="x", status="disabled"
                ),
                TaskRun(
                    id="t-fresh",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-fresh",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(minutes=2),
                ),
                TaskRun(
                    id="t-stale",
                    kind="ingest",
                    status="ok",
                    connector_instance_id="ci-stale",
                    started_at=now_ref,
                    finished_at=now_ref - timedelta(hours=6),
                ),
            ]
        )
        session.commit()

    resp = _client(sessions).get("/sources/freshness", headers=AUTH)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()

    assert set(body.keys()) == {"generated_at", "budget", "summary", "sources"}, (
        f"top-level envelope must be exactly the 4 spec §4.2 keys; got {list(body.keys())}"
    )
    assert isinstance(body["generated_at"], str) and body["generated_at"]

    assert body["budget"] == {"stale_after_seconds": 14400, "very_stale_after_seconds": 86400}, (
        f"budget must reflect the shipped defaults: {body['budget']}"
    )

    by_id = {row["instance_id"]: row for row in body["sources"]}
    assert by_id["ci-fresh"]["freshness_status"] == "fresh"
    assert by_id["ci-stale"]["freshness_status"] == "stale"
    assert by_id["ci-off"]["freshness_status"] == "disabled"
    assert by_id["ci-off"]["status"] == "disabled"
    assert by_id["ci-fresh"]["age_seconds"] is not None
    assert by_id["ci-off"]["age_seconds"] is None
    assert by_id["ci-off"]["last_success_at"] is None

    summary = body["summary"]
    assert summary["fresh"] == 1
    assert summary["stale"] == 1
    assert summary["disabled"] == 1
    assert summary["very_stale"] == 0
    assert summary["no_data"] == 0
    assert summary["error"] == 0
    assert summary["total"] == 3


def test_freshness_source_envelope_exact_keys() -> None:
    """Regression-lock (mirrors test_api_graph.py's G3 style): the per-source shape is EXACTLY
    the spec §4.2 7 keys — no more, no less."""
    sessions = _sqlite_sessions()
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id="ci-a", connector_id="feeds", config_encrypted="x", status="enabled"
            )
        )
        session.commit()

    resp = _client(sessions).get("/sources/freshness", headers=AUTH)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    sources = resp.json()["sources"]
    assert len(sources) == 1
    assert set(sources[0].keys()) == {
        "instance_id",
        "connector_id",
        "status",
        "freshness_status",
        "last_run",
        "last_success_at",
        "age_seconds",
    }, f"per-source envelope drifted from spec §4.2: {list(sources[0].keys())}"

    budget = resp.json()["budget"]
    assert set(budget.keys()) == {"stale_after_seconds", "very_stale_after_seconds"}


def test_freshness_summary_counts_sum_to_total() -> None:
    sessions = _sqlite_sessions()
    with sessions() as session:
        session.add_all(
            [
                ConnectorInstance(
                    id="ci-1", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
                ConnectorInstance(
                    id="ci-2", connector_id="feeds", config_encrypted="x", status="disabled"
                ),
                ConnectorInstance(
                    id="ci-3", connector_id="feeds", config_encrypted="x", status="error"
                ),
                ConnectorInstance(
                    id="ci-4", connector_id="feeds", config_encrypted="x", status="enabled"
                ),
            ]
        )
        session.commit()

    resp = _client(sessions).get("/sources/freshness", headers=AUTH)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    summary = body["summary"]
    state_keys = ("fresh", "stale", "very_stale", "no_data", "error", "disabled")
    assert sum(summary[k] for k in state_keys) == summary["total"]
    assert summary["total"] == len(body["sources"]) == 4


def test_freshness_empty_when_no_instances() -> None:
    sessions = _sqlite_sessions()
    resp = _client(sessions).get("/sources/freshness", headers=AUTH)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["sources"] == []
    assert body["summary"] == {
        "fresh": 0,
        "stale": 0,
        "very_stale": 0,
        "no_data": 0,
        "error": 0,
        "disabled": 0,
        "total": 0,
    }


# ================================================================================================
# INV-7 — opaque ids only (spec §3.3 / AC-4). Non-vacuous: seeds a REAL secret value and asserts
# it never appears anywhere in the response, plus a forbidden-key sweep over the decoded body.
# ================================================================================================
def test_freshness_exposes_only_opaque_ids() -> None:
    sessions = _sqlite_sessions()
    with sessions() as session:
        session.add(
            ConnectorInstance(
                id="ci-secret-bearer",
                connector_id="feeds",
                config_encrypted=_LEAKED_SECRET,
                status="enabled",
            )
        )
        session.commit()

    resp = _client(sessions).get("/sources/freshness", headers=AUTH)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"

    assert _LEAKED_SECRET not in resp.text, (
        "the Fernet-encrypted config_encrypted value leaked verbatim into the freshness response"
    )
    body = resp.json()
    leaked_keys = _collect_keys(body) & _FORBIDDEN_KEYS
    assert not leaked_keys, f"forbidden non-opaque key(s) present in the response: {leaked_keys}"

    # The only ids present are the opaque instance_id / connector_id.
    row = next(r for r in body["sources"] if r["instance_id"] == "ci-secret-bearer")
    assert row["connector_id"] == "feeds"
