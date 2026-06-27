"""Gate B-4c R1 — ``/ready`` is a REAL store probe, unlike the ``/health`` echo.

The audit's sharpest point (spec §2): a test asserts ``/health``==ok, which is the
false-confidence signal — every store can be down (or the driver dead) while ``/health``
still says ok. The fix is a NEW ``/ready`` that fails CLOSED on store reachability.

This is the failing-test-first oracle for slice 1. It is RED today because
``worldmonitor.api.readiness`` does not exist yet (and there is no ``/ready`` route);
GREEN once the builder adds ``readiness.check_readiness`` + the ``/ready`` route.

Injection contract the builder must satisfy (mirrors the existing ``verifier=`` injection
in :func:`create_app`):

* ``readiness.check_readiness(*, postgres_probe, neo4j_probe, minio_probe) -> ReadinessResult``
  where each probe is a zero-arg callable that RAISES on failure. A raising probe records
  that component ``"down"`` (never a 500). ``ReadinessResult`` exposes ``.ready: bool`` and
  ``.checks: dict[str, str]`` (component -> ``"ok"`` | ``"down"``).
* ``create_app(*, settings=None, verifier=None, readiness=None)`` where ``readiness`` is a
  zero-arg callable returning a ``ReadinessResult``; ``/ready`` returns **200** + body when
  ``.ready`` else **503** + body. Body is ``{"ready": bool, "checks": {...}}``.
* ``/ready`` is reachable WITHOUT auth (public, like ``/health``).

NOTE: ``tests/unit/test_api_health.py`` is FROZEN; the ``/health`` contrast assertion lives
here, not there.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from worldmonitor.api.main import create_app
from worldmonitor.api.readiness import ReadinessResult, check_readiness
from worldmonitor.authz.oidc import InvalidTokenError
from worldmonitor.settings import Settings

Probe = Callable[[], None]
_STORES = ("postgres", "neo4j", "minio")


def _ok_probe() -> None:
    """A reachable store: returns cleanly."""
    return None


def _down_probe() -> None:
    """An unreachable store: a probe raises (must be caught, never a 500)."""
    raise RuntimeError("store unreachable")


class _RejectAllVerifier:
    """Rejects every token — proves ``/ready`` does not require auth."""

    def verify(self, token: str) -> dict[str, str]:
        raise InvalidTokenError("nope")


def _readiness(
    *,
    postgres: Probe = _ok_probe,
    neo4j: Probe = _ok_probe,
    minio: Probe = _ok_probe,
) -> Callable[[], ReadinessResult]:
    """A zero-arg readiness callable wired to the real ``check_readiness`` + fake probes."""
    return lambda: check_readiness(
        postgres_probe=postgres,
        neo4j_probe=neo4j,
        minio_probe=minio,
    )


def _client(
    readiness: Callable[[], ReadinessResult],
    *,
    verifier: object | None = None,
) -> TestClient:
    app = create_app(
        settings=Settings(environment="test"),
        verifier=verifier,  # type: ignore[arg-type]
        readiness=readiness,  # type: ignore[call-arg]
    )
    return TestClient(app)


# --- check_readiness decision logic (no app, no stack) --------------------- #


def test_check_readiness_all_ok_is_ready() -> None:
    result = check_readiness(
        postgres_probe=_ok_probe,
        neo4j_probe=_ok_probe,
        minio_probe=_ok_probe,
    )
    assert isinstance(result, ReadinessResult)
    assert result.ready is True
    assert result.checks == {"postgres": "ok", "neo4j": "ok", "minio": "ok"}


@pytest.mark.parametrize("down", _STORES)
def test_check_readiness_one_store_down_is_not_ready(down: str) -> None:
    probes: dict[str, Probe] = dict.fromkeys(_STORES, _ok_probe)
    probes[down] = _down_probe
    result = check_readiness(
        postgres_probe=probes["postgres"],
        neo4j_probe=probes["neo4j"],
        minio_probe=probes["minio"],
    )
    assert result.ready is False
    assert result.checks[down] == "down"
    # A builder must not pass by marking everything down: the live stores stay "ok".
    for store in _STORES:
        if store != down:
            assert result.checks[store] == "ok"


# --- /ready route (TestClient + injected fake probes) ---------------------- #


def test_ready_returns_200_and_all_ok_body_when_every_store_up() -> None:
    resp = _client(_readiness()).get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["checks"] == {"postgres": "ok", "neo4j": "ok", "minio": "ok"}


@pytest.mark.parametrize("down", _STORES)
def test_ready_returns_503_naming_the_down_store(down: str) -> None:
    probes: dict[str, Probe] = dict.fromkeys(_STORES, _ok_probe)
    probes[down] = _down_probe
    resp = _client(_readiness(**probes)).get("/ready")  # type: ignore[arg-type]
    assert resp.status_code == 503
    body = resp.json()
    assert body["ready"] is False
    assert body["checks"][down] == "down"
    # The failing component is named in the response payload.
    assert down in resp.text
    # The reachable stores are still reported ok (no blanket "all down").
    for store in _STORES:
        if store != down:
            assert body["checks"][store] == "ok"


def test_ready_is_public_no_auth_required() -> None:
    # A verifier that rejects everything is installed; /ready must still be reachable.
    resp = _client(_readiness(), verifier=_RejectAllVerifier()).get("/ready")
    assert resp.status_code not in (401, 403)
    assert resp.status_code == 200


def test_health_stays_ok_while_ready_is_503_for_the_same_app() -> None:
    """The load-bearing false-confidence distinction (spec §2).

    In the SAME scenario (a store is down), ``/health`` keeps echoing ok (liveness) while
    ``/ready`` reports 503 (readiness). This is exactly the signal the audit demands.
    """
    client = _client(_readiness(neo4j=_down_probe))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    ready = client.get("/ready")
    assert ready.status_code == 503
    assert ready.json()["ready"] is False
    assert ready.json()["checks"]["neo4j"] == "down"
