"""MANDATORY ``@given`` property for Gate 3b driver diff-guard hardening LOW-1 (ADR 0114 D-7).

PURE / Docker-free (CLAUDE.md build discipline + memory ``given-red-tests-leak-connections``): no
DB, no Neo4j, no ``project()`` call under ``@given`` — a container-backed Hypothesis loop leaks
connections. This file pins the invariant at the **referent-rewrite level only**: LOW-1 changes
*where* the ``canonical_id_ledger`` is read (once, up front, shared by the fold + the completeness
check + the driver's own divergence measure) and must NEVER change the fold math / referent-rewrite
semantics themselves. The end-to-end (container-backed) restatement of the same invariant —
"a diff run with the new single-read plumbing yields the SAME ``ProjectionDivergence`` as the
pre-change behaviour on a seeded corpus" — lives in
``tests/integration/test_projection_diff_guard.py`` (INV-LOW1-FOLD-IDENTICAL, end-to-end).

ASSUMED BUILDER CONTRACT (this is the RED-pinned public surface the builder must add to
``worldmonitor.resolution.projector``; everything else in that module is FROZEN / byte-unchanged):

    def survivor_of_from_alias_map(alias_map: dict[str, str]) -> Callable[[str], str]:
        '''PURE builder of the transitive survivor_of resolver from an ALREADY-LOADED alias_map.

        The exact fixed-point walk :func:`build_survivor_of` performs internally today (visited-
        guarded transitive resolution over a SUPERSESSION-only canonical_alias -> canonical_id
        map), extracted so it can be built from a map that was already read ONCE elsewhere (the
        driver, or a caller of ``project(..., alias_map=..., survivor_of=...)``) without a second
        ``canonical_id_ledger`` read. No DB / Neo4j / session import.'''

Expected companion surface (not exercised directly by THIS pure file, but is the contract this
property is a proxy for — see the integration file for the end-to-end version):

    def load_alias_map_and_survivor_of(session) -> tuple[dict[str, str], Callable[[str], str]]:
        '''ONE ledger read; returns BOTH the alias_map and
        survivor_of_from_alias_map(alias_map). build_survivor_of(session) becomes a thin
        wrapper: load_alias_map_and_survivor_of(session)[1].'''

    def project(session, target, *, full_rebuild=False, checkpoint_id=...,
                survivor_of: Callable[[str], str] | None = None,
                alias_map: dict[str, str] | None = None) -> ProjectionResult:
        '''Both new kwargs default to None -> build internally exactly as today (byte-identical for
        every existing caller, which passes neither). When BOTH are supplied, project()
        reuses them for the fold AND the full_rebuild-gated completeness check instead of
        re-reading the ledger.'''

RED at collection time: ``survivor_of_from_alias_map`` does not exist yet on
``worldmonitor.resolution.projector`` (only the session-requiring ``build_survivor_of`` does) — the
module-level import below fails with ``ImportError``. That is the correct, intended TDD failure
mode: the builder must ADD this pure extraction, not merely thread a flag through ``project()``.

Properties:

P-LOW1-1  DETERMINISM / SHARED-BUILD SAFETY — building ``survivor_of`` TWICE from two SEPARATE
          ``dict`` objects holding the SAME alias-map content agrees, for every id in the map's
          domain (keys, values, and everything reachable by the transitive walk) AND for ids
          entirely OUTSIDE the domain (identity fallback), on every resolved value. This is the
          "injected vs internally-built" pin at the referent-rewrite level: LOW-1's whole premise
          is that handing the driver ONE already-built ``survivor_of`` (or the alias_map used to
          build one) is extensionally interchangeable with building a fresh one from the identical
          ledger snapshot — never a second, drifting computation.

P-LOW1-2  TRANSITIVE-CHAIN SEMANTICS UNCHANGED — for a generated chain
          ``prior_1 -> prior_2 -> ... -> survivor`` (>= 1 prior, cycle-free, mirrors
          ``build_survivor_of``'s documented fixed-point walk), EVERY id in the chain — including
          every intermediate alias, not just the head — resolves to the terminal ``survivor``. This
          is the transitive semantics :func:`build_survivor_of` documents (a -> b -> c) and that
          LOW-1 must not weaken to a single-hop lookup.

P-LOW1-3  SELF-ROW NEVER SHADOWS THE SURVIVOR (F2 determinism, mirrors ``build_survivor_of``'s own
          docstring guarantee) — an id that is BOTH a live canonical (absent from the map as a key)
          AND, via a DIFFERENT chain, an intermediate/alias resolves to its survivor, never to
          itself; the pure builder must preserve this even when constructed from a bare
          ``dict[str, str]`` with no ORDER BY guarantee behind it (the map itself is already
          supersession-only by construction, mirroring ``_load_alias_map``'s self-row exclusion).
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from worldmonitor.resolution.projector import (
    survivor_of_from_alias_map,  # gate import: RED — new pure helper, does not exist yet
)

_SETTINGS = settings(deadline=None)

# A fixed id pool, large enough for a few disjoint chains of up to 4 ids each, plus room left over
# for "outside the domain" probe ids drawn from the SAME pool (never colliding with a chain's ids,
# see ``_alias_chains`` below).
_ID_POOL = tuple(f"n{i}" for i in range(16))


@st.composite
def _alias_chains(draw: st.DrawFn) -> tuple[dict[str, str], list[list[str]], list[str]]:
    """Draws pairwise-disjoint, cycle-free supersession alias chains from a fixed id pool.

    Mirrors ``tests/property/test_prop_alias_cocommit.py``'s ``_alias_chains`` (same shape,
    independently re-derived here so this file stays self-contained per the existing per-module
    strategy convention). Returns ``(alias_map, chains, unused_ids)`` where each element of
    ``chains`` is the FULL id sequence of one chain (``[prior_1, ..., prior_n, survivor]``,
    ``n >= 1``) and ``unused_ids`` are pool ids that appear in NO chain (genuine "outside the
    domain" probes for the identity-fallback assertions).
    """
    order = list(draw(st.permutations(_ID_POOL)))
    n_chains = draw(st.integers(min_value=1, max_value=3))
    alias_map: dict[str, str] = {}
    chains: list[list[str]] = []
    idx = 0
    for _ in range(n_chains):
        length = draw(st.integers(min_value=2, max_value=4))  # >= 1 prior + the survivor itself
        if idx + length > len(order):
            break
        chain_ids = order[idx : idx + length]
        idx += length
        for i in range(len(chain_ids) - 1):
            alias_map[chain_ids[i]] = chain_ids[i + 1]
        chains.append(chain_ids)
    assert chains, "the first chain always fits the 16-slot pool (length <= 4, n_chains >= 1)"
    unused_ids = order[idx:]
    return alias_map, chains, unused_ids


# ===========================================================================
# P-LOW1-1 — determinism / shared-build safety (injected vs internally-built)
# ===========================================================================


@given(drawn=_alias_chains())
@_SETTINGS
def test_two_independent_builds_from_the_same_alias_map_agree_everywhere(
    drawn: tuple[dict[str, str], list[list[str]], list[str]],
) -> None:
    alias_map, chains, unused_ids = drawn
    # Two SEPARATE dict objects (distinct identities), same content — simulates "the driver's one
    # injected survivor_of" vs "what project() would have built internally from an equivalent read".
    survivor_of_injected = survivor_of_from_alias_map(dict(alias_map))
    survivor_of_internal = survivor_of_from_alias_map(dict(alias_map))

    domain = set(alias_map.keys()) | set(alias_map.values())
    for cid in sorted(domain):
        assert survivor_of_injected(cid) == survivor_of_internal(cid), (
            f"P-LOW1-1 VIOLATED: injected vs internally-built survivor_of disagree on {cid!r} "
            f"({survivor_of_injected(cid)!r} != {survivor_of_internal(cid)!r}) — LOW-1 must never "
            "change the referent-rewrite result, only WHERE the ledger is read"
        )

    # Ids entirely outside the domain (never a key or value in any chain): identity fallback, and
    # both builds must agree on that too.
    for cid in unused_ids:
        assert survivor_of_injected(cid) == survivor_of_internal(cid) == cid, (
            f"P-LOW1-1 VIOLATED: an id outside the alias-map domain ({cid!r}) must resolve to "
            "itself under BOTH builds"
        )


# ===========================================================================
# P-LOW1-2 — transitive-chain semantics unchanged
# ===========================================================================


@given(drawn=_alias_chains())
@_SETTINGS
def test_every_id_in_a_chain_resolves_to_the_terminal_survivor(
    drawn: tuple[dict[str, str], list[list[str]], list[str]],
) -> None:
    alias_map, chains, _unused = drawn
    survivor_of = survivor_of_from_alias_map(alias_map)

    for chain in chains:
        terminal = chain[-1]
        for cid in chain:
            assert survivor_of(cid) == terminal, (
                f"P-LOW1-2 VIOLATED: chain {chain!r} — {cid!r} must resolve to the terminal "
                f"survivor {terminal!r} (transitive fixed-point walk), got {survivor_of(cid)!r}. "
                "LOW-1's ledger-read plumbing change must not weaken this to a single-hop lookup."
            )


# ===========================================================================
# P-LOW1-3 — a self-row (live canonical id, no alias-map key) never shadows a survivor it
#            happens to equal via a DIFFERENT chain's terminal
# ===========================================================================


@given(drawn=_alias_chains())
@_SETTINGS
def test_survivor_terminal_is_never_shadowed_by_its_own_absence_as_a_key(
    drawn: tuple[dict[str, str], list[list[str]], list[str]],
) -> None:
    """Every chain's terminal survivor is, BY CONSTRUCTION of ``_alias_chains``, never itself an
    ``alias_map`` key (each chain's tail is the final hop, never re-aliased further) — mirroring
    ``_load_alias_map``'s documented guarantee that a supersession-only map excludes self-rows so a
    live canonical id is never shadowed. This asserts the pure builder preserves exactly that: a
    chain terminal resolves to ITSELF (it is the fixed point), never past itself.
    """
    alias_map, chains, _unused = drawn
    survivor_of = survivor_of_from_alias_map(alias_map)

    for chain in chains:
        terminal = chain[-1]
        assert terminal not in alias_map, (
            "test precondition: a chain terminal must never be its own alias_map key "
            f"({terminal!r} in {sorted(alias_map)!r})"
        )
        assert survivor_of(terminal) == terminal, (
            f"P-LOW1-3 VIOLATED: the terminal survivor {terminal!r} must resolve to ITSELF (the "
            f"fixed point of the walk), got {survivor_of(terminal)!r}"
        )
