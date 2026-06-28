"""Unit tests for graph read queries — result-LIMIT + internal hop clamp (ADR 0064).

Recording-fake unit tests for ``graph/queries.py::get_neighbors`` (and ``find_paths``),
mirroring the fake-client style in ``tests/unit/test_api_graph.py`` /
``tests/unit/test_mcp_server.py``: a fake ``Neo4jClient`` whose
``execute_read(query, **params)`` RECORDS ``(query, params)`` and returns ``[]``. The
read helpers interpolate BOTH the variable-length traversal bound AND the result
``LIMIT`` as literals into the Cypher string (Cypher cannot parameterize either), so
the recorded query string is the oracle for this gate.

ADR 0064 contract under test (the builder MUST match these exactly):
  - ``read_guards.NEIGHBOR_RESULT_LIMIT`` (positive int, default 500), read
    FULLY-QUALIFIED at call time so a test can monkeypatch it low.
  - ``get_neighbors`` clamps its OWN depth (defense-in-depth):
    ``depth = max(1, min(int(hops), read_guards.HOP_CAP))``.
  - ``get_neighbors`` appends ``LIMIT {read_guards.NEIGHBOR_RESULT_LIMIT}``.
  - ``find_paths`` sources its ``LIMIT`` from ``read_guards.PATH_RESULT_LIMIT`` — no
    module-local ``_PATH_RESULT_LIMIT`` literal remains.

RED today: ``get_neighbors`` carries NO ``LIMIT`` and clamps only ``max(1, int(hops))``
(so ``hops=99`` emits ``*1..99``); ``find_paths`` uses a module-local
``_PATH_RESULT_LIMIT``, so monkeypatching ``read_guards.PATH_RESULT_LIMIT`` is ignored
and its query still says ``LIMIT 50``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from worldmonitor.graph import read_guards
from worldmonitor.graph.queries import find_paths, get_neighbors

# Word-boundary write keywords — get_neighbors is a read; none of these may appear in
# its Cypher (RETURN/DISTINCT/properties contain none of them).
_WRITE_KEYWORDS = re.compile(r"\b(CREATE|MERGE|SET|DELETE|REMOVE)\b")


class _RecordingFake:
    """Duck-types ``Neo4jClient.execute_read``; records every ``(query, params)``.

    Returns ``[]`` (shape-agnostic — this gate asserts on the *query string*, not rows).
    Every WRITE path RAISES, so "read-only" is proven structurally: a helper that ever
    touched a write would blow up here rather than pass silently.
    """

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.read_calls.append((query, params))
        return []

    def execute_write(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("get_neighbors must NEVER call execute_write")

    def session(self) -> Any:
        raise AssertionError("get_neighbors must NEVER open a write session")

    def last_query(self) -> str:
        assert self.read_calls, "no query was recorded — the read helper never ran"
        return self.read_calls[-1][0]


def _limit_value(query: str) -> int:
    """Return the integer following the query's ``LIMIT`` clause (asserts one exists)."""
    match = re.search(r"LIMIT\s+(\d+)", query)
    assert match is not None, f"query carries no LIMIT clause: {query!r}"
    return int(match.group(1))


def _depth_bound(query: str) -> int:
    """Return the upper bound of the ``*1..N`` variable-length pattern in the query."""
    match = re.search(r"\*1\.\.(\d+)", query)
    assert match is not None, f"could not read the traversal depth bound from: {query!r}"
    return int(match.group(1))


# ======================================================================================
# get_neighbors carries a result LIMIT sourced from read_guards.NEIGHBOR_RESULT_LIMIT.
# RED today: the get_neighbors query has no LIMIT at all.
# ======================================================================================
def test_get_neighbors_query_has_result_limit() -> None:
    fake = _RecordingFake()
    get_neighbors(fake, entity_id="A", hops=1)
    query = fake.last_query()
    assert "LIMIT" in query, (
        f"get_neighbors must bound its result count with a LIMIT (ADR 0064): {query!r}"
    )
    # Compared to the IMPORTED constant, never a literal 500, so the test cannot silently
    # drift from the source of truth if the default is ever retuned.
    assert _limit_value(query) == read_guards.NEIGHBOR_RESULT_LIMIT, (
        "the LIMIT must equal read_guards.NEIGHBOR_RESULT_LIMIT (single source of truth)"
    )


# ======================================================================================
# get_neighbors clamps its OWN depth to HOP_CAP (defense-in-depth) — a DIRECT caller
# (not just the two surfaces, which already clamp) can never request unbounded depth.
# RED today: depth = max(1, int(hops)), so hops=99 emits "*1..99".
# ======================================================================================
@pytest.mark.parametrize("hops", [99, 1_000_000_000])
def test_get_neighbors_clamps_own_depth_to_hop_cap(hops: int) -> None:
    fake = _RecordingFake()
    get_neighbors(fake, entity_id="A", hops=hops)
    query = fake.last_query()
    assert _depth_bound(query) == read_guards.HOP_CAP, (
        f"get_neighbors must clamp its own depth to read_guards.HOP_CAP "
        f"({read_guards.HOP_CAP}), got *1..{_depth_bound(query)} for hops={hops}"
    )
    assert str(hops) not in query, (
        f"the unclamped hop count {hops} leaked into the query: {query!r}"
    )


# ======================================================================================
# The LIMIT value is read FULLY-QUALIFIED at call time — a monkeypatch on the module
# constant is honoured (it is NOT bound at import). raising=False so this is RED for the
# load-bearing reason on the current base (no LIMIT emitted) rather than at setattr.
# ======================================================================================
def test_get_neighbors_honors_monkeypatched_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_guards, "NEIGHBOR_RESULT_LIMIT", 7, raising=False)
    fake = _RecordingFake()
    get_neighbors(fake, entity_id="A")
    query = fake.last_query()
    assert "LIMIT 7" in query, (
        "get_neighbors must read read_guards.NEIGHBOR_RESULT_LIMIT fully-qualified at call "
        f"time (monkeypatched to 7); query was: {query!r}"
    )
    assert _limit_value(query) == 7


# ======================================================================================
# find_paths' LIMIT is sourced from read_guards.PATH_RESULT_LIMIT — the _PATH_RESULT_LIMIT
# literal has moved into read_guards. raising=False creates the attr on base so the test
# fails at the LOAD-BEARING assertion: find_paths still uses the local literal -> "LIMIT 50".
# ======================================================================================
def test_find_paths_limit_sourced_from_read_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(read_guards, "PATH_RESULT_LIMIT", 9, raising=False)
    fake = _RecordingFake()
    find_paths(fake, from_id="A", to_id="B", max_hops=1)
    query = fake.last_query()
    assert "LIMIT 9" in query, (
        "find_paths must source its LIMIT from read_guards.PATH_RESULT_LIMIT (no local "
        f"_PATH_RESULT_LIMIT literal); monkeypatched to 9 but query was: {query!r}"
    )
    assert _limit_value(query) == 9


# ======================================================================================
# get_neighbors stays READ-ONLY — only adds clamp + LIMIT, never a write. Structural lock
# (the fake's write paths raise), so a builder cannot satisfy the gate with a write.
# ======================================================================================
def test_get_neighbors_is_read_only() -> None:
    fake = _RecordingFake()
    get_neighbors(fake, entity_id="A", hops=2)
    query = fake.last_query()
    assert _WRITE_KEYWORDS.search(query) is None, (
        f"get_neighbors must issue a read-only query (no write clause): {query!r}"
    )
    # Exactly one read ran; no write method was ever invoked (they raise if touched).
    assert len(fake.read_calls) == 1
