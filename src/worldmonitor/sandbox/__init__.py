"""Sandbox-runner sidecar (ADR 0077) — the constrained execution backend for heavy ACTIVE CLI tools.

Two halves live here:

* :mod:`worldmonitor.sandbox.container_runner` — the APP-side ``ContainerRunner`` (a
  ``Runner``-compatible async callable) that delegates a container-level tool's execution to the
  sidecar over HTTP, returning the same :class:`~worldmonitor.runner.subprocess.RunResult` so
  ``collect()``/``map()`` are unchanged.
* :mod:`worldmonitor.sandbox.runner_service` — the sidecar SERVICE (a tiny FastAPI app, ``POST
  /run`` + ``/health``) that independently re-validates the request (allowlist, no-shell argv,
  bounded timeout, shared-secret) and executes via ``run_command`` — defense in depth (it never
  trusts the caller).

Slice 1 (ADR 0077 §D6) is the locally-verifiable app seam + service code behind the default-off
``container_sandbox_enabled`` flag; the Dockerfile/compose/nftables egress deploy infra is Slice 2.
"""
