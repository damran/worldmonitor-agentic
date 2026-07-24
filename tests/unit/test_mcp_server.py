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
        tool_get_entity_dossier(client, entity_id, hops=1)  # -> {entity, neighbors, provenance,
            merge_history} dict (Gate F-3, ADR 0122); ToolError if absent
        build_server(*, neo4j_client=None) -> FastMCP  # registers exactly the 5 tools
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
import json
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
# Tool set — exactly the five structured tools (2a's four + Gate F-3's get_entity_dossier),
# no raw-Cypher / query_graph. Gate F-3 (ADR 0122) deliberately breaks F-2's "exactly four"
# pin — 4 -> 5, spec §6.1 P-2. RED today: the tool is not yet registered.
# ======================================================================================
def test_tool_set_is_exactly_the_five() -> None:
    server = build_server(neo4j_client=_RecordingFake(entity=_entity_fixture()))
    assert _tool_names(server) == {
        "get_entity",
        "get_neighbors",
        "get_provenance",
        "find_paths",
        "get_entity_dossier",
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


# ========================================================================================
# Gate F-2 (MCP contract polish, ADR 0121) — behavioral annotations, typed output schemas,
# structured {error, hint} envelopes. Every assertion below targets a row of the per-tool
# contract table in docs/reviews/GATE_F2_MCP_CONTRACT_POLISH_SPEC.md §4, or one of the
# parity pins PP-1/PP-2 in §5. No new invariant is touched (§3.1 of the spec records the
# decision NOT to add a property test here).
# ========================================================================================

# Gate F-3 (ADR 0122, spec §6.1 P-3): 4 -> 5, deliberately breaking F-2's PP-3 pin. Propagates
# to _list_tools's set assertion below and the F-2 annotation/schema loops, which then also
# assert the dossier tool's annotations + non-null outputSchema once it is registered.
_ALL_TOOL_NAMES = frozenset(
    {"get_entity", "get_neighbors", "get_provenance", "find_paths", "get_entity_dossier"}
)


def _list_tools(server: Any) -> dict[str, Any]:
    """``tools/list``, keyed by name — asserts the tool SET is still exactly the five."""
    tools = asyncio.run(server.list_tools())
    by_name = {t.name: t for t in tools}
    assert by_name.keys() == _ALL_TOOL_NAMES, f"tool set drifted: {by_name.keys()!r}"
    return by_name


def _call_tool(server: Any, name: str, arguments: dict[str, Any]) -> tuple[list[Any], Any]:
    """``server.call_tool`` -> ``(content blocks, structuredContent)``."""
    return asyncio.run(server.call_tool(name, arguments))


def _all_seeded_server() -> tuple[Any, _RecordingFake]:
    """A built server whose fake carries TWO items for each list-returning tool, so the
    "one content block per item" parity claim (PP-1) is actually exercised at N > 1."""
    fake = _RecordingFake(
        entity=_entity_fixture(),
        neighbors=[
            _neighbor_with_prov(),
            {"id": "C", "name": ["Gamma"], "prov_source_id": "src:test"},
        ],
        provenance={
            "prov_source_id": "src:test",
            "prov_source_record": "s3://landing/test/a.json",
        },
        paths=[
            {"nodes": ["A", "B"], "relationships": ["OWNS"]},
            {"nodes": ["A", "M", "B"], "relationships": ["OWNS", "DIRECTS"]},
        ],
    )
    return build_server(neo4j_client=fake), fake


# --- AC-1: readOnlyHint / idempotentHint / openWorldHint / destructiveHint ------------
def test_all_tools_annotated_readonly_idempotent_closedworld() -> None:
    server, _fake = _all_seeded_server()
    tools = _list_tools(server)
    for name, tool in tools.items():
        ann = tool.annotations
        assert ann is not None, f"{name} carries no ToolAnnotations at all (AC-1)"
        assert ann.readOnlyHint is True, (
            f"{name}.readOnlyHint must be True; got {ann.readOnlyHint!r}"
        )
        assert ann.idempotentHint is True, (
            f"{name}.idempotentHint must be True; got {ann.idempotentHint!r}"
        )
        assert ann.openWorldHint is False, (
            f"{name}.openWorldHint must be False (closed self-hosted graph domain); "
            f"got {ann.openWorldHint!r}"
        )
        assert ann.destructiveHint is None, (
            f"{name}.destructiveHint must be left UNSET (meaningless when read-only); "
            f"got {ann.destructiveHint!r}"
        )


# --- AC-2: outputSchema is not None, and matches the contract table's shape -----------
def test_all_tools_have_output_schema() -> None:
    """Pin `outputSchema is not None` PLUS its shape per the contract table (§4): this
    catches BOTH a totally-absent schema (silent structured_output fallback) and a
    schema whose shape drifted from the documented per-tool contract."""
    server, _fake = _all_seeded_server()
    tools = _list_tools(server)

    entity_schema = tools["get_entity"].outputSchema
    assert entity_schema is not None, "get_entity carries no outputSchema (AC-2 — silent fallback?)"
    assert entity_schema.get("type") == "object"
    assert entity_schema.get("additionalProperties") is True, (
        f"get_entity's outputSchema must be a permissive object; got {entity_schema!r}"
    )

    for name in ("get_neighbors", "find_paths"):
        schema = tools[name].outputSchema
        assert schema is not None, f"{name} carries no outputSchema (AC-2 — silent fallback?)"
        assert schema.get("type") == "object"
        result_prop = schema.get("properties", {}).get("result")
        # Gate F-5 (ADR 0124, spec §6.1 P-1) — the ONE sanctioned relaxation: `result` may
        # still be the bare array (today) OR an anyOf UNION admitting an array branch (post
        # F-5's `summary` flag, which adds a dict `{count, sample}` return shape). Either
        # way the array guarantee must hold; this does NOT accept an array-less union.
        is_bare_array = result_prop is not None and result_prop.get("type") == "array"
        is_array_branch_of_union = result_prop is not None and any(
            branch.get("type") == "array" for branch in result_prop.get("anyOf", [])
        )
        assert is_bare_array or is_array_branch_of_union, (
            f"{name}'s list-tool outputSchema must still describe an array — either bare "
            f"(today) or as one branch of a summary-flag anyOf union (Gate F-5, ADR 0124); "
            f"got {schema!r}"
        )

    prov_schema = tools["get_provenance"].outputSchema
    assert prov_schema is not None, (
        "get_provenance carries no outputSchema (AC-2 — silent fallback?)"
    )
    assert prov_schema.get("type") == "object"
    assert prov_schema.get("additionalProperties") == {"type": "string"}, (
        f"get_provenance's outputSchema must constrain values to strings; got {prov_schema!r}"
    )

    # Gate F-3 (ADR 0122, spec §6.1 P-4): the fifth tool also carries a non-null, permissive
    # object outputSchema (same shape family as get_entity — a dict[str, Any] return).
    dossier_schema = tools["get_entity_dossier"].outputSchema
    assert dossier_schema is not None, (
        "get_entity_dossier carries no outputSchema (AC-2 — silent fallback?)"
    )
    assert dossier_schema.get("type") == "object"


# --- SHOULD (not a hard acceptance gate): a non-empty human-readable title ------------
def test_tool_titles_present() -> None:
    server, _fake = _all_seeded_server()
    tools = _list_tools(server)
    for name, tool in tools.items():
        assert tool.title, f"{name} carries no title (SHOULD per the spec §4, currently unset)"


# --- AC-3 / PP-2: structured {error, hint} envelope on the four raise sites -----------
def test_error_message_is_structured_envelope() -> None:
    """Every ``ToolError`` the four tools raise carries a JSON ``{"error", "hint"}``
    envelope (ADR 0121 D3, raise-based) whose ``error`` token is BYTE-IDENTICAL to
    today's bare string (PP-2: only the *shape* becomes structured, the *signal* does
    not change)."""
    fake_absent = _RecordingFake(entity=None, provenance=None)

    with pytest.raises(ToolError) as absent_entity_exc:
        _call(tool_get_entity, fake_absent, "does-not-exist")
    envelope = json.loads(str(absent_entity_exc.value))
    assert envelope.get("error") == "entity not found"
    assert isinstance(envelope.get("hint"), str) and envelope["hint"].strip() != "", (
        f"hint must be a non-empty, actionable string; got {envelope.get('hint')!r}"
    )

    with pytest.raises(ToolError) as absent_prov_exc:
        _call(tool_get_provenance, fake_absent, "zzz-absent")
    envelope = json.loads(str(absent_prov_exc.value))
    assert envelope.get("error") == "entity not found"
    assert isinstance(envelope.get("hint"), str) and envelope["hint"].strip() != ""

    fake_valid = _RecordingFake(entity=_entity_fixture())
    with pytest.raises(ToolError) as injection_exc:
        _call(tool_get_entity, fake_valid, _INJECTION_ID)
    envelope = json.loads(str(injection_exc.value))
    assert envelope.get("error") == "invalid entity id"
    assert isinstance(envelope.get("hint"), str) and envelope["hint"].strip() != ""


# --- PP-1: happy-path content bytes unchanged; structuredContent additive+consistent --
# The oracle is the THIN tool function called directly (bypassing add_tool's
# annotations/structured_output kwargs entirely) — its code is untouched by this gate,
# so it is exactly "today's behavior" per the spec's PP-1 wording (§5).
def test_get_entity_content_and_structured_content_pp1() -> None:
    server, fake = _all_seeded_server()
    expected = _call(tool_get_entity, fake, "A")
    content, structured = _call_tool(server, "get_entity", {"entity_id": "A"})
    assert len(content) == 1, "get_entity must emit exactly ONE content block (PP-1 block count)"
    assert json.loads(content[0].text) == expected, (
        "PP-1: get_entity's content block must be byte-identical (as decoded JSON) to the "
        "thin tool function's own return value"
    )
    assert structured == expected, "structuredContent for a dict tool must be the dict itself"


def test_get_provenance_content_and_structured_content_pp1() -> None:
    server, fake = _all_seeded_server()
    expected = _call(tool_get_provenance, fake, "A")
    content, structured = _call_tool(server, "get_provenance", {"entity_id": "A"})
    assert len(content) == 1
    assert json.loads(content[0].text) == expected
    assert structured == expected


def test_get_neighbors_content_and_structured_content_pp1() -> None:
    server, fake = _all_seeded_server()
    expected = _call(tool_get_neighbors, fake, "A")
    content, structured = _call_tool(server, "get_neighbors", {"entity_id": "A"})
    assert len(content) == len(expected) == 2, "one content block per neighbour (PP-1 block count)"
    assert [json.loads(c.text) for c in content] == expected
    assert structured == {"result": expected}, (
        "list-tool structuredContent must be {'result': [...]}"
    )


def test_find_paths_content_and_structured_content_pp1() -> None:
    server, fake = _all_seeded_server()
    expected = _call(tool_find_paths, fake, "A", "B", max_hops=1)
    content, structured = _call_tool(
        server, "find_paths", {"from_id": "A", "to_id": "B", "max_hops": 1}
    )
    assert len(content) == len(expected) == 2, "one content block per path (PP-1 block count)"
    assert [json.loads(c.text) for c in content] == expected
    assert structured == {"result": expected}


# ========================================================================================
# Gate F-3 slice 1 (get_entity_dossier, ADR 0122) — the fifth tool. The thin tool function
# is imported LOCALLY inside each test (not at module top) so a missing symbol fails ONLY
# these new tests, never the whole module's collection (fail-soft, mirrors the existing
# local-import idiom already used in this file / test_mcp_http_auth.py for a not-yet-shipped
# symbol). RED today: worldmonitor.mcp.server has no tool_get_entity_dossier.
# ========================================================================================


def _dossier_merge_history_sentinel() -> dict[str, Any]:
    return {"status": "not_assembled", "available": False}


def test_get_entity_dossier_tool_returns_sections() -> None:
    from worldmonitor.mcp.server import tool_get_entity_dossier

    fake = _RecordingFake(
        entity=_entity_fixture(),
        neighbors=[_neighbor_with_prov()],
        provenance={
            "prov_source_id": "src:test",
            "prov_source_record": "s3://landing/test/a.json",
        },
    )
    dossier = _call(tool_get_entity_dossier, fake, "A")

    assert set(dossier.keys()) == {"entity", "neighbors", "provenance", "merge_history"}, (
        f"dossier must have exactly the four top-level keys (§4); got {list(dossier.keys())}"
    )
    assert dossier["entity"]["id"] == "A"
    assert dossier["entity"]["prov_source_id"] == "src:test"
    neighbor = next(n for n in dossier["neighbors"] if n.get("id") == "B")
    assert neighbor["prov_source_id"] == "src:test"
    assert dossier["provenance"]["prov_source_id"] == "src:test"
    assert dossier["provenance"]["prov_source_record"] == "s3://landing/test/a.json"
    assert dossier["merge_history"] == _dossier_merge_history_sentinel()


def test_get_entity_dossier_absent_raises() -> None:
    from worldmonitor.mcp.server import tool_get_entity_dossier

    fake = _RecordingFake(entity=None)
    with pytest.raises(ToolError) as exc:
        _call(tool_get_entity_dossier, fake, "does-not-exist")
    envelope = json.loads(str(exc.value))
    assert envelope.get("error") == "entity not found"
    assert isinstance(envelope.get("hint"), str) and envelope["hint"].strip() != ""


def test_get_entity_dossier_injection_raises() -> None:
    from worldmonitor.mcp.server import tool_get_entity_dossier

    fake = _RecordingFake(entity=_entity_fixture())
    with pytest.raises(ToolError) as exc:
        _call(tool_get_entity_dossier, fake, _INJECTION_ID)
    envelope = json.loads(str(exc.value))
    assert envelope.get("error") == "invalid entity id"
    assert isinstance(envelope.get("hint"), str) and envelope["hint"].strip() != ""
    assert fake.read_calls == [], "an injection-shaped id must never reach execute_read"


def test_get_entity_dossier_read_only() -> None:
    from worldmonitor.mcp.server import tool_get_entity_dossier

    fake = _RecordingFake(
        entity=_entity_fixture(),
        neighbors=[_neighbor_with_prov()],
        provenance={"prov_source_id": "src:test"},
    )
    _call(tool_get_entity_dossier, fake, "A")
    assert fake.read_calls, "the tool must actually query (via execute_read)"
    for query, _ in fake.read_calls:
        assert not _WRITE_KEYWORDS.search(query), (
            f"get_entity_dossier issued Cypher containing a write keyword: {query!r}"
        )


def test_get_entity_dossier_content_and_structured_content_pp1() -> None:
    """Mirrors the four existing PP-1 parity tests: the tool's content block(s) + structuredContent,
    driven THROUGH the built server (server.add_tool machinery), are byte-identical (as decoded
    JSON) to the thin tool function's own return value."""
    from worldmonitor.mcp.server import tool_get_entity_dossier

    server, fake = _all_seeded_server()
    expected = _call(tool_get_entity_dossier, fake, "A")
    content, structured = _call_tool(server, "get_entity_dossier", {"entity_id": "A"})
    assert len(content) == 1, "get_entity_dossier must emit exactly ONE content block (a dict tool)"
    assert json.loads(content[0].text) == expected
    assert structured == expected, "structuredContent for a dict tool must be the dict itself"


def test_get_entity_dossier_absent_error_envelope_via_call_tool() -> None:
    """AC-3 via server.call_tool (not the bare thin function): Tool.run wraps ANY raised exception
    as ``Error executing tool <name>: <msg>`` regardless of transport (mcp 1.28.1, confirmed in
    Tool.run's except-Exception branch) — so this pins the SAME strip-prefix + json.loads pattern
    the wire-level integration test uses, at the unit level."""
    server = build_server(neo4j_client=_RecordingFake(entity=None))
    with pytest.raises(ToolError) as exc:
        _call_tool(server, "get_entity_dossier", {"entity_id": "does-not-exist"})
    text = str(exc.value)
    stripped = re.sub(r"^Error executing tool \S+: ", "", text, count=1)
    envelope = json.loads(stripped)
    assert set(envelope.keys()) == {"error", "hint"}
    assert envelope["error"] == "entity not found"
    assert isinstance(envelope["hint"], str) and envelope["hint"].strip() != ""


# ========================================================================================
# Gate F-5 (`summary` context-budget flag, ADR 0124/spec `docs/reviews/
# GATE_F5_SUMMARY_FLAG_SPEC.md`) — `get_neighbors` / `find_paths` gain an opt-in
# `summary: bool = False` argument; when set, both tools return the shared
# `graph.queries.summarize_result` envelope `{count, sample}` instead of the full list,
# and their return annotation widens to a union (`list[dict] | dict`).
#
# `tool_get_neighbors` / `tool_find_paths` are ALREADY imported at module top (they exist
# today, just without a `summary` parameter) — calling them with `summary=...` raises
# TypeError until the builder adds the kwarg. That TypeError IS the RED failure mode for
# the tests below that drive the thin functions directly.
# ========================================================================================


def _many_neighbors_for_summary() -> list[dict[str, Any]]:
    # Deliberately out of alphabetical order — exercises the canonical-sort determinism
    # (ADR 0124 §3.4): sorted by json.dumps(d, sort_keys=True) ascending on the shared "id"
    # key (the first alphabetical key every element carries) -> A, B, C, D, E.
    return [
        {"id": "E", "name": ["Echo"], "prov_source_id": "src:test"},
        {"id": "B", "name": ["Bravo"], "prov_source_id": "src:test"},
        {"id": "D", "name": ["Delta"], "prov_source_id": "src:test"},
        {"id": "A", "name": ["Alpha"], "prov_source_id": "src:test"},
        {"id": "C", "name": ["Charlie"], "prov_source_id": "src:test"},
    ]


def test_get_neighbors_summary_returns_envelope() -> None:
    """§6.6: `summary=True` -> exactly {count, sample}; count == len(seeded); sample capped
    at 3; every sample element carries its own prov_* verbatim (G1 not laundered)."""
    fake = _RecordingFake(neighbors=_many_neighbors_for_summary())
    result = _call(tool_get_neighbors, fake, "A", summary=True)
    assert set(result.keys()) == {"count", "sample"}, (
        f"summary envelope must be exactly {{count, sample}}; got keys {list(result.keys())}"
    )
    assert result["count"] == 5
    assert len(result["sample"]) == 3
    for item in result["sample"]:
        assert item in _many_neighbors_for_summary(), (
            f"sample element {item!r} is not one of the seeded neighbours"
        )
        assert item.get("prov_source_id") == "src:test", (
            f"a sample element must carry its own provenance verbatim (G1); got {item!r}"
        )
    assert [item["id"] for item in result["sample"]] == ["A", "B", "C"]


def test_find_paths_summary_returns_envelope() -> None:
    """§6.6 analogue for find_paths."""
    fake = _RecordingFake(
        paths=[
            {"nodes": ["A", "D"], "relationships": ["OWNS"]},
            {"nodes": ["A", "B"], "relationships": ["OWNS"]},
            {"nodes": ["A", "C"], "relationships": ["OWNS"]},
            {"nodes": ["A", "Z"], "relationships": ["OWNS"]},
        ]
    )
    result = _call(tool_find_paths, fake, "A", "B", max_hops=1, summary=True)
    assert set(result.keys()) == {"count", "sample"}
    assert result["count"] == 4
    assert len(result["sample"]) == 3
    assert [item["nodes"] for item in result["sample"]] == [["A", "B"], ["A", "C"], ["A", "D"]]


def test_get_neighbors_summary_read_only() -> None:
    """§6.6: driving the summary path touches ONLY execute_read (the fake raises on any
    write path — a structural, not merely asserted, read-only proof)."""
    fake = _RecordingFake(neighbors=_many_neighbors_for_summary())
    _call(tool_get_neighbors, fake, "A", summary=True)
    assert fake.read_calls, "the summary path must still actually query (via execute_read)"
    for query, _ in fake.read_calls:
        assert not _WRITE_KEYWORDS.search(query), (
            f"the summary path issued Cypher containing a write keyword: {query!r}"
        )


def test_get_neighbors_summary_absent_and_false_match_pp1_baseline() -> None:
    """PP-1-style differential pin: `summary` OMITTED and `summary=False` explicit must both
    be byte-identical to today's baseline list return — driving the SAME fake three ways
    proves the new union annotation adds ZERO normal-mode bytes."""
    fake = _RecordingFake(neighbors=_many_neighbors_for_summary())
    baseline = _call(tool_get_neighbors, fake, "A")
    explicit_false = _call(tool_get_neighbors, fake, "A", summary=False)
    assert explicit_false == baseline


def test_find_paths_summary_absent_and_false_match_pp1_baseline() -> None:
    fake = _RecordingFake(paths=[{"nodes": ["A", "B"], "relationships": ["OWNS"]}])
    baseline = _call(tool_find_paths, fake, "A", "B", max_hops=1)
    explicit_false = _call(tool_find_paths, fake, "A", "B", max_hops=1, summary=False)
    assert explicit_false == baseline


def test_get_neighbors_and_find_paths_output_schema_admits_object_branch() -> None:
    """AC-3 — the NEW half of the union that P-1's relaxed pin (above) merely tolerates:
    post-Gate-F-5 both tools' `outputSchema.result` is an `anyOf` admitting BOTH an array
    branch (today's shape) AND an object branch (the `{count, sample}` summary shape). RED
    today: `result` is a bare `{"type": "array", ...}` with no `anyOf` at all — a builder
    cannot satisfy this by merely deleting the array assertion (P-1's array branch is
    pinned separately, above, and stays green throughout)."""
    server, _fake = _all_seeded_server()
    tools = _list_tools(server)
    for name in ("get_neighbors", "find_paths"):
        schema = tools[name].outputSchema
        result_prop = schema.get("properties", {}).get("result")
        assert result_prop is not None
        any_of = result_prop.get("anyOf")
        assert any_of, (
            f"{name}'s outputSchema.result must be an anyOf union post-Gate-F-5; got "
            f"{result_prop!r}"
        )
        assert any(branch.get("type") == "array" for branch in any_of), (
            f"{name}'s anyOf must retain an array branch; got {any_of!r}"
        )
        assert any(branch.get("type") == "object" for branch in any_of), (
            f"{name}'s anyOf must gain an object branch for the summary shape; got {any_of!r}"
        )

    # The other three tools' outputSchema stays untouched (strict, unchanged shape family).
    entity_schema = tools["get_entity"].outputSchema
    assert entity_schema.get("additionalProperties") is True
    assert "anyOf" not in entity_schema.get("properties", {})
    prov_schema = tools["get_provenance"].outputSchema
    assert prov_schema.get("additionalProperties") == {"type": "string"}
    dossier_schema = tools["get_entity_dossier"].outputSchema
    assert dossier_schema.get("type") == "object"
    assert "anyOf" not in dossier_schema.get("properties", {})


def test_get_neighbors_summary_content_and_structured_content_via_call_tool() -> None:
    """Wire-level (through server.call_tool, not the bare thin function): `summary=True` ->
    exactly ONE content block whose decoded JSON is `{count, sample}`, and
    `structuredContent == {"result": {count, sample}}` (the SDK wraps the dict under
    "result" — spec §3.5). Uses `_all_seeded_server()`'s fake, which carries 2 neighbours."""
    server, _fake = _all_seeded_server()
    content, structured = _call_tool(server, "get_neighbors", {"entity_id": "A", "summary": True})
    assert len(content) == 1, "summary mode must emit exactly ONE content block"
    decoded = json.loads(content[0].text)
    assert set(decoded.keys()) == {"count", "sample"}
    assert decoded["count"] == 2
    assert structured == {"result": decoded}, (
        "structuredContent for the summary dict must be {'result': {count, sample}} (the "
        f"SDK wraps the dict); got {structured!r}"
    )


def test_find_paths_summary_content_and_structured_content_via_call_tool() -> None:
    """Wire-level analogue for find_paths — `_all_seeded_server()`'s fake carries 2 paths."""
    server, _fake = _all_seeded_server()
    content, structured = _call_tool(
        server, "find_paths", {"from_id": "A", "to_id": "B", "max_hops": 1, "summary": True}
    )
    assert len(content) == 1
    decoded = json.loads(content[0].text)
    assert set(decoded.keys()) == {"count", "sample"}
    assert decoded["count"] == 2
    assert structured == {"result": decoded}
