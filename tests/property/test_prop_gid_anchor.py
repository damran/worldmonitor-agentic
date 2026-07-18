"""Property: the `gid` canonical-anchor namespace (Gate S-3, ADR 0117).

MITRE ATT&CK's G-id (``G0032``) becomes a first-class canonical-id namespace so the same
intrusion set ingested twice converges onto ONE node, and two DIFFERENT intrusion sets can
never fuse (CLAUDE.md: *resolve to canonical IDs* + the catastrophic-merge guard). This pins
the four properties from ``docs/reviews/GATE_S3_ATTACK_CATALOG_SPEC.md``:

* **P-GID-1 (never-merge)** — two entities carrying DISTINCT valid G-ids are flagged as an
  anchor conflict by :func:`anchor_conflicts_across`, and their derived durable ids are DISTINCT.
* **P-GID-2 (convergence/injectivity)** — the SAME G-id on two records (different sources,
  different names) derives the IDENTICAL ``wm-anchor-gid-<gid>`` durable id.
* **P-GID-3 (precedence non-interference)** — (a) an entity carrying BOTH a QID and a G-id
  anchors by QID (tier order; RED pre-change — the gid tier does not exist yet), and (b) entities
  carrying ONLY the four pre-existing anchor tiers (QID/LEI/regNo/taxNo) derive ids byte-identical
  to the CURRENT implementation (the no-re-anchoring regression pin — this half is GREEN both
  before and after the change, by construction: it never touches ``mitre_gid`` at all).
* **P-GID-4 (validity gate)** — a malformed G-id (``G12``, ``g0001``, ``G00321``, ``""``,
  ``G-0001``, junk) never derives a gid durable id.

RED TODAY: ``mitre_gid`` is not yet in ``ontology.anchors.CANONICAL_ID_FIELDS``, so every
``set_anchor(entity, "mitre_gid", ...)`` call below raises ``ValueError`` — the acceptable RED
shape (the spec: "that raise IS an acceptable RED shape"). P-GID-3b (old-tier-only) never calls
``set_anchor`` with ``"mitre_gid"`` and must stay GREEN throughout — it is the regression pin
that inserting the ``gid`` tier does not disturb QID > LEI > regNo > taxNo for entities that
never carry a G-id.
"""

from __future__ import annotations

import re
import string

from followthemoney import registry
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.anchors import anchor_conflicts_across, set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.canonical import _anchor_id, pick_anchor

_SETTINGS = settings(max_examples=75, deadline=None, suppress_health_check=[HealthCheck.too_slow])

_VALID_GID_RE = re.compile(r"^G\d{4}$")
_FIXED_INVALID_GIDS = ("G12", "g0001", "G00321", "", "G-0001")


def _valid_gid() -> st.SearchStrategy[str]:
    """A well-formed G-id per the spec's validity regex ``^G\\d{4}$``."""
    return st.builds(lambda n: f"G{n:04d}", st.integers(min_value=0, max_value=9999))


def _invalid_gid() -> st.SearchStrategy[str]:
    """A malformed G-id: the spec's explicit examples plus fuzzed junk that never matches."""
    junk = st.text(alphabet=string.printable, min_size=0, max_size=12).filter(
        lambda s: not _VALID_GID_RE.fullmatch(s)
    )
    return st.one_of(st.sampled_from(_FIXED_INVALID_GIDS), junk)


def _org(entity_id: str, *, name: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Organization",
            "properties": {"name": [name]},
            "datasets": ["t"],
        }
    )


def _gid_entity(entity_id: str, gid: str, *, name: str = "Threat Group") -> FtmEntity:
    """An Organization carrying ONE ``mitre_gid`` anchor (and nothing else canonical)."""
    entity = _org(entity_id, name=name)
    set_anchor(entity, "mitre_gid", gid)  # RED pre-change: ValueError (field not yet registered)
    return entity


# --- old-tier-only builders (P-GID-3b) — NEVER touch mitre_gid, must stay GREEN throughout ------

_NAME = "Acme Corporation Ltd"


def _qid_entity(entity_id: str, qid: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [_NAME], "wikidataId": [qid]},
        }
    )


def _lei_entity(entity_id: str, lei: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [_NAME], "leiCode": [lei]},
        }
    )


def _regno_entity(entity_id: str, regno: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [_NAME], "registrationNumber": [regno]},
        }
    )


def _taxno_entity(entity_id: str, taxno: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [_NAME], "taxNumber": [taxno]},
        }
    )


def _all_four_tiers_entity(entity_id: str, qid: str, lei: str, regno: str, taxno: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {
                "name": [_NAME],
                "wikidataId": [qid],
                "leiCode": [lei],
                "registrationNumber": [regno],
                "taxNumber": [taxno],
            },
        }
    )


_QID_DIGITS = st.text(alphabet="0123456789", min_size=2, max_size=6)
_LEI = st.text(alphabet=string.ascii_uppercase + string.digits, min_size=20, max_size=20)
_REGNO_RAW = st.text(alphabet=string.ascii_uppercase + string.digits, min_size=1, max_size=10)


# ---------------------------------------------------------------------------------------------
# P-GID-1 — never-merge: distinct valid G-ids => flagged conflict + distinct durable ids.
# ---------------------------------------------------------------------------------------------


@given(
    gid_a=_valid_gid(),
    gid_b=_valid_gid(),
    name_a=st.sampled_from(["Silent Falcon", "Nightshade Panda", "Acme Corporation Ltd"]),
    name_b=st.sampled_from(["Silent Falcon", "Nightshade Panda", "Acme Corporation Ltd"]),
)
@_SETTINGS
def test_p_gid_1_distinct_gids_flag_conflict_and_derive_distinct_ids(
    gid_a: str, gid_b: str, name_a: str, name_b: str
) -> None:
    """Two entities with DISTINCT valid G-ids (names may freely overlap) never fuse."""
    assume(gid_a != gid_b)
    a = _gid_entity("a", gid_a, name=name_a)
    b = _gid_entity("b", gid_b, name=name_b)

    conflicts = anchor_conflicts_across([a, b])
    assert conflicts.get("mitre_gid") == sorted([gid_a, gid_b]), (
        f"anchor_conflicts_across must flag mitre_gid for distinct G-ids {gid_a!r}/{gid_b!r}, "
        f"got {conflicts!r}"
    )

    id_a = pick_anchor([a])
    id_b = pick_anchor([b])
    assert id_a is not None and id_b is not None
    assert id_a != id_b, (
        f"distinct G-ids {gid_a!r}/{gid_b!r} must derive DISTINCT durable ids, both got {id_a!r}"
    )


# ---------------------------------------------------------------------------------------------
# P-GID-2 — convergence/injectivity: the SAME G-id always derives the SAME durable id.
# ---------------------------------------------------------------------------------------------


@given(
    gid=_valid_gid(),
    name_a=st.sampled_from(["Silent Falcon", "SF Group"]),
    name_b=st.sampled_from(["Nightshade Panda", "Panda Ops"]),
)
@_SETTINGS
def test_p_gid_2_same_gid_converges_on_identical_durable_id(
    gid: str, name_a: str, name_b: str
) -> None:
    """The SAME G-id on two records (different names/ids) derives ONE `wm-anchor-gid-<gid>` id."""
    a = _gid_entity("a", gid, name=name_a)
    b = _gid_entity("b", gid, name=name_b)

    expected = f"wm-anchor-gid-{gid}"
    assert _anchor_id("gid", gid) == expected  # D1 item 3: exact serialization

    id_a = pick_anchor([a])
    id_b = pick_anchor([b])
    assert id_a == expected
    assert id_b == expected
    assert id_a == id_b
    # The union (a cluster's members) agrees too — convergence, not just per-singleton equality.
    assert pick_anchor([a, b]) == expected


# ---------------------------------------------------------------------------------------------
# P-GID-3a — precedence non-interference: QID beats G-id when both are present (tier order).
# ---------------------------------------------------------------------------------------------


@given(qid_digits=_QID_DIGITS, gid=_valid_gid())
@_SETTINGS
def test_p_gid_3a_qid_and_gid_together_anchors_by_qid(qid_digits: str, gid: str) -> None:
    """An entity carrying BOTH a QID and a G-id anchors by QID (QID outranks gid)."""
    qid = f"Q{qid_digits}"
    entity = _org("x", name="Dual Anchor Org")
    entity.add("wikidataId", qid)
    set_anchor(entity, "mitre_gid", gid)  # RED pre-change: ValueError

    assert pick_anchor([entity]) == _anchor_id("qid", qid), (
        "QID must win precedence over a co-present G-id (gid tier sits AFTER lei/qid)"
    )


# ---------------------------------------------------------------------------------------------
# P-GID-3b — regression pin: entities carrying ONLY pre-existing tiers are unaffected.
#
# NEVER calls set_anchor(..., "mitre_gid", ...) — this half must PASS both BEFORE and AFTER the
# gid tier is inserted (it is the "inserting a new tier re-anchors nothing" guarantee, ADR 0117).
# ---------------------------------------------------------------------------------------------


@given(
    qid_digits=_QID_DIGITS,
    lei=_LEI,
    regno=_REGNO_RAW,
    taxno=_REGNO_RAW,
)
@_SETTINGS
def test_p_gid_3b_old_tier_only_entities_unchanged_by_gid_tier_insertion(
    qid_digits: str, lei: str, regno: str, taxno: str
) -> None:
    """QID/LEI/regNo/taxNo-only entities derive EXACTLY what the current precedence derives.

    GREEN pre-change (this IS today's behavior) and must stay GREEN post-change: inserting the
    gid tier between lei and regno must not perturb any of the four pre-existing tiers.
    """
    qid = f"Q{qid_digits}"
    assume(registry.identifier.clean(regno))
    assume(registry.identifier.clean(taxno))

    assert pick_anchor([_qid_entity("a", qid)]) == _anchor_id("qid", qid)
    assert pick_anchor([_lei_entity("a", lei)]) == _anchor_id("lei", lei)

    cleaned_regno = registry.identifier.clean(regno)
    assert cleaned_regno is not None
    assert pick_anchor([_regno_entity("a", regno)]) == _anchor_id("regno", cleaned_regno)

    cleaned_taxno = registry.identifier.clean(taxno)
    assert cleaned_taxno is not None
    assert pick_anchor([_taxno_entity("a", taxno)]) == _anchor_id("taxno", cleaned_taxno)

    # All four tiers present at once -> the TOP tier (QID) still wins, unperturbed by the new
    # tier sitting between lei and regno in _PRECEDENCE.
    combo = _all_four_tiers_entity("a", qid, lei, regno, taxno)
    assert pick_anchor([combo]) == _anchor_id("qid", qid)


# ---------------------------------------------------------------------------------------------
# P-GID-4 — validity gate: a malformed G-id never derives a gid durable id.
# ---------------------------------------------------------------------------------------------


@given(bad_gid=_invalid_gid())
@_SETTINGS
def test_p_gid_4_invalid_gid_never_derives_a_durable_id(bad_gid: str) -> None:
    """A malformed G-id (wrong shape/case/length, or empty) anchors nothing."""
    entity = _org("x", name="Malformed Gid Org")
    set_anchor(entity, "mitre_gid", bad_gid)  # RED pre-change: ValueError

    assert pick_anchor([entity]) is None, (
        f"malformed G-id {bad_gid!r} must never derive a durable id, got {pick_anchor([entity])!r}"
    )


@given(bad_gid=_invalid_gid(), regno=_REGNO_RAW)
@_SETTINGS
def test_p_gid_4b_invalid_gid_falls_through_to_a_valid_lower_tier(bad_gid: str, regno: str) -> None:
    """A malformed G-id does not block/interfere with a lower, VALID tier (regNo)."""
    assume(registry.identifier.clean(regno))
    entity = _org("x", name="Malformed Gid Plus Regno Org")
    entity.add("registrationNumber", regno)
    set_anchor(entity, "mitre_gid", bad_gid)  # RED pre-change: ValueError

    cleaned = registry.identifier.clean(regno)
    assert cleaned is not None
    result = pick_anchor([entity])
    assert result == _anchor_id("regno", cleaned), (
        f"an invalid gid {bad_gid!r} must fall through to the agreeing regNo tier, got {result!r}"
    )
