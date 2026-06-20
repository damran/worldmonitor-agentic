"""/health is public; protected routes require a valid bearer token."""

from collections.abc import Mapping
from typing import Any

from fastapi.testclient import TestClient

from worldmonitor.api.main import create_app
from worldmonitor.authz.oidc import ORG_ID_CLAIM, InvalidTokenError
from worldmonitor.settings import Settings


class _FakeVerifier:
    """Accepts the token ``"good"``; rejects everything else."""

    def verify(self, token: str) -> Mapping[str, Any]:
        if token != "good":
            raise InvalidTokenError("bad token")
        return {"sub": "user-123", ORG_ID_CLAIM: "tenant-abc"}


def _client(verifier: object | None) -> TestClient:
    app = create_app(settings=Settings(environment="test"), verifier=verifier)  # type: ignore[arg-type]
    return TestClient(app)


def test_health_is_public() -> None:
    resp = _client(_FakeVerifier()).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_protected_route_requires_token() -> None:
    resp = _client(_FakeVerifier()).get("/me")
    assert resp.status_code == 401


def test_protected_route_rejects_bad_token() -> None:
    resp = _client(_FakeVerifier()).get("/me", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_protected_route_accepts_valid_token_and_sets_tenant() -> None:
    resp = _client(_FakeVerifier()).get("/me", headers={"Authorization": "Bearer good"})
    assert resp.status_code == 200
    assert resp.json() == {"subject": "user-123", "tenant_id": "tenant-abc"}


def test_unconfigured_auth_rejects_protected_route() -> None:
    # No verifier (Zitadel not configured) -> protected routes 401, health still ok.
    client = _client(None)
    assert client.get("/health").status_code == 200
    assert client.get("/me", headers={"Authorization": "Bearer good"}).status_code == 401
