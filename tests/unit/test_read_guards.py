"""Unit tests for the shared graph read-guards module (ADR 0063, slice 2b).

ADR 0063 centralises 2a's hop-cap, hop-clamp, and entity-id pattern into a single
``src/worldmonitor/graph/read_guards.py`` so the two sibling read surfaces (REST,
MCP) share ONE source of truth with the correct (downward) dependency direction.
This file pins that module's pure contract.

CONTRACT ASSUMED (the builder MUST match these names exactly):
    worldmonitor.graph.read_guards
        HOP_CAP: int = 4
        clamp_hops(n) -> int                # == max(1, min(int(n), HOP_CAP))
        ID_PATTERN: str                     # the canonical-id alphabet, == 2a's
                                            # r"^[A-Za-z0-9:._-]+$"
        validate_entity_id(s: str) -> bool  # True iff s matches the id alphabet
                                            # (a BOOLEAN predicate, not a raising
                                            # validator — locked here; builder matches)

RED today: ``worldmonitor.graph.read_guards`` does not exist, so this whole module
fails to import (ModuleNotFoundError) — the right red for a not-yet-built module.
"""

from __future__ import annotations

import inspect
import re

import pytest

from worldmonitor.api.graph import HOP_CAP as API_CAP
from worldmonitor.graph.read_guards import (
    HOP_CAP,
    ID_PATTERN,
    clamp_hops,
    validate_entity_id,
)

# 2a's frozen alphabet — the new shared pattern must be the same alphabet.
_EXPECTED_ALPHABET = r"^[A-Za-z0-9:._-]+$"


# ======================================================================================
# clamp_hops — max(1, min(int(n), HOP_CAP)).
# ======================================================================================
def test_clamp_hops_caps_above_the_ceiling() -> None:
    assert clamp_hops(99) == HOP_CAP
    assert HOP_CAP == 4, "the hard traversal ceiling is 4 (ADR 0062/0063)"


def test_clamp_hops_floors_at_one() -> None:
    assert clamp_hops(0) == 1
    assert clamp_hops(-5) == 1


def test_clamp_hops_passes_through_in_range() -> None:
    assert clamp_hops(3) == 3
    assert clamp_hops(1) == 1
    assert clamp_hops(HOP_CAP) == HOP_CAP


def test_clamp_hops_coerces_numeric_strings() -> None:
    # int("2") == 2 — a numeric string is coerced, then clamped.
    assert clamp_hops("2") == 2  # type: ignore[arg-type]
    assert clamp_hops("99") == HOP_CAP  # type: ignore[arg-type]


def test_clamp_hops_rejects_non_numeric() -> None:
    with pytest.raises((ValueError, TypeError)):
        clamp_hops("not-a-number")  # type: ignore[arg-type]


# ======================================================================================
# ID_PATTERN / validate_entity_id — canonical-id alphabet; reject injection shapes.
# ======================================================================================
_ACCEPT = ["Q42", "opensanctions:abc-1", "geonames:123", "iso-3166:US"]
_REJECT = [
    '") DETACH DELETE n //',  # the canonical injection probe
    "a b",  # whitespace
    "a{b}",  # braces (Cypher map literal)
    "a$b",  # parameter sigil
    "",  # empty
    "a\nMATCH",  # newline + a second clause
]


@pytest.mark.parametrize("good", _ACCEPT)
def test_validate_entity_id_accepts_canonical_ids(good: str) -> None:
    assert validate_entity_id(good) is True, f"{good!r} is a valid canonical id"
    # Cross-check against the raw pattern (fullmatch is anchor-agnostic + robust to the
    # `$`-before-trailing-newline gotcha).
    assert re.fullmatch(ID_PATTERN, good), f"{good!r} must match ID_PATTERN"


@pytest.mark.parametrize("bad", _REJECT)
def test_validate_entity_id_rejects_injection_shapes(bad: str) -> None:
    assert validate_entity_id(bad) is False, f"{bad!r} must be rejected by validate_entity_id"
    assert re.fullmatch(ID_PATTERN, bad) is None, f"{bad!r} must NOT match ID_PATTERN"


def test_id_pattern_is_the_2a_alphabet() -> None:
    # The shared pattern must be exactly 2a's alphabet (same characters), so REST and MCP
    # validate ids identically.
    assert ID_PATTERN == _EXPECTED_ALPHABET


# ======================================================================================
# SINGLE SOURCE OF TRUTH — api.graph's HOP_CAP must BE the shared read_guards.HOP_CAP.
#
# `is` alone is a weak lock for the value 4 (CPython interns small ints: `4 is int("4")`
# is True), so it cannot catch api/graph.py keeping its own `HOP_CAP = 4`. We therefore
# ALSO assert at the SOURCE level that api/graph.py imports the cap from read_guards and
# does not re-define it locally — the real "one cap, one place" lock from ADR 0063.
# ======================================================================================
def test_api_graph_hop_cap_is_the_shared_constant() -> None:
    # Identity assertion requested by the gate spec (necessary but, for an interned small
    # int, not sufficient — hence the source-level lock below).
    assert API_CAP is HOP_CAP
    assert API_CAP == HOP_CAP == 4


def test_api_graph_imports_hop_cap_from_read_guards_not_local() -> None:
    import worldmonitor.api.graph as api_graph

    src = inspect.getsource(api_graph)
    # It must pull the cap from the shared module...
    assert "read_guards" in src, (
        "api/graph.py must import its read-guards from worldmonitor.graph.read_guards "
        "(ADR 0063: one cap, one place)"
    )
    # ...and must NOT re-declare a local HOP_CAP literal (that would re-introduce the drift
    # the shared module exists to prevent).
    assert re.search(r"^HOP_CAP\s*=", src, re.MULTILINE) is None, (
        "api/graph.py must not redefine HOP_CAP locally — it must import it from "
        "graph.read_guards so the REST and MCP caps can never diverge"
    )
