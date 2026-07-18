"""Property / metamorphic test pinning WP-1 spec D2 — silver blocking is EXACT-equivalent.

``docs/reviews/GATE_WP1_MEASUREMENT_SPEC.md`` D2 requires replacing ``silver.py``'s current
``for i in range(n): for j in range(i+1, n)`` full cross-product with candidate-key blocking
(union over ``ANCHOR_PROPERTIES`` of within-property-value groups), while leaving the PER-PAIR
classification untouched. The equivalence argument is that a pair outside every anchor group
would classify to ``abstain`` in the naive loop anyway, so omitting it from the candidate set is
byte-identical output.

This test is the oracle for that equivalence claim. ``_naive_reference`` below is a
**self-contained re-implementation** of the CURRENT ``build_silver_pairs`` double-loop semantics
(copied, not imported, from ``resolution/silver.py`` as of ADR 0085) — it does NOT call
``build_silver_pairs`` or import its private helpers. It imports only the module's PUBLIC
constants (``GLOBALLY_UNIQUE``, ``JURISDICTION_SCOPED``, ``SILVER_SOURCE``) per the gate
instructions, so that after the builder replaces the internals with candidate-key blocking, this
test still independently proves output equivalence rather than testing the implementation against
itself.

Right now (before D2 lands) ``build_silver_pairs`` IS the naive double loop, so
``build_silver_pairs(entities) == _naive_reference(entities)`` must hold trivially and this test
PASSES today — that is the expected GREEN baseline the builder's blocked implementation must keep
passing byte-for-byte (same list, same order), never a weaker "same set" check.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, get_provenance, stamp
from worldmonitor.resolution.gold import GoldPair
from worldmonitor.resolution.silver import (
    GLOBALLY_UNIQUE,
    JURISDICTION_SCOPED,
    SILVER_SOURCE,
    build_silver_pairs,
)

# ---------------------------------------------------------------------------
# Self-contained naive oracle — copied semantics, no import of silver's privates.
# ---------------------------------------------------------------------------

_ANCHOR_PROPS: tuple[str, ...] = GLOBALLY_UNIQUE + JURISDICTION_SCOPED
_JURISDICTION_PROPS: tuple[str, ...] = ("jurisdiction", "country")


def _canonical(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left <= right else (right, left)


def _anchor_values(entity: FtmEntity, prop: str) -> frozenset[str]:
    return frozenset(str(v) for v in entity.get(prop, quiet=True) if str(v))


def _jurisdiction_values(entity: FtmEntity) -> frozenset[str]:
    vals: set[str] = set()
    for prop in _JURISDICTION_PROPS:
        for v in entity.get(prop, quiet=True):
            s = str(v).strip().lower()
            if s:
                vals.add(s)
    return frozenset(vals)


def _jurisdictions_corroborate(a_jur: frozenset[str], b_jur: frozenset[str]) -> bool:
    return bool(a_jur) and bool(b_jur) and bool(a_jur & b_jur)


def _naive_reference(entities: Sequence[FtmEntity]) -> list[GoldPair]:
    """Re-derivation of the CURRENT ``build_silver_pairs`` O(n^2) semantics (ADR 0079/0085)."""
    records: list[FtmEntity] = [e for e in entities if e.id is not None]
    n = len(records)
    if n < 2:
        return []

    anchor_cache: dict[str, dict[str, frozenset[str]]] = {}
    jurisdiction_cache: dict[str, frozenset[str]] = {}
    source_cache: dict[str, str | None] = {}
    for e in records:
        eid = e.id
        assert eid is not None
        anchor_cache[eid] = {prop: _anchor_values(e, prop) for prop in _ANCHOR_PROPS}
        jurisdiction_cache[eid] = _jurisdiction_values(e)
        prov = get_provenance(e)
        source_cache[eid] = prov.source_id if prov is not None else None

    by_pair: dict[tuple[str, str], GoldPair] = {}
    for i in range(n):
        for j in range(i + 1, n):
            a_id = records[i].id
            b_id = records[j].id
            assert a_id is not None and b_id is not None
            if a_id == b_id:
                continue

            key = _canonical(a_id, b_id)
            if key in by_pair:
                continue

            a_anchors = anchor_cache[a_id]
            b_anchors = anchor_cache[b_id]
            a_jur = jurisdiction_cache[a_id]
            b_jur = jurisdiction_cache[b_id]
            a_src = source_cache[a_id]
            b_src = source_cache[b_id]

            has_shared = False
            has_conflict = False

            for prop in GLOBALLY_UNIQUE:
                av = a_anchors[prop]
                bv = b_anchors[prop]
                if not av or not bv:
                    continue
                if av & bv:
                    has_shared = True
                else:
                    has_conflict = True

            if _jurisdictions_corroborate(a_jur, b_jur):
                for prop in JURISDICTION_SCOPED:
                    av = a_anchors[prop]
                    bv = b_anchors[prop]
                    if not av or not bv:
                        continue
                    if av & bv:
                        has_shared = True
                    else:
                        has_conflict = True

            if has_shared and has_conflict:
                continue
            elif has_shared:
                if a_src is not None and b_src is not None and a_src != b_src:
                    left, right = key
                    by_pair[key] = GoldPair(
                        left_id=left,
                        right_id=right,
                        label="match",
                        source=SILVER_SOURCE,
                        clerical_score=None,
                    )
            elif has_conflict:
                left, right = key
                by_pair[key] = GoldPair(
                    left_id=left,
                    right_id=right,
                    label="non_match",
                    source=SILVER_SOURCE,
                    clerical_score=None,
                )

    return sorted(by_pair.values(), key=lambda p: (p.left_id, p.right_id))


# ---------------------------------------------------------------------------
# Hypothesis corpus generator — forces shared / conflicting / absent anchors, same / distinct /
# missing sources, corroborating / disjoint / absent jurisdictions, occasional id-less entities.
# ---------------------------------------------------------------------------

_VALUE_POOL = ("V1", "V2", "V3")  # small pool -> forces shared AND conflicting values
_JUR_POOL = ("gb", "us", None)
_SRC_POOL = ("srcA", "srcB", None)  # None = never stamped (unstamped entity)
_RETRIEVED_AT = "2026-01-01T00:00:00Z"

_entity_spec = st.fixed_dictionaries(
    {
        "has_id": st.booleans(),  # False -> id-less entity (filtered by both implementations)
        "wikidata": st.one_of(st.none(), st.sampled_from(_VALUE_POOL)),
        "lei": st.one_of(st.none(), st.sampled_from(_VALUE_POOL)),
        "regno": st.one_of(st.none(), st.sampled_from(_VALUE_POOL)),
        "jurisdiction": st.sampled_from(_JUR_POOL),
        "source": st.sampled_from(_SRC_POOL),
    }
)


def _build_entity(index: int, spec: dict[str, Any]) -> FtmEntity:
    props: dict[str, list[str]] = {"name": ["Acme"]}
    if spec["wikidata"] is not None:
        props["wikidataId"] = [spec["wikidata"]]
    if spec["lei"] is not None:
        props["leiCode"] = [spec["lei"]]
    if spec["regno"] is not None:
        props["registrationNumber"] = [spec["regno"]]
    if spec["jurisdiction"] is not None:
        props["jurisdiction"] = [spec["jurisdiction"]]

    data: dict[str, Any] = {"schema": "Company", "properties": props}
    if spec["has_id"]:
        data["id"] = f"e{index}"
    entity = make_entity(data)

    if spec["source"] is not None:
        entity = stamp(
            entity,
            Provenance(
                source_id=spec["source"],
                retrieved_at=_RETRIEVED_AT,
                reliability="B",
                source_record=f"s3://landing/e{index}.json",
            ),
        )
    return entity


@st.composite
def _corpus(draw: st.DrawFn) -> list[FtmEntity]:
    """0..12 synthetic FtM entities (spec D2: bounded corpus size)."""
    specs = draw(st.lists(_entity_spec, min_size=0, max_size=12))
    return [_build_entity(i, spec) for i, spec in enumerate(specs)]


_SETTINGS = settings(max_examples=75, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ---------------------------------------------------------------------------
# The equivalence property
# ---------------------------------------------------------------------------


@given(entities=_corpus())
@_SETTINGS
def test_build_silver_pairs_matches_naive_reference_exactly(entities: list[FtmEntity]) -> None:
    """``build_silver_pairs`` output must be BYTE-IDENTICAL (same list, same order) to the
    self-contained naive-loop oracle, for every generated corpus.

    This is the D2 equivalence proof: candidate-key blocking must never change WHICH pairs are
    classified or HOW — only how the candidate set is generated. A weaker check (e.g. comparing
    ``set(pairs)`` instead of the ordered list, or comparing only ``len(pairs)``) would let a
    blocking implementation that drops or reorders classified pairs slip through, so the
    assertion below is the exact ``==`` on the ordered ``list[GoldPair]``.
    """
    expected = _naive_reference(entities)
    actual = build_silver_pairs(entities)
    assert actual == expected, (
        f"build_silver_pairs diverged from the naive-loop oracle for a {len(entities)}-entity "
        f"corpus.\nexpected={expected}\nactual={actual}"
    )


# ---------------------------------------------------------------------------
# Explicit example() regression cases (spec D2 instructions)
# ---------------------------------------------------------------------------


def _prov(source_id: str) -> Provenance:
    return Provenance(
        source_id=source_id,
        retrieved_at=_RETRIEVED_AT,
        reliability="B",
        source_record=f"s3://landing/{source_id}.json",
    )


def test_example_contradiction_shared_qid_conflicting_lei_is_dropped() -> None:
    """A pair sharing wikidataId (positive-eligible) but conflicting on leiCode
    (negative-eligible) must be DROPPED entirely — never emitted as either label."""
    a = make_entity(
        {
            "id": "e1",
            "schema": "Company",
            "properties": {"name": ["Acme"], "wikidataId": ["V1"], "leiCode": ["V2"]},
        }
    )
    b = make_entity(
        {
            "id": "e2",
            "schema": "Company",
            "properties": {"name": ["Acme"], "wikidataId": ["V1"], "leiCode": ["V3"]},
        }
    )
    stamp(a, _prov("srcA"))
    stamp(b, _prov("srcB"))

    result = build_silver_pairs([a, b])
    assert result == [], f"contradiction pair must be dropped, got {result}"
    assert result == _naive_reference([a, b])


def test_example_same_source_clean_positive_abstains_never_non_match() -> None:
    """Two entities sharing wikidataId from the SAME source must abstain — no ``match`` (the
    ≥2-distinct-sources rule) and, critically, NEVER downgraded to ``non_match``."""
    a = make_entity(
        {"id": "e1", "schema": "Company", "properties": {"name": ["Acme"], "wikidataId": ["V1"]}}
    )
    b = make_entity(
        {"id": "e2", "schema": "Company", "properties": {"name": ["Acme"], "wikidataId": ["V1"]}}
    )
    stamp(a, _prov("srcA"))
    stamp(b, _prov("srcA"))

    result = build_silver_pairs([a, b])
    assert result == [], f"same-source clean positive must abstain, got {result}"
    assert not any(p.label == "non_match" for p in result)
    assert result == _naive_reference([a, b])


def test_example_regno_match_without_jurisdiction_corroboration_abstains() -> None:
    """A shared registrationNumber with NO jurisdiction/country on either side must abstain —
    the jurisdiction-scoped tier requires corroboration before a shared value carries signal."""
    a = make_entity(
        {
            "id": "e1",
            "schema": "Company",
            "properties": {"name": ["Acme"], "registrationNumber": ["V1"]},
        }
    )
    b = make_entity(
        {
            "id": "e2",
            "schema": "Company",
            "properties": {"name": ["Acme"], "registrationNumber": ["V1"]},
        }
    )
    stamp(a, _prov("srcA"))
    stamp(b, _prov("srcB"))

    result = build_silver_pairs([a, b])
    assert result == [], (
        f"regNo match without jurisdiction corroboration must abstain, got {result}"
    )
    assert result == _naive_reference([a, b])
