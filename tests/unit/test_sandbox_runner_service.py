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


def test_empty_configured_secret_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-5 (fail-closed): a sidecar configured with an EMPTY secret (the default) must refuse ALL
    /run requests — never authenticate a missing/empty header via ``hmac.compare_digest("","")``.
    Closes the review-found fail-open edge before Slice 2 deploys the sidecar (ADR 0077 §D2)."""
    fake = _FakeRunCommand()
    monkeypatch.setattr("worldmonitor.sandbox.runner_service.run_command", fake, raising=False)
    settings = Settings(environment="test", sandbox_runner_secret="", _env_file=None)  # type: ignore[call-arg]
    client = TestClient(create_sandbox_app(settings=settings), raise_server_exceptions=False)
    body = {"argv": ["nmap", "-oX", "-", "--", "example.com"], "timeout": 5}

    # No header at all, and an explicit empty header — both must 401 (not execute).
    assert client.post("/run", json=body).status_code == 401
    assert client.post("/run", headers={"X-Sandbox-Secret": ""}, json=body).status_code == 401
    assert fake.calls == [], "an unconfigured (empty-secret) sidecar must NEVER exec a tool"
    assert fake.calls == [], "an out-of-bounds timeout must be rejected BEFORE run_command"


# --------------------------------------------------------------------------- #
# Slice 2 (ADR 0077 §D2 / gate.scope INV-1) — the per-tool argv allowlist.
#
# Beyond the Slice-1 ``argv[0] in {nmap,dig,whois}`` check, the sidecar must
# DEFAULT-DENY the rest of the command line: every MIDDLE token ``argv[1:-1]``
# must be in that tool's fixed allow-set, and the LAST token (the target) must
# pass the same host/IP validator the connectors use (regex ``[A-Za-z0-9.:-]+``,
# length <= 253, NO leading ``-``, NO ``/``). ``len(argv) >= 2`` (tool + target).
#
# The EXACT per-tool MIDDLE-token allow-sets the builder MUST match (these are
# precisely what the connectors' ``_build_argv`` emit — see
# ``plugins/connectors/{nmap,dig,whois}/connector.py``):
#
#     nmap : {"-oX", "-", "--"}
#     dig  : {"+short", "--"}
#     whois: {"--"}
#
# RED today: the Slice-1 validator accepts ANY ``list[str]`` whose ``argv[0]`` is
# allowlisted, so every dangerous form below is executed (200, run_command called)
# instead of being refused (4xx, run_command NOT called).
# --------------------------------------------------------------------------- #

# The connectors' REAL argv (must PASS) — pinned against their ``_build_argv``.
_CONNECTOR_ARGV = [
    ["nmap", "-oX", "-", "--", "example.com"],
    ["dig", "+short", "--", "example.com"],
    ["whois", "--", "example.com"],
]


@pytest.mark.parametrize("argv", _CONNECTOR_ARGV, ids=lambda a: a[0])
def test_connector_argv_passes_the_allowlist(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-1: each connector's REAL ``_build_argv`` output passes the per-tool allowlist ⇒ 200,
    and ``run_command`` runs exactly ONCE with that exact argv LIST (no shell, no rewrite)."""
    client, fake = _client_and_fake(monkeypatch)
    resp = client.post(
        "/run",
        headers={"X-Sandbox-Secret": _SECRET},
        json={"argv": argv, "timeout": 5},
    )
    assert resp.status_code == 200, resp.text
    assert len(fake.calls) == 1, f"the legit connector argv {argv!r} must be executed once"
    assert fake.calls[0]["cmd"] == argv


# Each form is dangerous OR malformed and MUST be refused (4xx) before any exec.
_REJECTED_ARGV = [
    # nmap NSE script engine — arbitrary script execution; ``--script``/``vuln`` not in the set.
    pytest.param(["nmap", "--script", "vuln", "--", "example.com"], id="nmap-nse-script"),
    # nmap normal-output-to-FILE — arbitrary file write; ``-oN``/``out.txt`` not in the set.
    pytest.param(["nmap", "-oN", "out.txt", "--", "example.com"], id="nmap-file-write"),
    # nmap random-internet scan — ``-iR`` not in the set (target ``100`` is irrelevant).
    pytest.param(["nmap", "-iR", "100"], id="nmap-random-internet"),
    # target contains '/' — fails the host/IP validator (path / traversal).
    pytest.param(["nmap", "-oX", "-", "--", "../etc/passwd"], id="nmap-target-has-slash"),
    # target has a leading '-' — would be parsed as a flag (flag injection past ``--``).
    pytest.param(["nmap", "-oX", "-", "--", "-oG"], id="nmap-target-leading-dash"),
    # dig: an unknown middle flag (only ``+short``/``--`` are allowed).
    pytest.param(["dig", "+time=1", "--", "example.com"], id="dig-unknown-flag"),
    # whois: an unknown middle flag (only ``--`` is allowed) — ``-h`` redirects the whois server.
    pytest.param(["whois", "-h", "evil.example", "--", "example.com"], id="whois-unknown-flag"),
    # just the tool, no target — ``len(argv) < 2``.
    pytest.param(["nmap"], id="nmap-no-target"),
]


@pytest.mark.parametrize("argv", _REJECTED_ARGV)
def test_dangerous_argv_is_rejected_before_exec(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """INV-1: a dangerous/unknown-flag/bad-target/no-target argv ⇒ 4xx, and ``run_command`` is
    NEVER called (validation-before-exec — the ``.calls`` ledger proves it). The correct secret is
    supplied, so the ONLY thing that can refuse these is the per-tool argv allowlist."""
    client, fake = _client_and_fake(monkeypatch)
    resp = client.post(
        "/run",
        headers={"X-Sandbox-Secret": _SECRET},
        json={"argv": argv, "timeout": 5},
    )
    assert resp.status_code in (400, 422), resp.text
    assert fake.calls == [], f"dangerous argv {argv!r} must be rejected BEFORE run_command"
