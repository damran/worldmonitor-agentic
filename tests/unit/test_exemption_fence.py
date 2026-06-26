"""Gate E (slice-3) — the STRUCTURED non-exemptibility probe ``has_nonexemptible_sensitivity``.

Spec: ``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md`` §15.1 (the short-circuit MASKING
fail-open), §15.2 (the structured probe that REPLACES the reason-string coupling), §15.4 (both
frozen T4 cases preserved), §15.6 (the STRUCTURED NON-EXEMPTIBILITY / NO-MASKING HARD INV), §16
(``T-MASK-chow`` + the two fence-contract directions). ADR
``docs/decisions/0047-fail-closed-sensitivity-guard.md`` Decision 5 + the slice-3 refinement.
DENY **E-MASK** (a facet of E-STALE-EXEMPT).

WHAT THIS FILE PINS. The pipeline's approved-group exemption (``pipeline.py``) un-flags a cluster
⊆ a human-approved group UNLESS the flag is non-exemptible. slice-2 derived non-exemptibility from
the SINGLE ``reason`` string ``needs_review`` returns — but ``needs_review`` SHORT-CIRCUITS on the
FIRST flag (order: size>10 → Stage-1 topic → anchor-conflict → Stage-2 k-hop → Stage-3 Chow). So
an EXEMPTIBLE flag firing first (size>10, anchor-conflict, or a LEGACY-CAUGHT topic like
``sanction``) MASKED a co-occurring NON-exemptible Stage-2 k-hop / Stage-3 Chow / newly-broadened
signal → a cluster ⊆ a STALE approval was silently un-flagged → auto-promoted despite real risk.
slice-3 replaces that with ``guard.sensitivity.has_nonexemptible_sensitivity(cluster, by_id, *,
neo4j=None)`` — a structured probe that evaluates ALL THREE non-exemptible axes INDEPENDENTLY of
the short-circuit and of the reason string.

These are PRIMARY invariant tests for the probe, written FROM the spec INDEPENDENT of the
implementation; they pin the probe's PUBLIC boolean contract directly (never "no exception"). They
exercise only the newly-broadened-topic + Chow-band axes (``neo4j=None``) — the k-hop axis is the
integration file's job (``tests/integration/test_sensitivity_guard_khop.py::test_t5e`` +
``tests/integration/test_exemption_fence_masking.py``).

WHY RED PRE-FIX: ``has_nonexemptible_sensitivity`` does NOT exist on slice-2 (the fence still
calls the to-be-deleted ``is_nonexemptible_reason``). Importing it raises ``ImportError`` → the
whole module is RED until the builder adds the structured probe. The masking pin
(``test_mask_chow_band_under_size_first_flag``) additionally proves the probe is NOT fooled by
``needs_review``'s first-flag short-circuit (it returns ``True`` while ``needs_review`` reports
only the masking SIZE reason).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from worldmonitor.guard.sensitivity import has_nonexemptible_sensitivity
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.review import needs_review
from worldmonitor.settings import get_settings


def _person(entity_id: str, *, topics: list[str] | None = None) -> FtmEntity:
    """A Person fixture; topic-clean unless ``topics`` is given."""
    props: dict[str, list[str]] = {"name": ["Vladimir Example"], "nationality": ["ru"]}
    if topics:
        props["topics"] = topics
    return make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": ["t"]}
    )


def _company(entity_id: str, *, anchor: str | None = None) -> FtmEntity:
    """A Company fixture; with ``anchor`` set on ``wikidata_id`` (for the anchor-conflict case)."""
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


def _cluster(member_ids: tuple[str, ...], *, score: float = 0.99) -> ResolvedCluster:
    """A frozen ``ResolvedCluster`` with an exact ``score`` (the value the Chow band reads).

    Built directly (not via the scorer) so ``score`` is pinned and ``is_merge`` is True (>=2
    members) — the probe is then exercised over a real merged cluster, never a vacuous singleton.
    """
    cluster = ResolvedCluster(
        canonical_id="wmc-test",
        member_ids=member_ids,
        entity=_merged_stub(),
        score=score,
    )
    assert cluster.is_merge is True, "fixture: the probe must be exercised over a real merge"
    return cluster


@pytest.fixture
def abstain_band(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set the Chow band from ``request.param`` (a ``(low, high)`` tuple) + clear the cache."""
    low, high = request.param
    monkeypatch.setenv("SENSITIVITY_ABSTAIN_LOW", str(low))
    monkeypatch.setenv("SENSITIVITY_ABSTAIN_HIGH", str(high))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def band_off(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the DEFAULT band-OFF posture (``low == high == 0.92`` ⇒ empty) for the exemptible cases.

    Guards the exemptible-direction assertions against a stray ambient ``SENSITIVITY_ABSTAIN_*``
    leaking a non-empty band into a cluster whose ``score`` would then land inside it.
    """
    monkeypatch.delenv("SENSITIVITY_ABSTAIN_LOW", raising=False)
    monkeypatch.delenv("SENSITIVITY_ABSTAIN_HIGH", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------------------------
# "No NARROWER than required" — every NON-exemptible signal makes the probe True (no under-park).
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "topic",
    [
        "role.rca",  # one of the 18 legacy-MISSED risk codes (newly-broadened)
        "crime.war",  # another legacy-missed risk code
        "cti.apt",  # off-ontology (not in registry.topic.names) — unknown ⇒ sensitive
    ],
)
def test_newly_broadened_topic_member_is_nonexemptible(band_off: None, topic: str) -> None:
    """``has_nonexemptible_sensitivity`` is True for a member with a NEWLY-BROADENED risk topic.

    A risk a prior approval could not have considered — a code the legacy denylist +
    ``role.pep*``/``sanction*`` prefix rule MISSED (``role.rca`` / ``crime.war``) or an
    off-ontology code (``cti.apt``) — is non-exemptible (ADR 0047 Decision 5 / §15.2, the
    member-derived Stage-1 axis). The band is OFF and ``neo4j`` defaults ``None``, so the ONLY axis
    that can fire is the newly-broadened-topic one — the probe's True here is provably that axis.

    RED pre-fix: ``has_nonexemptible_sensitivity`` does not exist on slice-2 (ImportError).
    """
    cluster = _cluster(("p1", "p2"))
    by_id = {"p1": _person("p1", topics=[topic]), "p2": _person("p2")}
    assert has_nonexemptible_sensitivity(cluster, by_id) is True, (
        f"a newly-broadened-sensitive member ({topic}) is NOT exemptible by a stale approval — a "
        "sign-off could not have considered a risk the legacy guard never saw (ADR 0047 Dec 5)"
    )


@pytest.mark.parametrize("abstain_band", [(0.90, 0.95)], indirect=True)
def test_chow_in_band_cluster_is_nonexemptible(abstain_band: None) -> None:
    """``has_nonexemptible_sensitivity`` is True for a topic-clean cluster whose ``score`` is in
    the configured Chow abstain band ``[0.90, 0.95)``.

    A marginal-confidence merge is not provably benign; the Stage-3 Chow band is a non-exemptible
    signal a prior approval (which had no score awareness) could not have considered (§15.2). The
    members are topic-clean and ``neo4j`` defaults ``None``, so the probe's True provably comes from
    the band axis. Score ``0.92`` lies in ``[0.90, 0.95)``.

    RED pre-fix: the probe does not exist on slice-2 (ImportError).
    """
    cluster = _cluster(("p1", "p2"), score=0.92)
    by_id = {"p1": _person("p1"), "p2": _person("p2")}
    assert has_nonexemptible_sensitivity(cluster, by_id) is True, (
        "a 0.92 cluster inside the configured [0.90, 0.95) Chow band is NOT exemptible — a "
        "marginal merge a prior approval could not have judged on confidence re-parks (spec §15.2)"
    )


# --------------------------------------------------------------------------------------------
# "No WIDER than allowed" — every EXEMPTIBLE-only cluster keeps the probe False (preserve the
# frozen approve→promote path; over-tightening would re-park a knowingly-approved merge).
# --------------------------------------------------------------------------------------------


def test_size_over_10_topic_clean_cluster_is_exemptible(band_off: None) -> None:
    """``has_nonexemptible_sensitivity`` is False for a pure size>10, topic-clean merge (band OFF).

    The size flag (ADR 0020 size half, conservative-by-default) was always visible and stays
    exemptible by an approved group. None of the three non-exemptible axes fires: no newly-broadened
    topic, ``neo4j=None`` (k-hop skipped), band OFF (0.99 ∉ ``[0.92, 0.92)``). RED pre-fix: the
    probe does not exist on slice-2 (ImportError).
    """
    member_ids = tuple(f"m{i:02d}" for i in range(11))
    by_id = {mid: _person(mid) for mid in member_ids}
    cluster = _cluster(member_ids, score=0.99)
    assert has_nonexemptible_sensitivity(cluster, by_id) is False, (
        "a size>10 topic-clean merge is exemptible — re-parking a knowingly-approved oversized "
        "merge breaks the frozen approve→promote path (the 'no wider' direction; DENY E-FROZEN)"
    )


def test_anchor_conflict_only_cluster_is_exemptible(band_off: None) -> None:
    """``has_nonexemptible_sensitivity`` is False for an anchor-conflict-only merge (ADR 0040).

    The anchor-conflict park (ADR 0040) stays exemptible by an approved group (spec §5). The
    members are topic-clean (no newly-broadened axis), ``neo4j=None`` (k-hop skipped), and the band
    is OFF — so the probe must be False even though ``needs_review`` WOULD flag this cluster for
    conflicting canonical anchors. RED pre-fix: the probe does not exist on slice-2 (ImportError).
    """
    by_id = {"a": _company("a", anchor="Q1"), "b": _company("b", anchor="Q2")}
    cluster = _cluster(("a", "b"), score=0.99)
    # Sanity: the cluster IS anchor-conflict-flagged; the point is exemptibility, not no-flag.
    flagged, reason = needs_review(cluster, by_id)
    assert flagged is True and "anchor" in reason.lower(), (
        "fixture: the cluster must actually carry an anchor-conflict flag (else this is vacuous)"
    )
    assert has_nonexemptible_sensitivity(cluster, by_id) is False, (
        "an anchor-conflict flag (ADR 0040) stays exemptible by an approved group (spec §5)"
    )


def test_legacy_caught_sanction_member_is_exemptible(band_off: None) -> None:
    """``has_nonexemptible_sensitivity`` is False for a LEGACY-CAUGHT sanction member (band OFF).

    The unit twin of the frozen T4 discriminator
    (``test_sensitivity_guard.py::...legacy_caught_sanction...stays_exemptible``): a ``sanction``
    topic was visible to the legacy guard at approval time, so ``is_newly_broadened_sensitive`` is
    False; with ``neo4j=None`` (no k-hop) and the band OFF the probe is False ⇒ a knowingly-approved
    ``sanction`` merge STILL auto-promotes (spec §15.4). RED pre-fix: probe absent on slice-2.
    """
    cluster = _cluster(("p1", "p2"), score=0.99)
    by_id = {"p1": _person("p1", topics=["sanction"]), "p2": _person("p2")}
    # Sanity: needs_review DOES flag it (Stage-1 topic) — the point is that the flag is exemptible.
    flagged, _ = needs_review(cluster, by_id)
    assert flagged is True, "fixture: a sanction member must be flagged by Stage 1 (else vacuous)"
    assert has_nonexemptible_sensitivity(cluster, by_id) is False, (
        "a LEGACY-CAUGHT sanction merge that was knowingly approved stays exemptible and "
        "auto-promotes — preserving the frozen T4 discriminator (spec §15.4; DENY E-FROZEN)"
    )


def test_clean_merge_is_exemptible(band_off: None) -> None:
    """``has_nonexemptible_sensitivity`` is False for an ordinary clean, high-confidence merge.

    No risk topic, no k-hop (``neo4j=None``), score 0.99 outside the OFF band — none of the three
    non-exemptible axes fires, so an approved clean re-merge keeps auto-promoting (spec §9 floor).
    RED pre-fix: the probe does not exist on slice-2 (ImportError).
    """
    cluster = _cluster(("p1", "p2"), score=0.99)
    by_id = {"p1": _person("p1"), "p2": _person("p2")}
    assert has_nonexemptible_sensitivity(cluster, by_id) is False, (
        "a clean merge has no non-exemptible signal — the approve→promote floor must hold (spec §9)"
    )


# --------------------------------------------------------------------------------------------
# T-MASK-chow — THE MASKING UNIT PIN (spec §16). An EXEMPTIBLE size>10 flag fires FIRST and masks
# a co-occurring Chow-band signal in needs_review's single reason — but the probe is NOT fooled.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("abstain_band", [(0.90, 0.95)], indirect=True)
def test_mask_chow_band_under_size_first_flag(abstain_band: None) -> None:
    """T-MASK-chow: a size>10 cluster whose ``score`` is inside the Chow band ``[0.90, 0.95)``.

    ``needs_review`` SHORT-CIRCUITS on the size flag (the FIRST in its order), so it returns the
    SIZE reason and NEVER reaches the Stage-3 band — proving the masking the slice-2 reason-string
    fence fell for. The structured probe evaluates the Chow band INDEPENDENTLY, so
    ``has_nonexemptible_sensitivity`` is True. This is the unit twin of the integration masking
    oracle (``test_exemption_fence_masking.py``) and needs no Neo4j (``neo4j`` defaults ``None``).

    RED pre-fix: ``has_nonexemptible_sensitivity`` does not exist on slice-2 (ImportError) — and
    even were it present, the OLD reason-string classifier would see only the SIZE reason and (for
    the OLD design) call it exemptible, which is exactly the masking fail-open this pins. DENY
    E-MASK if the probe does not see the masked band signal.
    """
    member_ids = tuple(f"m{i:02d}" for i in range(11))
    by_id = {mid: _person(mid) for mid in member_ids}
    cluster = _cluster(member_ids, score=0.92)

    flagged, reason = needs_review(cluster, by_id)
    assert flagged is True, "fixture: an 11-member cluster must be flagged"
    assert "exceeds auto-merge limit" in reason, (
        "needs_review must return the SIZE reason first — this is the masking the probe must "
        f"defeat (got: {reason!r})"
    )
    assert "abstain" not in reason.lower() and "band" not in reason.lower(), (
        "the size flag short-circuits BEFORE Stage 3 — the Chow band is absent from the reason, "
        "which is precisely why a reason-string classifier would miss it"
    )
    assert has_nonexemptible_sensitivity(cluster, by_id) is True, (
        "the structured probe evaluates the Chow band INDEPENDENTLY of needs_review's first-flag "
        "short-circuit — the masked band signal makes the cluster NON-exemptible (spec §15.2/§16; "
        "DENY E-MASK)"
    )
