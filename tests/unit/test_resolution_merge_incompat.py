"""H-2 (ADR 0041, gate B-6 slice-1) — schema-incompatible members of a TRANSITIVE
cluster are isolated, never silently swallowed into a wrong-schema merge.

Splink (``score_pairs``) scores A~B and B~C separately and NEVER compares A~C, so its
schema-compat gate cannot stop a transitive cluster being gathered: ``Person p1 ~ Person p2``
(compatible) and ``Person p2 ~ Company c1`` (incompatible) collapse into one group. FtM
``merge()`` then raises ``InvalidData`` ("No common schema") on the cross-schema member, and
the pre-fix ``_merge_entities`` only logged + dropped it while ``cluster_and_merge`` kept the
FULL ``member_ids`` — so the dropped id stayed in the merge's ``member_ids``, was audited as a
source, and was referent-rewired onto the WRONG-schema canonical node.

These unit tests drive ``cluster_and_merge`` directly (pure — no Splink, no DB), forcing the
transitive cluster through ``StoredJudgement`` positives (``score_pairs`` alone cannot assemble
it, since it never compares p1 vs c1), mirroring the audit's runtime reproduction. They assert
INV-1 (no merge contains incompatible schemas / the dropped id absent from every merge), INV-2
(each dropped member re-emitted as its own correct-schema singleton with ``merge_incompatible``
True; an ordinary cluster keeps it False), INV-3 (``build_referent_map`` maps the dropped id to
ITSELF), and INV-5 (the kept cluster's ``canonical_id`` is the content-address of the ACTUAL
kept set, stable across a re-run — ADR 0036).

These MUST FAIL on the pre-fix tree: ``ResolvedCluster`` has no ``merge_incompatible`` field and
``cluster_and_merge`` does not re-emit singletons (the dropped id stays in the merge).
"""

from __future__ import annotations

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import (
    ResolvedCluster,
    StoredJudgement,
    _canonical_id,
    cluster_and_merge,
)
from worldmonitor.resolution.referents import build_referent_map, rewrite_referents

# ids chosen so ``sorted(member_ids)[0]`` is a PERSON ('a1' < 'a2' < 'z1'): the Person is the
# FtM merge base, a1~a2 merge (KEPT), and the Company z1 is the lone schema-incompatible drop.
# This yields a deterministic, two-way outcome (one kept Person merge + one dropped Company
# singleton) rather than the symmetric Company-base case (all three become singletons).
_KEPT_IDS = ("a1", "a2")
_DROPPED_ID = "z1"
_ALL_IDS = ("a1", "a2", "z1")


def _person(entity_id: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Person",
            "properties": {"name": ["Ivan Petrov"], "nationality": ["ru"]},
            "datasets": ["t"],
        }
    )


def _company(entity_id: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": ["Petrov Holdings Ltd"], "jurisdiction": ["cy"]},
            "datasets": ["t"],
        }
    )


def _transitive_cluster() -> list[ResolvedCluster]:
    """Force the transitive Person~Person~Company cluster via StoredJudgement positives.

    p1~p2 (Person~Person, compatible) AND p2~c1 (Person~Company, incompatible). Splink's
    ``score_pairs`` could never assemble this (it never compares p1 vs c1), so positives are
    seeded directly — equivalent to A~B and B~C scored separately, A~C never compared.
    """
    entities = [_person("a1"), _person("a2"), _company("z1")]
    judgements = [
        StoredJudgement("a1", "a2", "positive"),
        StoredJudgement("a2", "z1", "positive"),
    ]
    return cluster_and_merge(entities, [], judgements=judgements)


def _merge_clusters(clusters: list[ResolvedCluster]) -> list[ResolvedCluster]:
    return [c for c in clusters if c.is_merge]


def _singleton_for(clusters: list[ResolvedCluster], member_id: str) -> ResolvedCluster:
    matches = [c for c in clusters if c.member_ids == (member_id,)]
    assert len(matches) == 1, f"expected exactly one singleton for {member_id!r}, got {matches}"
    return matches[0]


# --------------------------------------------------------------------------- #
# INV-1 — no merge contains incompatible schemas; the dropped id absent from all merges
# --------------------------------------------------------------------------- #
def test_dropped_member_absent_from_every_merge() -> None:
    """INV-1: the schema-incompatible member must not appear in ANY multi-member merge."""
    clusters = _transitive_cluster()
    merges = _merge_clusters(clusters)
    # Pre-fix: the single merge has member_ids == ('a1','a2','z1') with z1 (Company) dropped
    # from the merged entity but STILL in member_ids — this assertion catches that bug.
    for merge in merges:
        assert _DROPPED_ID not in merge.member_ids, (
            f"the schema-incompatible member {_DROPPED_ID!r} must not remain in a merge's "
            f"member_ids; found {merge.member_ids}"
        )

    # Every merge must be schema-homogeneous: the merged entity's schema is the schema of
    # every one of its surviving members (no Person swallowed into a Company merge, or vice
    # versa). Build a schema lookup from the original entities.
    schema_by_id = {
        "a1": "Person",
        "a2": "Person",
        "z1": "Company",
    }
    for merge in merges:
        member_schemas = {schema_by_id[m] for m in merge.member_ids}
        assert len(member_schemas) == 1, (
            f"merge {merge.canonical_id} spans incompatible schemas {member_schemas}"
        )
        assert merge.entity.schema.name in member_schemas


# --------------------------------------------------------------------------- #
# INV-2 — each dropped member re-emitted as its OWN correct-schema singleton
# --------------------------------------------------------------------------- #
def test_dropped_member_reemitted_as_correct_schema_singleton() -> None:
    """INV-2: z1 becomes its OWN Company singleton (canonical_id==id, score 1.0, flag True)."""
    clusters = _transitive_cluster()
    singleton = _singleton_for(clusters, _DROPPED_ID)

    assert singleton.canonical_id == _DROPPED_ID, "a singleton keeps its own id"
    assert singleton.member_ids == (_DROPPED_ID,)
    assert singleton.entity.schema.name == "Company", (
        "the dropped member keeps its OWN correct schema, not the merge base's schema"
    )
    assert singleton.score == 1.0
    assert singleton.merge_incompatible is True, (
        "a singleton re-emitted because it was schema-incompatible with its transitive "
        "cluster must be flagged merge_incompatible=True"
    )


def test_ordinary_cluster_is_not_flagged_incompatible() -> None:
    """INV-2: the genuinely-merged (kept) cluster keeps merge_incompatible False."""
    clusters = _transitive_cluster()
    kept = next(c for c in clusters if set(c.member_ids) == set(_KEPT_IDS))
    assert kept.is_merge, "a1~a2 must still merge into one cluster"
    assert kept.entity.schema.name == "Person"
    assert kept.merge_incompatible is False, (
        "an ordinary (non-dropped) cluster must NOT be flagged merge_incompatible"
    )


def test_clean_cluster_without_a_drop_is_not_flagged() -> None:
    """INV-2 (no false positives): a clean homogeneous merge sets merge_incompatible False."""
    entities = [_person("a1"), _person("a2")]
    clusters = cluster_and_merge(entities, [], judgements=[StoredJudgement("a1", "a2", "positive")])
    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"a1", "a2"}
    assert clusters[0].merge_incompatible is False


# --------------------------------------------------------------------------- #
# INV-3 — build_referent_map maps the dropped id to ITSELF (id->id no-op)
# --------------------------------------------------------------------------- #
def test_referent_map_for_dropped_member_is_a_noop() -> None:
    """INV-3: z1 must map to z1, NEVER to the (wrong-schema) Person canonical id."""
    clusters = _transitive_cluster()
    referents = build_referent_map(clusters)

    assert referents[_DROPPED_ID] == _DROPPED_ID, (
        f"the dropped member {_DROPPED_ID!r} must map to itself; pre-fix it maps to the "
        f"wrong-schema canonical id {referents.get(_DROPPED_ID)!r}"
    )
    # The kept members still redirect onto the kept Person canonical.
    kept = next(c for c in clusters if set(c.member_ids) == set(_KEPT_IDS))
    for member_id in _KEPT_IDS:
        assert referents[member_id] == kept.canonical_id


def test_edge_naming_dropped_member_stays_on_its_own_node() -> None:
    """INV-3: rewrite_referents leaves an edge endpoint naming z1 on z1's own node.

    A Directorship.director pointing at z1 must NOT be redirected onto the Person canonical
    (which would create a wrong-schema dangling edge). The kept members ARE redirected.
    """
    clusters = _transitive_cluster()
    referents = build_referent_map(clusters)
    kept = next(c for c in clusters if set(c.member_ids) == set(_KEPT_IDS))

    edge = make_entity(
        {
            "id": "dir-1",
            "schema": "Directorship",
            "properties": {"director": ["a1"], "organization": [_DROPPED_ID]},
        }
    )
    rewrite_referents(edge, referents)
    assert edge.get("organization") == [_DROPPED_ID], (
        "an edge naming the dropped member must stay on the dropped member's own node"
    )
    assert edge.get("director") == [kept.canonical_id], (
        "a kept member is still redirected onto the kept canonical id"
    )


# --------------------------------------------------------------------------- #
# INV-5 — kept canonical id is the content-address of the ACTUAL kept set, and stable
# --------------------------------------------------------------------------- #
def test_kept_cluster_canonical_id_is_rederived_from_the_kept_set() -> None:
    """INV-5: kept.canonical_id == _canonical_id(kept), NOT _canonical_id(pre-drop set)."""
    clusters = _transitive_cluster()
    kept = next(c for c in clusters if set(c.member_ids) == set(_KEPT_IDS))

    expected = _canonical_id(_KEPT_IDS)
    pre_drop = _canonical_id(_ALL_IDS)
    assert expected != pre_drop, "sanity: the two member sets hash to different ids"
    assert kept.canonical_id == expected, (
        "the kept cluster's canonical id must be re-derived from the ACTUAL merged set, "
        "so a crash+retry converges (ADR 0036)"
    )
    assert kept.canonical_id != pre_drop, (
        "pre-fix the canonical id is derived from the pre-drop set (a1,a2,z1) — that id is "
        "non-convergent because z1 is never actually merged"
    )


def test_rerun_is_deterministic() -> None:
    """INV-5: re-running the same batch yields the same kept canonical id and singleton ids."""
    first = _transitive_cluster()
    second = _transitive_cluster()

    def _by_members(clusters: list[ResolvedCluster]) -> dict[tuple[str, ...], str]:
        return {c.member_ids: c.canonical_id for c in clusters}

    assert _by_members(first) == _by_members(second), (
        "re-resolving the same member set must re-derive the SAME canonical/singleton ids "
        "(ADR 0036 determinism)"
    )
    # The dropped singleton id is its own id on every run.
    assert _singleton_for(first, _DROPPED_ID).canonical_id == _DROPPED_ID
    assert _singleton_for(second, _DROPPED_ID).canonical_id == _DROPPED_ID
