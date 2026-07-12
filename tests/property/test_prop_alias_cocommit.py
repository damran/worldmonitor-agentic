"""Property/metamorphic tests for Gate WPI-2 — the alias<->co-commit invariant (ADR 0111).

MANDATORY ``@given`` property suite (CLAUDE.md build discipline) for
:func:`worldmonitor.resolution.spine_integrity.find_incomplete_aliased_survivors`. PURE —
no DB, no Neo4j, no testcontainers: the function under test only reads ``.canonical_id`` off
its row arguments, so lightweight ``@dataclass`` stand-ins are used instead of real
``StatementRecord`` ORM rows (avoids the heavy-@given testcontainer-connection-leak footgun
entirely — see memory ``given-red-tests-leak-connections``). This file therefore carries NO
``pytest.mark.integration`` marker and runs under the default (fast) suite.

INV-ALIAS-COCOMMIT (docs/reviews/GATE_WPI2_ALIAS_COCOMMIT_SPEC.md): for every supersession alias
``prior -> survivor`` in the ledger, the final survivor ``survivor_of(prior)`` must have >= 1
**statement row** folding into it at rebuild — equivalently, ``reconstruct_entities`` must
materialise a node for it. ``find_incomplete_aliased_survivors`` is the pure decision function:

    targets = {survivor_of(a) for a in alias_map}   # aliased FINAL survivors, transitively
    covered = {survivor_of(r.canonical_id) for r in statement_rows}   # STATEMENT lane only
    return targets - covered

Statement lane ONLY (checker-confirmed, ADR 0111 §Decision): ``reconstruct_entities`` materialises
a node solely from statement rows, so a survivor with only context-claim rows yields NO node.
Counting a context row as coverage would be a false-pass; the context lane is therefore not a
parameter of this function at all. A zero-prop merge survivor (with or without anchors) has zero
statement rows and correctly fails loud here — its materialisation is WPI-1 / ADR 0112.

Four properties (ADR 0111 / spec Acceptance Criterion 1):

P-ALIAS-1  POSITIVE — every alias target has >= 1 statement row folding into it => the result is
           the EMPTY set.

P-ALIAS-2  METAMORPHIC NEGATIVE — starting from a P-ALIAS-1-positive case, inject a NEW alias
           ``ghost_prior -> ghost_survivor`` with NO statement row folding into ``ghost_survivor``
           => it IS in the result (and the result is non-empty). Pins the fail-loud direction.

P-ALIAS-3  TRANSITIVE-CHAIN — ``a -> b -> c`` in ``alias_map`` with a statement row ONLY under
           ``c`` => EMPTY result. This is the exact correctness point ADR 0111 pins: the target
           set is ``{survivor_of(a) for a in alias_map}`` (== ``{c}`` here via the transitive
           walk), NOT ``set(alias_map.values())`` (== ``{b, c}``, which would WRONGLY require the
           intermediate ``b`` to carry its own row and false-fire).

P-ALIAS-4  STATEMENT-LANE-ONLY (the closed false-pass) — an aliased survivor with ZERO statement
           rows IS reported incomplete, even though it would previously have been "rescued" by a
           context-claim row. This pins the checker-confirmed fix: context-claims do NOT count as
           coverage, because the fold materialises no node from context rows alone.

All tests are RED at collection time: the module-level import of
``find_incomplete_aliased_survivors`` / ``IncompleteAliasedSurvivorError`` from
``worldmonitor.resolution.spine_integrity`` fails with ``ImportError`` because that module does
not exist until the builder creates it (ADR 0111 option (a)).
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from worldmonitor.resolution.spine_integrity import (
    IncompleteAliasedSurvivorError,  # gate import: RED until builder lands the module
    find_incomplete_aliased_survivors,  # gate import: RED until builder lands the module
)

_SETTINGS = settings(deadline=None)

# A fixed id pool, large enough for up to 3 disjoint chains of up to 4 ids each (12 slots).
_ID_POOL = tuple(f"n{i}" for i in range(12))


@dataclass(frozen=True)
class _Row:
    """Lightweight stand-in for a StatementRecord row.

    ``find_incomplete_aliased_survivors`` only reads ``.canonical_id`` off its statement-row
    argument (per the gate spec's pinned signature) — a real ORM instance is unnecessary here.
    """

    canonical_id: str


@st.composite
def _alias_chains(draw: st.DrawFn) -> tuple[dict[str, str], list[str]]:
    """Draws pairwise-disjoint, cycle-free supersession alias chains from a fixed id pool.

    Each chain is ``prior_1 -> prior_2 -> ... -> survivor`` (>= 1 prior alias per chain), so
    every ``survivor`` returned is a genuine alias TARGET (it appears as a value reachable from
    at least one ``alias_map`` key via the transitive walk). Chains never share ids, so no cycle
    is possible. Returns ``(alias_map, survivors)`` — ``survivors`` never appears as an
    ``alias_map`` key (each is the tail of its own chain).
    """
    order = list(draw(st.permutations(_ID_POOL)))
    n_chains = draw(st.integers(min_value=1, max_value=3))
    alias_map: dict[str, str] = {}
    survivors: list[str] = []
    idx = 0
    for _ in range(n_chains):
        length = draw(st.integers(min_value=2, max_value=4))  # >= 1 prior + the survivor itself
        if idx + length > len(order):
            break
        chain_ids = order[idx : idx + length]
        idx += length
        survivor = chain_ids[-1]
        for i in range(len(chain_ids) - 1):
            alias_map[chain_ids[i]] = chain_ids[i + 1]
        survivors.append(survivor)
    assert survivors, "the first chain always fits the 12-slot pool (length <= 4, n_chains >= 1)"
    return alias_map, survivors


# ===========================================================================
# P-ALIAS-1 — positive: full statement coverage => empty result
# ===========================================================================


@given(chains=_alias_chains())
@_SETTINGS
def test_positive_full_coverage_returns_empty_set(
    chains: tuple[dict[str, str], list[str]],
) -> None:
    alias_map, survivors = chains
    statement_rows = [_Row(canonical_id=s) for s in survivors]

    result = find_incomplete_aliased_survivors(alias_map, statement_rows)

    assert result == set(), (
        f"P-ALIAS-1 VIOLATED: every alias target in {sorted(survivors)!r} has >= 1 statement "
        f"row, so the result must be empty; got {sorted(result)!r}"
    )


# ===========================================================================
# P-ALIAS-2 — metamorphic negative: an uncovered ghost alias target IS reported
# ===========================================================================


@given(chains=_alias_chains())
@_SETTINGS
def test_metamorphic_negative_ghost_alias_target_is_reported_incomplete(
    chains: tuple[dict[str, str], list[str]],
) -> None:
    alias_map, survivors = chains
    statement_rows = [_Row(canonical_id=s) for s in survivors]

    # Baseline: the positive case (P-ALIAS-1) holds before the injection.
    baseline = find_incomplete_aliased_survivors(alias_map, statement_rows)
    assert baseline == set(), f"metamorphic baseline precondition failed: {sorted(baseline)!r}"

    # Inject a NEW alias whose target has NO statement row.
    ghost_alias_map = dict(alias_map)
    ghost_alias_map["ghost_prior"] = "ghost_survivor"

    result = find_incomplete_aliased_survivors(ghost_alias_map, statement_rows)

    assert "ghost_survivor" in result, (
        "P-ALIAS-2 VIOLATED: an alias target with ZERO statement rows ('ghost_survivor') must be "
        f"reported incomplete; got {sorted(result)!r}"
    )
    assert result, "P-ALIAS-2: result must be non-empty once an uncovered alias is injected"
    # Every OTHER (already-covered) survivor must still be absent — the injection is isolated.
    assert result == {"ghost_survivor"}, (
        f"P-ALIAS-2: expected exactly {{'ghost_survivor'}} incomplete, got {sorted(result)!r} "
        "— the injection must not spuriously mark an already-covered survivor incomplete"
    )


# ===========================================================================
# P-ALIAS-3 — transitive chain: a -> b -> c, statement only under c => empty
# ===========================================================================


_LOWER = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=4
)


@given(ids=st.lists(_LOWER, min_size=3, max_size=3, unique=True))
@_SETTINGS
def test_transitive_chain_intermediate_needs_no_own_row(ids: list[str]) -> None:
    a, b, c = ids
    alias_map = {a: b, b: c}  # a -> b -> c: survivor_of(a) == survivor_of(b) == c
    statement_rows = [_Row(canonical_id=c)]  # statement ONLY under the FINAL survivor c

    result = find_incomplete_aliased_survivors(alias_map, statement_rows)

    assert result == set(), (
        f"P-ALIAS-3 VIOLATED: chain {a!r} -> {b!r} -> {c!r} with a statement only under {c!r} "
        f"must resolve to EMPTY (targets = {{survivor_of(a)}} == {{{c!r}}}, not "
        f"set(alias_map.values()) == {{{b!r}, {c!r}}}); got incomplete={sorted(result)!r} — "
        "the intermediate alias 'b' must NOT be required to carry its own statement row."
    )


# ===========================================================================
# P-ALIAS-4 — statement-lane-only: a statement-less survivor IS flagged
#             (the checker-confirmed false-pass, now closed)
# ===========================================================================


@given(chains=_alias_chains())
@_SETTINGS
def test_statement_less_survivor_is_flagged_incomplete(
    chains: tuple[dict[str, str], list[str]],
) -> None:
    alias_map, survivors = chains
    # ZERO statement rows anywhere: every aliased survivor is statement-less. Previously such a
    # survivor could be "rescued" by a context-claim row (a false-pass, since the fold materialises
    # no node from context rows alone); it must now be flagged incomplete.
    result = find_incomplete_aliased_survivors(alias_map, [])

    assert result == set(survivors), (
        f"P-ALIAS-4 VIOLATED: with ZERO statement rows every aliased survivor "
        f"{sorted(survivors)!r} must be flagged incomplete (context-claims do NOT count as "
        f"coverage — the fold materialises a node only from statement rows); got {sorted(result)!r}"
    )


# ===========================================================================
# Sanity: the exception class shape (ADR 0111 / spec §Mechanism)
# ===========================================================================


def test_exception_class_is_a_runtime_error_subclass() -> None:
    assert issubclass(IncompleteAliasedSurvivorError, RuntimeError), (
        "IncompleteAliasedSurvivorError must subclass RuntimeError (spec §Mechanism)"
    )
