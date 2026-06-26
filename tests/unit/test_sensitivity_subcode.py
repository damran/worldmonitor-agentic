"""Gate PEP-subcode — RISKS-parented sub-code sensitivity (failing-test-first oracle).

Spec: ``docs/reviews/GATE_PEP_SUBCODE_SPEC.md`` (§5 test plan) + the amendment in
``docs/decisions/0047-fail-closed-sensitivity-guard.md`` ("Post-merge fix (PEP/sub-code coverage)").
Scope: ``.claude/gate.scope`` (allow-list + FROZEN list + DENY codes).

THE BUG (confirmed fail-open, cross-line audit vs Workflow A): ``guard/sensitivity.py``'s
``is_sensitive`` decides topic sensitivity by EXACT set membership ``topic_codes &
registry.topic.RISKS`` (+ the unknown-hinge). FtM 4.9.2 ``registry.topic.RISKS`` (28 codes) holds
the PARENT risk codes but NOT their sub-classifications — which ARE in ``registry.topic.names`` (so
the unknown-hinge misses them too). 7 RISKS-parented sub-codes (``role.pep.natl/intl/frmr``,
``crime.cyber``, ``crime.env``, ``crime.traffick.drug/human``) are therefore ``is_sensitive ==
False`` → a cluster whose only risk signal is one of them AUTO-MERGES UNFLAGGED, violating CLAUDE.md
"never auto-merge a sensitive entity".

THE FIX this suite is the oracle for (one OR-clause in ``is_sensitive``): a topic ``code`` is
sensitive iff (a) ``code ∈ RISKS`` (unchanged), OR (b) a DOT-ANCESTOR of ``code`` ∈ ``RISKS``
(``any(code == r or code.startswith(r + ".") for r in RISKS)`` — a sub-classification inherits its
parent's risk), OR (c) ``code ∉ registry.topic.names`` (unknown-hinge, unchanged).

This file is written FROM THE SPEC, independent of the implementation; it pins OUTCOMES at the
guard's public boolean entry points (never "no exception"). RED on the current tree (clause (b) does
not exist yet → the 7 sub-codes are ``is_sensitive == False``); GREEN post-fix. The 7 sub-codes are
enumerated programmatically from the installed ``registry.topic`` so the suite auto-tracks the FtM
pin; the literal list is repeated only as the snapshot the guardrail (T-PEP2) re-confirms.
"""

from __future__ import annotations

import pytest
from followthemoney.types import registry

from worldmonitor.guard.sensitivity import (
    is_newly_broadened_sensitive,
    is_sensitive,
    needs_review,
)
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import ResolvedCluster

# --------------------------------------------------------------------------------------------
# The 7 RISKS-parented sub-codes (spec §2, ADR 0047 Post-merge fix). Each is ∈ names, ∉ RISKS, and
# has a RISKS dot-ancestor. Kept as a literal SNAPSHOT — T-PEP2 re-derives the set live from the
# installed registry and asserts it equals exactly this, so a FtM bump that shifts the set fails
# loudly (re-verify) rather than silently widening/narrowing the oracle.
ROLE_PEP_SUBCODES: list[str] = ["role.pep.natl", "role.pep.intl", "role.pep.frmr"]
CRIME_SUBCODES: list[str] = [
    "crime.cyber",
    "crime.env",
    "crime.traffick.drug",
    "crime.traffick.human",
]
SUBCODES_7: list[str] = ROLE_PEP_SUBCODES + CRIME_SUBCODES


def _has_risks_dot_ancestor(code: str) -> bool:
    """True iff a STRICT dot-ancestor of ``code`` is in ``registry.topic.RISKS``.

    Mirrors clause (b) of the fix restricted to a genuine ancestor (``code`` itself excluded): the
    trailing dot makes it a true ancestor test, never a bare string-prefix (``crime`` matches
    ``crime.cyber`` but never a hypothetical ``crimes``).
    """
    return any(code != r and code.startswith(r + ".") for r in registry.topic.RISKS)


def _person(entity_id: str = "p", topics: list[str] | None = None) -> FtmEntity:
    """A Person fixture carrying ``topics`` (and no canonical anchors → no anchor-conflict flag)."""
    props: dict[str, list[str]] = {"name": ["Pat Example"]}
    if topics:
        props["topics"] = topics
    return make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": ["t"]}
    )


# --------------------------------------------------------------------------------------------
# T-PEP1 — every missed RISKS-parented sub-code is sensitive (the failing-first oracle).
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", SUBCODES_7, ids=SUBCODES_7)
def test_t_pep1_risks_parented_subcode_is_sensitive(code: str) -> None:
    """T-PEP1: a Person whose ONLY topic is a RISKS-parented sub-code is ``is_sensitive == True``.

    PRE-FIX: ``code`` is ∉ ``RISKS`` and ∈ ``names``, so neither the exact-membership clause nor the
    unknown-hinge fires → ``is_sensitive`` returns ``False`` → the cluster would AUTO-MERGE
    UNFLAGGED. **FAILS** for all 7. POST-FIX (dot-ancestor clause): the sub-code inherits its
    parent's risk → ``True``.
    """
    # Non-vacuity: the entity actually carries the sub-code, and the sub-code genuinely has a RISKS
    # ancestor (so this is the real fail-open, not a dropped/cleaned topic value).
    entity = _person(topics=[code])
    assert list(entity.get("topics", quiet=True)) == [code], "topic must survive FtM cleaning"
    assert code in registry.topic.names and code not in registry.topic.RISKS
    assert _has_risks_dot_ancestor(code), f"{code} must have a RISKS dot-ancestor"

    assert is_sensitive(entity) is True, (
        f"{code} is a RISKS-parented sub-code — deny-by-default must flag it (never auto-merge)"
    )


# --------------------------------------------------------------------------------------------
# T-PEP2 — snapshot guardrail (non-vacuous, FtM-pin-tracking) + is_sensitive flags exactly the 7.
# --------------------------------------------------------------------------------------------


def test_t_pep2_dot_ancestor_set_is_exactly_the_seven() -> None:
    """T-PEP2: the live registry-derived dot-ancestor set is EXACTLY the 7 snapshot sub-codes.

    Computed from the installed FtM: ``{c ∈ names : c ∉ RISKS ∧ a dot-ancestor of c ∈ RISKS}``.
    Asserts (a) each of the 7 is ∈ ``names``, ∉ ``RISKS``, and has a RISKS dot-ancestor; and (b)
    that derived set equals exactly the 7 literal sub-codes. This proves §2's zero-over-flag claim
    (no other KNOWN code has a RISKS ancestor) and fails loudly if a FtM bump adds/removes a
    RISKS-parented sub-code — forcing a re-verify rather than a silent oracle drift. This part reads
    only the registry, so it is GREEN now and stays a guardrail post-fix.
    """
    for code in SUBCODES_7:
        assert code in registry.topic.names, f"{code} must be a known FtM topic"
        assert code not in registry.topic.RISKS, f"{code} must NOT be a parent RISKS code"
        assert _has_risks_dot_ancestor(code), f"{code} must have a RISKS dot-ancestor"

    derived = {
        code
        for code in registry.topic.names
        if code not in registry.topic.RISKS and _has_risks_dot_ancestor(code)
    }
    assert derived == set(SUBCODES_7), (
        "the set of KNOWN non-RISKS codes with a RISKS dot-ancestor must be EXACTLY the 7 snapshot "
        f"sub-codes — FtM pin drift detected: {sorted(derived ^ set(SUBCODES_7))}"
    )
    assert len(derived) == 7


@pytest.mark.parametrize("code", SUBCODES_7, ids=SUBCODES_7)
def test_t_pep2_is_sensitive_flags_each_of_the_seven(code: str) -> None:
    """T-PEP2 (oracle half): ``is_sensitive`` returns ``True`` for each of the registry-derived 7.

    Pairs with the registry-derived guardrail above: the guard must flag EXACTLY the set that the
    dot-ancestor rule identifies. RED pre-fix (``False``); GREEN post-fix.
    """
    assert is_sensitive(_person(topics=[code])) is True


# --------------------------------------------------------------------------------------------
# T-PEP3 — no over-flag: a KNOWN code with NO RISKS dot-ancestor stays benign.
# --------------------------------------------------------------------------------------------


def test_t_pep3_known_non_risk_descendant_is_not_sensitive() -> None:
    """T-PEP3: ``corp.public`` — a KNOWN topic (∈ ``names``) with NO RISKS dot-ancestor — is
    ``is_sensitive == False``. Pins that the dot-ancestor rule does not degenerate into flagging
    every known sub-code (D-OVERFLAG). Non-vacuous: the probe is asserted ∈ ``names``, ∉ ``RISKS``,
    and ancestor-free, so it cannot silently become vacuous if FtM re-classifies it.
    """
    probe = "corp.public"
    assert probe in registry.topic.names, "probe must be a KNOWN topic (else T-PEP3 is vacuous)"
    assert probe not in registry.topic.RISKS
    assert not _has_risks_dot_ancestor(probe), "probe must have NO RISKS ancestor (non-vacuous)"

    assert is_sensitive(_person(topics=[probe])) is False, (
        "a known non-RISKS-descendant topic must NOT be flagged (no over-flag)"
    )
    # A plain entity carrying no topic at all is never sensitive (empty-topics early return).
    assert is_sensitive(_person()) is False, "an entity with no topics is not sensitive"


# --------------------------------------------------------------------------------------------
# T-PEP4 — exemption interaction (pure unit pins; spec §4 / ADR 0047 Decision 5). PIN, do NOT
# change the fence. is_newly_broadened_sensitive = is_sensitive AND NOT _legacy_is_sensitive.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ROLE_PEP_SUBCODES, ids=ROLE_PEP_SUBCODES)
def test_t_pep4_role_pep_subcode_is_sensitive_but_exemptible(code: str) -> None:
    """T-PEP4 (legacy-caught): a ``role.pep.*`` sub-code is ``is_sensitive == True`` (clause b) AND
    ``is_newly_broadened_sensitive == False``.

    The legacy guard's ``role.pep`` prefix already saw these, so a prior approval could have
    considered them → they STAY EXEMPTIBLE (not a stale exemption). RED pre-fix on the
    ``is_sensitive is True`` assertion (currently ``False``); GREEN post-fix.
    """
    entity = _person(topics=[code])
    assert is_sensitive(entity) is True, f"{code} must be sensitive (dot-ancestor clause)"
    assert is_newly_broadened_sensitive(entity) is False, (
        f"{code} was legacy-caught by the role.pep* prefix → must stay EXEMPTIBLE"
    )


@pytest.mark.parametrize("code", CRIME_SUBCODES, ids=CRIME_SUBCODES)
def test_t_pep4_crime_subcode_is_sensitive_and_non_exemptible(code: str) -> None:
    """T-PEP4 (newly-broadened): a ``crime.*`` sub-code is ``is_sensitive == True`` (clause b) AND
    ``is_newly_broadened_sensitive == True``.

    The legacy guard had NO ``crime*`` prefix → it never caught these → a stale approval could not
    have considered them → NON-exemptible (re-parks past a stale approval). RED pre-fix on both
    assertions (currently both ``False``); GREEN post-fix.
    """
    entity = _person(topics=[code])
    assert is_sensitive(entity) is True, f"{code} must be sensitive (dot-ancestor clause)"
    assert is_newly_broadened_sensitive(entity) is True, (
        f"{code} was NOT legacy-caught (no crime* prefix) → must be NON-exemptible"
    )


# --------------------------------------------------------------------------------------------
# T-PEP5 — through needs_review: a 2-member merge with a role.pep.natl member parks.
# --------------------------------------------------------------------------------------------


def test_t_pep5_subcode_member_parks_the_merge() -> None:
    """T-PEP5: a 2-member (``is_merge``) cluster whose one member carries ``role.pep.natl`` routes
    to review — ``needs_review(...)[0] is True`` with a "sensitive" reason.

    Built as a directly-constructed ``ResolvedCluster`` of two members (``is_merge`` True since
    ``len(member_ids) > 1``), score ``1.0`` so the default-OFF Chow band cannot park it, and the
    members carry no canonical anchors so the anchor-conflict park cannot fire — the ONLY axis that
    can flag here is Stage-1 topic sensitivity. PRE-FIX: ``is_sensitive('role.pep.natl')`` is
    ``False`` → Stage 1 does not fire → no other axis fires → ``needs_review`` returns
    ``(False, "")`` → the cluster AUTO-MERGES UNFLAGGED. **FAILS.** POST-FIX: Stage 1 flags it.
    """
    pep = _person("m1", topics=["role.pep.natl"])
    plain = _person("m2")
    cluster = ResolvedCluster(
        canonical_id="wmc-pep-subcode-test",
        member_ids=("m1", "m2"),
        entity=pep,
        score=1.0,
    )
    by_id: dict[str, FtmEntity] = {"m1": pep, "m2": plain}
    assert cluster.is_merge is True, "the cluster must be a merge (else the guard skips it)"

    flagged, reason = needs_review(cluster, by_id)
    assert flagged is True, "a role.pep.natl member must park the merge, never auto-merge"
    assert "sensitive" in reason.lower(), "the park reason must identify the sensitive member"
