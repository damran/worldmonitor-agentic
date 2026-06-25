"""Gate E (slice-2) — the approved-group-exemption fence's reason-marker coupling (regression-pin).

Spec: ``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md`` §5 (the approved-group exemption / the one
fail-closed hole) + §3.3/§3.4 (the Stage-2 k-hop and Stage-3 Chow flags a prior approval could not
have considered). ADR: ``docs/decisions/0047-fail-closed-sensitivity-guard.md`` Decision 5.

WHY THIS FILE EXISTS (a judge-flagged HIGH fragility). The pipeline's approved-group exemption
un-flags a cluster ⊆ an approved group UNLESS the flag is non-exemptible. For a NEWLY-DETECTED
sensitivity a prior approval could not have considered — a Stage-2 k-hop graph-proximity flag or a
Stage-3 Chow abstain flag — the fence keys on a MARKER STRING baked into the guard's returned
``reason`` (``guard.sensitivity.is_nonexemptible_reason``). That marker-string coupling has no other
test: if a reason-builder in ``needs_review`` and the ``is_nonexemptible_reason`` classifier ever
DRIFT (someone edits one but not the other), the fence silently FAILS OPEN — a stale approval would
un-flag a k-hop/Chow-sensitive cluster → auto-merge. That is a person-relevant fail-open (a
sanctioned/criminal-adjacent or marginal-confidence cluster slipping the human net).

These are REGRESSION-PINS (they PASS on the current wired code), NOT failing-first oracles:

  * ``test_is_nonexemptible_reason_false_for_exemptible_reasons`` — the "no wider" direction: the
    classifier is FALSE for a topic-sensitivity reason, a size reason, an anchor-conflict reason,
    and the empty string. Exemptibility must NOT re-park a flag a prior approval COULD have
    considered (that would break the frozen approve->promote path). The reasons are taken from REAL
    ``needs_review`` emissions, so a future reason-builder edit that accidentally embedded a marker
    is caught here too.
  * ``test_real_chow_reason_is_nonexemptible`` — the "no narrower" direction + the LOAD-BEARING
    coupling-guard: it feeds the ACTUAL ``reason`` string ``needs_review`` EMITS for an in-band
    cluster into ``is_nonexemptible_reason`` and asserts True, so the test breaks the instant the
    Stage-3 reason-builder drifts from the classifier's marker. The k-hop half of the coupling-guard
    (the real emitted k-hop reason) lives in
    ``tests/integration/test_sensitivity_guard_khop.py::test_t5e_real_khop_reason_is_nonexemptible``
    (it needs Neo4j); it is not duplicated here.

No PRIVATE name is imported from ``guard.sensitivity`` — the classifier's TRUE coverage rides the
REAL emitted Chow reason, never a hand-referenced marker constant.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from worldmonitor.guard.sensitivity import is_nonexemptible_reason
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.review import needs_review
from worldmonitor.settings import get_settings


def _person(entity_id: str, *, topics: list[str] | None = None) -> FtmEntity:
    props: dict[str, list[str]] = {"name": ["Vladimir Example"], "nationality": ["ru"]}
    if topics:
        props["topics"] = topics
    return make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": ["t"]}
    )


def _company(entity_id: str, *, anchor: str | None = None) -> FtmEntity:
    entity = make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": ["Acme Trading Limited"], "country": ["gb"]},
            "datasets": ["t"],
        }
    )
    if anchor is not None:
        set_anchor(entity, "wikidata_id", anchor)
    return entity


def _merged_stub(canonical_id: str = "wmc-test") -> FtmEntity:
    return make_entity(
        {
            "id": canonical_id,
            "schema": "Person",
            "properties": {"name": ["Vladimir Example"]},
            "datasets": ["t"],
        }
    )


def _real_topic_reason() -> str:
    """The ACTUAL Stage-1 topic reason ``needs_review`` emits (a ``crime.war`` member)."""
    sensitive = _person("p1", topics=["crime.war"])
    cluster = ResolvedCluster("wmc", ("p1", "p2"), _merged_stub(), score=0.99)
    flagged, reason = needs_review(cluster, {"p1": sensitive, "p2": _person("p2")})
    assert flagged is True and reason, "fixture: a crime.war member must yield a topic reason"
    return reason


def _real_size_reason() -> str:
    """The ACTUAL size-cap reason ``needs_review`` emits for an 11-member (> 10) cluster."""
    member_ids = tuple(f"m{i}" for i in range(11))
    by_id = {mid: _person(mid) for mid in member_ids}
    cluster = ResolvedCluster("wmc", member_ids, _merged_stub(), score=0.99)
    flagged, reason = needs_review(cluster, by_id)
    assert flagged is True and reason, "fixture: an 11-member cluster must yield a size reason"
    return reason


def _real_anchor_conflict_reason() -> str:
    """The ACTUAL anchor-conflict reason ``needs_review`` emits (ADR 0040): a Q1 vs Q2 clash."""
    a = _company("a", anchor="Q1")
    b = _company("b", anchor="Q2")
    cluster = ResolvedCluster("wmc", ("a", "b"), _company("wmc"), score=0.99)
    flagged, reason = needs_review(cluster, {"a": a, "b": b})
    assert flagged is True and reason, "fixture: conflicting anchors must yield an anchor reason"
    return reason


@pytest.fixture
def abstain_band(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure the Chow band from ``request.param`` (a ``(low, high)`` tuple); clear the cache."""
    low, high = request.param
    monkeypatch.setenv("SENSITIVITY_ABSTAIN_LOW", str(low))
    monkeypatch.setenv("SENSITIVITY_ABSTAIN_HIGH", str(high))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------------------------
# "No wider" — every flag a prior approval COULD have considered stays exemptible (no over-park).
# --------------------------------------------------------------------------------------------


def test_is_nonexemptible_reason_false_for_exemptible_reasons() -> None:
    """``is_nonexemptible_reason`` is False for a topic / size / anchor-conflict reason + the empty
    string — the flags a prior approval COULD have considered stay exemptible.

    Each reason is a REAL ``needs_review`` emission (not a hand-typed literal), so a reason-builder
    that accidentally embedded a Stage-2/Stage-3 marker would be caught here. Re-parking a
    knowingly-approved topic merge (legacy-visibility is handled separately by
    ``is_newly_broadened_sensitive`` over the MEMBERS), the size flag (ADR 0020 size half,
    conservative-by-default), or the anchor-conflict flag (ADR 0040) would break the frozen
    approve->promote path — the "no wider" direction of the fence.
    """
    assert is_nonexemptible_reason(_real_topic_reason()) is False, (
        "a topic-sensitivity flag is exemptible — re-parking a knowingly-approved topic "
        "merge would break the frozen approve->promote path"
    )
    assert is_nonexemptible_reason(_real_size_reason()) is False, (
        "the size flag stays exemptible (ADR 0020 size half is conservative-by-default)"
    )
    assert is_nonexemptible_reason(_real_anchor_conflict_reason()) is False, (
        "the anchor-conflict flag (ADR 0040) stays exemptible by an approved group"
    )
    assert is_nonexemptible_reason("") is False, "the empty (not-flagged) reason is exemptible"


# --------------------------------------------------------------------------------------------
# "No narrower" + the load-bearing coupling-guard — the REAL emitted Chow reason is non-exemptible.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("abstain_band", [(0.90, 0.95)], indirect=True)
def test_real_chow_reason_is_nonexemptible(abstain_band: None) -> None:
    """Coupling-guard: the ACTUAL reason ``needs_review`` EMITS for an in-band cluster is classified
    non-exemptible by ``is_nonexemptible_reason``.

    With the band configured to ``[0.90, 0.95)``, a non-sensitive, non-oversized, anchor-clean
    merge scoring ``0.92`` parks via Stage 3; the returned ``reason`` is captured and fed straight
    into the classifier. This breaks the instant the Stage-3 reason-builder and the abstain marker
    the classifier checks DRIFT apart — the exact marker-string coupling the fence depends on.
    ``neo4j`` is omitted so Stage 2 is skipped and the members are topic-clean so Stage 1 is silent,
    so ``flagged`` provably comes from the Chow band (the "no narrower" classifier-TRUE coverage, on
    a REAL emitted reason rather than a hand-referenced marker constant).
    """
    a = _person("p1")
    b = _person("p2")
    cluster = ResolvedCluster("wmc-test", ("p1", "p2"), _merged_stub(), score=0.92)
    flagged, reason = needs_review(cluster, {"p1": a, "p2": b})
    assert flagged is True, "fixture: a 0.92 merge inside [0.90, 0.95) must park via the Chow band"
    assert is_nonexemptible_reason(reason) is True, (
        "the REAL emitted Chow reason must be classified non-exemptible — if this fails "
        "the Stage-3 reason-builder has DRIFTED from is_nonexemptible_reason's marker and "
        "the approved-group exemption fence would silently fail OPEN "
        "(spec §5 / ADR 0047 Dec 5; DENY E-STALE-EXEMPT)"
    )
