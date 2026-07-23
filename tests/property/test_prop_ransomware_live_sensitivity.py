"""Property: the ransomware_live victim/group shapes never bypass the catastrophic-merge guard
(Gate S-4, ``docs/reviews/GATE_S4_RANSOMWARE_LIVE_SPEC.md`` §1/§9 Slice 3, ADR 0120/0047).

This is the gate's mandatory ``@given`` property/metamorphic test (CLAUDE.md "Build discipline":
any gate touching an invariant — here ER/merge + the sensitivity guard — MUST add one, not just an
example test). It pins THREE independently-oracled facts about the shapes this connector's
``map()`` emits, built here from scratch (never imported from the connector) so a weakened
implementation can't drag the oracle down with it:

* a group ``Organization`` built like ``map()``'s output (``topics=["crime.cyber"]``) is ALWAYS
  ``is_sensitive`` — independent oracle: ``crime.cyber`` has the RISKS dot-ancestor ``crime``
  (``guard/sensitivity.py`` clause b). Fail = FAIL-OPEN, the exact class ADR 0047 exists to close.
* a victim ``Company`` built like ``map()``'s output (name/country/sector, deliberately carrying
  NO ``topics``) is NEVER ``is_sensitive`` — pins "the victim node carries no allegation
  topic/score" (spec §1/§3.2, "leads, not verdicts").
* **the gate-mandatory negative property**: for ANY generated victim Company + group
  Organization(crime.cyber), driving the REAL ``resolution.merge.cluster_and_merge`` with a
  high-confidence ``ScoredPair`` between them — mirroring what Splink WOULD hand back if it ever
  fuzzy-matched a victim's name against a group's (Company/Organization share the LegalEntity
  family, so the pair is schema-compatible and DOES cluster; the sensitivity park, not a
  schema-incompatibility, is what stops the silent fuse) — and then calling
  ``resolution.review.needs_review`` exactly as ``resolution/pipeline.py:404`` does, ALWAYS parks
  (flags) any resulting cluster whose members include the group. A regression here would let a
  criminal self-declaration silently canonicalize into a group Organization with zero human
  review — the exact "never auto-merge a sensitive entity" invariant (CLAUDE.md).

These three are pins on EXISTING guard behaviour for the NEW S-4 entity shapes (not new guard
code) — GREEN is the correct/expected color; the value is regression-proofing the S-4 lane against
a future guard change that reopens the fuse. Mirrors ``tests/property/test_prop_merge_guard.py``'s
oracle style and ``tests/property/test_prop_indicator_lane.py::test_p_ind_2c_...`` for driving
``cluster_and_merge`` end-to-end. ``Settings(enforcement_profile="strict")`` is pinned on the
guard's ``get_settings()`` read (defence against the local-``.env``-off footgun — see
``guard/sensitivity.py::needs_review`` Stage 2/3) even though, for THESE fixtures, Stage 1 (topics)
already short-circuits before ``get_settings()`` is ever called; the pin future-proofs the file
against a fixture change that reaches Stage 2/3.
"""

from __future__ import annotations

import string
from collections.abc import Iterator

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import worldmonitor.guard.sensitivity as sensitivity_mod
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import ScoredPair
from worldmonitor.settings import Settings

_SETTINGS = settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])

_CRIME_CYBER_TOPIC = "crime.cyber"

_NAME = st.text(alphabet=string.ascii_letters + " ", min_size=1, max_size=30)
_ALIAS = st.text(alphabet=string.ascii_letters + string.digits + " -", min_size=0, max_size=20)
_COUNTRY = st.sampled_from(["US", "DE", "FR", "RU", "CN", "GB", "BR", "IN", "", "ZZ"])
_SECTOR = st.text(alphabet=string.ascii_letters + " ", min_size=0, max_size=30)
_SAFE_ID = st.text(alphabet=string.ascii_lowercase + string.digits + "-", min_size=1, max_size=40)
_SCORE = st.floats(min_value=0.92, max_value=1.0, allow_nan=False, allow_infinity=False)


@pytest.fixture(autouse=True)
def _pin_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin ``Settings(enforcement_profile="strict")`` on the guard's own ``get_settings()`` read
    (``worldmonitor.guard.sensitivity``), isolating this file from a local dev ``.env`` (the
    documented ``ENFORCEMENT_PROFILE=off`` footgun) — see the module docstring."""
    monkeypatch.setattr(
        sensitivity_mod, "get_settings", lambda: Settings(enforcement_profile="strict")
    )
    yield


# ---------------------------------------------------------------------------------------------
# Builders — shaped exactly like ransomware_live's map() output (built independently here, never
# imported from the connector, so the oracle can't be dragged down by a weakened implementation).
# ---------------------------------------------------------------------------------------------


def _group_org(entity_id: str, name: str, weak_alias: str) -> FtmEntity:
    """A group Organization shaped like map()'s victim-path thin org OR groups-path rich org.

    Both ALWAYS carry ``topics=["crime.cyber"]`` (spec §3.2/§3.3 -- "every group Org is
    sensitive")."""
    props: dict[str, list[str]] = {"name": [name], "topics": [_CRIME_CYBER_TOPIC]}
    if weak_alias:
        props["weakAlias"] = [weak_alias]
    return make_entity({"id": entity_id, "schema": "Organization", "properties": props})


def _victim_company(entity_id: str, name: str, country: str, sector: str) -> FtmEntity:
    """A victim Company shaped like map()'s victim path — name/country/sector, NEVER topics
    (spec §3.2: "victims never get a risk topic", the leads-not-verdicts invariant)."""
    props: dict[str, list[str]] = {"name": [name]}
    if country:
        props["country"] = [country]
    if sector:
        props["sector"] = [sector]
    return make_entity({"id": entity_id, "schema": "Company", "properties": props})


# ---------------------------------------------------------------------------------------------
# Pin 1 — every group Org is sensitive.
# ---------------------------------------------------------------------------------------------


@given(entity_id=_SAFE_ID, name=_NAME, weak_alias=_ALIAS)
@_SETTINGS
def test_group_org_with_cyber_topic_is_sensitive(
    entity_id: str, name: str, weak_alias: str
) -> None:
    org = _group_org(entity_id, name, weak_alias)
    assert sensitivity_mod.is_sensitive(org) is True, (
        f"FAIL OPEN: a ransomware_live group Organization (topics=['{_CRIME_CYBER_TOPIC}'], "
        f"name={name!r}) was NOT flagged sensitive — group merges MUST park for human review"
    )


# ---------------------------------------------------------------------------------------------
# Pin 2 — a victim Company is never topic-sensitive (no allegation on the node).
# ---------------------------------------------------------------------------------------------


@given(entity_id=_SAFE_ID, name=_NAME, country=_COUNTRY, sector=_SECTOR)
@_SETTINGS
def test_victim_company_is_never_topic_sensitive(
    entity_id: str, name: str, country: str, sector: str
) -> None:
    victim = _victim_company(entity_id, name, country, sector)
    assert victim.get("topics", quiet=True) == [], (
        "generator contract: a ransomware_live victim Company must never carry a topics value"
    )
    assert sensitivity_mod.is_sensitive(victim) is False, (
        f"a topic-clean ransomware_live victim Company (name={name!r}, country={country!r}, "
        f"sector={sector!r}) was wrongly flagged sensitive — victims carry no allegation topic"
    )


# ---------------------------------------------------------------------------------------------
# Pin 3 (THE gate-mandatory negative property) — a victim can never auto-merge into a sensitive
# group: cluster_and_merge WILL cluster them (schema-compatible LegalEntity family), but
# needs_review MUST park every such cluster.
# ---------------------------------------------------------------------------------------------


@given(
    victim_id=_SAFE_ID,
    group_id=_SAFE_ID,
    victim_name=_NAME,
    group_name=_NAME,
    weak_alias=_ALIAS,
    country=_COUNTRY,
    sector=_SECTOR,
    score=_SCORE,
)
@_SETTINGS
def test_p_s4_victim_never_auto_merges_into_a_sensitive_group(
    victim_id: str,
    group_id: str,
    victim_name: str,
    group_name: str,
    weak_alias: str,
    country: str,
    sector: str,
    score: float,
) -> None:
    if victim_id == group_id:
        return  # cluster_and_merge keys entities by id; distinct ids are the generator contract
    victim = _victim_company(victim_id, victim_name, country, sector)
    group = _group_org(group_id, group_name, weak_alias)

    clusters = cluster_and_merge(
        [victim, group], [ScoredPair(victim_id, group_id, score)], merge_threshold=0.92
    )

    # Sanity: this fixture pair IS schema-compatible and DOES form a single 2-member cluster —
    # otherwise the property below would vacuously pass by never exercising the park at all.
    merged = [c for c in clusters if c.is_merge]
    assert len(merged) == 1 and set(merged[0].member_ids) == {victim_id, group_id}, (
        f"expected Company+Organization(crime.cyber) to schema-compatibly cluster into ONE "
        f"2-member merge (LegalEntity family) — got clusters={clusters!r}"
    )

    by_id = {victim_id: victim, group_id: group}
    for cluster in clusters:
        if group_id in cluster.member_ids:
            flagged, reason = needs_review(cluster, by_id)
            assert flagged is True, (
                f"a cluster containing the sensitive group {group_id!r} (crime.cyber) was NOT "
                f"parked for human review — a criminal self-declaration must never silently "
                f"auto-promote a merge into a sensitive Organization. cluster={cluster!r} "
                f"reason={reason!r}"
            )
