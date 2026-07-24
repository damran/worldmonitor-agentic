"""``wm`` — a thin, read-only command-line client over our own REST API.

Gate F-6 slice 1 (``docs/reviews/GATE_F6_WM_CLI_SPEC.md``, ADR 0127): three commands —
``wm health``, ``wm ready``, ``wm entity <id>`` — driven by ``WM_BASE_URL`` / ``WM_TOKEN`` /
``WM_TIMEOUT``, GET-only, with a pinned exit-code contract (0 success / 1 API-reported error /
2 usage-or-missing-config / 3 connection failure).

This module imports **only** ``httpx`` and the standard library — never
``worldmonitor.settings``, ``worldmonitor.api.*``, or any other package submodule — so
``import worldmonitor.cli`` pulls no server weight (it is a genuinely thin external client).
It performs no write, no ER, no merge, no resolution, no scoring, and no MCP coupling.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import NoReturn, cast

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 10.0

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_USAGE = 2
EXIT_CONNECTION = 3


def build_client(
    *,
    base_url: str,
    token: str | None,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Build the ``httpx.Client`` used for every request.

    This is the test seam: production code (``main()``) always calls this with
    ``transport=None`` (the real network transport); unit tests monkeypatch
    ``worldmonitor.cli.build_client`` with a wrapper that forces an injected
    ``httpx.MockTransport`` so no live server/network is needed.
    """
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if transport is not None:
        return httpx.Client(
            base_url=base_url, headers=headers, timeout=timeout, transport=transport
        )
    return httpx.Client(base_url=base_url, headers=headers, timeout=timeout)


class _UsageError(Exception):
    """Raised by :class:`_ArgumentParser.error` instead of argparse's default stderr+exit.

    argparse's default ``error()`` prints a message that can echo back the offending argv
    value (e.g. ``unrecognized arguments: --token <value>``) before exiting — which would leak
    an argv-supplied secret onto stderr (AC-17). We intercept it, discard the message, and let
    ``main()`` print a generic, argv-content-free usage line instead.
    """


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # noqa: D102 - argparse override
        raise _UsageError(message)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default=None, help="Override WM_BASE_URL for this call.")
    parser.add_argument(
        "--timeout", type=float, default=None, help="Override WM_TIMEOUT (seconds) for this call."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="wm", description="Thin read-only CLI for the WorldMonitor REST API."
    )
    subparsers = parser.add_subparsers(dest="command")

    health_parser = subparsers.add_parser("health", help="GET /health")
    _add_common_flags(health_parser)

    ready_parser = subparsers.add_parser("ready", help="GET /ready")
    _add_common_flags(ready_parser)

    entity_parser = subparsers.add_parser("entity", help="GET /entities/{id}")
    entity_parser.add_argument("entity_id", help="Entity id, e.g. NRC-abc123.")
    _add_common_flags(entity_parser)

    return parser


def _print_success(body: object) -> None:
    print(json.dumps(body, indent=2))


def _parse_body(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return response.text


def _error_message(body: object, fallback: str) -> str:
    if isinstance(body, dict):
        data = cast("dict[str, object]", body)
        detail = data.get("detail") or data.get("error") or fallback
        message = str(detail)
        hint = data.get("hint")
        if hint:
            message = f"{message} ({hint})"
        return message
    if isinstance(body, str) and body:
        return body
    return fallback


def _print_error(body: object, fallback: str) -> None:
    print(_error_message(body, fallback), file=sys.stderr)


def _handle_response(command: str, response: httpx.Response) -> int:
    body = _parse_body(response)

    if command == "ready":
        if response.status_code == 200:
            _print_success(body)
            return EXIT_OK
        if response.status_code == 503:
            # ADR 0059 / spec §3.2: report the endpoint's verdict verbatim on stdout even
            # though it is a non-ready outcome — never fold it into the generic stderr path.
            _print_success(body)
            return EXIT_API_ERROR
        _print_error(body, f"unexpected status {response.status_code}")
        return EXIT_API_ERROR

    if response.status_code == 200:
        _print_success(body)
        return EXIT_OK

    _print_error(body, f"unexpected status {response.status_code}")
    return EXIT_API_ERROR


def _resolve_base_url(args: argparse.Namespace) -> str | None:
    flag_value = args.base_url
    if flag_value is not None:
        if not flag_value:
            print("--base-url must not be empty", file=sys.stderr)
            return None
        return str(flag_value)
    return os.environ.get("WM_BASE_URL") or DEFAULT_BASE_URL


def _resolve_timeout(args: argparse.Namespace) -> float:
    flag_value = args.timeout
    if flag_value is not None:
        return float(flag_value)
    return float(os.environ.get("WM_TIMEOUT") or DEFAULT_TIMEOUT)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    except SystemExit as exc:
        # argparse's own -h/--help path (already printed usage to stdout) exits 0; anything
        # else that reaches SystemExit directly (not via our _UsageError) is a usage error.
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE

    command = args.command
    if command is None:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE

    base_url = _resolve_base_url(args)
    if base_url is None:
        return EXIT_USAGE
    timeout = _resolve_timeout(args)
    token = os.environ.get("WM_TOKEN") or None

    if command == "entity" and not token:
        print("WM_TOKEN is required for 'entity'", file=sys.stderr)
        return EXIT_USAGE

    client = build_client(base_url=base_url, token=token, timeout=timeout)
    try:
        with client:
            if command == "health":
                response = client.get("/health")
            elif command == "ready":
                response = client.get("/ready")
            else:
                response = client.get(f"/entities/{args.entity_id}")
    except httpx.RequestError as exc:
        print(f"could not reach {base_url}: {exc}", file=sys.stderr)
        return EXIT_CONNECTION

    return _handle_response(command, response)


if __name__ == "__main__":
    sys.exit(main())
