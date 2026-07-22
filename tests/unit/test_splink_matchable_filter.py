"""Concrete oracles for the `score_pairs`-consults-`schema.matchable` gate (Gate S-2 phase 2
slice A, `docs/decisions/GATE_S2P2_ABUSECH_SIBLINGS_SPEC.md` S4, ADR 0119).

`wm:Indicator` (`matchable: false`, `extends: [Thing]` only -- ADR 0118) must never enter
Splink's DataFrame/blocking/linker/`predict` at all; today it does, and only the post-`predict`
`_schema_compatible` guard (schema-incompatible pairs, e.g. Indicator vs Organization) filters
it out. That guard is a no-op for an Indicator<->Indicator pair -- `_schema_compatible(Indicator,
Indicator)` is trivially `True` (identical schema) -- so two Indicators fuzzy-pair through the
live path exactly like a real Person/Org duplicate would.

RED TODAY (verified empirically, 2026-07-22): `test_all_indicator_corpus_returns_no_pairs` fails
because `score_pairs` currently returns THREE non-empty pairs for a 3-Indicator corpus, each with
`match_probability == 0.9083402146985962` -- deterministic and independent of the Indicators'
actual values. This happens because `Indicator.yaml` declares no `caption:` property list, so
every Indicator's FtM `caption` falls back to the schema LABEL `"Indicator"` (FollowTheMoney's
`Schema.caption` is read from a schema's own spec only, never inherited via `extends`) --
`splink_model._name_fingerprint` fingerprints `entity.caption`, so EVERY Indicator collapses onto
the identical `name_fp` regardless of `indicatorValue`, blocks with every other Indicator
(`substr(name_fp, 1, 4)`), and hits the name comparison's exact-match level. GREEN once
`score_pairs` filters non-matchable entities out before frame construction (the fix makes this
caption quirk moot for scoring, since Indicators never reach the frame).

The other two scenarios below (the "acme ltd" cross-schema collision, and one Indicator + one
Organization) already return `[]` on the pre-fix tree too -- by the EXISTING `_schema_compatible`
post-predict guard for the cross-schema pairing, and because that same guard also empties the
single Indicator+Organization corpus -- so they are not independently RED. They are still
required regression pins: the fix must not regress this pre-existing (correct) behavior, and they
pin acceptance criteria 3+4 (the short-circuit and the non-interference result) as concrete,
non-property oracles alongside `tests/property/test_prop_matchable_gate.py`'s `@given` suite.
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import FtmEntity, make_entity, register_wm_schemata
from worldmonitor.resolution.splink_model import score_pairs

register_wm_schemata()  # Indicator must exist before ANY test below constructs one.


def _indicator(entity_id: str, value: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Indicator",
            "properties": {
                "name": [value],
                "indicatorValue": [value],
                "indicatorType": ["ipv4"],
            },
            "datasets": ["t"],
        }
    )


def _org(entity_id: str, name: str) -> FtmEntity:
    return make_entity({"id": entity_id, "schema": "Organization", "properties": {"name": [name]}})


def _person(entity_id: str, name: str) -> FtmEntity:
    return make_entity({"id": entity_id, "schema": "Person", "properties": {"name": [name]}})


def _pair_tuples(pairs: list) -> set[tuple[str, str, float]]:
    return {(p.left_id, p.right_id, p.probability) for p in pairs}


def test_acme_ltd_indicator_never_pairs_and_org_person_result_is_unchanged() -> None:
    """An Indicator whose `indicatorValue`/`name` equals an Org's AND a Person's own name."""
    indicator = _indicator("ind-acme", "acme ltd")
    org = _org("org-acme", "Acme Ltd")
    person = _person("per-acme", "Acme Ltd")

    without_indicator = score_pairs([org, person])
    with_indicator = score_pairs([indicator, org, person])

    for pair in with_indicator:
        assert pair.left_id != "ind-acme" and pair.right_id != "ind-acme", (
            f"the Indicator must never appear in a returned pair, got {pair!r}"
        )

    assert _pair_tuples(with_indicator) == _pair_tuples(without_indicator), (
        "adding the Indicator must not change the Org/Person result: "
        f"without={without_indicator!r} with={with_indicator!r}"
    )


def test_all_indicator_corpus_returns_no_pairs() -> None:
    """A corpus made ENTIRELY of Indicators (>=2) must score to `[]` -- the `< 2` short-circuit
    applies to the matchable subset (zero matchable entities here), per acceptance criterion 3."""
    indicators = [
        _indicator("ind-1", "203.0.113.7:443"),
        _indicator("ind-2", "198.51.100.9:8080"),
        _indicator("ind-3", "bad.example.com"),
    ]
    assert score_pairs(indicators) == []


def test_one_indicator_and_one_org_returns_no_pairs() -> None:
    """One Indicator + one Organization (one matchable entity) must score to `[]` -- the
    matchable subset has size 1, under the `< 2` short-circuit (acceptance criterion 3)."""
    indicator = _indicator("ind-solo", "203.0.113.7:443")
    org = _org("org-solo", "Acme Corporation Ltd")
    assert score_pairs([indicator, org]) == []
