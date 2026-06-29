"""Primary invariant tests (RED) for the app-side ContainerRunner — ADR 0077 Slice 1.

Pins the ``gate.scope`` INV-4 / INV-6 contract for ``make_container_runner`` (the
``Runner``-compatible async callable that delegates a container-level CLI tool's execution to the
sandbox-runner sidecar over HTTP). Everything runs against a FAKE ``httpx`` transport
(``httpx.MockTransport``) so NO real network call is ever made; the runner is exercised directly
(``await runner(argv, timeout=...)``), independent of ``operator_run`` wiring.

What is pinned here:

* INV-4 request shape: a single ``POST <sandbox_runner_url>/run`` carrying the argv **LIST** and the
  ``timeout`` in the JSON body, plus the shared secret PLAINTEXT in the ``X-Sandbox-Secret`` header
  (the ``SecretStr`` value via ``.get_secret_value()`` — never the masked repr).
* INV-4 response mapping: a JSON body ``{returncode, stdout, stderr, timed_out, duration}`` maps
  field-for-field into a :class:`RunResult` (stdout/stderr are **bytes**, base64-decoded off the
  wire — see the WIRE-ENCODING note below), preserving the ``collect()``/``map()`` contract.
* INV-4 fail-loud: a non-2xx response, a transport error, a malformed body, and a body missing
  fields each raise a clear ``RuntimeError`` — NEVER a silent empty-success ``RunResult``.
* INV-6 no-shell: argv is transmitted as a JSON **list** (each element verbatim), never joined into
  a shell string — even when an element carries shell metacharacters.

WIRE-ENCODING CONTRACT (flagged for the builder): ``RunResult.stdout``/``.stderr`` are arbitrary
``bytes``, which are not JSON-serialisable, so this oracle pins **base64** as the on-the-wire
encoding for those two fields (lossless for arbitrary bytes — "decode stdout/stderr ... as the
contract requires", spec §2.2). The sidecar service test pins the symmetric encode. If you prefer a
different lossless encoding, reconcile BOTH halves together.

ASSUMED SEAMS (the builder must match):
* ``make_container_runner(settings, *, transport=None) -> Runner`` — an optional keyword-only
  ``transport`` (``httpx.MockTransport`` in tests; ``None`` ⇒ real HTTP in prod), mirroring the
  repo's ``TelegramNotifier(transport=...)`` injection convention. The core signature
  ``make_container_runner(settings) -> Runner`` is preserved.
* The runner posts to ``f"{settings.sandbox_runner_url}/run"`` with header name
  ``X-Sandbox-Secret`` and body ``{"argv": list[str], "timeout": float}``.

RED today: ``worldmonitor.sandbox.container_runner`` does not exist, so the top-level import raises
``ModuleNotFoundError`` and the whole module errors at collection (the correct RED).
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from worldmonitor.runner.subprocess import RunResult

# Top-level import of the not-yet-built module — ModuleNotFoundError today (correct RED).
from worldmonitor.sandbox.container_runner import make_container_runner
from worldmonitor.settings import Settings

_URL = "http://sandbox-runner:9000"
_SECRET = "sidecar-shared-secret"


def _settings(*, url: str = _URL, secret: str = _SECRET) -> Settings:
    return Settings(
        environment="test",
        sandbox_runner_url=url,
        sandbox_runner_secret=secret,
        _env_file=None,
    )  # type: ignore[call-arg]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _ok_body(stdout: bytes = b"", stderr: bytes = b"") -> dict[str, object]:
    return {
        "returncode": 0,
        "stdout": _b64(stdout),
        "stderr": _b64(stderr),
        "timed_out": False,
        "duration": 0.0,
    }


async def test_posts_argv_secret_and_timeout_then_maps_runresult() -> None:
    """INV-4: a single POST to ``<url>/run`` carrying the argv LIST + timeout in the body and the
    secret in the header; the JSON response maps field-for-field into a ``RunResult``."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "returncode": 0,
                "stdout": _b64(b"93.184.216.34\n"),
                "stderr": _b64(b"warn: slow\n"),
                "timed_out": False,
                "duration": 0.125,
            },
        )

    runner = make_container_runner(_settings(), transport=httpx.MockTransport(handler))
    argv = ["dig", "+short", "--", "example.com"]
    result = await runner(argv, timeout=5.0)

    # --- request shape (INV-4 / INV-6) -------------------------------------------------------- #
    assert len(captured) == 1, "the runner must issue exactly one /run request"
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == f"{_URL}/run"
    # The shared-secret PLAINTEXT rides in the X-Sandbox-Secret header (never the SecretStr mask).
    assert request.headers.get("X-Sandbox-Secret") == _SECRET
    body = json.loads(request.content)
    assert isinstance(body["argv"], list)
    assert body["argv"] == argv
    assert body["timeout"] == 5.0

    # --- response mapping (INV-4, field-for-field; stdout/stderr base64-decoded to bytes) ----- #
    assert isinstance(result, RunResult)
    assert result.returncode == 0
    assert result.stdout == b"93.184.216.34\n"
    assert result.stderr == b"warn: slow\n"
    assert result.timed_out is False
    assert result.duration == 0.125


async def test_argv_is_sent_as_a_list_never_a_shell_string() -> None:
    """INV-6: a metachar-bearing element rides as a verbatim JSON list element — never joined into a
    shell string."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_ok_body())

    runner = make_container_runner(_settings(), transport=httpx.MockTransport(handler))
    argv = ["nmap", "-oX", "-", "--", "example.com; rm -rf /"]
    await runner(argv, timeout=2.0)

    body = json.loads(captured[0].content)
    assert isinstance(body["argv"], list)
    assert body["argv"] == argv
    assert all(isinstance(part, str) for part in body["argv"])


async def test_non_2xx_response_raises_runtime_error() -> None:
    """INV-4: a non-2xx sidecar response surfaces as a clear ``RuntimeError`` — not a silent
    empty-success ``RunResult``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "sidecar down"})

    runner = make_container_runner(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await runner(["dig", "+short", "--", "example.com"], timeout=5.0)


async def test_transport_error_raises_runtime_error() -> None:
    """INV-4: a transport failure (sidecar unreachable) raises a clear ``RuntimeError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    runner = make_container_runner(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await runner(["dig", "+short", "--", "example.com"], timeout=5.0)


async def test_malformed_body_raises_runtime_error() -> None:
    """INV-4: a 2xx response whose body is not valid JSON raises (never a silent success)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<<< not json >>>")

    runner = make_container_runner(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await runner(["dig", "+short", "--", "example.com"], timeout=5.0)


async def test_response_missing_fields_raises_runtime_error() -> None:
    """INV-4: a 2xx JSON body missing the RunResult fields raises (never a partial/silent
    ``RunResult``)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"returncode": 0}
        )  # stdout/stderr/timed_out/duration absent

    runner = make_container_runner(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await runner(["dig", "+short", "--", "example.com"], timeout=5.0)
