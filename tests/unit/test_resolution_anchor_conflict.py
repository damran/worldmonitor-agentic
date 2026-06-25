"""Gate B-5 — ER anchor-conflict + identifier-override negative evidence.

Spec: ``docs/reviews/GATE_B5_SPEC.md`` §5 (INV-1..INV-3 / INV-1b / INV-1c) and §6 (the named-test
table). ADR: ``docs/decisions/0040-er-anchor-conflict-negative-evidence.md`` — the anchor-conflict
policy fork is **RESOLVED → (C) HYBRID** (2026-06-24): hard-block the pairwise anchor clash in
Splink scoring AND park any residual / transitive anchor-conflict cluster in ``needs_review`` as
defense-in-depth. This file therefore asserts BOTH mechanisms.

These tests are the gate's acceptance oracle, written FROM THE SPEC and independent of any
implementation. They pin OUTCOMES only — merge / no-merge / park at the 0.92 boundary and the
``get_anchors`` conflict representation — never the m/u calibration or the chosen encoding, which
the builder is free to pick (ADR 0040 §Decision parts 1-3).

Three over-merge holes this gate closes (all reproduced against HEAD ``0ffc1a6`` — see ADR
§Context):

- Finding 1 (NEW HIGH, INV-1 / INV-1b / INV-1c): conflicting single-valued canonical anchors are
  not negative evidence. Two records with DISTINCT authoritative ids (``wikidata_id`` Q1 vs Q2)
  auto-merge (non-sensitive, size 2), and ``get_anchors`` then silently drops the losing anchor
  (``[0]`` winner).
- Finding 2 (H-5, INV-2): a shared ``wikidata_id`` exact level (BF 199 800) overrides total name
  disagreement — a shared anchor alone clears 0.92.
- Finding 3 (Judge MEDIUM, INV-3): a shared ``wikidataId`` overrides a CLASHING B-3 distinguishing
  id.

Sensitivity is OFF (no ``topics`` / ``sanction``) on every fixture: that is LOAD-BEARING. The
catastrophic-merge guard parks on sensitivity (Gate E / ADR 0047 broadened this axis to FtM's
full ``registry.topic.RISKS`` set + any off-ontology topic code — deny-by-default), cluster size
> 10, or the anchor conflict itself; since these fixtures carry NO ``topics`` at all they stay
non-sensitive even after Gate E, so a non-sensitive anchor-conflict pair is auto-merged with NO
review absent this park — the exact hole B-5 exposes, and the anchor-conflict flag remains the
load-bearing one here. A sensitive fixture would mask the auto-merge behind the sensitivity guard
and make these tests vacuous (B-3 spec §note).

Anchor representation (ADR §Context, spec §6): canonical anchors live in
``entity.context["wm_anchor_<field>"]`` (set via :func:`set_anchor`), NOT in FtM properties. The
anchor-CLASH *scoring* level (Finding 1) is over that context, so the INV-1 / INV-1b fixtures set
the context via ``set_anchor`` (their ``_flatten`` ``wikidata_id`` column stays ``None``). The
shared-anchor-OVERRIDE findings (2, 3) exercise the EXISTING ``wikidata_id`` exact comparison, which
reads the ``wikidataId`` FtM *property* (``_flatten``'s already-projected column) — so the INV-2 /
INV-2b / INV-3 fixtures set the ``wikidataId`` property, not the anchor context.

Convention mirrors ``tests/unit/test_resolution_distinguishing_evidence.py`` (B-3) and
``tests/unit/test_resolution_multiscript.py`` (ADR-0035) — both FROZEN regression guards that prove
INV-4 / INV-5 and MUST pass unchanged (this file does not duplicate them): the local
``_company`` / ``_top_probability`` / ``_merges`` helpers, ``score_pairs`` + ``cluster_and_merge``
(+ ``needs_review``) for merge/park assertions, and the ``DEFAULT_MERGE_THRESHOLD`` (0.92) boundary
taken from the code, not a hardcoded literal.
"""

from __future__ import annotations

import pytest

from worldmonitor.ontology.anchors import (
    CANONICAL_ID_FIELDS,
    get_anchors,
    set_anchor,
)
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import (
    DEFAULT_MERGE_THRESHOLD,
    ResolvedCluster,
    cluster_and_merge,
)
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import score_pairs

# Two distinct authoritative Wikidata Q-numbers: by definition two different real-world entities.
Q1 = "Q1"
Q2 = "Q2"

NAME = "Acme Trading Limited"


# --------------------------------------------------------------------------------------------
# Fixtures / helpers (mirror the B-3 + multiscript convention)
# --------------------------------------------------------------------------------------------


def _company(
    entity_id: str,
    name: str,
    *,
    anchor_field: str | None = None,
    anchor_value: str | None = None,
    wikidata_id: str | None = None,
    registration_number: list[str] | None = None,
    country: str | None = "gb",
) -> FtmEntity:
    """A NON-sensitive Company fixture (no ``topics``/``sanction``; sensitivity off is
    load-bearing).

    ``anchor_field``/``anchor_value`` set a CANONICAL ANCHOR in ``entity.context`` via the real
    :func:`set_anchor` API (the column the anchor-CLASH scoring level reads). ``wikidata_id`` sets
    the FtM ``wikidataId`` *property* (the column the EXISTING shared-anchor exact level reads —
    Findings 2/3). ``registration_number`` is the B-3 ``identifier``-typed distinguishing id.
    """
    props: dict[str, list[str]] = {"name": [name]}
    if country is not None:
        props["country"] = [country]
    if wikidata_id is not None:
        props["wikidataId"] = [wikidata_id]
    if registration_number is not None:
        props["registrationNumber"] = registration_number
    entity = make_entity(
        {"id": entity_id, "schema": "Company", "properties": props, "datasets": ["t"]}
    )
    if anchor_field is not None and anchor_value is not None:
        set_anchor(entity, anchor_field, anchor_value)
    return entity


def _top_probability(entities: list[FtmEntity]) -> float:
    """Top pairwise match probability (0.0 if no pair survives blocking/schema)."""
    return max((pair.probability for pair in score_pairs(entities)), default=0.0)


def _merges(entities: list[FtmEntity]) -> list[ResolvedCluster]:
    """Real merge clusters (``c.is_merge``) at the 0.92 boundary — ADR-0035 merge convention."""
    return [c for c in cluster_and_merge(entities, score_pairs(entities)) if c.is_merge]


# --------------------------------------------------------------------------------------------
# INV-1 (fork C) — conflicting canonical anchors hard-block the pairwise merge (scoring level)
# AND park any residual / transitive anchor-conflict cluster (needs_review defense-in-depth).
# --------------------------------------------------------------------------------------------


def test_conflicting_wikidata_anchor_blocks_merge() -> None:
    """INV-1 (A/C): two non-sensitive Companies, same name+country, CONFLICTING ``wikidata_id``
    anchor (Q1 vs Q2) → the anchor-clash scoring level drops the pair below the merge threshold and
    no cluster forms.

    HEAD reproduction: the anchor lives only in ``entity.context`` (``_flatten``'s ``wikidata_id``
    column is ``None``), so it is not scored at all and the pair auto-merges at ~0.9825 on
    name+country alone. After fork-C's anchor-clash level it must score below 0.92.
    """
    a = _company("a", NAME, anchor_field="wikidata_id", anchor_value=Q1)
    b = _company("b", NAME, anchor_field="wikidata_id", anchor_value=Q2)
    assert _top_probability([a, b]) < DEFAULT_MERGE_THRESHOLD
    assert _merges([a, b]) == []


def test_conflicting_anchor_cluster_parks() -> None:
    """INV-1 (B/C): a residual anchor-conflict cluster (members holding Q1 vs Q2) is PARKED by
    ``needs_review`` — flagged True with a reason naming the conflicting anchor field + values — and
    is therefore NOT silently auto-promoted.

    The conflict is computed over the cluster's SOURCE members (``by_id``), not the merged
    ``cluster.entity`` (whose ``merge_context`` unions Q1+Q2 and which ``get_anchors`` would mask
    — that masking is Finding 1). The cluster is built directly so this asserts the guard regardless
    of whether the pairwise scoring level also blocks it (defense-in-depth).
    """
    a = _company("a", NAME, anchor_field="wikidata_id", anchor_value=Q1)
    b = _company("b", NAME, anchor_field="wikidata_id", anchor_value=Q2)
    member_ids = ("a", "b")
    # A merged FtM entity whose context unions the two conflicting anchors, exactly as
    # ``cluster_and_merge``'s ``merge_context`` produces it for a fused anchor-conflict cluster.
    merged = make_entity(
        {
            "id": "wmc-anchor-conflict",
            "schema": "Company",
            "properties": {"name": [NAME]},
            "datasets": ["t"],
        }
    )
    merged.context["wm_anchor_wikidata_id"] = [Q1, Q2]
    cluster = ResolvedCluster(
        canonical_id="wmc-anchor-conflict",
        member_ids=member_ids,
        entity=merged,
        score=0.99,
    )
    flagged, reason = needs_review(cluster, {"a": a, "b": b})
    assert flagged is True
    # The reason must name the conflicting anchor field and BOTH values so the park is a usable
    # lead.
    lowered = reason.lower()
    assert "wikidata_id" in lowered
    assert Q1 in reason and Q2 in reason
    # And it must read as an anchor conflict, not the size / sensitivity trigger.
    assert "anchor" in lowered or "conflict" in lowered


def test_transitive_conflicting_anchor_cluster_parks() -> None:
    """INV-1 (C, the case pairwise scoring alone MISSES): A~M and M~Z each clean (same name+country,
    middle ``m`` carries NO anchor), A and Z carry the only anchor clash (Q1 vs Q2). The three are
    assembled into ONE cluster by ``cluster_and_merge`` (clean bridges), and ``needs_review`` PARKS
    it because its source members span two distinct ``wikidata_id`` anchors.

    Splink only scores PAIRS, so the fork-C scoring level cannot see this transitive conflict — only
    the assembled-cluster guard can. This is the reason the hybrid park exists (ADR 0040
    §trade-offs).
    """
    a = _company("a", NAME, anchor_field="wikidata_id", anchor_value=Q1)
    m = _company("m", NAME)  # clean bridge: shares name+country with both, carries no anchor
    z = _company("z", NAME, anchor_field="wikidata_id", anchor_value=Q2)
    entities = [a, m, z]
    by_id = {e.id: e for e in entities}
    clusters = cluster_and_merge(entities, score_pairs(entities))
    merged = [c for c in clusters if c.is_merge]
    # The clean bridges assemble the three into one cluster (the transitive conflict the guard
    # exists to catch). If the cluster did not form there would be nothing to park — wrong tree.
    assert len(merged) == 1, "clean bridges A~M, M~Z must assemble one transitive cluster"
    transitive = merged[0]
    assert set(transitive.member_ids) == {"a", "m", "z"}
    flagged, reason = needs_review(transitive, by_id)
    assert flagged is True, "a transitive anchor-conflict cluster must be parked, not auto-promoted"
    lowered = reason.lower()
    assert "wikidata_id" in lowered
    assert Q1 in reason and Q2 in reason
    assert "anchor" in lowered or "conflict" in lowered


# --------------------------------------------------------------------------------------------
# INV-1b — the anchor-clash rule is over CANONICAL_ID_FIELDS, not hard-coded to wikidata.
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", CANONICAL_ID_FIELDS)
def test_conflicting_anchor_blocks_or_parks_per_field(field: str) -> None:
    """INV-1b: a conflict on ANY single-valued canonical anchor field (``wikidata_id``, ``lei``,
    ``geonames_id``, ``opencorporates_id``) drops the same name+country pair below threshold and
    yields no merge — proving the rule iterates ``CANONICAL_ID_FIELDS`` rather than special-casing
    wikidata.

    HEAD: every field currently auto-merges at ~0.9825 (no anchor scored). Under fork C each must
    block (< 0.92, no cluster), exactly like ``test_conflicting_wikidata_anchor_blocks_merge``.
    """
    a = _company("a", NAME, anchor_field=field, anchor_value="V1")
    b = _company("b", NAME, anchor_field=field, anchor_value="V2")
    assert _top_probability([a, b]) < DEFAULT_MERGE_THRESHOLD
    assert _merges([a, b]) == []


# --------------------------------------------------------------------------------------------
# INV-1c — get_anchors does not silently pick a wrong winner on a conflicting field, and the
# clean single-value case is unchanged (writer contract preserved, dict[str, str]).
# --------------------------------------------------------------------------------------------


def test_get_anchors_omits_conflicting_field() -> None:
    """INV-1c: an entity whose context holds ``wm_anchor_wikidata_id=['Q1','Q2']`` (the union a
    fused anchor-conflict cluster carries) MUST NOT yield ``{'wikidata_id': 'Q1'}`` — the silent
    ``[0]`` winner of Finding 1. Per ADR 0040 §Decision the conflicting field is OMITTED (no
    arbitrary value projected onto the node); the conflict is the guard's job to park (INV-1), not
    ``get_anchors``'s job to invent a winner.
    """
    entity = make_entity(
        {"id": "c", "schema": "Company", "properties": {"name": [NAME]}, "datasets": ["t"]}
    )
    entity.context["wm_anchor_wikidata_id"] = [Q1, Q2]
    anchors = get_anchors(entity)
    # The bug is returning the [0] winner; the fix omits the conflicting field entirely.
    assert anchors.get("wikidata_id") != Q1
    assert "wikidata_id" not in anchors


def test_get_anchors_clean_single_value_unchanged() -> None:
    """INV-1c: a clean single-value anchor context (``['Q1']``) STILL returns
    ``{'wikidata_id':'Q1'}`` as a ``dict[str, str]`` — the writer contract (``graph/writer.py:165``
    spread) is preserved; the only behavior change is for a CONFLICTING field.
    """
    entity = _company("c", NAME, anchor_field="wikidata_id", anchor_value=Q1)
    anchors = get_anchors(entity)
    assert anchors == {"wikidata_id": Q1}
    assert isinstance(anchors["wikidata_id"], str)


# --------------------------------------------------------------------------------------------
# INV-2 — a shared anchor cannot ALONE clear 0.92 against an active name disagreement (Finding 2).
# Exercises the EXISTING wikidata_id exact level via the wikidataId FtM property.
# --------------------------------------------------------------------------------------------


def test_shared_wikidata_with_name_disagreement_does_not_merge() -> None:
    """INV-2: two Companies sharing a ``wikidataId`` (Q42) + same country but TOTAL name
    disagreement (name at the ``else`` level — no shared tokens) → top probability below
    threshold, no merge.

    HEAD reproduction (ADR §Context, Finding 2): posterior 0.9795 — the shared-anchor BF 199 800
    swamps the name ``else`` BF (0.0421) and clears 0.92. The Part-1 m/u relax must drop this below
    0.92.
    """
    a = _company("a", "Alpha Manufacturing", wikidata_id="Q42", country="gb")
    b = _company("b", "Zeta Logistics", wikidata_id="Q42", country="gb")
    assert _top_probability([a, b]) < DEFAULT_MERGE_THRESHOLD
    assert _merges([a, b]) == []


def test_shared_wikidata_alone_does_not_merge() -> None:
    """INV-2: a shared ``wikidataId`` (Q42) with NO other corroboration (different name, no country)
    → top probability below threshold.

    HEAD reproduction (Finding 2): posterior 0.995 — a single shared anchor alone clears 0.92. After
    the relax, one shared anchor with no name/country support must NOT alone clear the threshold.
    """
    a = _company("a", "Alpha Manufacturing", wikidata_id="Q42", country=None)
    b = _company("b", "Zeta Logistics", wikidata_id="Q42", country=None)
    assert _top_probability([a, b]) < DEFAULT_MERGE_THRESHOLD
    assert _merges([a, b]) == []


# --------------------------------------------------------------------------------------------
# INV-2b — no recall loss: a shared anchor WITH name corroboration and no clash still merges.
# --------------------------------------------------------------------------------------------


def test_shared_wikidata_with_matching_name_still_merges() -> None:
    """INV-2b: two Companies, same ``wikidataId`` (Q7) + same name (exact fingerprint) + same
    country, no clash → still merges (>= 0.92). A genuine duplicate that legitimately shares a QID
    must NOT regress from the Part-1 m/u relax.

    HEAD: ~0.99999 (already merges); the relax must keep this above the threshold.
    """
    a = _company("a", NAME, wikidata_id="Q7")
    b = _company("b", NAME, wikidata_id="Q7")
    assert _top_probability([a, b]) >= DEFAULT_MERGE_THRESHOLD
    assert len(_merges([a, b])) == 1


# --------------------------------------------------------------------------------------------
# INV-3 — a clashing B-3 distinguishing id is NOT overridden by a shared anchor (Finding 3).
# --------------------------------------------------------------------------------------------


def test_clashing_reg_id_not_overridden_by_shared_wikidata() -> None:
    """INV-3: two Companies, same name + country, SAME ``wikidataId`` (Q99) but CLASHING
    ``registrationNumber`` (the exact B-3 negative-evidence case) → top probability below threshold,
    no merge. The B-3 clash must win against a single shared anchor.

    HEAD reproduction (ADR §Context, Finding 3): posterior 0.999947 — the shared-wikidata BF swamps
    the B-3 clash BF (negative-evidence precedence inverted). Parts 1+2 must restore the clash's
    veto.
    """
    a = _company("a", NAME, wikidata_id="Q99", registration_number=["111111"])
    b = _company("b", NAME, wikidata_id="Q99", registration_number=["222222"])
    assert _top_probability([a, b]) < DEFAULT_MERGE_THRESHOLD
    assert _merges([a, b]) == []
