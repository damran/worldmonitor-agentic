"""Sandbox-runner sidecar SERVICE — the constrained-execution ASGI app (ADR 0077 §D2).

A tiny FastAPI app the in-network sidecar exposes:

* ``GET /health`` → ``{"status": "ok"}``.
* ``POST /run`` — authenticated by the ``X-Sandbox-Secret`` header (constant-time compare,
  ``hmac.compare_digest``; 401 BEFORE anything else). The body ``{"argv": list[str], "timeout":
  float}`` is re-validated **INDEPENDENTLY of the caller** (defense in depth — the sidecar never
  trusts the app): ``argv`` is a non-empty ``list[str]`` (no shell), ``argv[0]`` ∈ the sidecar's OWN
  allowlist ``{nmap, dig, whois}``, and ``timeout`` is bounded. Only then does it execute via the
  shared :func:`~worldmonitor.runner.subprocess.run_command` (argv-list, no-shell, process-group
  SIGKILL on timeout) and return the :class:`RunResult` as JSON.

``RunResult.stdout``/``.stderr`` are arbitrary bytes, so they cross the wire **base64**-encoded
(symmetric with the app-side decode in :mod:`worldmonitor.sandbox.container_runner`).

``run_command`` is imported into THIS module's namespace so it is the monkeypatch seam under test
(``worldmonitor.sandbox.runner_service.run_command``); never reach through the source module.

Slice 1 (ADR 0077 §D6): this is just the ASGI app + validator, unit-tested with a fake
``run_command``. The container/egress/image hardening (CAP_NET_ADMIN + nftables, resource limits) is
Slice 2 (deploy infra).
"""

from __future__ import annotations

import base64
import hmac
import logging
import re
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException, Request

from worldmonitor.runner.subprocess import run_command
from worldmonitor.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# The sidecar's OWN allowlist (defense in depth — NOT trusting the app's allowlist, ADR 0077 §D2).
_ALLOWED_TOOLS = frozenset({"nmap", "dig", "whois"})

# Per-tool DEFAULT-DENY allow-set for the MIDDLE tokens (``argv[1:-1]``), ADR 0077 §D2 Slice-2
# hardening. These are EXACTLY what the connectors' ``_build_argv`` emit (see
# ``plugins/connectors/{nmap,dig,whois}/connector.py``); anything else (``--script``/``-oN``/``-iR``
# /``-iL``/``-h``) is refused before exec. Intentional coupling: adding a connector flag means
# extending the matching set here (a safe, explicit edit).
_ALLOWED_MIDDLE: dict[str, frozenset[str]] = {
    "nmap": frozenset({"-oX", "-", "--"}),
    "dig": frozenset({"+short", "--"}),
    "whois": frozenset({"--"}),
}

# The SAME host/IP target shape the connectors enforce (cli_tool._TARGET_RE / _MAX_TARGET_LEN,
# ADR 0072 §3) — re-derived here (NOT imported) so the sidecar's validation stays self-contained,
# independent of the caller (defense in depth, ADR 0077 §D2; mirrors the inlined ``_is_str_list``).
_TARGET_RE = re.compile(r"[A-Za-z0-9.:-]+")
_MAX_TARGET_LEN = 253

# Wall-clock bound on a single delegated tool run. A non-positive or absurdly large timeout is a
# misuse / abuse and is refused before any subprocess is spawned.
_MAX_TIMEOUT = 3600.0


def _is_valid_target(token: str) -> bool:
    """True iff ``token`` is a plain host/IP — the SAME rule the connectors enforce (ADR 0072 §3):
    matches ``[A-Za-z0-9.:-]+`` fully, ``<= _MAX_TARGET_LEN`` chars, NO leading ``-`` (flag
    injection), NO ``/`` (traversal). The sidecar's last-token (target) gate (ADR 0077 §D2)."""
    return (
        bool(token)
        and not token.startswith("-")
        and "/" not in token
        and len(token) <= _MAX_TARGET_LEN
        and _TARGET_RE.fullmatch(token) is not None
    )


def _is_str_list(value: object) -> bool:
    """True iff ``value`` is a ``list`` of ``str`` (the no-shell argv shape) — mirrors
    ``cli_tool._is_str_list`` (inlined so the sidecar's validation is self-contained, ADR 0077 §D2).
    """
    if not isinstance(value, list):
        return False
    return all(isinstance(item, str) for item in cast("list[Any]", value))


def create_sandbox_app(settings: Settings | None = None) -> FastAPI:
    """Build the sidecar FastAPI app. ``settings`` is injectable for tests; ``None`` ⇒
    :func:`get_settings` (the secret + bounds come from the environment in prod)."""
    settings = settings or get_settings()
    app = FastAPI(title="worldmonitor sandbox-runner", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Liveness probe — no auth, no execution."""
        return {"status": "ok"}

    @app.post("/run")
    async def run(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        x_sandbox_secret: str | None = Header(default=None),
    ) -> dict[str, object]:
        """Authenticate, INDEPENDENTLY re-validate, then execute via ``run_command``.

        Order matters: the shared-secret check (constant-time) fires BEFORE the body is read or
        validated, and ALL validation fires before ``run_command`` is ever called.
        """
        # --- AUTH (constant-time) — before reading/validating anything else (INV-5). ----------- #
        expected = settings.sandbox_runner_secret.get_secret_value()
        provided = x_sandbox_secret or ""
        # FAIL CLOSED on a misconfigured sidecar: an EMPTY configured secret must never authenticate
        # anyone (``hmac.compare_digest("", "")`` is True, so without this an unconfigured sidecar
        # would accept a missing header). The sidecar refuses ALL execution until it is given a real
        # secret — "never trust the caller", ADR 0077 §D2. (App-side routing also refuses to call an
        # unconfigured sidecar, operator_run INV-2; this is the independent second gate.)
        if not expected:
            raise HTTPException(
                status_code=401, detail="sandbox-runner secret is not configured (refusing)"
            )
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="invalid or missing sandbox secret")

        # --- BODY (re-validate independently of the caller — defense in depth, INV-5/INV-6). --- #
        try:
            raw: Any = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="body must be valid JSON") from exc
        if not isinstance(raw, dict):
            raise HTTPException(status_code=422, detail="body must be a JSON object")
        body = cast("dict[str, Any]", raw)

        argv = body.get("argv")
        timeout = body.get("timeout")

        # argv: a non-empty list[str] (no shell string, no non-str element).
        if not _is_str_list(argv) or not argv:
            raise HTTPException(
                status_code=422, detail="argv must be a non-empty list of strings (no shell)"
            )
        # argv[0] in the sidecar's OWN allowlist.
        if argv[0] not in _ALLOWED_TOOLS:
            raise HTTPException(
                status_code=422,
                detail=f"tool {argv[0]!r} is not in the sandbox allowlist {sorted(_ALLOWED_TOOLS)}",
            )
        # timeout: a positive, bounded number (reject bool — ``True`` is an int in Python).
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
            or timeout > _MAX_TIMEOUT
        ):
            raise HTTPException(
                status_code=422,
                detail=f"timeout must be a number in (0, {_MAX_TIMEOUT}]",
            )

        # --- ARGV ALLOWLIST (per-tool DEFAULT-DENY, ADR 0077 §D2 Slice-2). --------------------- #
        # Beyond ``argv[0] in {nmap,dig,whois}``: require ``[tool, ...flags, target]`` (>=2 tokens);
        # every MIDDLE token (``argv[1:-1]``) must be in that tool's fixed allow-set; the LAST token
        # (the target) must be a plain host/IP. Closes the ``--script`` (NSE) / ``-oN`` (file-write)
        # / ``-iR`` (random-net) / ``-iL`` (file-read) / ``-h`` (redirect) smuggling surface — those
        # flags are not in the set. Independent of the app-side validator (never trust the caller).
        # Any violation ⇒ 422, ``run_command`` is NOT called.
        if len(argv) < 2:
            raise HTTPException(
                status_code=422,
                detail="argv must be [tool, ...flags, target] (at least tool + target)",
            )
        allowed_middle = _ALLOWED_MIDDLE[argv[0]]  # argv[0] in _ALLOWED_TOOLS is already enforced
        for token in argv[1:-1]:
            if token not in allowed_middle:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"flag {token!r} is not in the {argv[0]} allow-set "
                        f"{sorted(allowed_middle)} (default-deny)"
                    ),
                )
        if not _is_valid_target(argv[-1]):
            raise HTTPException(
                status_code=422,
                detail=f"target {argv[-1]!r} is not a valid host/IP",
            )

        # --- EXEC (only after auth + full validation). argv stays a LIST (no shell). ----------- #
        result = await run_command(argv, timeout=float(timeout))
        return {
            "returncode": result.returncode,
            "stdout": base64.b64encode(result.stdout).decode("ascii"),
            "stderr": base64.b64encode(result.stderr).decode("ascii"),
            "timed_out": result.timed_out,
            "duration": result.duration,
        }

    return app
