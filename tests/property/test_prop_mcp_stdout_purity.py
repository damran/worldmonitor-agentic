"""Property: MCP stdout purity + bounded traversal + id-gated reads (ADR 0063, slice 2b).

ADR 0063's "Invariant gate note" RECOMMENDS a ``@given`` property over the MCP tool
inputs: for EVERY input (random id incl. injection-shaped + canonical-shaped; random
hops incl. huge / zero / negative), assert

    (1) stdout stays byte-empty — even on the raise path (stdout is the JSON-RPC channel);
    (2) any recorded traversal bound (``*1..N`` / ``..N``) never exceeds read_guards.HOP_CAP;
    (3) an id failing ID_PATTERN never produces an execute_read carrying that id.

This drives the THIN tool functions directly against a recording fake (no JSON-RPC loop)
so the input space is cheap to sweep. The fake RAISES on any write path.

RED today: ``worldmonitor.mcp.server`` does not exist (ModuleNotFoundError on import).

Settings mirror tests/property convention (deadline=None + HealthCheck suppression) so a
busy runner never fails a CORRECT example for being slow; ``function_scoped_fixture`` is
suppressed because ``capfd`` is read-and-cleared inside each example (intentional).
"""

from __future__ import annotations

import asyncio
import inspect
import re
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mcp.server.fastmcp.exceptions import ToolError

from worldmonitor.graph.read_guards import HOP_CAP, validate_entity_id
from worldmonitor.mcp.server import (
    configure_stderr_logging,
    tool_find_paths,
    tool_get_neighbors,
)

_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# Ids: canonical-shaped, injection-shaped, and arbitrary unicode text — so both the
# valid (read issued) and invalid (rejected) branches are exercised every run.
_CANONICAL = st.sampled_from(
    ["Q42", "opensanctions:abc-1", "geonames:123", "iso-3166:US", "lei:5493001KJTIIGC8Y1R12"]
)
_INJECTION = st.sampled_from(
    ['") DETACH DELETE n //', "a b", "a{b}", "a$b", "", "a\nMATCH", "p1' OR '1'='1"]
)
_IDS = st.one_of(_CANONICAL, _INJECTION, st.text(max_size=24))
_HOPS = st.integers(min_value=-100, max_value=100_000)

# Traversal bounds appear as `*1..N` (neighbours) or `..N` (paths); this catches both.
_BOUND = re.compile(r"\.\.(\d+)")


class _RecordingFake:
    def __init__(self) -> None:
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.read_calls.append((query, params))
        if "RETURN properties(n) AS props" in query and "properties(m) AS props" not in query:
            # get_entity (incl. via get_entity_dossier) -> a present, prov-stamped node, so
            # the dossier property test below also exercises the get_neighbors leg instead
            # of always short-circuiting on an absent entity.
            return [{"props": {"id": params.get("entity_id", "x"), "prov_source_id": "src:test"}}]
        if "properties(m) AS props" in query:
            return []  # get_neighbors -> empty neighbour set
        if "STARTS WITH 'prov_'" in query:
            return [{"prov": [["prov_source_id", "src:test"]]}]
        # find_paths fallback -> no path.
        return []

    def execute_write(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("a read tool must NEVER call execute_write")

    def session(self) -> Any:
        raise AssertionError("a read tool must NEVER open a write session")


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _drive(fn: Any, fake: _RecordingFake, *args: Any) -> bool:
    """Run a tool; return whether it raised ToolError."""
    try:
        _call(fn, fake, *args)
        return False
    except ToolError:
        return True


def _assert_invariants(
    fake: _RecordingFake,
    entity_id: str,
    raised: bool,
    captured: pytest.CaptureResult[str],
) -> None:
    # (1) stdout stays byte-empty on every path, including the raise path.
    assert captured.out == "", f"stdout must stay empty; leaked: {captured.out!r}"
    # (2) every issued traversal bound is clamped to the shared cap.
    for query, _ in fake.read_calls:
        for bound in _BOUND.findall(query):
            assert int(bound) <= HOP_CAP, f"traversal bound {bound} exceeds HOP_CAP={HOP_CAP}"
    # (3) an id that fails ID_PATTERN must raise and must NEVER reach execute_read.
    if not validate_entity_id(entity_id):
        assert raised, f"id {entity_id!r} fails ID_PATTERN but the tool did not raise ToolError"
        assert fake.read_calls == [], (
            f"id {entity_id!r} fails ID_PATTERN but reached execute_read: {fake.read_calls}"
        )


@given(entity_id=_IDS, hops=_HOPS)
@_SETTINGS
def test_get_neighbors_stdout_pure_and_bounded(
    capfd: pytest.CaptureFixture[str], entity_id: str, hops: int
) -> None:
    configure_stderr_logging()
    fake = _RecordingFake()
    raised = _drive(tool_get_neighbors, fake, entity_id, hops)
    _assert_invariants(fake, entity_id, raised, capfd.readouterr())


@given(entity_id=_IDS, max_hops=_HOPS)
@_SETTINGS
def test_find_paths_stdout_pure_and_bounded(
    capfd: pytest.CaptureFixture[str], entity_id: str, max_hops: int
) -> None:
    configure_stderr_logging()
    fake = _RecordingFake()
    # Use the same (possibly hostile) id for both endpoints — either bad endpoint must reject.
    raised = _drive(tool_find_paths, fake, entity_id, entity_id, max_hops)
    _assert_invariants(fake, entity_id, raised, capfd.readouterr())


# ========================================================================================
# Gate F-3 slice 1 (get_entity_dossier, ADR 0122) — the fifth tool joins stdout-purity +
# bounded-traversal + id-gated-read coverage (scope §2 lists this file as "extend").
# ``tool_get_entity_dossier`` is imported LOCALLY (fail-soft): a missing symbol fails ONLY
# this test, never the two tests above. RED today: worldmonitor.mcp.server has no
# tool_get_entity_dossier.
# ========================================================================================
@given(entity_id=_IDS, hops=_HOPS)
@_SETTINGS
def test_get_entity_dossier_stdout_pure_and_bounded(
    capfd: pytest.CaptureFixture[str], entity_id: str, hops: int
) -> None:
    from worldmonitor.mcp.server import tool_get_entity_dossier

    configure_stderr_logging()
    fake = _RecordingFake()
    raised = _drive(tool_get_entity_dossier, fake, entity_id, hops)
    _assert_invariants(fake, entity_id, raised, capfd.readouterr())
