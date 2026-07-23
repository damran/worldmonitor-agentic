"""Property: the ransomware_live ``UnknownLink`` edge never enters fuzzy resolution (Gate S-4,
``docs/reviews/GATE_S4_RANSOMWARE_LIVE_SPEC.md`` §1/§9 Slice 3, ADR 0119/0120).

``UnknownLink`` is ``matchable: false`` in FollowTheMoney (verified below independently of the
connector), so ADR 0119's ``score_pairs`` matchable pre-filter (``splink_model.py:523``) must
exclude it from Splink entirely BEFORE any frame construction — the edge can never fuzzy-fuse with
another edge or with a Company/Organization node. This is the codebase's FIRST edge-emitting
connector (spec §0), so it is the first concrete exercise of that pre-filter against a REAL edge
schema rather than only against ``wm:Indicator`` (``test_prop_matchable_gate.py``'s existing pin).
Mirrors that file's mechanics (``deadline=None`` + ``suppress_health_check=[HealthCheck.too_slow]``,
pure in-process Splink/DuckDB, no DB/network) and its two named properties, applied to the S-4
victim Company / group Organization / UnknownLink-edge shapes:

* ``test_p_s4_no_pair_references_an_unknownlink_edge`` — over a generated corpus of victim
  Companies + group Organizations + UnknownLink edges (edges wired subject/object/role exactly the
  way the connector's ``map()`` builds them — subject=group id, object=victim id, a fixed
  allegation-grade ``role``), every ``score_pairs(corpus)`` pair references only matchable-schema
  entities; no edge id ever appears in a returned pair.
* ``test_p_s4_adding_edges_is_non_interfering`` — adding a disjoint UnknownLink edge set to a
  matchable corpus never perturbs the pairs scored among the matchable subset
  (``prior``/m/u are fixed expert-set constants, not corpus-size-derived — ADR 0119).

Both are expected GREEN today: ADR 0119's slice-A fix already lives at HEAD (see the git log —
"score_pairs consults schema.matchable first"), so this pins that existing invariant against the
FIRST real edge-emitting connector's shapes, closing the same class of gap
``test_prop_matchable_gate.py``'s module docstring describes for Indicators (RED-until-fixed
there; here the fix already exists, so GREEN is correct and expected).
"""

from __future__ import annotations

import string

from followthemoney import model
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.resolution.splink_model import ScoredPair, score_pairs

_SETTINGS = settings(max_examples=75, deadline=None, suppress_health_check=[HealthCheck.too_slow])

_CLAIM_ROLE = "ransomware victim (claimed by group)"

# Independent oracle: UnknownLink really is non-matchable in the installed FtM model (never
# re-derived from the connector) — a collection-time assertion, loud if the FtM pin ever changes
# this and silently reopens the whole premise of this file.
assert model.schemata["UnknownLink"].matchable is False, (
    "FtM's UnknownLink schema is no longer matchable:false -- the whole premise of this file "
    "(edges can never fuzzy-fuse) has changed upstream; re-derive before trusting this pin"
)

# --------------------------------------------------------------------------------------------------
# Builders — shaped like ransomware_live's map() output (Company victim / Organization group /
# UnknownLink claim edge, subject=group/object=victim/role fixed).
# --------------------------------------------------------------------------------------------------


def _victim(entity_id: str, name: str) -> FtmEntity:
    return validate_or_raise(
        {"id": entity_id, "schema": "Company", "properties": {"name": [name]}, "datasets": ["t"]}
    )


def _group(entity_id: str, name: str) -> FtmEntity:
    return validate_or_raise(
        {
            "id": entity_id,
            "schema": "Organization",
            "properties": {"name": [name], "weakAlias": [name], "topics": ["crime.cyber"]},
            "datasets": ["t"],
        }
    )


def _claim_edge(entity_id: str, group_id: str, victim_id: str) -> FtmEntity:
    """An UnknownLink built the way the connector's map() does: subject=group, object=victim, a
    fixed allegation-grade role (spec §3.2) — never a fuzzy-matchable participant."""
    return validate_or_raise(
        {
            "id": entity_id,
            "schema": "UnknownLink",
            "properties": {"subject": [group_id], "object": [victim_id], "role": [_CLAIM_ROLE]},
            "datasets": ["t"],
        }
    )


# --------------------------------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------------------------------

_ID_ALPHABET = string.ascii_lowercase + string.digits
_NAME = st.text(alphabet=string.ascii_letters, min_size=2, max_size=20)


def _unique_ids(prefix: str, min_size: int, max_size: int) -> st.SearchStrategy[list[str]]:
    return st.lists(
        st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=12).map(lambda s: f"{prefix}-{s}"),
        min_size=min_size,
        max_size=max_size,
        unique=True,
    )


@st.composite
def _matchable_subset(draw: st.DrawFn) -> list[FtmEntity]:
    """0-3 victim Company + 0-3 group Organization entities; the first two of the combined set
    share ONE name so a real candidate pair exists among the matchable subset (non-interference
    must hold on actual content, mirroring test_prop_matchable_gate.py's ``_matchable_corpus``)."""
    victim_ids = draw(_unique_ids("victim", 0, 3))
    group_ids = draw(_unique_ids("group", 0, 3))
    shared_name = draw(_NAME)
    combined: list[tuple[str, str]] = [("v", vid) for vid in victim_ids] + [
        ("g", gid) for gid in group_ids
    ]
    entities: list[FtmEntity] = []
    for idx, (kind, entity_id) in enumerate(combined):
        name = shared_name if idx < 2 else draw(_NAME)
        entities.append(_victim(entity_id, name) if kind == "v" else _group(entity_id, name))
    return entities


@st.composite
def _s4_corpus(draw: st.DrawFn) -> tuple[list[FtmEntity], list[FtmEntity]]:
    """A matchable subset (victim Companies + group Organizations) plus 1-3 UnknownLink edges,
    wired subject/object the connector's way. When the matchable subset is empty, subject/object
    are synthesized ids (still not present as nodes -- score_pairs never dereferences them, only
    filters by schema.matchable) so the generator ALWAYS produces >=1 edge (the generator
    contract every test below asserts)."""
    matchable = draw(_matchable_subset())
    group_ids = [e.id for e in matchable if e.schema.name == "Organization" and e.id is not None]
    victim_ids = [e.id for e in matchable if e.schema.name == "Company" and e.id is not None]

    n_edges = draw(st.integers(min_value=1, max_value=3))
    edges: list[FtmEntity] = []
    for i in range(n_edges):
        subject = draw(st.sampled_from(group_ids)) if group_ids else f"synth-group-{i}"
        obj = draw(st.sampled_from(victim_ids)) if victim_ids else f"synth-victim-{i}"
        edges.append(_claim_edge(f"edge-{i}", subject, obj))
    return matchable, edges


def _pair_set(pairs: list[ScoredPair]) -> set[tuple[str, str, float]]:
    return {(p.left_id, p.right_id, p.probability) for p in pairs}


# ---------------------------------------------------------------------------------------------
# test_p_s4_no_pair_references_an_unknownlink_edge
# ---------------------------------------------------------------------------------------------


@given(corpus=_s4_corpus())
@_SETTINGS
def test_p_s4_no_pair_references_an_unknownlink_edge(
    corpus: tuple[list[FtmEntity], list[FtmEntity]],
) -> None:
    matchable, edges = corpus
    assert len(edges) >= 1, "generator contract: >=1 UnknownLink edge in every corpus"
    edge_ids = {e.id for e in edges}
    matchable_ids = {e.id for e in matchable}

    pairs = score_pairs([*matchable, *edges])

    for pair in pairs:
        assert pair.left_id not in edge_ids and pair.right_id not in edge_ids, (
            f"pair ({pair.left_id!r}, {pair.right_id!r}, p={pair.probability!r}) references an "
            "UnknownLink edge id -- schema.matchable must be consulted BEFORE frame "
            f"construction. edge_ids={edge_ids!r}"
        )
        assert pair.left_id in matchable_ids and pair.right_id in matchable_ids, (
            f"pair ({pair.left_id!r}, {pair.right_id!r}) has an id outside the matchable "
            f"(Company/Organization) subset entirely: matchable_ids={matchable_ids!r}"
        )


# ---------------------------------------------------------------------------------------------
# test_p_s4_adding_edges_is_non_interfering
# ---------------------------------------------------------------------------------------------


@given(corpus=_s4_corpus())
@_SETTINGS
def test_p_s4_adding_edges_is_non_interfering(
    corpus: tuple[list[FtmEntity], list[FtmEntity]],
) -> None:
    matchable, edges = corpus
    base = score_pairs(matchable)
    combined = score_pairs([*matchable, *edges])

    base_set = _pair_set(base)
    combined_set = _pair_set(combined)
    assert base_set == combined_set, (
        "adding a disjoint UnknownLink edge set perturbed the pairs scored among the "
        f"matchable-only corpus. only-in-combined={combined_set - base_set!r} "
        f"only-in-base={base_set - combined_set!r}"
    )
