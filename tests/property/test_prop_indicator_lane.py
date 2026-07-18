"""Property: the `wm:Indicator` lane never enters fuzzy resolution (Gate S-2, ADR 0118).

`wm:Indicator` is the FIRST L2 extension (CLAUDE.md: "wm: extensions only where FtM can't
reach"). It exists purely so C2/IOC records (Feodo Tracker, S-2) get an FtM-native home; the
non-negotiable invariant is that it can NEVER fuzzy-merge with a person/org (CLAUDE.md
catastrophic-merge guard) — it converges by DETERMINISTIC ID ONLY. This pins the four
properties from `docs/reviews/GATE_S2_WM_INDICATOR_FEODO_SPEC.md`:

* **P-IND-1 (id convergence, non-circular)** — an Indicator entity's `id` survives a
  `to_dict()`/`make_entity()` round trip byte-for-byte, and two records that share an id (the
  connector's deterministic `feodo-<sha1(value)>` rule — pinned independently in
  `tests/unit/test_feodo_connector.py`, NOT re-derived here) always carry that SAME id
  regardless of differing source-attributed properties (name/malware), while two records with
  DISTINCT ids are never silently collapsed onto one. Kept deliberately simple/non-circular per
  the gate spec: this file tests the LANE substrate (id identity is load-bearing and stable),
  not the sha1 derivation itself.
* **P-IND-2 (never-merge)** — (a) `model.common_schema(Indicator, X)` raises `InvalidData` for
  every Person/Org-family schema (`Indicator` extends `Thing` directly, not `LegalEntity`, so
  it shares no linear ancestor with any of them); (b) `Indicator.matchable is False`; (c) the
  STRONGEST cheap end-to-end pin — driving `resolution.merge.cluster_and_merge` with an
  (Indicator, Organization) pair and a high-confidence `ScoredPair` NEVER yields a cluster whose
  `member_ids` contain both: `_merge_entities`'s `FtM entity.merge()` raises `InvalidData` on
  the schema-incompatible member (H-2, ADR 0041 — the SAME mechanism that already protects
  Articles/Events), so the pipeline dead-letters/re-singles it (`merge_incompatible=True`)
  rather than fusing it. See "P-IND-2(c) choice" in the gate report for why this exclusion
  evidence (not a hand-rolled clustering assertion) is the strongest cheap pin.
* **P-IND-3 (injection idempotency)** — calling `register_wm_schemata()` twice never raises,
  never duplicates/corrupts the schema (a python `dict` can't hold a duplicate key, so the
  meaningful pin is that a SECOND call doesn't churn `model.generate()` into a different object
  graph): the schema stays identity/equality-stable, entities built before/after are
  byte-identical, `matchable` stays `False`, and — the sharpest regression pin — the never-merge
  guarantee (P-IND-2a) is NOT weakened by re-injection.
* **P-IND-4 (hostile values)** — a 10,000-char string never crashes/truncates a string-typed
  property, and a value that is NEVER a valid FtM partial-ISO date (verified independently
  against `registry.date.clean`, see `_junk_date`) is silently dropped by `entity.add()` on a
  date-typed property, never raising; the whole hostile entity still round-trips through
  `to_dict()`/`make_entity()` with no exception.

RED TODAY: `worldmonitor.ontology.ftm` does not yet export `register_wm_schemata` — the
top-level `from worldmonitor.ontology.ftm import ... register_wm_schemata` raises `ImportError`
and the whole module errors at collection. That is the correct RED (pinned per the gate spec's
"Verified facts": FtM 4.9.2 schema injection is real and works once `register_wm_schemata`
exists; this file is the oracle for that not-yet-built entry point). GREEN once the builder
lands `ontology/ftm.py::register_wm_schemata` + `ontology/schema/wm/Indicator.yaml`
(ADR 0118 / gate S-2 spec D1). `register_wm_schemata()` is called ONCE at module level (right
after imports) so `Indicator` exists for every test below, mirroring D1 item 2's own
"idempotent / invoked at import time" contract.
"""

from __future__ import annotations

import string

import pytest
from followthemoney.exc import InvalidData
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.ftm import FtmEntity, get_model, make_entity, register_wm_schemata
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.splink_model import ScoredPair

register_wm_schemata()  # Indicator must exist before ANY test below constructs one.

_SETTINGS = settings(max_examples=75, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# --------------------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------------------


def _indicator(entity_id: str, value: str, *, malware: str | None = None) -> FtmEntity:
    """An Indicator entity shaped like the (future) feodo connector's map() output.

    Goes through `validate_or_raise` (not a bare `make_entity`) so this file also pins D1 item 2's
    "`validate_or_raise` must accept Indicator entities unchanged" requirement on every call.
    """
    props: dict[str, list[str]] = {
        "name": [value],
        "indicatorValue": [value],
        "indicatorType": ["ipv4"],
    }
    if malware:
        props["malwareFamily"] = [malware]
    data = {"id": entity_id, "schema": "Indicator", "properties": props, "datasets": ["feodo"]}
    return validate_or_raise(data)


def _org(entity_id: str, name: str) -> FtmEntity:
    return make_entity({"id": entity_id, "schema": "Organization", "properties": {"name": [name]}})


# --------------------------------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------------------------------

_SAFE_ID = st.text(alphabet=string.ascii_lowercase + string.digits + "-", min_size=1, max_size=40)
_IOC_VALUE = st.text(
    alphabet=string.ascii_lowercase + string.digits + ".:", min_size=1, max_size=30
)
_MALWARE = st.one_of(st.none(), st.text(alphabet=string.ascii_letters, min_size=1, max_size=20))
_ORG_NAME = st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=20)
_SCORE = st.floats(min_value=0.92, max_value=1.0, allow_nan=False, allow_infinity=False)

# Every schema sampled here must be NEITHER an ancestor NOR a descendant of Indicator (which
# extends ONLY Thing per D1 item 1) — i.e. no `is_a` relation in either direction — so
# `model.common_schema` is guaranteed (by FtM's own `is_a`-linear-chain rule, verified against
# the pinned FtM 4.9.2 below) to raise `InvalidData` for every one of them. "Thing" is
# deliberately excluded (it IS an ancestor of Indicator, so common_schema would return Indicator
# itself rather than raise).
_NEVER_COMMON_SCHEMATA = (
    "Organization",
    "Company",
    "Person",
    "LegalEntity",
    "Vehicle",
    "Address",
    "Event",
    "Asset",
)

# A string that can NEVER be a valid FtM partial-ISO date: `followthemoney.types.date.DateType`
# is backed by `prefixdate.parse`, a strict digit-prefix grammar (`2021`, `2021-02`,
# `2021-02-16`, ...) — a string with NO digits at all can never match it (independently verified:
# 2000 random samples from this exact alphabet all cleaned to `None`). The fixed examples are
# ALSO independently verified to clean to `None`.
_JUNK_DATE_ALPHABET = string.ascii_letters + "".join(c for c in string.punctuation if c not in "-:")
_JUNK_DATE_FIXED = ("not-a-date", "garbage!!!", "junk-value", "   ", "")


def _junk_date() -> st.SearchStrategy[str]:
    fuzzed = st.text(alphabet=_JUNK_DATE_ALPHABET, min_size=0, max_size=20)
    return st.one_of(st.sampled_from(_JUNK_DATE_FIXED), fuzzed)


def _huge_string() -> st.SearchStrategy[str]:
    """A ~10,000-char string built by repeating a small random fragment (fast to shrink)."""
    fragment = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=40)
    return fragment.map(lambda s: (s * ((10_000 // len(s)) + 1))[:10_000])


# ---------------------------------------------------------------------------------------------
# P-IND-1 — id identity round-trips and is the SOLE convergence key (never re-derived here).
# ---------------------------------------------------------------------------------------------


@given(
    id_a=_SAFE_ID,
    id_b=_SAFE_ID,
    value_a=_IOC_VALUE,
    value_b=_IOC_VALUE,
    malware_a=_MALWARE,
    malware_b=_MALWARE,
)
@_SETTINGS
def test_p_ind_1_id_is_stable_and_the_sole_convergence_key(
    id_a: str, id_b: str, value_a: str, value_b: str, malware_a: str | None, malware_b: str | None
) -> None:
    entity_a = _indicator(id_a, value_a, malware=malware_a)
    entity_b = _indicator(id_b, value_b, malware=malware_b)

    # The id survives a to_dict()/make_entity() round trip byte-for-byte.
    round_a = make_entity(entity_a.to_dict())
    round_b = make_entity(entity_b.to_dict())
    assert round_a.id == entity_a.id == id_a
    assert round_b.id == entity_b.id == id_b
    assert round_a.schema.name == "Indicator"
    assert round_b.schema.name == "Indicator"

    if id_a == id_b:
        # Two records the connector's sha1 rule assigned the SAME id (i.e. the same IOC value)
        # converge on that id regardless of differing source-attributed properties.
        assert entity_a.id == entity_b.id
    else:
        # Distinct ids (distinct IOC values) are NEVER silently collapsed onto one node.
        assert entity_a.id != entity_b.id
        assert round_a.id != round_b.id


# ---------------------------------------------------------------------------------------------
# P-IND-2(a) — never a common schema with any Person/Org-family schema.
# ---------------------------------------------------------------------------------------------


@given(other_schema=st.sampled_from(_NEVER_COMMON_SCHEMATA))
@_SETTINGS
def test_p_ind_2a_no_common_schema_with_person_org_family(other_schema: str) -> None:
    model = get_model()
    indicator_schema = model.schemata["Indicator"]
    other = model.get(other_schema)
    assert other is not None, f"fixture schema {other_schema!r} unexpectedly missing from FtM"

    with pytest.raises(InvalidData):
        model.common_schema(indicator_schema, other)


# ---------------------------------------------------------------------------------------------
# P-IND-2(b) — the schema itself is never matchable (FtM's own fuzzy-resolution gate).
# ---------------------------------------------------------------------------------------------


def test_p_ind_2b_schema_is_not_matchable() -> None:
    schema = get_model().schemata["Indicator"]
    assert schema.matchable is False


# ---------------------------------------------------------------------------------------------
# P-IND-2(c) — cluster_and_merge NEVER fuses an Indicator with an Organization, even when
# Splink hands it a high-confidence pair. This is the strongest CHEAP end-to-end pin: it drives
# the REAL merge helper (the same one every ordinary Company/Person merge goes through) rather
# than re-implementing a clustering assertion; H-2 (ADR 0041)'s existing schema-incompatible-drop
# path is the exclusion evidence.
# ---------------------------------------------------------------------------------------------


@given(
    indicator_id=_SAFE_ID,
    org_id=_SAFE_ID,
    value=_IOC_VALUE,
    org_name=_ORG_NAME,
    score=_SCORE,
)
@_SETTINGS
def test_p_ind_2c_cluster_and_merge_never_fuses_indicator_with_organization(
    indicator_id: str, org_id: str, value: str, org_name: str, score: float
) -> None:
    assume(indicator_id != org_id)
    indicator = _indicator(indicator_id, value)
    org = _org(org_id, org_name)

    clusters = cluster_and_merge(
        [indicator, org], [ScoredPair(indicator_id, org_id, score)], merge_threshold=0.92
    )

    # Never a single cluster whose member_ids contain BOTH ids — the never-merge invariant.
    for cluster in clusters:
        assert not ({indicator_id, org_id} <= set(cluster.member_ids)), (
            f"an Indicator ({indicator_id!r}) and an Organization ({org_id!r}) must never "
            f"co-occur in one cluster's member_ids, got {cluster.member_ids!r}"
        )
        assert len(cluster.member_ids) == 1, (
            f"every resulting cluster must be a schema-safe singleton, got {cluster.member_ids!r}"
        )
        assert cluster.is_merge is False

    member_sets = {frozenset(c.member_ids) for c in clusters}
    assert member_sets == {frozenset([indicator_id]), frozenset([org_id])}, (
        f"expected exactly the two singletons, got {member_sets!r}"
    )

    # H-2 (ADR 0041): the schema-incompatible member is surfaced as `merge_incompatible=True`
    # (dead-lettered), NOT silently dropped and NOT silently fused — exactly one of the two
    # singletons must carry the flag.
    flags = {c.merge_incompatible for c in clusters}
    assert flags == {True, False}, (
        "exactly one of the two singletons must be flagged merge_incompatible (H-2) — the "
        "schema-incompatible pairing must be surfaced, not silently dropped or merged: "
        f"{clusters!r}"
    )


# ---------------------------------------------------------------------------------------------
# P-IND-3 — injection idempotency: a second `register_wm_schemata()` call is a true no-op.
# ---------------------------------------------------------------------------------------------


@given(value=_IOC_VALUE, malware=_MALWARE)
@_SETTINGS
def test_p_ind_3_register_twice_is_idempotent_and_preserves_the_never_merge_guard(
    value: str, malware: str | None
) -> None:
    model = get_model()

    register_wm_schemata()
    assert "Indicator" in model.schemata
    schema_first = model.schemata["Indicator"]
    entity_first = _indicator("ind-idem", value, malware=malware)

    register_wm_schemata()  # second call — must be a no-op: no raise, no churn.

    schema_second = model.schemata["Indicator"]
    # A python dict can't hold a duplicate key by construction; the meaningful pin is that the
    # SECOND call produced the SAME schema (identity or, failing that, equal name+properties) —
    # not a different object with a divergent property set from re-running `model.generate()`.
    assert (schema_second is schema_first) or (
        schema_second.name == schema_first.name
        and set(schema_second.properties) == set(schema_first.properties)
    )
    assert schema_second.matchable is False

    entity_second = _indicator("ind-idem", value, malware=malware)
    assert entity_second.schema.name == "Indicator"
    assert entity_second.to_dict() == entity_first.to_dict()

    # The sharpest regression pin: double-injection must not weaken the never-merge guarantee
    # (P-IND-2a) by corrupting the schema's ancestor chain.
    with pytest.raises(InvalidData):
        model.common_schema("Indicator", "Organization")


# ---------------------------------------------------------------------------------------------
# P-IND-4 — hostile values never crash construction; FtM's date type drops junk silently.
# ---------------------------------------------------------------------------------------------


@given(huge=_huge_string(), junk_date=_junk_date())
@_SETTINGS
def test_p_ind_4_hostile_values_never_crash_construction(huge: str, junk_date: str) -> None:
    entity = _indicator("ind-hostile", huge)
    assert entity.get("indicatorValue") == [huge]
    assert len(entity.get("indicatorValue")[0]) == 10_000

    entity.add("firstSeenAt", junk_date)
    entity.add("firstSeenAt", huge)  # a 10k-char string is equally never a valid date
    assert entity.get("firstSeenAt") == [], (
        f"junk date {junk_date!r} (and the huge string) must be dropped, not stored, "
        f"got {entity.get('firstSeenAt')!r}"
    )

    round_tripped = make_entity(entity.to_dict())
    assert round_tripped.schema.name == "Indicator"
    assert round_tripped.get("indicatorValue") == [huge]
    assert round_tripped.get("firstSeenAt") == []
