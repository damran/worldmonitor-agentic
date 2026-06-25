"""Gate E (slice-2) — Stage-3 Chow (1970) abstain band over ``ResolvedCluster.score``.

Spec: ``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md`` §3.4 (the Chow reject-option band), §6
(config: ``sensitivity_abstain_low``/``high`` default 0.92/0.92 = OFF), §8 (the Stage-3 band test).
ADR: ``docs/decisions/0047-fail-closed-sensitivity-guard.md`` Decision 3.3 + Decision 6.

These are PRIMARY invariant tests for the abstain band — the park-vs-auto-merge axis on an
ALREADY-FORMED cluster (NOT the merge-vs-no-merge axis Splink owns). Written FROM the spec,
independent of the implementation; they pin OUTCOMES at ``needs_review`` over a cluster's
already-computed ``ResolvedCluster.score`` (``merge.py:77`` — the weakest-link match probability).

Why the CONFIGURED-band case is RED on the current tree: slice-1's ``needs_review`` has NO Stage-3
band logic — a non-sensitive, non-oversized, non-anchor-conflicting merge returns ``(False, "")``
regardless of its score. With the band configured to ``[0.90, 0.95)`` a cluster scoring 0.92 must
park; pre-fix it auto-merges. **FAILS for the right reason.**

The frozen-half (default band OFF) cases stay GREEN: with the default ``low == high == 0.92`` the
band is empty, so a marginal-score non-sensitive cluster STILL auto-merges (slice-2 ships OFF by
default — it does NOT over-park). And these tests MUST NOT assert anything about
``DEFAULT_MERGE_THRESHOLD`` changing — the band is a distinct axis (spec §3.4, DENY E-THRESHOLD).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.review import needs_review
from worldmonitor.settings import get_settings


def _person(entity_id: str) -> FtmEntity:
    """A topic-CLEAN Person (no risk topic) so Stage 1 never flags — the band is the only axis."""
    return make_entity(
        {
            "id": entity_id,
            "schema": "Person",
            "properties": {
                "name": ["Vladimir Example"],
                "nationality": ["ru"],
                "birthDate": ["1960-01-01"],
            },
            "datasets": ["t"],
        }
    )


def _merge_with_score(score: float) -> tuple[ResolvedCluster, dict[str, FtmEntity]]:
    """A 2-member, non-sensitive, non-oversized, anchor-clean merge with the given ``score``.

    Built directly (the dataclass is frozen) so the cluster's already-computed weakest-link score is
    exactly ``score`` — the value the Chow band reads. ``is_merge`` is True (2 members) so the guard
    does not skip it as a singleton; both members are topic-clean so Stage 1 is silent; ``neo4j`` is
    omitted at the call site so Stage 2 is skipped — the ONLY axis left is Stage 3.
    """
    a = _person("p1")
    b = _person("p2")
    merged = make_entity({**a.to_dict(), "id": "wmc-test"})
    cluster = ResolvedCluster(
        canonical_id="wmc-test",
        member_ids=("p1", "p2"),
        entity=merged,
        score=score,
    )
    assert cluster.is_merge is True, "the cluster must be a real merge (>=2 members)"
    return cluster, {"p1": a, "p2": b}


@pytest.fixture
def abstain_band(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Configure ``sensitivity_abstain_low``/``high`` from ``request.param`` and clear the cache.

    The Chow band bounds come from ``Settings`` (ADR 0047 Decision 6); the guard reads them via
    ``get_settings()``. This fixture sets the two env vars and clears the cached settings so the
    guard sees the configured band, restoring the cache afterwards. ``request.param`` is a
    ``(low, high)`` tuple.
    """
    low, high = request.param
    monkeypatch.setenv("SENSITIVITY_ABSTAIN_LOW", str(low))
    monkeypatch.setenv("SENSITIVITY_ABSTAIN_HIGH", str(high))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def default_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the DEFAULT (band-OFF) posture: no abstain env vars set, settings cache cleared.

    Guards against a stray ``SENSITIVITY_ABSTAIN_*`` in the ambient env leaking into the
    default-OFF assertions. With nothing set, the default ``low == high == 0.92`` (band empty)
    must hold and a marginal-score non-sensitive merge must STILL auto-merge.
    """
    monkeypatch.delenv("SENSITIVITY_ABSTAIN_LOW", raising=False)
    monkeypatch.delenv("SENSITIVITY_ABSTAIN_HIGH", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------------------------
# Configured band [0.90, 0.95): a score INSIDE parks; a score >= high auto-merges. (RED pre-fix.)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("abstain_band", [(0.90, 0.95)], indirect=True)
def test_score_inside_configured_band_parks(abstain_band: None) -> None:
    """A non-sensitive merge whose ``score`` is INSIDE the configured ``[0.90, 0.95)`` abstain band
    is flagged with a DISTINCT abstain reason.

    The cluster is non-sensitive (topic-clean members), not oversized, and anchor-clean — so neither
    Stage 1 nor the size/anchor flags fire — and ``neo4j`` is not passed (Stage 2 skipped). The ONLY
    thing that can flag it is the Chow band. Score ``0.92`` lies in ``[0.90, 0.95)``.

    PRE-FIX: no Stage-3 band exists → ``needs_review`` returns ``(False, "")`` → auto-merge.
    **FAILS.** POST-FIX: the band parks it; the reason names the abstain band (a distinct,
    marginal-confidence reason — not a sensitivity / size / anchor reason).
    """
    cluster, by_id = _merge_with_score(0.92)
    flagged, reason = needs_review(cluster, by_id)
    assert flagged is True, (
        "a merge scoring 0.92 inside the configured [0.90, 0.95) abstain band must PARK — the Chow "
        "reject-option band routes a marginal-confidence cluster to review (spec §3.4)"
    )
    assert reason, "a band-parked cluster must carry a human-readable reason"
    lowered = reason.lower()
    assert "abstain" in lowered or "band" in lowered or "confidence" in lowered, (
        "the Stage-3 reason must name the abstain band / marginal confidence — a distinct reason "
        f"from the sensitivity, size, and anchor flags (got: {reason!r})"
    )
    assert "sensitive (PEP/sanctioned)" not in reason and "auto-merge limit" not in reason, (
        "the band flag is NOT a sensitivity or size flag"
    )


@pytest.mark.parametrize("abstain_band", [(0.90, 0.95)], indirect=True)
def test_score_at_band_lower_bound_parks_inclusive(abstain_band: None) -> None:
    """The band's lower bound is INCLUSIVE: a score exactly equal to ``abstain_low`` (0.90) parks.

    Pins the half-open ``[low, high)`` contract on the inclusive side. PRE-FIX: no band →
    auto-merge. **FAILS.** POST-FIX: 0.90 is in ``[0.90, 0.95)`` and parks.
    """
    cluster, by_id = _merge_with_score(0.90)
    flagged, _ = needs_review(cluster, by_id)
    assert flagged is True, "score == abstain_low (0.90) is INSIDE the half-open band [0.90, 0.95)"


@pytest.mark.parametrize("abstain_band", [(0.90, 0.95)], indirect=True)
def test_score_at_band_upper_bound_auto_merges_exclusive(abstain_band: None) -> None:
    """The band's upper bound is EXCLUSIVE: a near-certain score >= ``abstain_high`` (0.95) does NOT
    park — it auto-merges.

    Pins the half-open ``[low, high)`` contract on the exclusive side, and proves the band does not
    degenerate into "park every merge": a cluster at/above the high bound auto-promotes. This is the
    discriminator that keeps the band from over-parking. PASSES pre- and post-fix (pre-fix nothing
    parks; post-fix 0.95 is OUTSIDE the band) — the no-over-park floor under the parking cases.
    """
    cluster, by_id = _merge_with_score(0.95)
    flagged, reason = needs_review(cluster, by_id)
    assert flagged is False, (
        "score == abstain_high (0.95) is OUTSIDE the half-open band [0.90, 0.95) — a near-certain "
        "merge auto-promotes; the band must not over-park (spec §3.4)"
    )
    assert reason == "", "an auto-merged cluster carries no reason"


# --------------------------------------------------------------------------------------------
# Default band OFF (low == high == 0.92): a marginal-score merge STILL auto-merges (frozen GREEN).
# --------------------------------------------------------------------------------------------


def test_default_band_off_marginal_score_still_auto_merges(default_settings: None) -> None:
    """With the DEFAULT band (``low == high == 0.92`` ⇒ empty), a marginal-score non-sensitive merge
    STILL auto-merges — slice-2 ships the band OFF and must NOT over-park by default.

    The default ``[0.92, 0.92)`` is an empty half-open interval, so NO score falls in it — not even
    0.92 itself (``0.92 >= 0.92`` excludes it on the open upper side). This pins the spec's
    ship-OFF-by-default invariant (§6) and the no-regression floor (§10): with no band configured
    and no risk neighbour, no NEW parks. PASSES pre- and post-fix.
    """
    settings = get_settings()
    assert settings.sensitivity_abstain_low == settings.sensitivity_abstain_high == 0.92, (
        "the default abstain band must be OFF (low == high == 0.92 ⇒ empty interval)"
    )
    cluster, by_id = _merge_with_score(0.92)
    flagged, reason = needs_review(cluster, by_id)
    assert flagged is False, (
        "with the default empty band, a marginal-score (0.92) non-sensitive merge must STILL "
        "auto-merge — the band ships OFF and does not over-park (spec §6 / §10)"
    )
    assert reason == "", "an auto-merged cluster carries no reason"


def test_default_band_off_high_score_auto_merges(default_settings: None) -> None:
    """A high-confidence non-sensitive merge auto-merges under the default OFF band — the floor."""
    cluster, by_id = _merge_with_score(0.99)
    flagged, _ = needs_review(cluster, by_id)
    assert flagged is False, "a 0.99 non-sensitive merge auto-merges (band OFF by default)"
