"""Integration tests for the graph-read FastMCP stdio server (ADR 0063, slice 2b).

These spawn the REAL server entrypoint ``python -m worldmonitor.mcp`` as a child
process pointed at the ephemeral testcontainer Neo4j, speak JSON-RPC over its
stdin/stdout pipe (the SDK frames messages as newline-delimited JSON — confirmed by
introspecting ``mcp.server.stdio``), and assert the gate's HEADLINE invariant:

    STDOUT PURITY — every byte the server writes to stdout is a valid JSON-RPC frame,
    even on an error path; all logs / tracebacks go to STDERR only.

RED today: ``worldmonitor.mcp`` has no ``__main__`` / ``server`` module, so the
subprocess fails to start and emits no JSON-RPC frames — the assertions below fail
with the spawned process's stderr attached for the reason.

ASSUMPTIONS (builder must match): the server reads Neo4j creds from the standard
Settings env vars (``NEO4J_URI`` / ``NEO4J_USER`` / ``NEO4J_PASSWORD``); the four
tools are named ``get_entity`` / ``get_neighbors`` / ``get_provenance`` /
``find_paths``; ``get_entity`` returns node props incl. ``prov_*``; ``get_neighbors``
returns neighbour prop dicts (each with an ``id``); ``find_paths`` returns objects
carrying a ``nodes`` list.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src"

_PROV = Provenance(
    source_id="opensanctions:us_ofac_sdn",
    retrieved_at="2026-06-21T00:00:00Z",
    reliability="A",
    source_record="s3://landing/test-tenant/opensanctions/p1.json",
)
_INJECTION_ID = '") DETACH DELETE n //'


def _stamped(data: dict[str, object]) -> FtmEntity:
    return stamp(make_entity(data), _PROV)


def _seed_owns_chain(client: Neo4jClient) -> None:
    """p1 -OWNS-> c1 -OWNS-> c2 (each entity + the edge entities carry provenance)."""
    ensure_constraints(client)
    person = _stamped(
        {"id": "p1", "schema": "Person", "properties": {"name": ["Jane Target"]}, "datasets": ["t"]}
    )
    company1 = _stamped(
        {"id": "c1", "schema": "Company", "properties": {"name": ["Shell Co"]}, "datasets": ["t"]}
    )
    company2 = _stamped(
        {"id": "c2", "schema": "Company", "properties": {"name": ["Sub Co"]}, "datasets": ["t"]}
    )
    own1 = _stamped(
        {
            "id": "o1",
            "schema": "Ownership",
            "properties": {"owner": ["p1"], "asset": ["c1"]},
            "datasets": ["t"],
        }
    )
    own2 = _stamped(
        {
            "id": "o2",
            "schema": "Ownership",
            "properties": {"owner": ["c1"], "asset": ["c2"]},
            "datasets": ["t"],
        }
    )
    write_entities(client, [person, company1, company2, own1, own2])


def _server_env(conn: tuple[str, str, str]) -> dict[str, str]:
    uri, user, password = conn
    env = os.environ.copy()
    env["ENVIRONMENT"] = "test"  # allow placeholder secrets (ADR 0061)
    env["NEO4J_URI"] = uri
    env["NEO4J_USER"] = user
    env["NEO4J_PASSWORD"] = password
    # Ensure the spawned interpreter can import the package even without an editable install.
    env["PYTHONPATH"] = os.pathsep.join([str(_SRC), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _stdio_session(
    conn: tuple[str, str, str], calls: list[tuple[str, dict[str, Any]]]
) -> tuple[list[dict[str, Any]], str, str]:
    """Spawn the server, run initialize + the given tool calls, return (frames, stdout, stderr).

    This is a proper STREAMING JSON-RPC client (what Hermes is): it writes every request
    frame and flushes BUT keeps stdin open, then reads responses off stdout as they arrive
    until every request that carries an id has been answered. Only THEN does it close stdin,
    so the server EOFs and exits without ever cancelling an in-flight handler. Each request
    carries an id (initialize=1, calls start at 2); the ``notifications/initialized``
    notification carries none and so expects no response.
    """
    frames_in: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "wm-stdio-test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    for i, (method, params) in enumerate(calls, start=2):
        frames_in.append({"jsonrpc": "2.0", "id": i, "method": method, "params": params})

    # Every frame that carries an id expects exactly one response frame back.
    expected_ids: set[int] = {f["id"] for f in frames_in if isinstance(f.get("id"), int)}
    payload = "".join(json.dumps(frame) + "\n" for frame in frames_in)

    proc = subprocess.Popen(
        [sys.executable, "-m", "worldmonitor.mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_server_env(conn),
        cwd=str(_ROOT),
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    # Drain stderr on a daemon thread so a full OS pipe buffer can never deadlock the server.
    errbuf: list[str] = []
    t = threading.Thread(target=lambda: errbuf.append(proc.stderr.read()), daemon=True)
    t.start()

    frames: list[dict[str, Any]] = []
    out = ""
    answered: set[int] = set()

    def _ingest(line: str) -> None:
        """Parse one stdout line, assert STDOUT PURITY, record the frame + answered id."""
        if not line.strip():
            return
        obj = json.loads(line)
        assert obj.get("jsonrpc") == "2.0", f"non-JSON-RPC line on stdout: {line!r}"
        frames.append(obj)
        frame_id = obj.get("id")
        if isinstance(frame_id, int) and ("result" in obj or "error" in obj):
            answered.add(frame_id)

    # Write the full payload and flush, but DO NOT close stdin (a streaming client stays open).
    proc.stdin.write(payload)
    proc.stdin.flush()

    deadline = time.monotonic() + 60.0
    while answered != expected_ids:
        if time.monotonic() > deadline:
            proc.kill()
            t.join(timeout=10)
            err_so_far = "".join(errbuf)
            raise AssertionError(
                f"stdio server did not answer all of {expected_ids} within 60s "
                f"(answered {answered}); stderr:\n{err_so_far}"
            )
        line = proc.stdout.readline()
        if line == "":  # stdout EOF — server exited; stop reading.
            break
        out += line
        _ingest(line)

    # Every expected response collected: close stdin so the server EOFs and exits cleanly.
    proc.stdin.close()
    proc.wait(timeout=30)

    trailing = proc.stdout.read()
    if trailing:
        out += trailing
        for line in trailing.splitlines():
            _ingest(line)

    t.join(timeout=10)
    err = "".join(errbuf)
    return frames, out, err


def _by_id(frames: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {f["id"]: f for f in frames if isinstance(f.get("id"), int)}


def _is_error_outcome(frame: dict[str, Any]) -> bool:
    """A JSON-RPC error: a top-level ``error`` object OR a result with ``isError`` true.

    (This SDK surfaces a raised ToolError as a result with ``isError: true``; either way it
    is a valid JSON-RPC frame on stdout — never a traceback.)
    """
    if "error" in frame:
        return True
    result = frame.get("result")
    return isinstance(result, dict) and bool(result.get("isError"))


def _result_objs(frame: dict[str, Any]) -> list[Any]:
    """Parse the JSON payload(s) a successful tools/call returned (one per content block)."""
    objs: list[Any] = []
    result = frame.get("result")
    if not isinstance(result, dict):
        return objs
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            with contextlib.suppress(ValueError, TypeError):
                objs.append(json.loads(block["text"]))
    structured = result.get("structuredContent")
    if structured is not None:
        objs.append(structured)
    return objs


def _walk(obj: Any) -> Any:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _collect_ids(objs: list[Any]) -> set[str]:
    found: set[str] = set()
    for obj in objs:
        for node in _walk(obj):
            if isinstance(node, dict) and isinstance(node.get("id"), str):
                found.add(node["id"])
    return found


def _collect_path_nodes(objs: list[Any]) -> list[list[str]]:
    paths: list[list[str]] = []
    for obj in objs:
        for node in _walk(obj):
            if isinstance(node, dict) and isinstance(node.get("nodes"), list):
                paths.append([str(n) for n in node["nodes"]])
    return paths


# ======================================================================================
# HEADLINE — stdout is ONLY JSON-RPC frames, even on the error path; logs on stderr.
# ======================================================================================
def test_stdio_stdout_is_only_jsonrpc_frames(
    clean_graph: Neo4jClient, neo4j_conn: tuple[str, str, str]
) -> None:
    _seed_owns_chain(clean_graph)

    frames, out, err = _stdio_session(
        neo4j_conn,
        [
            ("tools/list", {}),  # id 2
            ("tools/call", {"name": "get_entity", "arguments": {"entity_id": "p1"}}),
            ("tools/call", {"name": "get_entity", "arguments": {"entity_id": "zzz-absent"}}),
            ("tools/call", {"name": "get_entity", "arguments": {"entity_id": _INJECTION_ID}}),
        ],
    )
    assert frames, f"server produced no JSON-RPC frames on stdout; stderr:\n{err}"
    by_id = _by_id(frames)

    # No traceback / raw log ever appears on stdout (each line already parsed as JSON above).
    assert "Traceback" not in out, "a Python traceback leaked onto stdout"

    # tools/list exposes EXACTLY the four structured tools — no raw-Cypher / query_graph.
    list_frame = by_id[2]
    names = {t["name"] for t in list_frame["result"]["tools"]}
    assert names == {"get_entity", "get_neighbors", "get_provenance", "find_paths"}, names

    # Present id -> success result carrying provenance.
    ok = by_id[3]
    assert not _is_error_outcome(ok), f"present entity must not error: {ok}"
    ok_objs = _result_objs(ok)
    assert any(
        isinstance(node, dict) and "prov_source_id" in node
        for obj in ok_objs
        for node in _walk(obj)
    ), f"get_entity payload must carry prov_source_id; got {ok_objs}"

    # Absent id AND injection-shaped id -> error OUTCOMES, still as valid JSON-RPC frames.
    assert _is_error_outcome(by_id[4]), f"absent entity must surface as an error frame: {by_id[4]}"
    assert _is_error_outcome(by_id[5]), f"injection id must surface as an error frame: {by_id[5]}"

    # The error path logged to STDERR (the tool logs-and-rejects) — proving logs route to
    # stderr, never stdout.
    assert err.strip() != "", "the server must emit its diagnostics/logs on stderr"


# ======================================================================================
# get_neighbors / find_paths over stdio — results correct and bounds clamp.
# ======================================================================================
def test_stdio_get_neighbors_and_paths_bounded(
    clean_graph: Neo4jClient, neo4j_conn: tuple[str, str, str]
) -> None:
    _seed_owns_chain(clean_graph)

    frames, _out, err = _stdio_session(
        neo4j_conn,
        [
            ("tools/call", {"name": "get_neighbors", "arguments": {"entity_id": "p1"}}),  # id 2
            (
                "tools/call",
                {
                    "name": "find_paths",
                    "arguments": {"from_id": "p1", "to_id": "c2", "max_hops": 1},
                },
            ),  # id 3 — two hops, bounded to one -> no path
            (
                "tools/call",
                {
                    "name": "find_paths",
                    "arguments": {"from_id": "p1", "to_id": "c2", "max_hops": 3},
                },
            ),  # id 4 — reachable within three hops
        ],
    )
    assert frames, f"server produced no JSON-RPC frames on stdout; stderr:\n{err}"
    by_id = _by_id(frames)

    # get_neighbors(p1) at the default single hop -> c1 is a neighbour, c2 (two hops) is not.
    neighbour_ids = _collect_ids(_result_objs(by_id[2]))
    assert "c1" in neighbour_ids, f"c1 must be a one-hop neighbour of p1; got {neighbour_ids}"
    assert "c2" not in neighbour_ids, f"c2 is two hops away, not one-hop; got {neighbour_ids}"

    # find_paths p1->c2 bounded to one hop -> no path.
    near = _collect_path_nodes(_result_objs(by_id[3]))
    assert all("c2" not in nodes for nodes in near), (
        f"p1 -> c2 is two hops and must NOT be returned at max_hops=1; got {near}"
    )

    # find_paths p1->c2 at three hops -> the OWNS chain p1..c1..c2.
    far = _collect_path_nodes(_result_objs(by_id[4]))
    assert any("c1" in nodes and "c2" in nodes for nodes in far), (
        f"p1 -> c2 via c1 must be returned at max_hops=3; got {far}"
    )
