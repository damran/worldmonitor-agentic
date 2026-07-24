"""MCP live-smoke (Gate F-8, ADR 0126): stdio ``tools/list``/``prompts/list`` set-pin.

``python -m worldmonitor.mcp.smoke`` spawns the REAL deployed entrypoint
(``python -m worldmonitor.mcp``) as a subprocess, forces it onto the **stdio** transport
(overriding the ``mcp`` compose service's ``MCP_TRANSPORT=streamable-http``), speaks the
same minimal newline-delimited JSON-RPC handshake the ``tests/integration/test_mcp_stdio.py``
suite already uses (``initialize`` -> ``notifications/initialized`` -> ``tools/list`` ->
``prompts/list``), and asserts the served surface is **exactly** the registered 5 tools +
2 prompts (:data:`EXPECTED_TOOLS` / :data:`EXPECTED_PROMPTS`). Any drift — an extra member,
a missing member, a renamed member, or a handshake/spawn/timeout failure — exits non-zero
with a diagnostic; an exact match exits 0.

This is a **CI smoke, not a library**: no bearer, no Zitadel, no Neo4j connection (stdio +
no tool call, ADR 0063/0126 §1.4). It is appended as one additive step to the
``compose-boot`` job (``.github/workflows/compose-boot.yml``) and is also directly
unit-testable in-process via :func:`compare` (see ``tests/unit/test_mcp_smoke.py``).

STDOUT PURITY: the CHILD's stdout is the JSON-RPC wire and is read ONLY for JSON-RPC
frames here. This module's OWN stdout (the smoke process itself, not the child) is not a
JSON-RPC stream, so printing the final OK/diff summary to stdout is fine; diagnostics
(handshake failures, the child's captured stderr) go to this module's stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any

from mcp.types import LATEST_PROTOCOL_VERSION

EXPECTED_TOOLS = frozenset(
    {
        "get_entity",
        "get_neighbors",
        "get_provenance",
        "find_paths",
        "get_entity_dossier",
    }
)  # the 5 read tools (ADR 0063 + 0122)

EXPECTED_PROMPTS = frozenset(
    {
        "entity-workup",
        "freshness-audit",
    }
)  # the 2 analyst-playbook prompts (ADR 0125 / Gate F-4)

_READ_DEADLINE_SECONDS = 60.0


def _diff_section(label: str, expected: set[str], served: set[str]) -> str:
    """One ``<label>: missing={...} unexpected={...}`` section of the drift message.

    ``missing`` = expected-but-absent (in ``expected`` but not ``served``); ``unexpected``
    = present-but-unexpected (in ``served`` but not ``expected``).
    """
    missing = expected - served
    unexpected = served - expected
    return f"{label}: missing={sorted(missing)} unexpected={sorted(unexpected)}"


def compare(served_tools: set[str], served_prompts: set[str]) -> tuple[int, str]:
    """Compare served tool/prompt name sets against the pinned :data:`EXPECTED_*` sets.

    Pure, no I/O. Returns ``(0, ok_message)`` iff both sets match exactly; otherwise
    ``(1, diff_message)`` where ``diff_message`` names, per surface, BOTH directions of the
    symmetric difference (``missing=`` / ``unexpected=``) for every surface that drifted.
    """
    tools_ok = served_tools == set(EXPECTED_TOOLS)
    prompts_ok = served_prompts == set(EXPECTED_PROMPTS)
    if tools_ok and prompts_ok:
        return 0, (
            f"OK: MCP surface matches exactly — {len(EXPECTED_TOOLS)} tools, "
            f"{len(EXPECTED_PROMPTS)} prompts"
        )

    sections: list[str] = []
    if not tools_ok:
        sections.append(_diff_section("tools", set(EXPECTED_TOOLS), served_tools))
    if not prompts_ok:
        sections.append(_diff_section("prompts", set(EXPECTED_PROMPTS), served_prompts))
    return 1, "MCP surface drift detected — " + "; ".join(sections)


def _send(proc: subprocess.Popen[str], frame: dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(frame) + "\n")
    proc.stdin.flush()


def _read_response(
    proc: subprocess.Popen[str], deadline: float, errbuf: list[str]
) -> dict[str, Any]:
    """Read one newline-delimited JSON-RPC frame off the child's stdout, bounded by ``deadline``."""
    assert proc.stdout is not None
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"MCP smoke: timed out waiting for a response frame "
                f"(stderr so far:\n{''.join(errbuf)})"
            )
        line = proc.stdout.readline()
        if line == "":
            raise RuntimeError(
                f"MCP smoke: child stdout closed before answering (stderr:\n{''.join(errbuf)})"
            )
        line = line.strip()
        if not line:
            continue
        return json.loads(line)


def run_smoke() -> int:
    """Spawn the real ``python -m worldmonitor.mcp`` entrypoint over stdio and assert the
    served tool/prompt surface is exactly :data:`EXPECTED_TOOLS` / :data:`EXPECTED_PROMPTS`.

    Forces ``MCP_TRANSPORT=stdio`` on the child (overriding any inherited value — the `mcp`
    compose service configures ``streamable-http``). Bounds every read with a ~60s deadline
    and always terminates the child in a ``finally``. Prints the one-line summary
    (:func:`compare`'s message) to stdout on completion; diagnostics (spawn/handshake
    failures, the child's captured stderr) go to stderr. Returns the process exit code
    (0 = exact match, non-zero = drift or failure) — never raises.
    """
    env = os.environ | {"MCP_TRANSPORT": "stdio"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "worldmonitor.mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None
    child_stderr = proc.stderr

    # Drain stderr on a daemon thread so a full OS pipe buffer can never deadlock the child
    # (mirrors the ``test_mcp_stdio.py`` idiom). This is the ONLY reader of ``child_stderr``
    # anywhere in this function — never read it again elsewhere (avoid a racy double-read).
    errbuf: list[str] = []
    drain = threading.Thread(target=lambda: errbuf.append(child_stderr.read()), daemon=True)
    drain.start()

    error: Exception | None = None
    served_tools: set[str] = set()
    served_prompts: set[str] = set()
    try:
        deadline = time.monotonic() + _READ_DEADLINE_SECONDS
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "wm-mcp-smoke", "version": "0"},
                },
            },
        )
        _read_response(proc, deadline, errbuf)  # the initialize result

        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_response = _read_response(proc, deadline, errbuf)
        if "error" in tools_response:
            raise RuntimeError(f"tools/list returned a JSON-RPC error: {tools_response['error']}")
        served_tools = {t["name"] for t in tools_response["result"]["tools"]}

        _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "prompts/list", "params": {}})
        prompts_response = _read_response(proc, deadline, errbuf)
        if "error" in prompts_response:
            raise RuntimeError(
                f"prompts/list returned a JSON-RPC error: {prompts_response['error']}"
            )
        served_prompts = {p["name"] for p in prompts_response["result"]["prompts"]}
    except Exception as exc:  # noqa: BLE001 - a smoke must report, never crash the CI step opaquely
        error = exc
    finally:
        if not proc.stdin.closed:
            proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        drain.join(timeout=10)

    if error is not None:
        print(f"MCP smoke FAILED: {error}", file=sys.stderr)
        stderr_so_far = "".join(errbuf)
        if stderr_so_far.strip():
            print(f"--- spawned child stderr ---\n{stderr_so_far}", file=sys.stderr)
        return 1

    code, message = compare(served_tools, served_prompts)
    print(message)
    return code


def main() -> int:
    return run_smoke()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
