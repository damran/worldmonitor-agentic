"""App-side ``ContainerRunner`` — delegate a container-level tool's exec to the sidecar (ADR 0077).

``make_container_runner(settings)`` returns a ``Runner``-compatible
(``worldmonitor.plugins.cli_tool.Runner``)
async callable (``runner(argv, *, timeout) -> RunResult``) that, instead of spawning a host
subprocess, issues a single ``POST {sandbox_runner_url}/run`` carrying the argv **LIST** + the
timeout in the JSON body and the shared secret PLAINTEXT in the ``X-Sandbox-Secret`` header, then
maps the sidecar's JSON response back into the SAME :class:`RunResult` contract — so the connector's
``collect()``/``map()`` are unchanged (ADR 0077 §D1).

Invariants (gate.scope INV-4 / INV-6):

* argv rides as a JSON **list** (never joined into a shell string) — no shell-interpolation surface.
* ``RunResult.stdout``/``.stderr`` are arbitrary bytes, not JSON-serialisable, so they cross the
  wire **base64**-encoded; this runner base64-DECODES them back to bytes (symmetric with the
  sidecar's encode in :mod:`worldmonitor.sandbox.runner_service`).
* a transport error / non-2xx / malformed / field-missing response raises a clear ``RuntimeError``
  (a recorded runner failure) — NEVER a silent empty-success ``RunResult``.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, cast

import httpx

from worldmonitor.plugins.cli_tool import Runner
from worldmonitor.runner.subprocess import RunResult
from worldmonitor.settings import Settings

logger = logging.getLogger(__name__)

# The sidecar enforces the wall-clock timeout on the subprocess; the HTTP client waits a little
# longer than the tool timeout so the transport never trips before the sidecar can answer.
_CLIENT_TIMEOUT_MARGIN = 10.0

# The RunResult fields the sidecar must return (a body missing any of them is a protocol error).
_REQUIRED_FIELDS = ("returncode", "stdout", "stderr", "timed_out", "duration")


def make_container_runner(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> Runner:
    """Build a ``Runner`` that delegates execution to the sandbox-runner sidecar over HTTP.

    ``transport`` (an ``httpx.MockTransport`` in tests) is injected onto the client so no real
    network call is ever made under test; production passes ``None`` (a real client). The client's
    own timeout is bound to the tool timeout plus a margin so a slow-but-answering sidecar is not
    cut off by the transport.
    """
    base_url = settings.sandbox_runner_url
    secret = settings.sandbox_runner_secret.get_secret_value()
    endpoint = f"{base_url}/run"

    async def _run(argv: Any, *, timeout: float, **_kwargs: Any) -> RunResult:
        payload = {"argv": list(argv), "timeout": float(timeout)}
        headers = {"X-Sandbox-Secret": secret}
        client_timeout = float(timeout) + _CLIENT_TIMEOUT_MARGIN
        try:
            async with httpx.AsyncClient(transport=transport, timeout=client_timeout) as client:
                response = await client.post(endpoint, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"sandbox-runner request to {endpoint} failed: {type(exc).__name__}: {exc}"
            ) from exc

        if not response.is_success:
            raise RuntimeError(
                f"sandbox-runner returned HTTP {response.status_code} for {endpoint} "
                "(execution refused / sidecar error)"
            )

        try:
            raw: Any = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"sandbox-runner returned a non-JSON body for {endpoint}: {type(exc).__name__}"
            ) from exc

        if not isinstance(raw, dict):
            raise RuntimeError(
                f"sandbox-runner returned a non-object body for {endpoint}: {type(raw).__name__}"
            )
        body = cast("dict[str, Any]", raw)
        missing = [field for field in _REQUIRED_FIELDS if field not in body]
        if missing:
            raise RuntimeError(
                f"sandbox-runner response from {endpoint} is missing RunResult field(s): {missing}"
            )

        try:
            return RunResult(
                returncode=int(body["returncode"]),
                stdout=base64.b64decode(body["stdout"]),
                stderr=base64.b64decode(body["stderr"]),
                timed_out=bool(body["timed_out"]),
                duration=float(body["duration"]),
            )
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"sandbox-runner response from {endpoint} could not be mapped to a RunResult: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    return _run
