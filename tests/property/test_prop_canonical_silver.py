"""Property / metamorphic tests for ``resolution.silver`` — the canonical-anchor SILVER labels.

These are the **mandatory** ``@given`` invariant tests for ADR 0079 (INV-NONCIRCULAR is the
load-bearing one).  ALL inputs are **synthetic FtM entities** — no real OpenSanctions data.

Test plan (gate.scope §(B)):

* **P-POS** — shared anchor across distinct sources → exactly one ``"match"`` pair.
* **P-SAME-SOURCE** — shared anchor, same source → **no** ``"match"`` (≥2-distinct-sources rule).
* **P-NEG** — conflicting anchor values → exactly one ``"non_match"`` pair.
* **P-ABSTAIN** — no shared anchor, no conflict → **no** silver label.
* **P-CONTRADICTION** — shared on ``P`` AND conflicting on ``Q`` → **no** label (dropped).
* **P-MM** (load-bearing metamorphic, proves N2) — mutating name / non-anchor fields with
  anchors + ``source_id`` fixed leaves the emitted label set **identical**.  Reverse direction:
  collapsing two distinct sources to one removes the corresponding positive.
* **P-ORDER** — output is invariant under input permutation; every pair is canonically ordered
  (``left_id <= right_id``); no duplicate ``(left_id, right_id)``; no self-pair.
* **P-SIGNATURE** (proves N1) — ``build_silver_pairs`` has no score/probability/threshold/linker
  parameter; the module source references no scoring symbol.
"""

from __future__ import annotations

import inspect
import random

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import silver
from worldmonitor.resolution.silver import (
    ANCHOR_PROPERTIES,
    SILVER_SOURCE,
    build_silver_pairs,
)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS = settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SOURCE_POOL = ("src-A", "src-B", "src-C", "src-D", "src-E")
_ID_POOL = ("id-1", "id-2", "id-3", "id-4", "id-5")

_NAME_ALPHABET = st.characters(min_codepoint=65, max_codepoint=122, categories=("Lu", "Ll"))
_ANCHOR_VALUE_ALPHABET = st.text(alphabet="ABCDEF0123456789", min_size=1, max_size=12)

# FtM schema that carries each anchor property.  ``isin`` lives on ``Security``, not on
# ``Company`` — using the wrong schema means FtM silently drops the property value on
# ``make_entity``, so anchor_values() returns empty and no label is emitted (a vacuous pass).
_ANCHOR_SCHEMA: dict[str, str] = {
    "wikidataId": "Company",
    "leiCode": "Company",
    "registrationNumber": "Company",
    "ogrnCode": "Company",
    "innCode": "Company",
    "swiftBic": "Company",
    "isin": "Security",
    "okpoCode": "Company",
    "permId": "Company",
}


def _prov(source_id: str, entity_id: str) -> Provenance:
    return Provenance(
        source_id=source_id,
        retrieved_at="2026-01-01T00:00:00Z",
        reliability="B",
        source_record=f"s3://landing/{entity_id}.json",
    )


def _entity(
    entity_id: str,
    source_id: str,
    *,
    schema: str = "Company",
    name: str = "Acme",
    anchor_prop: str | None = None,
    anchor_val: str | None = None,
) -> FtmEntity:
    """Build a provenance-stamped FtM entity with an optional anchor property value.

    When ``anchor_prop`` is given, the entity schema is automatically set to the schema that
    carries that property (from ``_ANCHOR_SCHEMA``), so FtM does not silently drop the value.
    The caller may override with an explicit ``schema`` keyword.
    """
    used_schema = schema
    if anchor_prop is not None:
        used_schema = _ANCHOR_SCHEMA.get(anchor_prop, schema)
    props: dict[str, list[str]] = {"name": [name]}
    if anchor_prop is not None and anchor_val is not None:
        props[anchor_prop] = [anchor_val]
    entity = make_entity({"id": entity_id, "schema": used_schema, "properties": props})
    return stamp(entity, _prov(source_id, entity_id))


# ---------------------------------------------------------------------------
# P-POS: shared anchor + distinct sources → match
# ---------------------------------------------------------------------------


@given(
    anchor=st.sampled_from(ANCHOR_PROPERTIES),
    val=_ANCHOR_VALUE_ALPHABET,
    src_a=st.sampled_from(_SOURCE_POOL),
    src_b=st.sampled_from(_SOURCE_POOL),
)
@_SETTINGS
def test_p_pos_shared_anchor_distinct_sources_yields_match(
    anchor: str, val: str, src_a: str, src_b: str
) -> None:
    """P-POS: two entities sharing the same non-empty anchor value from ≥2 distinct sources must
    produce exactly one ``"match"`` pair with ``source=SILVER_SOURCE`` and ``clerical_score=None``,
    canonically ordered.
    """
    if not val:
        return  # empty values carry no anchor signal — skip
    if src_a == src_b:
        return  # same source is P-SAME-SOURCE, not P-POS

    a = _entity("id-1", src_a, anchor_prop=anchor, anchor_val=val)
    b = _entity("id-2", src_b, anchor_prop=anchor, anchor_val=val)
    pairs = build_silver_pairs([a, b])

    assert len(pairs) == 1, f"expected 1 match pair, got {pairs}"
    pair = pairs[0]
    assert pair.label == "match", f"expected 'match', got {pair.label!r}"
    assert pair.source == SILVER_SOURCE
    assert pair.clerical_score is None
    assert pair.left_id <= pair.right_id, "canonical ordering violated"
    assert pair.left_id in {"id-1", "id-2"} and pair.right_id in {"id-1", "id-2"}


# ---------------------------------------------------------------------------
# P-SAME-SOURCE: shared anchor, same source → no match
# ---------------------------------------------------------------------------


@given(
    anchor=st.sampled_from(ANCHOR_PROPERTIES),
    val=_ANCHOR_VALUE_ALPHABET,
    src=st.sampled_from(_SOURCE_POOL),
)
@_SETTINGS
def test_p_same_source_shared_anchor_yields_no_match(anchor: str, val: str, src: str) -> None:
    """P-SAME-SOURCE: two entities sharing the same anchor value but the SAME source_id must NOT
    produce a ``"match"`` pair — within-source duplicates are excluded by the ≥2-distinct-sources
    rule (ADR 0079 §Decision 3).
    """
    if not val:
        return
    a = _entity("id-1", src, anchor_prop=anchor, anchor_val=val)
    b = _entity("id-2", src, anchor_prop=anchor, anchor_val=val)
    pairs = build_silver_pairs([a, b])

    match_pairs = [p for p in pairs if p.label == "match"]
    assert not match_pairs, f"same-source shared anchor must not yield a 'match'; got {match_pairs}"


# ---------------------------------------------------------------------------
# P-NEG: conflicting anchor values → non_match (source-independent)
# ---------------------------------------------------------------------------


@given(
    anchor=st.sampled_from(ANCHOR_PROPERTIES),
    val_a=_ANCHOR_VALUE_ALPHABET,
    val_b=_ANCHOR_VALUE_ALPHABET,
    src_a=st.sampled_from(_SOURCE_POOL),
    src_b=st.sampled_from(_SOURCE_POOL),
)
@_SETTINGS
def test_p_neg_conflicting_anchors_yield_non_match(
    anchor: str, val_a: str, val_b: str, src_a: str, src_b: str
) -> None:
    """P-NEG: two entities with conflicting (both non-empty, disjoint) values for the same anchor
    property must produce exactly one ``"non_match"`` pair, regardless of source.
    """
    if not val_a or not val_b:
        return
    if val_a == val_b:
        return  # shared value → P-POS branch

    a = _entity("id-1", src_a, anchor_prop=anchor, anchor_val=val_a)
    b = _entity("id-2", src_b, anchor_prop=anchor, anchor_val=val_b)
    pairs = build_silver_pairs([a, b])

    # When src_a != src_b and no shared anchor value exists, this is a pure negative.
    # When src_a == src_b, still a negative (conflict is source-independent).
    non_match_pairs = [p for p in pairs if p.label == "non_match"]
    assert len(non_match_pairs) == 1, (
        f"expected 1 non_match pair for conflicting {anchor!r} values "
        f"({val_a!r} vs {val_b!r}), got {pairs}"
    )
    pair = non_match_pairs[0]
    assert pair.source == SILVER_SOURCE
    assert pair.clerical_score is None
    assert pair.left_id <= pair.right_id


# ---------------------------------------------------------------------------
# P-ABSTAIN: no shared anchor, no conflict → no label
# ---------------------------------------------------------------------------


@given(src_a=st.sampled_from(_SOURCE_POOL), src_b=st.sampled_from(_SOURCE_POOL))
@_SETTINGS
def test_p_abstain_no_anchor_overlap_no_conflict_yields_nothing(src_a: str, src_b: str) -> None:
    """P-ABSTAIN: two entities with NO anchor property set at all must produce no silver label
    (neither match nor non_match — there is nothing to evaluate).
    """
    a = _entity("id-1", src_a)  # no anchor_prop set
    b = _entity("id-2", src_b)
    pairs = build_silver_pairs([a, b])
    assert pairs == [], f"expected no silver labels for anchor-free entities, got {pairs}"


# ---------------------------------------------------------------------------
# P-CONTRADICTION: shared on P AND conflicting on Q → no label emitted
# ---------------------------------------------------------------------------


# Anchors that can both coexist on a single Company entity (needed for P-CONTRADICTION which
# puts TWO anchor properties on the SAME entity).  ``isin`` is Security-only — if we put it
# on a Company entity via make_entity the value is silently dropped, breaking the test's
# premise that both P and Q are actually present on the entity.
_COMPANY_ANCHORS: tuple[str, ...] = tuple(a for a in ANCHOR_PROPERTIES if a != "isin")


@given(
    anchor_p=st.sampled_from(_COMPANY_ANCHORS),
    anchor_q=st.sampled_from(_COMPANY_ANCHORS),
    shared_val=_ANCHOR_VALUE_ALPHABET,
    val_a=_ANCHOR_VALUE_ALPHABET,
    val_b=_ANCHOR_VALUE_ALPHABET,
    src_a=st.sampled_from(_SOURCE_POOL),
    src_b=st.sampled_from(_SOURCE_POOL),
)
@_SETTINGS
def test_p_contradiction_pos_and_neg_on_different_anchors_drops_pair(
    anchor_p: str,
    anchor_q: str,
    shared_val: str,
    val_a: str,
    val_b: str,
    src_a: str,
    src_b: str,
) -> None:
    """P-CONTRADICTION: a pair that qualifies as BOTH positive (shared value on anchor P, distinct
    sources) AND negative (conflict on anchor Q) must be DROPPED — never emitted as either label.

    Both anchor properties are drawn from ``_COMPANY_ANCHORS`` so that both can coexist on a
    single Company entity without being silently dropped by FtM (``isin`` is excluded because it
    only exists on the ``Security`` schema and would be ignored on a Company entity, making the
    contradiction premise impossible to construct).
    """
    if anchor_p == anchor_q:
        return  # need two different anchor props for a contradiction
    if not shared_val or not val_a or not val_b:
        return
    if val_a == val_b:
        return  # not a conflict on Q
    if src_a == src_b:
        return  # need distinct sources for the positive branch on P

    props_a: dict[str, list[str]] = {"name": ["Acme"], anchor_p: [shared_val], anchor_q: [val_a]}
    props_b: dict[str, list[str]] = {"name": ["Acme"], anchor_p: [shared_val], anchor_q: [val_b]}

    a = make_entity({"id": "id-1", "schema": "Company", "properties": props_a})
    b = make_entity({"id": "id-2", "schema": "Company", "properties": props_b})
    stamp(a, _prov(src_a, "id-1"))
    stamp(b, _prov(src_b, "id-2"))

    pairs = build_silver_pairs([a, b])
    assert pairs == [], (
        f"contradiction pair (pos on {anchor_p!r}, neg on {anchor_q!r}) must be dropped, "
        f"got {pairs}"
    )


# ---------------------------------------------------------------------------
# P-MM: metamorphic score-independence (proves N2)
# ---------------------------------------------------------------------------


@given(
    anchor=st.sampled_from(ANCHOR_PROPERTIES),
    val=_ANCHOR_VALUE_ALPHABET,
    src_a=st.sampled_from(_SOURCE_POOL),
    src_b=st.sampled_from(_SOURCE_POOL),
    name_a=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    name_b=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    name_a2=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
    name_b2=st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20),
)
@_SETTINGS
def test_p_mm_name_mutation_leaves_label_set_identical(
    anchor: str,
    val: str,
    src_a: str,
    src_b: str,
    name_a: str,
    name_b: str,
    name_a2: str,
    name_b2: str,
) -> None:
    """P-MM (metamorphic, proves N2): arbitrarily mutating the ``name`` property (the dominant
    Splink feature) with anchor + ``source_id`` held fixed must leave the emitted label set
    **byte-identical**.

    This is the load-bearing non-circularity check: if silver labels depended on the name (or any
    feature a score model reads), changing the name would change the label — but the invariant says
    they must not.
    """
    if not val:
        return
    if src_a == src_b:
        return  # same-source: neither baseline nor mutated emits a match

    # Baseline
    a1 = _entity("id-1", src_a, anchor_prop=anchor, anchor_val=val, name=name_a)
    b1 = _entity("id-2", src_b, anchor_prop=anchor, anchor_val=val, name=name_b)
    base_pairs = build_silver_pairs([a1, b1])

    # Mutate names (keep anchor value + source_id identical)
    a2 = _entity("id-1", src_a, anchor_prop=anchor, anchor_val=val, name=name_a2)
    b2 = _entity("id-2", src_b, anchor_prop=anchor, anchor_val=val, name=name_b2)
    mutated_pairs = build_silver_pairs([a2, b2])

    assert base_pairs == mutated_pairs, (
        "silver label set must not change when name is mutated (N2 / P-MM): "
        f"base={base_pairs}, mutated={mutated_pairs} "
        f"(anchor={anchor!r}, val={val!r}, src_a={src_a!r}, src_b={src_b!r})"
    )


@given(
    anchor=st.sampled_from(ANCHOR_PROPERTIES),
    val=_ANCHOR_VALUE_ALPHABET,
    src_a=st.sampled_from(_SOURCE_POOL),
    src_b=st.sampled_from(_SOURCE_POOL),
)
@_SETTINGS
def test_p_mm_source_collapse_removes_positive(
    anchor: str, val: str, src_a: str, src_b: str
) -> None:
    """P-MM reverse direction: collapsing two distinct sources to ONE removes the positive label —
    proving ``source_id`` distinctness is load-bearing, not vestigial.

    With two distinct sources sharing an anchor value: one ``"match"`` pair.
    Collapse to the SAME source: no ``"match"`` pair (≥2-distinct-sources rule).
    """
    if not val:
        return
    if src_a == src_b:
        return  # already the same source — can't demonstrate the collapse

    # Distinct sources → expect match
    a_pos = _entity("id-1", src_a, anchor_prop=anchor, anchor_val=val)
    b_pos = _entity("id-2", src_b, anchor_prop=anchor, anchor_val=val)
    pos_pairs = build_silver_pairs([a_pos, b_pos])
    match_count_before = sum(1 for p in pos_pairs if p.label == "match")

    # Collapse to single source → no match
    a_same = _entity("id-1", src_a, anchor_prop=anchor, anchor_val=val)
    b_same = _entity("id-2", src_a, anchor_prop=anchor, anchor_val=val)  # same source as a
    same_pairs = build_silver_pairs([a_same, b_same])
    match_count_after = sum(1 for p in same_pairs if p.label == "match")

    assert match_count_before >= 1, (
        f"expected ≥1 match with distinct sources {src_a!r}/{src_b!r}, anchor={anchor!r}, "
        f"val={val!r}; got {pos_pairs}"
    )
    assert match_count_after == 0, (
        f"expected 0 matches after source collapse to {src_a!r}; got {same_pairs}"
    )


# ---------------------------------------------------------------------------
# P-ORDER: output invariant under input permutation + canonical ordering
# ---------------------------------------------------------------------------


@given(
    anchor=st.sampled_from(ANCHOR_PROPERTIES),
    val=_ANCHOR_VALUE_ALPHABET,
    src_a=st.sampled_from(_SOURCE_POOL),
    src_b=st.sampled_from(_SOURCE_POOL),
    seed=st.integers(min_value=0, max_value=10_000),
)
@_SETTINGS
def test_p_order_output_invariant_under_permutation(
    anchor: str, val: str, src_a: str, src_b: str, seed: int
) -> None:
    """P-ORDER: ``build_silver_pairs`` output is invariant under input permutation; every emitted
    pair has ``left_id <= right_id``; no duplicate ``(left_id, right_id)``; no self-pair.
    """
    if not val:
        return

    entities = [
        _entity("id-1", src_a, anchor_prop=anchor, anchor_val=val),
        _entity("id-2", src_b, anchor_prop=anchor, anchor_val=val),
        _entity("id-3", src_a),  # no anchor — anchors-only labels are unaffected by order
    ]
    base = build_silver_pairs(entities)

    rng = random.Random(seed)
    permuted = list(entities)
    rng.shuffle(permuted)
    perm_result = build_silver_pairs(permuted)

    assert base == perm_result, (
        f"output must be order-independent: base={base}, permuted={perm_result}"
    )

    # Structural invariants on the output.
    seen: set[tuple[str, str]] = set()
    for pair in base:
        assert pair.left_id <= pair.right_id, f"pair {pair} not canonically ordered"
        assert pair.left_id != pair.right_id, f"self-pair in output: {pair}"
        key = (pair.left_id, pair.right_id)
        assert key not in seen, f"duplicate pair {key} in output"
        seen.add(key)


# ---------------------------------------------------------------------------
# P-SIGNATURE: proves N1 — no scoring parameter / symbol in build_silver_pairs
# ---------------------------------------------------------------------------

_FORBIDDEN_SCORE_PARAMS = frozenset(
    {"score", "probability", "match_probability", "threshold", "linker"}
)
_FORBIDDEN_SCORE_SYMBOLS = frozenset({"score", "probability", "match_probability", "score_pairs"})


def test_p_signature_build_silver_pairs_has_no_score_parameter() -> None:
    """P-SIGNATURE (N1 part 1): ``build_silver_pairs`` must have no score / probability /
    threshold / linker parameter — the function signature is the structural proof of
    non-circularity.
    """
    sig = inspect.signature(build_silver_pairs)
    param_names = set(sig.parameters)
    forbidden_found = param_names & _FORBIDDEN_SCORE_PARAMS
    assert not forbidden_found, (
        f"build_silver_pairs must not have any scoring parameter; found: {forbidden_found}"
    )


def test_p_signature_silver_module_references_no_scoring_symbol() -> None:
    """P-SIGNATURE (N1 part 2): the ``silver`` module must not import or call any scoring
    symbol (``score_pairs``, ``match_probability``, bare ``probability``) in its executable
    code — proving non-circularity at the source level, not just the API surface.

    Uses the Python AST to check for actual code-level references (imports, calls, attribute
    accesses, Name nodes), skipping docstrings.  This is more reliable than text-searching the
    raw source, which would flag docstring text that DESCRIBES the invariant being proven.

    ADR 0079 N1: "its source text references no scoring symbol" means the code body never uses
    these names as identifiers; explaining in docstrings what the module does NOT do is fine.
    """
    import ast

    source = inspect.getsource(silver)
    tree = ast.parse(source)

    # Collect all Name and Attribute identifiers from the AST, EXCLUDING nodes that appear
    # only inside Expr nodes that are string literals (docstrings / string constants).
    def _collect_code_names(node: ast.AST) -> set[str]:
        """Recursively collect all Name/Attribute identifiers outside docstring Expr nodes."""
        names: set[str] = set()
        for child in ast.walk(node):
            # Skip module/class/function docstrings: an Expr whose value is a Constant str.
            if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                continue  # the walk already visited this subtree — this prune is best-effort
            if isinstance(child, ast.Name):
                names.add(child.id)
            elif isinstance(child, ast.Attribute):
                names.add(child.attr)
        return names

    code_names = _collect_code_names(tree)

    # Also check imports explicitly (they are never inside docstrings).
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)

    all_code_symbols = code_names | imported_names

    # Forbidden scoring symbols (N1).
    for forbidden in ("score_pairs", "match_probability"):
        assert forbidden not in all_code_symbols, (
            f"silver.py must not reference {forbidden!r} in its code (N1 scoring-symbol check); "
            f"found in: {sorted(all_code_symbols)}"
        )

    # ``probability`` as a standalone name is also forbidden.  ``clerical_score`` is allowed
    # (it is a gold-harness concept, not a model score on this pair).
    assert "probability" not in all_code_symbols, (
        "silver.py must not reference bare 'probability' identifier in code (N1 check); "
        f"symbols found: {sorted(all_code_symbols)}"
    )
