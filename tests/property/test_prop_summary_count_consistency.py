"""Property: `summarize_result` never disagrees with the full list (Gate F-5, ADR 0124).

CLAUDE.md's build-discipline rule ("any gate that touches an invariant MUST add a `@given`
property test") does NOT strictly apply here — F-5 touches no ER/merge/canonical-id/
merge-guard/provenance invariant (ADR 0124 §Invariant-gate-note / spec §3.7 records this as
a DECISION, not an omission). We include ONE cheap metamorphic `@given` anyway because the
load-bearing correctness guarantee of the whole gate — *the summary must never disagree with
the full list* — is exactly a metamorphic relation and is essentially free to pin:

    (a) ``summarize_result(x)["count"] == len(x)`` for arbitrary ``x``;
    (b) every element of ``["sample"]`` is a member of ``x`` (never a synthesised/foreign
        record — the sample is a SUBSET, never a projection: ``sample`` elements are the
        FULL dicts, G1 provenance rides along verbatim);
    (c) ``len(["sample"]) == min(3, len(x))`` — the sample is never over- or under-filled;
    (d) determinism: repeated calls on the SAME input are identical, AND the result is
        STABLE under input reordering of the same multiset (the canonical-sort determinism
        guarantee established at the summary layer, ADR 0124 §3.4 — the underlying
        ``get_neighbors``/``find_paths`` Cypher carries no ``ORDER BY``, ADR 0064);
    (e) idempotence of the canonicalization: re-summarizing an already-produced ``sample``
        reproduces it unchanged (the canonical sort is idempotent, not merely deterministic).

Pure in-process — no DB, no container, no client (``summarize_result`` takes only an
in-memory ``list[dict]``), so there is no connection-leak risk on the RED path.

``summarize_result`` is imported LOCALLY inside each test function (not at module top): it
does not exist yet on the current tree (it lives in the EXISTING ``graph/queries.py``, not
a wholly-new module), so a module-level import would turn a missing-symbol ImportError into
a whole-file collection failure. RED today: ``ImportError: cannot import name
'summarize_result' from 'worldmonitor.graph.queries'``.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# A field-agnostic, JSON-serialisable "record" strategy: string/int/float/bool/None atoms,
# unicode text, nested (shallow) lists/dicts — the same shape family as a real neighbour
# ({"id", "name": [...], "prov_*": ...}) or path ({"nodes": [...], "relationships": [...]})
# dict, but adversarially varied so the property does not accidentally only exercise one
# field shape.
_ATOM = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=12),  # hypothesis's default text() is JSON-dumps-safe (no lone surrogates)
)

_JSON_VALUE = st.recursive(
    _ATOM,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(
            st.text(alphabet="abcdefgh_", min_size=1, max_size=6), children, max_size=3
        ),
    ),
    max_leaves=6,
)

_RESULT_DICT = st.dictionaries(
    st.text(alphabet="abcdefghijklmnop_", min_size=1, max_size=8),
    _JSON_VALUE,
    min_size=0,
    max_size=5,
)

_RESULT_LIST = st.lists(_RESULT_DICT, max_size=15)


@given(items=_RESULT_LIST)
@_SETTINGS
def test_prop_summary_count_equals_full_len(items: list[dict[str, Any]]) -> None:
    from worldmonitor.graph.queries import summarize_result

    result = summarize_result(items)

    # (a) count is EXACTLY the length of the full list — never a lossy/derived approximation
    # (AC-5 / spec §6.2a).
    assert result["count"] == len(items), (
        f"count ({result['count']}) must equal len(items) ({len(items)})"
    )

    # (b) every sample element is a MEMBER of the full input (subset-by-equality, never a
    # synthesised/foreign/projected record — AC-5 / spec §6.2b, G1 note in §3.1).
    for element in result["sample"]:
        assert element in items, f"sample element {element!r} is not a member of the input list"

    # (c) len(sample) == min(3, count) — never over- or under-fills the cap (AC-5 / §6.2c).
    assert len(result["sample"]) == min(3, len(items)), (
        f"len(sample) ({len(result['sample'])}) must equal min(3, count) ({min(3, len(items))})"
    )

    # (d) determinism: repeated calls on the SAME input are identical (AC-6)...
    assert summarize_result(items) == result, (
        "summarize_result must be a pure function of its input (repeat call diverged)"
    )

    # ...and STABLE under input reordering of the same multiset (the canonical-sort
    # determinism guarantee, ADR 0124 §3.4 — there is no query ORDER BY to inherit order
    # from; determinism is established here, at the summary layer).
    reversed_items = list(reversed(items))
    assert summarize_result(reversed_items) == result, (
        "summarize_result must be stable under input reordering (canonical sort, ADR 0124 §3.4)"
    )


@given(items=_RESULT_LIST)
@_SETTINGS
def test_prop_summarize_canonicalization_is_idempotent(items: list[dict[str, Any]]) -> None:
    """Re-summarizing an already-produced ``sample`` is a no-op: the sample IS the
    canonical-sorted prefix of the input, so feeding it back through ``summarize_result``
    (whose own sample_size default, 3, is >= any sample's length) must reproduce it
    unchanged — the canonical sort is idempotent, not merely deterministic."""
    from worldmonitor.graph.queries import summarize_result

    first = summarize_result(items)
    second = summarize_result(first["sample"])

    assert second["count"] == len(first["sample"])
    assert second["sample"] == first["sample"], (
        "re-summarizing an already-canonically-sorted sample must reproduce it unchanged "
        f"(idempotent canonicalization); first={first['sample']!r} second={second['sample']!r}"
    )


def test_prop_summary_count_regression_witness_canonical_json_key() -> None:
    """A single concrete witness (not `@given`) documenting the EXACT canonicalization key
    the property above treats as a black box: ``json.dumps(d, sort_keys=True, default=str)``
    (ADR 0124 §3.4, verbatim). Locks the *mechanism*, not just the outcome, so a future
    refactor that swaps in a different (non-canonical, e.g. insertion-order) sort key would
    fail here even if it happened to preserve count/subset/cap on this particular input."""
    from worldmonitor.graph.queries import summarize_result

    items = [{"id": "b"}, {"id": "a"}, {"id": "c"}]
    expected_sample = sorted(items, key=lambda d: json.dumps(d, sort_keys=True, default=str))[:3]

    result = summarize_result(items)
    assert result["sample"] == expected_sample
