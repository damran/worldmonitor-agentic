"""Unit tests for the graph-read FastMCP stdio server (ADR 0063, slice 2b).

These drive the THIN, module-level tool functions and ``build_server`` directly
against a recording FAKE Neo4j client — no real connection, no JSON-RPC loop. The
fake RAISES if a tool ever touches a write path (``execute_write`` / ``session`` /
``verify``), so "read-only" is proven structurally, not merely asserted.

CONTRACT ASSUMED (the builder MUST match these names/signatures exactly):
    worldmonitor.mcp.server
        configure_stderr_logging() -> None          # idempotent; stderr-only; no print()
        tool_get_entity(client, entity_id)  # -> entity props (incl prov_*); ToolError if absent
        tool_get_neighbors(client, entity_id, hops=1)  # -> list of neighbour props (each w/ prov_*)
        tool_get_provenance(client, entity_id)      # -> prov_* map; ToolError if absent
        tool_find_paths(client, from_id, to_id, max_hops=1)  # -> list of {nodes, relationships}
        build_server(*, neo4j_client=None) -> FastMCP  # registers exactly the 4 tools
    Each tool validates id-shape via read_guards BEFORE any execute_read (raising
    ``mcp.server.fastmcp.exceptions.ToolError`` on a bad id), clamps hops via
    ``read_guards.clamp_hops``, and calls the matching ``graph.queries`` helper.

    The tools may be ``def`` or ``async def`` — the ``_call`` helper awaits a
    coroutine if returned, so both shapes pass.

RED today: ``worldmonitor.mcp.server`` does not exist (ModuleNotFoundError on import),
so every test below is red for the right reason.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sys
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from worldmonitor.graph import read_guards
from worldmonitor.mcp.server import (
    build_server,
    configure_stderr_logging,
    tool_find_paths,
    tool_get_entity,
    tool_get_neighbors,
    tool_get_provenance,
)

# Query fragments that uniquely identify each existing queries.py helper's Cypher — the
# thin MCP tools wrap those helpers verbatim (ADR 0063), so these are stable.
_ENTITY_FRAGMENT = "RETURN properties(n) AS props"
_NEIGHBORS_FRAGMENT = "properties(m) AS props"
_PROVENANCE_FRAGMENT = "STARTS WITH 'prov_'"

_INJECTION_ID = '") DETACH DELETE n //'
_WRITE_KEYWORDS = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE|DROP)\b")


# --------------------------------------------------------------------------------------
# Recording fake — duck-types Neo4jClient's read path; explodes on any write path.
# --------------------------------------------------------------------------------------
class _RecordingFake:
    def __init__(
        self,
        *,
        entity: dict[str, Any] | None = None,
        neighbors: list[dict[str, Any]] | None = None,
        provenance: dict[str, str] | None = None,
        paths: list[dict[str, Any]] | None = None,
    ) -> None:
        self.entity = entity
        self.neighbors = neighbors or []
        self.provenance = provenance
        self.paths = paths or []
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.read_calls.append((query, params))
        if _ENTITY_FRAGMENT in query and _NEIGHBORS_FRAGMENT not in query:
            return [{"props": self.entity}] if self.entity is not None else []
        if _NEIGHBORS_FRAGMENT in query:
            return [{"props": n} for n in self.neighbors]
        if _PROVENANCE_FRAGMENT in query:
            if self.provenance is None:
                return []
            return [{"prov": [[k, v] for k, v in self.provenance.items()]}]
        # Fallback: find_paths' shortestPath query.
        return [{"nodes": p["nodes"], "relationships": p["relationships"]} for p in self.paths]

    # Any of these being touched is a contract breach — a read tool must never write.
    def execute_write(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("a read tool must NEVER call execute_write")

    def session(self) -> Any:
        raise AssertionError("a read tool must NEVER open a write session")

    def verify(self) -> None:
        raise AssertionError("build_server must not verify/connect an injected client")

    # --- assertion helpers --------------------------------------------------------------
    def call_with(self, fragment: str) -> tuple[str, dict[str, Any]] | None:
        for query, params in self.read_calls:
            if fragment in query:
                return query, params
        return None

    def neighbors_call(self) -> tuple[str, dict[str, Any]] | None:
        return self.call_with(_NEIGHBORS_FRAGMENT)

    def paths_call(self) -> tuple[str, dict[str, Any]] | None:
        for query, params in self.read_calls:
            if (
                _ENTITY_FRAGMENT not in query
                and _NEIGHBORS_FRAGMENT not in query
                and _PROVENANCE_FRAGMENT not in query
            ):
                return query, params
        return None


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a thin tool whether it is sync or async (await a returned coroutine)."""
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _tool_names(server: Any) -> set[str]:
    """Enumerate a built server's registered tool names (public async list_tools API)."""
    tools = asyncio.run(server.list_tools())
    return {t.name for t in tools}


def _entity_fixture() -> dict[str, Any]:
    return {
        "id": "A",
        "name": ["Acme Holdings"],
        "prov_source_id": "src:test",
        "prov_source_record": "s3://landing/test/a.json",
        "prov_retrieved_at": "2026-06-21T00:00:00Z",
        "prov_reliability": "A",
    }


def _neighbor_with_prov() -> dict[str, Any]:
    return {
        "id": "B",
        "name": ["Beta"],
        "prov_source_id": "src:test",
        "prov_source_record": "s3://landing/b.json",
    }


# ======================================================================================
# HEADLINE — stdout purity: a tool that logs-and-raises leaves stdout empty (capfd).
# A print()-to-stdout or a stdout-bound log handler MUST fail this.
# ======================================================================================
def test_stdout_clean_when_tool_logs_and_raises(capfd: pytest.CaptureFixture[str]) -> None:
    configure_stderr_logging()
    fake = _RecordingFake(entity=_entity_fixture())

    # An injection-shaped id: the tool must log a warning then raise ToolError.
    with pytest.raises(ToolError):
        _call(tool_get_entity, fake, _INJECTION_ID)

    captured = capfd.readouterr()
    assert captured.out == "", (
        f"stdout MUST stay empty (it is the JSON-RPC frame channel); leaked: {captured.out!r}"
    )
    # The warning/diagnostic must have gone to stderr, not vanished and not to stdout.
    assert captured.err != "", "the tool's log-on-rejection must be written to stderr"
    # And the injection-shaped id never reached the Neo4j client.
    assert fake.read_calls == [], "validation must short-circuit before execute_read"


def test_configure_stderr_logging_is_idempotent_and_never_stdout() -> None:
    wm_logger = logging.getLogger("worldmonitor")
    configure_stderr_logging()
    after_first = list(wm_logger.handlers)
    stream_handlers = [h for h in after_first if isinstance(h, logging.StreamHandler)]
    assert stream_handlers, "configure_stderr_logging must attach a StreamHandler to 'worldmonitor'"
    for handler in stream_handlers:
        assert getattr(handler, "stream", None) is not sys.stdout, (
            "no logging handler may target stdout (it corrupts the JSON-RPC stream)"
        )
    configure_stderr_logging()
    after_second = list(wm_logger.handlers)
    assert len(after_second) == len(after_first), (
        "configure_stderr_logging must be idempotent — calling it twice must not add a "
        "duplicate handler"
    )


# ======================================================================================
# Tool set — exactly the four 2a structured tools, no raw-Cypher / query_graph.
# ======================================================================================
def test_tool_set_is_exactly_the_four() -> None:
    server = build_server(neo4j_client=_RecordingFake(entity=_entity_fixture()))
    assert _tool_names(server) == {
        "get_entity",
        "get_neighbors",
        "get_provenance",
        "find_paths",
    }


def test_build_server_does_not_open_a_real_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    from worldmonitor.graph.neo4j_client import Neo4jClient

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("from_settings must NOT be called when a client is injected")

    monkeypatch.setattr(Neo4jClient, "from_settings", _boom)
    # The injected fake's verify() also raises if touched — build_server must not connect.
    server = build_server(neo4j_client=_RecordingFake(entity=_entity_fixture()))
    assert server is not None


# ======================================================================================
# get_entity — props incl provenance; absent -> ToolError.
# ======================================================================================
def test_get_entity_returns_props_with_provenance() -> None:
    fake = _RecordingFake(entity=_entity_fixture())
    entity = _call(tool_get_entity, fake, "A")
    assert entity["id"] == "A"
    assert entity["prov_source_id"] == "src:test"
    assert entity["prov_source_record"] == "s3://landing/test/a.json"


def test_get_entity_absent_raises() -> None:
    fake = _RecordingFake(entity=None)
    with pytest.raises(ToolError):
        _call(tool_get_entity, fake, "does-not-exist")


# ======================================================================================
# get_provenance — the prov_* map; absent -> ToolError (parity with 2a's 404).
# ======================================================================================
def test_get_provenance_returns_prov_star() -> None:
    fake = _RecordingFake(
        provenance={
            "prov_source_id": "src:test",
            "prov_source_record": "s3://landing/test/a.json",
        }
    )
    prov = _call(tool_get_provenance, fake, "A")
    assert prov["prov_source_id"] == "src:test"
    assert prov["prov_source_record"] == "s3://landing/test/a.json"


def test_get_provenance_absent_raises() -> None:
    fake = _RecordingFake(provenance=None)  # get_provenance -> {} -> must raise (not fail-open)
    with pytest.raises(ToolError):
        _call(tool_get_provenance, fake, "zzz-absent")


# ======================================================================================
# get_neighbors — neighbours carry their own provenance; hops clamped to the shared cap.
# ======================================================================================
def test_get_neighbors_carries_provenance() -> None:
    fake = _RecordingFake(neighbors=[_neighbor_with_prov()])
    neighbors = _call(tool_get_neighbors, fake, "A")
    neighbor = next(n for n in neighbors if n.get("id") == "B")
    assert neighbor["prov_source_id"] == "src:test"
    assert neighbor["prov_source_record"] == "s3://landing/b.json"


def test_neighbors_clamps_hops_to_shared_cap() -> None:
    fake = _RecordingFake(neighbors=[_neighbor_with_prov()])
    _call(tool_get_neighbors, fake, "A", hops=99)

    call = fake.neighbors_call()
    assert call is not None, "the neighbors helper was never invoked"
    query, _ = call
    match = re.search(r"\*1\.\.(\d+)", query)
    assert match is not None, f"could not read the traversal depth from: {query!r}"
    # ASSERT against the imported shared constant — the MCP cap must equal read_guards.HOP_CAP,
    # not a literal that could silently drift.
    assert int(match.group(1)) == read_guards.HOP_CAP, "MCP hop bound must equal HOP_CAP"
    assert "99" not in query, "the unclamped hop count leaked into the query"


# ======================================================================================
# find_paths — max_hops clamped; bounded.
# ======================================================================================
def test_find_paths_clamps_max_hops() -> None:
    fake = _RecordingFake(paths=[{"nodes": ["A", "M", "B"], "relationships": ["OWNS", "DIRECTS"]}])
    _call(tool_find_paths, fake, "A", "B", max_hops=99)

    call = fake.paths_call()
    assert call is not None, "the find_paths helper was never invoked"
    query, _ = call
    assert "99" not in query, "max_hops not clamped: 99 leaked into the find_paths query"
    bounds = re.findall(r"\.\.(\d+)", query)
    assert bounds, f"could not read a traversal bound from: {query!r}"
    for bound in bounds:
        assert int(bound) <= read_guards.HOP_CAP, f"find_paths bound {bound} exceeds the shared cap"


# ======================================================================================
# Injection + parameterization — id-shape validated BEFORE any execute_read; ids are
# bound params, never string-interpolated into Cypher.
# ======================================================================================
def test_injection_shaped_id_rejected_before_execute_read() -> None:
    fake = _RecordingFake(entity=_entity_fixture())
    with pytest.raises(ToolError):
        _call(tool_get_entity, fake, _INJECTION_ID)
    assert fake.read_calls == [], "an injection-shaped id must never reach execute_read"


def test_valid_id_is_a_bound_param() -> None:
    fake = _RecordingFake(entity=_entity_fixture())
    _call(tool_get_entity, fake, "Q42")
    call = fake.call_with(_ENTITY_FRAGMENT)
    assert call is not None, "get_entity helper was never invoked"
    query, params = call
    assert params.get("entity_id") == "Q42", "the id must be passed as a BOUND parameter"
    assert "Q42" not in query, "the id must NOT be string-interpolated into the Cypher"
    assert "$entity_id" in query, "the query must reference the id via the $entity_id param"


def test_tools_only_call_execute_read() -> None:
    fake = _RecordingFake(
        entity=_entity_fixture(),
        neighbors=[_neighbor_with_prov()],
        provenance={"prov_source_id": "src:test"},
        paths=[{"nodes": ["A", "B"], "relationships": ["OWNS"]}],
    )
    # Drive all four tools — execute_write / session would RAISE inside the fake if touched.
    _call(tool_get_entity, fake, "A")
    _call(tool_get_neighbors, fake, "A", hops=1)
    _call(tool_get_provenance, fake, "A")
    _call(tool_find_paths, fake, "A", "B", max_hops=1)

    assert fake.read_calls, "the tools must actually query (via execute_read)"
    for query, _ in fake.read_calls:
        assert not _WRITE_KEYWORDS.search(query), (
            f"a read tool issued Cypher containing a write keyword: {query!r}"
        )
