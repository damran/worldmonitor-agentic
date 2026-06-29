"""Primary invariant tests (RED) for the sandbox-runner sidecar service — ADR 0077 Slice 1.

Pins the ``gate.scope`` INV-5 / INV-6 contract for ``create_sandbox_app()`` — the FastAPI app the
sidecar exposes (``GET /health`` + ``POST /run``) — driven through ``fastapi.testclient.TestClient``
with ``run_command`` FAKED (monkeypatched) so NO real subprocess is ever spawned. The sidecar NEVER
trusts the caller: it re-validates **independently** (defense-in-depth, ADR 0077 §D2).

What is pinned here:

* ``GET /health`` → ``200 {"status": "ok"}``.
* ``POST /run`` with the correct ``X-Sandbox-Secret`` + an allowlisted tool → 200 carrying the
  ``RunResult`` JSON, and ``run_command`` is invoked exactly once with the argv **LIST** (no shell).
* AUTH (INV-5): a missing secret AND a wrong-but-same-length secret each → 401, and ``run_command``
  is NEVER called (the same-length case exercises the constant-time ``hmac.compare_digest`` path).
* VALIDATION-BEFORE-EXEC (INV-5 / INV-6): a tool ∉ ``{nmap, dig, whois}``, a shell-string argv, a
  non-``str`` argv element, and an out-of-bounds timeout each → 4xx, and ``run_command`` is NEVER
  called. The fake's ``.calls`` ledger proves validation happens *before* exec.

WIRE-ENCODING CONTRACT (flagged for the builder, symmetric with the ContainerRunner test):
``RunResult.stdout``/``.stderr`` are returned **base64**-encoded in the JSON body (lossless for
arbitrary bytes). Reconcile both halves together if you change it.

ASSUMED SEAMS (the builder must match):
* ``create_sandbox_app(settings=None) -> FastAPI`` — an optional ``settings`` for testability,
  mirroring ``api.main.create_app(settings=...)``. The no-arg ``create_sandbox_app()`` stays valid
  in prod (reads ``get_settings()``); the secret is read from ``settings.sandbox_runner_secret``.
* The service calls ``worldmonitor.runner.subprocess.run_command`` imported into its OWN namespace
  (``worldmonitor.sandbox.runner_service.run_command``) so this monkeypatch intercepts it.
* The auth header name is ``X-Sandbox-Secret``; ``/run`` accepts a JSON body
  ``{"argv": list[str], "timeout": float}``.

RED today: ``worldmonitor.sandbox.runner_service`` does not exist, so the top-level import raises
``ModuleNotFoundError`` and the whole module errors at collection (the correct RED).
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from worldmonitor.runner.subprocess import RunResult

# Top-level import of the not-yet-built service — ModuleNotFoundError today (correct RED).
from worldmonitor.sandbox.runner_service import create_sandbox_app
from worldmonitor.settings import Settings

_SECRET = "sidecar-service-secret"  # 22 chars — the configured shared secret


class _FakeRunCommand:
    """Async stand-in for ``run_command``: records every call, returns a canned ``RunResult``, and
    NEVER spawns a real subprocess. Its ``.calls`` ledger proves validation-happens-before-exec."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, cmd: Sequence[str], *, timeout: float, **kwargs: Any) -> RunResult:
        self.calls.append({"cmd": list(cmd), "timeout": timeout})
        return RunResult(
            returncode=0,
            stdout=b"93.184.216.34\n",
            stderr=b"",
            timed_out=False,
            duration=0.05,
        )


def _client_and_fake(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, _FakeRunCommand]:
    fake = _FakeRunCommand()
    monkeypatch.setattr("worldmonitor.sandbox.runner_service.run_command", fake, raising=False)
    settings = Settings(
        environment="test",
        sandbox_runner_secret=_SECRET,
        _env_file=None,
    )  # type: ignore[call-arg]
    app = create_sandbox_app(settings=settings)
    return TestClient(app, raise_server_exceptions=False), fake


def test_health_returns_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake = _client_and_fake(monkeypatch)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_run_executes_allowlisted_tool_with_correct_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Correct secret + an allowlisted tool ⇒ 200 + the RunResult JSON, and run_command runs once
    with the argv LIST (no shell)."""
    client, fake = _client_and_fake(monkeypatch)
    resp = client.post(
        "/run",
        headers={"X-Sandbox-Secret": _SECRET},
        json={"argv": ["dig", "+short", "--", "example.com"], "timeout": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["returncode"] == 0
    assert body["timed_out"] is False
    assert base64.b64decode(body["stdout"]) == b"93.184.216.34\n"
    assert base64.b64decode(body["stderr"]) == b""

    # The tool actually ran via run_command, exactly once, with the argv LIST (INV-6).
    assert len(fake.calls) == 1
    assert fake.calls[0]["cmd"] == ["dig", "+short", "--", "example.com"]
    assert fake.calls[0]["timeout"] == 5


def test_wrong_but_same_length_secret_is_401_and_does_not_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-5: a wrong secret of the SAME LENGTH (the constant-time-compare path) ⇒ 401, no exec."""
    client, fake = _client_and_fake(monkeypatch)
    wrong = "x" * len(_SECRET)
    assert wrong != _SECRET and len(wrong) == len(_SECRET)
    resp = client.post(
        "/run",
        headers={"X-Sandbox-Secret": wrong},
        json={"argv": ["dig", "+short", "--", "example.com"], "timeout": 5},
    )
    assert resp.status_code == 401, resp.text
    assert fake.calls == [], "a bad secret must be refused BEFORE run_command"


def test_missing_secret_is_401_and_does_not_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-5: an absent ``X-Sandbox-Secret`` header ⇒ 401, no exec."""
    client, fake = _client_and_fake(monkeypatch)
    resp = client.post(
        "/run",
        json={"argv": ["dig", "+short", "--", "example.com"], "timeout": 5},
    )
    assert resp.status_code == 401, resp.text
    assert fake.calls == [], "a missing secret must be refused BEFORE run_command"


def test_tool_not_in_allowlist_is_4xx_and_does_not_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-5: ``argv[0]`` ∉ {nmap, dig, whois} ⇒ 4xx, no exec — even with the correct secret."""
    client, fake = _client_and_fake(monkeypatch)
    resp = client.post(
        "/run",
        headers={"X-Sandbox-Secret": _SECRET},
        json={"argv": ["cat", "/etc/passwd"], "timeout": 5},
    )
    assert resp.status_code in (400, 422), resp.text
    assert fake.calls == [], "an off-allowlist tool must be rejected BEFORE run_command"


def test_shell_string_argv_is_4xx_and_does_not_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-6: an argv that is a shell STRING (not a list) ⇒ 4xx, no exec."""
    client, fake = _client_and_fake(monkeypatch)
    resp = client.post(
        "/run",
        headers={"X-Sandbox-Secret": _SECRET},
        json={"argv": "dig example.com; rm -rf /", "timeout": 5},
    )
    assert resp.status_code in (400, 422), resp.text
    assert fake.calls == [], "a shell-string argv must be rejected BEFORE run_command"


def test_non_str_argv_element_is_4xx_and_does_not_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-6: an argv list with a non-``str`` element ⇒ 4xx, no exec."""
    client, fake = _client_and_fake(monkeypatch)
    resp = client.post(
        "/run",
        headers={"X-Sandbox-Secret": _SECRET},
        json={"argv": ["dig", 1234], "timeout": 5},
    )
    assert resp.status_code in (400, 422), resp.text
    assert fake.calls == [], "a non-str argv element must be rejected BEFORE run_command"


def test_out_of_bounds_timeout_is_4xx_and_does_not_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-5: a non-positive timeout AND an absurdly-large timeout each ⇒ 4xx, no exec."""
    client, fake = _client_and_fake(monkeypatch)
    low = client.post(
        "/run",
        headers={"X-Sandbox-Secret": _SECRET},
        json={"argv": ["dig", "+short", "--", "example.com"], "timeout": 0},
    )
    assert low.status_code in (400, 422), low.text
    high = client.post(
        "/run",
        headers={"X-Sandbox-Secret": _SECRET},
        json={"argv": ["dig", "+short", "--", "example.com"], "timeout": 1_000_000_000},
    )
    assert high.status_code in (400, 422), high.text
    assert fake.calls == [], "an out-of-bounds timeout must be rejected BEFORE run_command"
