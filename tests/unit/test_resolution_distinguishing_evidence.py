"""Gate B-3 — ER distinguishing evidence (registration-number + generic-token guard).

Spec: ``docs/reviews/GATE_B3_SPEC.md`` §4 (INV-1..INV-8) / §5 (the named-test table).
ADR: ``docs/decisions/0039-er-distinguishing-evidence.md`` (extends ADR 0035, does NOT
relitigate it).

These tests are the gate's acceptance oracle, written FROM THE SPEC and independent of any
implementation. They pin OUTCOMES only — merge / no-merge at the 0.92 boundary, the top pair
probability, and the ``_name_fingerprint`` return value — never the m/u calibration or the
column representation, which the builder is free to choose (§3.1.1).

Two defects this gate closes (both silently fuse DISTINCT real legal entities, §1):

- Defect 1 (INV-2): ``_name_fingerprint`` over-strips a single generic descriptor, so
  ``"International Trading Co Ltd"`` and ``"Import Export Trading Co Ltd"`` both collapse to
  the bare token ``"trading"`` and hit the exact-name level.
- Defect 2 (INV-1): the comparison set carries no ``registrationNumber`` / ``taxNumber``
  column, so a present-but-CLASHING government id — the strongest "distinct" signal — is
  ignored.

Sensitivity is OFF (no ``topics`` / ``sanction``) on the INV-1 / INV-2 fixtures: that is
load-bearing. The catastrophic-merge guard fires only on sensitivity or cluster size > 10, so
a same-trade-name pair of two NON-sensitive companies is auto-merged with no review — the exact
hole B-3 exposes. A sensitive fixture would mask the auto-merge behind the guard and make these
tests vacuous.

Convention mirrors ``tests/unit/test_resolution_multiscript.py`` (ADR-0035 regression suite,
frozen): the local ``_org`` / ``_top_probability`` helpers, ``score_pairs`` +
``cluster_and_merge`` for merge assertions, and the 0.92 merge boundary. That suite proves
INV-4..INV-8 and must pass unchanged.
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.splink_model import _name_fingerprint, score_pairs

# ADR-0035 anchor (INV-4): the two stored names of the real us_ofac_sdn "Legion Komplekt"
# pair. Both fingerprint to "komplekt legion" (2 real, non-generic tokens) — untouched by the
# B-3 guard.
LEGION_CYR = "ООО Легион Комплект"
LEGION_LAT = "LIMITED LIABILITY COMPANY LEGION KOMPLEKT"

# Two DISTINCT real orgs whose names both over-strip to the single generic token "trading"
# (Defect 1).
NAME_INTL_TRADING = "International Trading Co Ltd"
NAME_IMPEXP_TRADING = "Import Export Trading Co Ltd"


def _company(
    entity_id: str,
    name: str,
    *,
    registration_number: list[str] | None = None,
    tax_number: list[str] | None = None,
    country: str = "gb",
) -> FtmEntity:
    """A NON-sensitive Company fixture (no ``topics``/``sanction``; sensitivity off is
    load-bearing).

    ``registrationNumber`` / ``taxNumber`` are real FtM ``identifier``-typed properties on
    ``Company``; they are set through the same properties dict the existing suite uses for
    ``name`` / ``country``.
    """
    props: dict[str, list[str]] = {"name": [name], "country": [country]}
    if registration_number is not None:
        props["registrationNumber"] = registration_number
    if tax_number is not None:
        props["taxNumber"] = tax_number
    return make_entity(
        {"id": entity_id, "schema": "Company", "properties": props, "datasets": ["t"]}
    )


def _org(name: str) -> FtmEntity:
    """A minimal named Organization, for the ``_name_fingerprint`` unit tests."""
    return make_entity(
        {
            "id": "x",
            "schema": "Organization",
            "properties": {"name": [name], "country": ["ru"]},
            "datasets": ["t"],
        }
    )


def _top_probability(entities: list[FtmEntity]) -> float:
    """Top pairwise match probability (0.0 if no pair survives blocking/schema)."""
    return max((pair.probability for pair in score_pairs(entities)), default=0.0)


def _merges(entities: list[FtmEntity]) -> list[object]:
    """Real merge clusters (``c.is_merge``) at the 0.92 boundary — ADR-0035 merge convention."""
    return [c for c in cluster_and_merge(entities, score_pairs(entities)) if c.is_merge]


# --------------------------------------------------------------------------------------------
# Defect 2 — registration / tax-number distinguishing evidence (INV-1, INV-1b, INV-3, INV-3b)
# --------------------------------------------------------------------------------------------


def test_clashing_registration_number_blocks_nonsensitive_merge() -> None:
    """INV-1: non-sensitive Companies, same name+country, DIFFERENT reg number → no merge."""
    a = _company("a", "Acme Trading Limited", registration_number=["111111"])
    b = _company("b", "Acme Trading Limited", registration_number=["222222"])
    assert _top_probability([a, b]) < 0.92
    assert _merges([a, b]) == []


def test_clashing_id_crossfield_tax_vs_registration_blocks_merge() -> None:
    """INV-1b (§3.1.1): clash across fields — one side taxNumber, the other registrationNumber."""
    a = _company("a", "Acme Trading Limited", registration_number=["111111"])
    b = _company("b", "Acme Trading Limited", tax_number=["222222"])
    assert _top_probability([a, b]) < 0.92
    assert _merges([a, b]) == []


def test_matching_registration_number_still_merges() -> None:
    """INV-3: same name+country+MATCHING registrationNumber → duplicate still merges (>=0.92)."""
    a = _company("a", "Acme Trading Limited", registration_number=["111111"])
    b = _company("b", "Acme Trading Limited", registration_number=["111111"])
    assert _top_probability([a, b]) >= 0.92
    assert len(_merges([a, b])) == 1


def test_missing_id_one_side_still_merges() -> None:
    """INV-3b: same name+country, id on one side only → null level neutral, still merges."""
    a = _company("a", "Acme Trading Limited", registration_number=["111111"])
    b = _company("b", "Acme Trading Limited")
    assert _top_probability([a, b]) >= 0.92
    assert len(_merges([a, b])) == 1


def test_missing_id_both_sides_still_merges() -> None:
    """INV-3b: same name+country, no id either side → ADR-0035 baseline preserved, still merges."""
    a = _company("a", "Acme Trading Limited")
    b = _company("b", "Acme Trading Limited")
    assert _top_probability([a, b]) >= 0.92
    assert len(_merges([a, b])) == 1


def test_multivalued_id_overlap_is_not_a_clash() -> None:
    """§3.1.1: A ids {X, Y}, B ids {Y} → sets OVERLAP, so NOT a clash → still merges (>=0.92)."""
    a = _company("a", "Acme Trading Limited", registration_number=["111111", "222222"])
    b = _company("b", "Acme Trading Limited", registration_number=["222222"])
    assert _top_probability([a, b]) >= 0.92
    assert len(_merges([a, b])) == 1


# --------------------------------------------------------------------------------------------
# Defect 1 — generic-token fingerprint guard (INV-2, plus the two _name_fingerprint anchors)
# --------------------------------------------------------------------------------------------


def test_generic_single_token_names_do_not_merge_on_name_alone() -> None:
    """INV-2: distinct orgs that both over-strip to "trading", same country, no ids → no merge."""
    a = _company("a", NAME_INTL_TRADING)
    b = _company("b", NAME_IMPEXP_TRADING)
    assert _top_probability([a, b]) < 0.92
    assert _merges([a, b]) == []


def test_name_fingerprint_demotes_single_generic_token() -> None:
    """INV-2 (unit): the over-stripped single generic token is demoted to a richer, DISTINCT key."""
    intl = _name_fingerprint(_org(NAME_INTL_TRADING))
    impexp = _name_fingerprint(_org(NAME_IMPEXP_TRADING))
    # The demotion must not collapse either name to the bare generic token...
    assert intl != "trading"
    assert impexp != "trading"
    # ...and the two distinct orgs must get DISTINCT keys (so they cannot hit the exact level).
    assert intl != impexp
    # The guard returns a richer key, never None (None would kill country/id signals too).
    assert intl is not None
    assert impexp is not None


def test_legion_pair_fingerprint_unaffected_two_real_tokens() -> None:
    """INV-4 anchor: the 2-token, non-generic Legion key is UNCHANGED — the guard touches only
    the pathological single-generic-token case (ADR 0035 must not regress)."""
    assert _name_fingerprint(_org(LEGION_CYR)) == "komplekt legion"
    assert _name_fingerprint(_org(LEGION_LAT)) == "komplekt legion"
