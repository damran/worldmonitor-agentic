"""Gate E (slice-1) — fail-closed sensitivity guard: topics-first deny-by-default.

Spec: ``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md`` (slice-1 cases T1/T2/T3/T6 + APPROVE/DENY).
ADR: ``docs/decisions/0047-fail-closed-sensitivity-guard.md`` (esp. Decision 1/2 — programmatic
``registry.topic.RISKS`` + unknown ⇒ sensitive). Closes audit gap **G6**.

This file is the gate's acceptance ORACLE — written FROM THE SPEC, independent of the
implementation. It pins OUTCOMES at the guard's public entry points (``is_sensitive`` /
``needs_review`` in ``resolution/review.py``): a member carrying ANY of FtM 4.9.2's 28
``registry.topic.RISKS`` codes — or an off-ontology code unknown to ``registry.topic.names`` —
routes a MERGED cluster to review (``needs_review(...)[0] is True``), while a plain non-sensitive
cluster still auto-merges (``False``). It never asserts "no exception"; every case pins the flag
and (where load-bearing) the reason.

Why it is RED on the current tree (``review.py:22`` ``SENSITIVE_TOPICS``, 7 hardcoded codes + a
``role.pep*``/``sanction*`` prefix rule): that legacy guard catches only **10** of the 28 risk
codes and **MISSES 18** (verified — ``corp.disqual``, ``crime.boss``, ``crime.fin``,
``crime.theft``, ``crime.traffick``, ``crime.war``, ``debarment``, ``export.control``,
``export.control.linked``, ``export.risk``, ``invest.ban``, ``invest.risk``, ``mare.detained``,
``mare.shadow``, ``reg.action``, ``reg.warn``, ``role.oligarch``, ``role.rca``). A cluster whose
only risk signal is one of those 18 is non-sensitive to the guard → **auto-merges with no human
review** — the live catastrophic-merge fail-open this gate closes.

The 28 risk codes are NOT hardcoded here: they are enumerated from ``registry.topic.RISKS`` so the
suite auto-tracks the FtM pin (the literal list lives only in ``VERIFIED_API.md`` / the spec as the
verification record). ``crime.war`` and ``role.rca`` are the spec's named exemplars of the 18
misses.
"""

from __future__ import annotations

import pytest
from followthemoney.types import registry

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import ResolvedCluster, cluster_and_merge
from worldmonitor.resolution.review import is_sensitive, needs_review
from worldmonitor.resolution.splink_model import score_pairs

# FtM 4.9.2 (VERIFIED_API.md "Gate E" section): registry.topic.RISKS is FtM's own counterparty-risk
# tag — a set[str] of exactly 28 codes. Enumerated (not hardcoded) so a FtM pin bump auto-tracks.
RISKS: list[str] = sorted(registry.topic.RISKS)

# The legacy denylist + the role.pep*/sanction* prefix rule (review.py:22-33). Derived here ONLY to
# split RISKS into "caught" vs "missed" for documentation/sub-parametrisation — the guard itself
# must NOT keep a denylist as its source of truth (deny-by-default; spec DENY E-DENYLIST).
_LEGACY_DENYLIST = frozenset(
    {"sanction", "sanction.linked", "poi", "crime", "crime.fraud", "crime.terror", "wanted"}
)


def _legacy_catches(code: str) -> bool:
    return code in _LEGACY_DENYLIST or code.startswith("role.pep") or code.startswith("sanction")


# The 18 codes the legacy guard MISSES (the headline G6 holes) — fail pre-fix; the 10 it catches
# already pass (no regression). Computed from RISKS so it stays in lockstep with the FtM pin.
LEGACY_MISSED: list[str] = sorted(code for code in RISKS if not _legacy_catches(code))


def test_verify_before_code_risks_snapshot_matches_spec() -> None:
    """Guardrail on the ORACLE itself: the FtM pin this suite runs against is the gate's snapshot.

    Pins the verify-before-code record (VERIFIED_API.md "Gate E" §2): exactly 28 RISKS codes, the
    legacy guard catches 10 and misses 18, and ``crime.war``/``role.rca`` are among the misses. If
    a FtM bump changes the set this test fails loudly so the snapshot is re-verified — it is NOT a
    tautology against the implementation (it reads only the installed FtM registry).
    """
    assert type(registry.topic.RISKS).__name__ == "set"
    assert len(RISKS) == 28
    assert set(RISKS) <= set(registry.topic.names)  # RISKS is a subset of the full vocabulary
    assert len(LEGACY_MISSED) == 18, "the 18 missed risk codes are the headline G6 holes"
    assert "crime.war" in LEGACY_MISSED and "role.rca" in LEGACY_MISSED


# --------------------------------------------------------------------------------------------
# Fixtures / helpers — a real 2-member cluster assembled via the production score/cluster path,
# so the cluster is .is_merge (the guard skips singletons) and the topic is read off a real member.
# --------------------------------------------------------------------------------------------


def _person(entity_id: str, *, topics: list[str] | None = None) -> FtmEntity:
    """A Person fixture; identical name+nationality+dob so two of them cluster as a merge."""
    props: dict[str, list[str]] = {
        "name": ["Vladimir Example"],
        "nationality": ["ru"],
        "birthDate": ["1960-01-01"],
    }
    if topics:
        props["topics"] = topics
    return make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": ["t"]}
    )


def _company(entity_id: str, *, topics: list[str] | None = None) -> FtmEntity:
    props: dict[str, list[str]] = {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]}
    if topics:
        props["topics"] = topics
    return make_entity(
        {"id": entity_id, "schema": "Company", "properties": props, "datasets": ["t"]}
    )


def _merge_of(a: FtmEntity, b: FtmEntity) -> ResolvedCluster:
    """Cluster two duplicates through the real score/cluster path and return the merge.

    Asserts the merge actually formed (otherwise the guard would skip a singleton and the test
    would be vacuous — RED for the wrong reason).
    """
    clusters = cluster_and_merge([a, b], score_pairs([a, b]))
    merges = [c for c in clusters if c.is_merge]
    assert len(merges) == 1, "the two identical records must cluster into one merge"
    assert set(merges[0].member_ids) == {a.id, b.id}
    return merges[0]


# --------------------------------------------------------------------------------------------
# T1 — denylist-MISSED risk topic parks (the failing-test-first oracle).
# --------------------------------------------------------------------------------------------


def test_t1_denylist_missed_risk_topic_parks() -> None:
    """T1: a 2-member merge whose only risk signal is ``crime.war`` (a real ``registry.topic.RISKS``
    code OMITTED from the legacy 7-element denylist) is flagged by ``needs_review``.

    PRE-FIX: ``crime.war`` is not in ``SENSITIVE_TOPICS`` and does not match the
    ``role.pep*``/``sanction*`` prefix rule, so ``is_sensitive`` returns ``False`` →
    ``needs_review`` returns ``(False, "")`` → the cluster AUTO-MERGES with no review. **FAILS.**
    POST-FIX (programmatic ``registry.topic.RISKS``): flagged, routed to ``pending_review``.
    """
    a = _person("p1", topics=["crime.war"])  # a war criminal — risk topic, one of the 18 misses
    b = _person("p2")
    merge = _merge_of(a, b)

    # The guard's element entry point: the topic-bearing member is sensitive.
    assert is_sensitive(a) is True, "a crime.war member must be sensitive (deny-by-default)"
    # The cluster entry point: a merge touching it parks.
    flagged, reason = needs_review(merge, {a.id: a, b.id: b})
    assert flagged is True, "a crime.war merge must route to review, not auto-merge"
    assert "sensitive" in reason.lower()


# --------------------------------------------------------------------------------------------
# T2 — EVERY one of the 28 registry.topic.RISKS codes parks (parametrised, auto-tracks the pin).
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", RISKS, ids=RISKS)
def test_t2_every_risks_code_parks(code: str) -> None:
    """T2: for EVERY one of the 28 ``registry.topic.RISKS`` codes, a merged cluster with a member
    carrying that topic is flagged — the cluster NEVER auto-merges.

    Parametrised over ``sorted(registry.topic.RISKS)`` so it enumerates all 28 (and auto-tracks a
    FtM bump). The 18 the legacy guard misses FAIL pre-fix; the 10 it already catches stay green —
    proving the inversion has no recall loss while closing the 18 holes.
    """
    a = _person("p1", topics=[code])
    b = _person("p2")
    merge = _merge_of(a, b)

    assert is_sensitive(a) is True, f"{code} is a registry.topic.RISKS code — must be sensitive"
    flagged, reason = needs_review(merge, {a.id: a, b.id: b})
    assert flagged is True, f"a cluster carrying RISKS code {code!r} must park, never auto-merge"
    assert reason, "a parked cluster must carry a human-readable reason"


# --------------------------------------------------------------------------------------------
# T3 — a sanctioned entity is flagged REGARDLESS of graph edges (topic-driven, slice-1).
# --------------------------------------------------------------------------------------------


def test_t3_sanctioned_entity_flagged_without_any_graph_edges() -> None:
    """T3: an entity carrying ``topics:["sanction"]`` and NO graph edges is still flagged.

    Proves the slice-1 guard is purely topic-driven and does NOT depend on graph presence (Stage 2
    k-hop is slice-2). The guard's element entry point ``is_sensitive`` sees only the FtM entity —
    there is no neo4j handle in scope — so a structurally edge-less sanctioned record is sensitive.
    Also asserted at the cluster level via a 2-member merge that touches it.
    """
    sanctioned = _person("s1", topics=["sanction"])
    clean = _person("s2")
    # Element level — no graph anywhere in this call; the decision is from the topic alone.
    assert is_sensitive(sanctioned) is True

    merge = _merge_of(sanctioned, clean)
    flagged, reason = needs_review(merge, {sanctioned.id: sanctioned, clean.id: clean})
    assert flagged is True, "a sanctioned member parks the merge with no graph dependency"
    assert "sensitive" in reason.lower()


def test_t3_fixture_ira_sanction_entity_is_sensitive() -> None:
    """T3 (corroboration): the IRA fixture (``tests/fixtures/opensanctions_entity.json``, an
    Organization carrying ``topics:["sanction"]``, edge-less here) is flagged sensitive.

    Builds the entity inline mirroring the fixture's risk-bearing shape so the test is hermetic and
    cannot break on an unrelated fixture edit, while still exercising the real OpenSanctions topic.
    Caught by BOTH the old denylist AND ``registry.topic.RISKS`` — the no-regression anchor.
    """
    ira = make_entity(
        {
            "id": "NK-CRxrz3RXD3GZS85Edg3r9U",
            "schema": "Organization",
            "properties": {"name": ["Irish Republican Army"], "topics": ["sanction"]},
            "datasets": ["ie_unlawful_organizations"],
        }
    )
    assert is_sensitive(ira) is True, "topics:['sanction'] must be sensitive (old AND new guard)"


# --------------------------------------------------------------------------------------------
# T6 — an off-ontology topic code (NOT in registry.topic.names) ⇒ sensitive (the inversion hinge).
# --------------------------------------------------------------------------------------------


def test_t6_off_ontology_topic_is_sensitive() -> None:
    """T6: a member carrying ``topics:["totally.madeup"]`` — a code FtM has never seen
    (``not in registry.topic.names``) — is flagged sensitive (unknown ⇒ sensitive /
    deny-by-default).

    PRE-FIX: the legacy guard ignores any code outside its 7-element denylist + prefix rule, so an
    off-ontology code is NOT flagged → auto-merge. **FAILS.** POST-FIX: the ``unknown ⇒ sensitive``
    hinge (ADR 0047 Decision 2) parks it. The probe code is asserted off-ontology so the test does
    not silently pass on a future code that FtM adopts.
    """
    off_ontology = "totally.madeup"
    assert off_ontology not in registry.topic.names, "probe must be genuinely off-ontology"

    a = _person("p1", topics=[off_ontology])
    b = _person("p2")
    merge = _merge_of(a, b)

    assert is_sensitive(a) is True, "an off-ontology topic code must be treated as sensitive"
    flagged, reason = needs_review(merge, {a.id: a, b.id: b})
    assert flagged is True, "an unknown topic code must park the merge (unknown ⇒ sensitive)"
    assert reason


# --------------------------------------------------------------------------------------------
# Deny-by-default must NOT degenerate into "park everything": a plain non-sensitive cluster still
# AUTO-MERGES. This fence keeps the inversion from over-parking (spec §10 no-regression / A8).
# --------------------------------------------------------------------------------------------


def test_non_sensitive_cluster_still_auto_merges() -> None:
    """A plain Company merge with NO topics and NO off-ontology code is NOT flagged — it
    auto-promotes exactly as before. Proves deny-by-default holds for review only the unsafe
    (a member tags is_sensitive False) and does not collapse into parking every merge.
    """
    a = _company("c1")
    b = _company("c2")
    assert is_sensitive(a) is False, "a plain Company with no topics is not sensitive"

    merge = _merge_of(a, b)
    flagged, reason = needs_review(merge, {a.id: a, b.id: b})
    assert flagged is False, "a non-sensitive merge must still auto-merge, not over-park"
    assert reason == ""


def test_singleton_is_never_flagged() -> None:
    """A singleton (nothing is being merged) is never flagged even if it is sensitive — the guard
    only gates MERGES (``review.py`` ``if not cluster.is_merge: return False``). Pins the contract
    so the inversion does not start parking lone sensitive records (no merge = no catastrophic
    merge).
    """
    sole = _person("p1", topics=["crime.war"])
    singleton = ResolvedCluster(canonical_id="p1", member_ids=("p1",), entity=sole, score=1.0)
    assert singleton.is_merge is False
    flagged, _ = needs_review(singleton, {"p1": sole})
    assert flagged is False, "a singleton is not a merge — the guard must not park it"
