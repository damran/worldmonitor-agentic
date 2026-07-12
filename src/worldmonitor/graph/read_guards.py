"""Shared read-access guards for the graph read surfaces (ADR 0063, slice 2b).

The hop-cap, hop-clamp, and entity-id pattern are *read-access* guards shared by the
two sibling read surfaces — REST (``api/graph.py``, ADR 0062) and MCP
(``mcp/server.py``, ADR 0063). Centralising them here gives both a single source of
truth with the correct (downward) dependency direction (``api`` → ``graph``,
``mcp`` → ``graph``), so the REST and MCP caps can never silently drift.

Pure module: depends only on the standard library, opens no connection, imports
nothing from ``worldmonitor`` — safe to import from anywhere in the read stack.
"""

from __future__ import annotations

import re

# Hard ceiling on traversal depth (ADR 0062/0063): no unbounded traversal. One cap,
# one place — ``api/graph.py`` and ``graph/queries.py`` import this rather than
# re-declaring a literal ``4``.
HOP_CAP: int = 4

# Hard ceiling on the neighbour count returned by ``get_neighbors`` (ADR 0064): a
# high-degree hub node can never return its whole N-hop neighbourhood — bounded payload,
# bounded Neo4j expansion. Generous enough not to truncate ordinary queries.
NEIGHBOR_RESULT_LIMIT: int = 500

# Hard ceiling on the paths returned by ``find_paths`` (ADR 0064): centralised here next
# to the other read caps (moved from ``queries.py::_PATH_RESULT_LIMIT``) so every read cap
# lives in one place.
PATH_RESULT_LIMIT: int = 50

# Hard ceiling on the rows any dashboard read endpoint returns (ADR 0115): a bounded payload
# and bounded Neo4j scan for the public, unauthenticated consumption surface. Endpoints accept a
# caller ``limit`` but never exceed this (the API layer validates ``le=`` and the query helpers
# floor it again defensively).
DASHBOARD_RESULT_LIMIT: int = 500

# Shape allowed for an entity id (canonical-id alphabets: Q-numbers, LEI, GeoNames,
# ISO codes, prefixed ids like ``opensanctions:...``). Anchored to the whole string;
# rejects injection-shaped input (whitespace, quotes, braces, ``$``, newlines, ``;``).
ID_PATTERN: str = r"^[A-Za-z0-9:._-]+$"


def clamp_hops(n: int) -> int:
    """Clamp a requested hop count to ``[1, HOP_CAP]`` before it reaches a query.

    ``max(1, min(int(n), HOP_CAP))`` — floors at 1, caps at :data:`HOP_CAP`. A
    non-numeric value raises (via ``int()``: ``ValueError``/``TypeError``); the caller
    is expected to pass an int (or an int-coercible string).
    """
    return max(1, min(int(n), HOP_CAP))


def validate_entity_id(s: str) -> bool:
    """Return ``True`` iff ``s`` matches the canonical-id alphabet.

    A boolean *predicate* (not a raising validator): the read tools call this BEFORE
    any ``execute_read`` and reject (raise / 422) an id that fails it, so an
    injection-shaped id never reaches the query layer. ``re.fullmatch`` anchors against
    the whole string and is robust to the ``$``-before-trailing-newline gotcha.
    """
    return re.fullmatch(ID_PATTERN, s) is not None
