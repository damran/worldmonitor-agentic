"""Multi-script name resolution (ADR 0035) — the fingerprint name projection.

`_flatten` projected `entity.first("name")`, whose value flips script for bilingual
records (FtM `first()` is sort-order-dependent), so two records of ONE entity that store
their names in a different script order never matched — a core-ER miss on exactly the
multi-feed sanctions entities we most need to resolve. The projection now uses a
`fingerprints` key (transliterate + sort tokens + strip legal-form), so cross-script
duplicates collapse to the same key and merge.

These pin the fix on REAL OpenSanctions name strings AND prove over-merge stays low: for a
CTI graph, fusing two DISTINCT sanctioned entities is worse than missing a merge, so the
over-merge controls assert genuinely-different entities stay below the 0.92 threshold.

KNOWN GAP (ADR 0035, deferred): `fingerprints` renders abjad scripts (Arabic/Persian) as
lossy consonant skeletons — nomenklatura `LogicV2` is the robust follow-up.
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import _name_fingerprint, score_pairs

# Real OpenSanctions name strings: the two stored names of the us_ofac_sdn "Legion Komplekt"
# pair (one Russian-Cyrillic, one English-Latin) — the records carry the SAME name set in a
# different stored order, which is exactly what broke the sort-first projection.
LEGION_CYR = "Общество С Ограниченной Ответственностью Легион Комплект"
LEGION_LAT = "LIMITED LIABILITY COMPANY LEGION KOMPLEKT"


def _org(entity_id: str, names: list[str], *, sensitive: bool = False) -> FtmEntity:
    props: dict[str, list[str]] = {"name": names, "country": ["ru"]}
    if sensitive:
        props["topics"] = ["sanction"]
    return make_entity(
        {"id": entity_id, "schema": "Organization", "properties": props, "datasets": ["t"]}
    )


def _top_probability(entities: list[FtmEntity]) -> float:
    return max((pair.probability for pair in score_pairs(entities)), default=0.0)


def test_bilingual_record_merges_and_parks() -> None:
    """The real Legion case: both records list both scripts but in opposite order.

    Pre-fix `first("name")` gave one record the Cyrillic name and the other the Latin name
    (jaro_winkler 0.378 -> no merge). The fingerprint key is identical for both, so they
    merge — and because both are sanctioned, the catastrophic-merge guard PARKS it.
    """
    a = _org("a", [LEGION_CYR, LEGION_LAT], sensitive=True)
    b = _org("b", [LEGION_LAT, LEGION_CYR], sensitive=True)
    merges = [c for c in cluster_and_merge([a, b], score_pairs([a, b])) if c.is_merge]
    assert len(merges) == 1, "bilingual records of one entity must merge (was 0.378, no merge)"
    flagged, reason = needs_review(merges[0], {"a": a, "b": b})
    assert flagged is True and "sensitive" in reason.lower()


def test_cross_script_no_shared_variant_merges() -> None:
    """The harder case caption-matching could not solve: one record holds ONLY the Cyrillic
    name, the other ONLY the Latin one. Transliteration inside `fingerprints` bridges them
    (both -> "komplekt legion"); pre-fix this scored ~0.38."""
    a = _org("a", [LEGION_CYR], sensitive=True)
    b = _org("b", [LEGION_LAT], sensitive=True)
    assert _top_probability([a, b]) >= 0.92


def test_same_script_duplicate_still_merges_no_regression() -> None:
    """Regression guard: a same-script real duplicate (my_aob_sanctions "Sathiea Seelean")
    must still merge exactly as before."""
    name = "Sathiea Seelean A/L Manickam"
    a = make_entity(
        {
            "id": "a",
            "schema": "LegalEntity",
            "properties": {"name": [name], "country": ["my"]},
            "datasets": ["t"],
        }
    )
    b = make_entity(
        {
            "id": "b",
            "schema": "LegalEntity",
            "properties": {"name": [name], "country": ["my"]},
            "datasets": ["t"],
        }
    )
    assert _top_probability([a, b]) >= 0.92


def test_distinct_orgs_sharing_legal_form_do_not_merge() -> None:
    """Over-merge control: genuinely different orgs that SHARE a legal-form token. Stripping
    the legal form must leave the DISTINCT brand, not collapse them. Real us_dod company
    names (all "Co., Ltd."/"Company Limited") must stay below threshold."""
    orgs = [
        _org("c1", ["Shenzhen DJI Innovation Technology Co., Ltd."], sensitive=True),
        _org("c2", ["SMIC Hong Kong International Company Limited"], sensitive=True),
        _org("c3", ["AVIC Jonhon Optronic Technology Co., Ltd."], sensitive=True),
    ]
    assert _top_probability(orgs) < 0.92
    assert all(not c.is_merge for c in cluster_and_merge(orgs, score_pairs(orgs)))


def test_distinct_orgs_sharing_a_brand_token_do_not_merge() -> None:
    """Over-merge control (similar skeletons): two different RU LLCs that share ONE brand
    token ("Легион"/"Legion") but are distinct entities must not fuse."""
    a = _org("a", ["Общество С Ограниченной Ответственностью Легион Комплект"], sensitive=True)
    b = _org("b", ["Общество С Ограниченной Ответственностью Легион Снаб"], sensitive=True)
    assert _top_probability([a, b]) < 0.92


def test_no_name_entity_projects_null_fingerprint() -> None:
    """The guard: a no-name entity (Sanction) projects a NULL fingerprint, not its caption
    fallback (a programme code like "RUSSIA-EO14024"), so it falls to the null comparison
    level and can never spuriously match on a non-name string."""
    sanction = make_entity(
        {
            "id": "s1",
            "schema": "Sanction",
            "properties": {"program": ["RUSSIA-EO14024"]},
            "datasets": ["t"],
        }
    )
    assert sanction.caption == "RUSSIA-EO14024"  # caption WOULD fall back to the programme code
    assert _name_fingerprint(sanction) is None  # ...but the name-guard keeps it null
    # A named entity, by contrast, projects its script-stable key.
    assert _name_fingerprint(_org("a", [LEGION_CYR])) == "komplekt legion"


def _named(entity_id: str, schema: str, name: str, country: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": schema,
            "properties": {"name": [name], "country": [country], "topics": ["sanction"]},
            "datasets": ["t"],
        }
    )


def test_company_named_after_owner_does_not_merge_with_the_person() -> None:
    """Over-merge + crash guard (real us_ofac 'Mamoun Darkazanli'): a company named after its
    owner shares the name fingerprint with the Person ('darkazanli mamoun'), but they are
    DISTINCT entities with NO common schema. The schema-compatibility gate drops the candidate
    pair, so it neither over-merges nor crashes FtM's merge (which raises on Org+Person)."""
    org = _named("o", "Organization", "MAMOUN DARKAZANLI IMPORT-EXPORT COMPANY", "de")
    person = _named("p", "Person", "Mamoun Darkazanli", "de")
    assert _name_fingerprint(org) == _name_fingerprint(person)  # the keys DO collide...
    assert score_pairs([org, person]) == []  # ...but the incompatible pair is dropped
    assert all(not c.is_merge for c in cluster_and_merge([org, person], score_pairs([org, person])))


def test_organization_and_vessel_namesake_does_not_merge() -> None:
    """Real us_ofac 'Yuzhmorgeologiya': an Organization and a Vessel of the same name have no
    common schema — dropped, no over-merge, no crash."""
    org = _named("o", "Organization", "Yuzhmorgeologiya AO", "ru")
    vessel = _named("v", "Vessel", "YUZHMORGEOLOGIYA", "ru")
    assert score_pairs([org, vessel]) == []
    assert all(not c.is_merge for c in cluster_and_merge([org, vessel], score_pairs([org, vessel])))


def test_compatible_schemas_still_merge() -> None:
    """The gate must NOT block COMPATIBLE schemas: an Organization and a Company of the same
    name share a common schema (Company) and must still merge (regression guard for the gate
    being too broad)."""
    org = _named("o", "Organization", "Acme Holdings", "us")
    company = _named("c", "Company", "Acme Holdings", "us")
    assert _top_probability([org, company]) >= 0.92
    assert any(c.is_merge for c in cluster_and_merge([org, company], score_pairs([org, company])))
