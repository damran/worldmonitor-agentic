"""Property: `score_pairs` consults `schema.matchable` FIRST (Gate S-2 phase 2 slice A, ADR 0119).

`wm:Indicator` is `matchable: false` and `extends: [Thing]` only (ADR 0118), so it never shares a
common schema with a Person/Organization -- `_schema_compatible` (post-`predict`) already drops
every Indicator<->Person/Org candidate pair. But that guard runs AFTER Splink has already
blocked, framed, and scored the pair, and it does nothing for an Indicator<->Indicator pair:
`_schema_compatible(Indicator, Indicator)` is trivially `True` (identical schema). So TWO
Indicators can fuzzy-pair with each other through the exact same live path an ordinary
Person/Org pair goes through. `docs/decisions/GATE_S2P2_ABUSECH_SIBLINGS_SPEC.md` S4 requires
the fix to sit BEFORE frame construction: any entity whose `schema.matchable` is `False` must
never enter the DataFrame, the blocking, the linker, or `predict` at all. Under that fix, a
corpus of all-Indicators (or any Indicator mixed into an otherwise-matchable corpus) can never
surface an Indicator id in a returned pair (`test_p_match_1`), and adding a disjoint Indicator
set to a matchable corpus can never perturb the pairs among the matchable entities
(`test_p_match_2`) -- `prior`/m/u are fixed expert-set constants, not corpus-size-derived, so the
non-interference is exact, not approximate (ADR 0119 person_affecting argument).

RED TODAY, confirmed empirically (2026-07-22): every `wm:Indicator` entity's FtM `caption`
resolves to the schema LABEL `"Indicator"`, not its `name`/`indicatorValue`. `Indicator.yaml`
declares no `caption:` property list, and FollowTheMoney's `Schema.caption` is read from the
schema's OWN spec ONLY -- it is NOT inherited from `extends` (verified against
`followthemoney.schema.Schema.__init__`). `_name_fingerprint` (`splink_model.py`) fingerprints
`entity.caption`, so it collapses EVERY Indicator onto the SAME `name_fp` regardless of its
actual IOC value. Any two (or more) Indicator entities therefore always block together
(`substr(name_fp, 1, 4)`) and always hit the name comparison's EXACT level, producing the exact
same deterministic `match_probability` -- measured `0.9083402146985962`, independent of corpus
size or the entities' actual values -- far above `DEFAULT_PREDICT_THRESHOLD` (0.5). So
`test_p_match_1` fails today whenever the generated corpus contains its (generator-forced) >=2
Indicators, and `test_p_match_2` fails today whenever the generated (generator-forced) >=2-member
Indicator set `I` is added to `M`: the mutual Indicator<->Indicator pair(s) appear in
`score_pairs(M | I)` but not in `score_pairs(M)`. Neither failure depends on jaro_winkler
near-miss tuning -- the collision is an EXACT match, deterministic under any hypothesis-drawn
value, so the generator does not need to hunt for it.
"""

from __future__ import annotations

import string

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.ftm import FtmEntity, register_wm_schemata
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.resolution.splink_model import ScoredPair, score_pairs

register_wm_schemata()  # Indicator must exist before ANY test below constructs one.

_SETTINGS = settings(max_examples=75, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# --------------------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------------------


def _indicator(entity_id: str, value: str) -> FtmEntity:
    """An Indicator shaped like a connector's map() output (feodo/threatfox precedent)."""
    data = {
        "id": entity_id,
        "schema": "Indicator",
        "properties": {"name": [value], "indicatorValue": [value], "indicatorType": ["ipv4"]},
        "datasets": ["t"],
    }
    return validate_or_raise(data)


def _org(entity_id: str, name: str) -> FtmEntity:
    data = {"id": entity_id, "schema": "Organization", "properties": {"name": [name]}}
    return validate_or_raise(data)


def _person(entity_id: str, name: str) -> FtmEntity:
    data = {"id": entity_id, "schema": "Person", "properties": {"name": [name]}}
    return validate_or_raise(data)


# --------------------------------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------------------------------

_ID_ALPHABET = string.ascii_lowercase + string.digits
_IOC_VALUE = st.text(
    alphabet=string.ascii_lowercase + string.digits + ".:", min_size=1, max_size=30
)
# Letters-only, min_size=2: guarantees `fingerprints.generate` never returns empty (verified
# against 20,000 random 2-20 char samples), so a Person/Org corpus never degenerates into an
# ALL-null `name_fp` DataFrame column -- an UNRELATED, pre-existing DuckDB type-inference
# fragility (an all-None column is inferred INTEGER, and `block_on("substr(name_fp,1,4)")`
# then raises a Binder error) that a single-char name (e.g. "A") can trip. That crash is not
# the matchable-gate invariant this file pins, so the generator avoids it rather than let it
# masquerade as a false RED.
_NAME = st.text(alphabet=string.ascii_letters, min_size=2, max_size=20)


def _unique_ids(prefix: str, min_size: int, max_size: int) -> st.SearchStrategy[list[str]]:
    """Distinct entity ids under ``prefix`` -- different prefixes across strategies (``ioc-``
    vs ``m-``) guarantee two independently-drawn corpora never accidentally share an id."""
    return st.lists(
        st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=12).map(lambda s: f"{prefix}-{s}"),
        min_size=min_size,
        max_size=max_size,
        unique=True,
    )


@st.composite
def _indicator_set(draw: st.DrawFn) -> list[FtmEntity]:
    """>=2 Indicators with arbitrary (possibly wildly different) IOC values. Forced to >=2
    rather than left to chance: under the CURRENT code any two Indicators collide (see module
    docstring), so this shape reliably reproduces the violation on virtually every example."""
    ids = draw(_unique_ids("ioc", 2, 4))
    values = draw(st.lists(_IOC_VALUE, min_size=len(ids), max_size=len(ids)))
    return [_indicator(i, v) for i, v in zip(ids, values, strict=True)]


@st.composite
def _matchable_corpus(draw: st.DrawFn) -> list[FtmEntity]:
    """0-3 Person/Organization entities; the first two share ONE name so a real candidate pair
    exists among the matchable subset (non-interference must hold on actual content, not just
    on an empty result)."""
    ids = draw(_unique_ids("m", 0, 3))
    shared_name = draw(_NAME)
    entities: list[FtmEntity] = []
    for idx, entity_id in enumerate(ids):
        name = shared_name if idx < 2 else draw(_NAME)
        builder = _org if idx % 2 == 0 else _person
        entities.append(builder(entity_id, name))
    return entities


@st.composite
def _mixed_corpus(draw: st.DrawFn) -> list[FtmEntity]:
    """>=2 Indicators (forces the current-code violation) plus 0-3 Person/Org entities with
    deliberate name collisions: the first two Person/Org entities share ONE name (a real
    matchable candidate pair), and the third -- if present -- is named after one Indicator's
    raw IOC value (the exact Indicator<->Org/Person collision the gate spec calls out; already
    guarded post-predict by ``_schema_compatible``, exercised here against the pre-filter too)."""
    ind_ids = draw(_unique_ids("ioc", 2, 4))
    ind_values = draw(st.lists(_IOC_VALUE, min_size=len(ind_ids), max_size=len(ind_ids)))
    indicators = [_indicator(i, v) for i, v in zip(ind_ids, ind_values, strict=True)]

    other_ids = draw(_unique_ids("ent", 0, 3))
    shared_name = draw(_NAME)
    collision_names = [shared_name, shared_name, ind_values[0]]
    others = [
        (_org if idx % 2 == 0 else _person)(entity_id, collision_names[idx])
        for idx, entity_id in enumerate(other_ids)
    ]
    return indicators + others


def _pair_set(pairs: list[ScoredPair]) -> set[tuple[str, str, float]]:
    return {(p.left_id, p.right_id, p.probability) for p in pairs}


# ---------------------------------------------------------------------------------------------
# P-MATCH-1 -- no returned pair ever references a non-matchable-schema entity.
# ---------------------------------------------------------------------------------------------


@given(corpus=_mixed_corpus())
@_SETTINGS
def test_p_match_1_no_pair_references_a_non_matchable_entity(corpus: list[FtmEntity]) -> None:
    matchable_ids = {e.id for e in corpus if e.schema.matchable}
    non_matchable_ids = {e.id for e in corpus if not e.schema.matchable}
    assert len(non_matchable_ids) >= 2, "generator contract: >=2 Indicators in every corpus"

    pairs = score_pairs(corpus)

    for pair in pairs:
        assert pair.left_id not in non_matchable_ids and pair.right_id not in non_matchable_ids, (
            f"pair ({pair.left_id!r}, {pair.right_id!r}, p={pair.probability!r}) references a "
            "non-matchable-schema entity (an Indicator) -- schema.matchable must be consulted "
            f"BEFORE frame construction. non_matchable_ids={non_matchable_ids!r}"
        )
        assert pair.left_id in matchable_ids and pair.right_id in matchable_ids, (
            f"pair ({pair.left_id!r}, {pair.right_id!r}) has an id outside the matchable subset "
            f"entirely: matchable_ids={matchable_ids!r}"
        )


# ---------------------------------------------------------------------------------------------
# P-MATCH-2 -- adding a disjoint Indicator set never perturbs the matchable-only pairs.
# ---------------------------------------------------------------------------------------------


@given(matchable=_matchable_corpus(), indicators=_indicator_set())
@_SETTINGS
def test_p_match_2_adding_indicators_is_non_interfering(
    matchable: list[FtmEntity], indicators: list[FtmEntity]
) -> None:
    base = score_pairs(matchable)
    combined = score_pairs(matchable + indicators)

    base_set = _pair_set(base)
    combined_set = _pair_set(combined)
    assert base_set == combined_set, (
        "adding a disjoint, non-matchable Indicator set perturbed the pairs scored among the "
        f"matchable-only corpus. only-in-combined={combined_set - base_set!r} "
        f"only-in-base={base_set - combined_set!r}"
    )
