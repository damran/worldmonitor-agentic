"""Gate E (slice-2) — T5: Stage-2 k-hop graph sensitivity (the GRAPH half of fail-closed).

Spec: ``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md`` §3.3 (Stage 2 k-hop + ``:Ghost`` exclusion),
§4 (the ``needs_review(..., *, neo4j=)`` thread), §7 A5, §8 T5, DENY E-GHOST / E-CYPHER /
E-GRAPHONLY. ADR: ``docs/decisions/0047-fail-closed-sensitivity-guard.md`` Decision 3.2 + 4.

These are the PRIMARY invariant tests for the graph half of the guard. They are written FROM the
spec, INDEPENDENT of the implementation, and pin OUTCOMES at ``needs_review``'s public contract with
a real Neo4j handle threaded in — never "no exception". They close the fail-open slice-1 could NOT
close: an entity that carries NO risk topic of its own but is structurally ADJACENT to a risk node
(an edge-less ``Sanction`` pointing at it, an intermediary between two sanctioned parties).

Why these are RED on the current tree (slice-1's ``guard/sensitivity.py`` ``needs_review`` accepts a
keyword-only ``neo4j`` handle but IGNORES it — Stage 2 is a no-op stub): a cluster member that is
risk-ADJACENT but topic-clean (``near-1`` below) is not caught by Stage 1 (no risk topic of its own)
and Stage 2 does nothing, so ``needs_review`` returns ``(False, "")`` — exactly the edge-adjacent
fail-open. POST-FIX, the Stage-2 k-hop MATCHes the member's durable id and flags the cluster iff a
risk-labelled node (a ``registry.topic.RISKS``-derived PascalCase label — VERIFIED_API.md "Gate E")
lies within ``k`` hops, with ``:Ghost`` HARD-excluded (never a sensitivity signal, never bridged
through — ADR 0046 / Decision 4).

Faithful fixtures (VERIFIED_API.md "Gate E" k-hop label-casing record): ftmg encodes a topic as a
PascalCase node label (``config.nodes.topics[code].label`` — e.g. ``sanction → Sanction``,
``crime.war → CrimeWar``, ``role.rca → RoleRca``); ``gds.py:27`` keys ``is_sanctioned`` off the
``"Sanction"`` label. The risk node here is seeded with that exact label set, and the topic-clean
neighbour carries NO ``topics`` so Stage 1 provably does not catch it — the flag, post-fix, can ONLY
come from Stage 2. The member's durable id is the thing the query MATCHes (passed as a ``$param``).

Stage-2 ORDERING (VERIFIED_API.md): ``needs_review`` runs at ``pipeline.py:357`` BEFORE
``write_entities`` (``pipeline.py:466``), so the k-hop reads PRIOR-batch nodes; a cluster member id
that is not yet a node ⇒ ``count == 0`` ⇒ a clean no-flag (NOT an error) — pinned by T5d.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from followthemoney.types import registry

from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import ResolvedCluster, cluster_and_merge
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import score_pairs
from worldmonitor.settings import get_settings

pytestmark = pytest.mark.integration


# VERIFIED_API.md "Gate E" — ftmg encodes a topic as a PascalCase node label derived from the
# dot-split FtM code; verified 0 mismatches over all 28 registry.topic.RISKS codes (incl. the 3-part
# export.control.linked -> ExportControlLinked). The k-hop Cypher must MATCH these label strings.
# Derived here (not hardcoded) so the oracle auto-tracks the FtM/ftmg pin in VERIFIED_API.md.
def _risk_label(code: str) -> str:
    return "".join(part.capitalize() for part in code.split("."))


# A couple of the verified labels used as fixtures. "Sanction" is gds.py:27's is_sanctioned label.
SANCTION_LABEL = _risk_label("sanction")  # "Sanction"
CRIMEWAR_LABEL = _risk_label("crime.war")  # "CrimeWar" — one of the 18 legacy-missed codes


def test_verify_risk_label_casing_matches_spec() -> None:
    """Guardrail on the ORACLE's fixtures: the seeded risk-node labels match VERIFIED_API.md.

    Pins the k-hop label-casing record (VERIFIED_API.md "Gate E"): every registry.topic.RISKS code
    maps to a PascalCase label, ``sanction -> Sanction`` (== gds.py:27's ``is_sanctioned`` label),
    ``crime.war -> CrimeWar``. If a FtM/ftmg bump changes the casing this fails loudly so the
    fixture labels are re-verified — it does NOT read the impl, only the installed FtM registry.
    """
    assert len(registry.topic.RISKS) == 28
    assert SANCTION_LABEL == "Sanction"
    assert CRIMEWAR_LABEL == "CrimeWar"
    assert "crime.war" in registry.topic.RISKS  # the seeded risk label is a real RISKS code


@pytest.fixture
def khop_depth(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """Set ``sensitivity_khop_depth`` for the Stage-2 traversal and clear the settings cache.

    The guard reads the hop depth from ``settings.sensitivity_khop_depth`` (ADR 0047 Decision 6,
    default 1). This fixture sets the env var to ``request.param`` and clears the cached
    ``get_settings`` so the guard picks the new depth up, restoring the cache afterwards. Tests that
    need a non-default depth request it via ``@pytest.mark.parametrize(..., indirect=...)``.
    """
    depth = getattr(request, "param", 1)
    monkeypatch.setenv("SENSITIVITY_KHOP_DEPTH", str(depth))
    get_settings.cache_clear()
    yield depth
    get_settings.cache_clear()


def _person(entity_id: str, *, topics: list[str] | None = None) -> FtmEntity:
    """A Person fixture; two identical (name+nationality+dob) ones cluster as a merge."""
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


def _merge_pair(member_id: str) -> ResolvedCluster:
    """Cluster ``member_id`` with a topic-clean duplicate through the real score/cluster path.

    Returns a genuine ``.is_merge`` cluster whose member set contains ``member_id`` (the id the
    Stage-2 k-hop MATCHes in the graph). Asserts the merge actually formed so a skipped singleton
    cannot make the test vacuously RED/GREEN. The DUPLICATE id is suffixed so it never collides with
    a seeded graph node — only ``member_id`` is meant to be graph-resolvable.
    """
    primary = _person(member_id)
    twin = _person(f"{member_id}-dup")
    clusters = cluster_and_merge([primary, twin], score_pairs([primary, twin]))
    merges = [c for c in clusters if c.is_merge]
    assert len(merges) == 1, "the two identical records must cluster into one merge"
    assert member_id in merges[0].member_ids, "the graph-resolvable id must be a cluster member"
    return merges[0]


def _by_id(cluster: ResolvedCluster) -> dict[str, FtmEntity]:
    """Build the ``by_id`` map the cluster's members are read from (all topic-clean here).

    Every member carries NO ``topics`` so Stage 1 provably cannot flag the cluster — any flag in
    these tests is, by construction, Stage 2 (or, in T5b/T5c/T5d, the absence of a Stage-2 flag).
    """
    return {mid: _person(mid) for mid in cluster.member_ids}


# --------------------------------------------------------------------------------------------
# T5a — k-hop flags a risk-ADJACENT, topic-CLEAN member (the slice-2 crux).
# --------------------------------------------------------------------------------------------


def test_t5a_khop_flags_risk_adjacent_topic_clean_member(
    clean_graph: Neo4jClient, khop_depth: int
) -> None:
    """T5a: a member ``near-1`` that carries NO risk topic but sits ONE hop from a risk-labelled
    node ``risk-1`` is flagged by Stage 2 — the edge-adjacent fail-open slice-1 could NOT close.

    Graph (faithful to the writer: PascalCase topic label + ``:Entity`` base + an edge):
        ``(:Entity:Person:Sanction {id:"risk-1"})  --[:LINKED]-->  (:Entity:Person {id:"near-1"})``
    ``near-1`` carries NO ``topics`` (Stage 1 provably cannot catch it); ``risk-1`` carries the
    verified ``:Sanction`` risk label (== gds.py:27's ``is_sanctioned`` label). The merge cluster's
    member ``near-1`` exists in the graph within ``k=1`` of ``risk-1`` (the default depth).

    PRE-FIX (Stage 2 ignores ``neo4j``): ``near-1`` has no risk topic so Stage 1 returns clean, and
    the no-op Stage-2 stub never reads the graph ⇒ ``needs_review`` returns ``(False, "")``.
    **FAILS.** POST-FIX: the k-hop MATCHes ``near-1`` (a ``$param``), traverses ``[*1..k]`` (``k``
    inlined as a validated int), finds the ``:Sanction``-labelled neighbour, and flags with a
    DISTINCT k-hop reason (VERIFIED_API.md: the Stage-2 reason is distinct from the topic reason).
    """
    assert khop_depth == 1, "T5a exercises the DEFAULT depth (k=1) — a direct one-hop neighbour"
    # Seed PRIOR-batch graph state: a risk node one hop from a topic-clean node.
    clean_graph.execute_write(
        "CREATE (risk:Entity:Person:Sanction {id: $risk}) "
        "CREATE (near:Entity:Person {id: $near}) "
        "CREATE (risk)-[:LINKED]->(near)",
        risk="risk-1",
        near="near-1",
    )
    # Sanity-pin the fixture: near-1 has NO risk label, risk-1 IS Sanction-labelled and adjacent.
    near_labels = clean_graph.execute_read("MATCH (n {id: 'near-1'}) RETURN labels(n) AS labels")[
        0
    ]["labels"]
    assert SANCTION_LABEL not in near_labels and CRIMEWAR_LABEL not in near_labels, (
        "near-1 must carry NO risk label, so Stage 1 cannot catch it — the flag can only be Stage 2"
    )
    adjacent = clean_graph.execute_read(
        "MATCH (:Sanction {id: 'risk-1'})-[]-(:Person {id: 'near-1'}) RETURN count(*) AS n"
    )[0]["n"]
    assert adjacent == 1, "fixture: risk-1 (Sanction) must be exactly one hop from near-1"

    merge = _merge_pair("near-1")  # member set = {near-1, near-1-dup}, both topic-clean
    by_id = _by_id(merge)

    flagged, reason = needs_review(merge, by_id, neo4j=clean_graph)
    assert flagged is True, (
        "a topic-clean member ONE hop from a risk node must be flagged by Stage 2 k-hop — this is "
        "the edge-adjacent fail-open slice-1 could not close (spec §3.3 / A5)"
    )
    assert reason, "a Stage-2 park must carry a human-readable k-hop reason"
    assert "sensitive (PEP/sanctioned) entity" not in reason, (
        "the Stage-2 reason must be DISTINCT from the Stage-1 topic reason (VERIFIED_API.md) — "
        "near-1 has no topic of its own; the flag is structural proximity, not a topic"
    )


# --------------------------------------------------------------------------------------------
# T5b — a :Ghost neighbour does NOT flag (adversarial — DENY E-GHOST).
# --------------------------------------------------------------------------------------------


def test_t5b_ghost_neighbour_does_not_flag(clean_graph: Neo4jClient, khop_depth: int) -> None:
    """T5b: a member ``near-2`` adjacent ONLY to a ``:Ghost`` node does NOT get flagged.

    Graph (same shape as T5a but the neighbour is a Gate-D ``:Ghost`` dangling endpoint — a
    never-ingested, structurally-inert traversal target, ADR 0046):
        ``(:Ghost {id:"ghost-1"})  --[:LINKED]-->  (:Entity:Person {id:"near-2"})``
    A ``:Ghost`` MUST NEVER count as a sensitivity signal (HARD INV, §3.3 ``AND NOT n:Ghost``); the
    Stage-2 traversal excludes ghosts. ``near-2`` carries no topic, so neither Stage 1 nor a correct
    Stage 2 may flag it.

    This is the adversarial guard: a Stage-2 implementation that forgot the ghost exclusion would
    flag ``near-2`` here. Asserting NOT-flagged pins the ghost exclusion. (Pre-fix this is trivially
    not-flagged because Stage 2 is a no-op; POST-FIX it must STAY not-flagged — the discriminator
    against an over-broad k-hop. DENY E-GHOST if a ghost neighbour flags a cluster.)
    """
    assert khop_depth == 1
    clean_graph.execute_write(
        "CREATE (ghost:Ghost {id: $ghost}) "
        "CREATE (near:Entity:Person {id: $near}) "
        "CREATE (ghost)-[:LINKED]->(near)",
        ghost="ghost-1",
        near="near-2",
    )
    labels = clean_graph.execute_read("MATCH (g {id: 'ghost-1'}) RETURN labels(g) AS labels")[0][
        "labels"
    ]
    assert "Ghost" in labels, "fixture: ghost-1 must be a :Ghost node (structurally inert)"

    merge = _merge_pair("near-2")
    by_id = _by_id(merge)

    flagged, reason = needs_review(merge, by_id, neo4j=clean_graph)
    assert flagged is False, (
        "a member adjacent ONLY to a :Ghost must NOT be flagged — a ghost is never a sensitivity "
        "signal (HARD INV, ADR 0046 / spec §3.3 `AND NOT n:Ghost`); DENY E-GHOST"
    )
    assert reason == "", "an unflagged cluster carries no reason"


# --------------------------------------------------------------------------------------------
# T5c — no risk-bridge THROUGH a :Ghost (DENY E-GHOST: a ghost must not be a risk-bridge).
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("khop_depth", [2], indirect=True)
def test_t5c_no_risk_bridge_through_ghost(clean_graph: Neo4jClient, khop_depth: int) -> None:
    """T5c: a real risk node reachable from the member ONLY via a path THROUGH a ``:Ghost`` must
    NOT flag — the ghost must not be a risk-bridge (spec §3.3: terminate-at, never through).

    Graph (the risk node is 2 hops away, but the only path runs through a ghost):
        ``(:Entity:Person {id:"near-3"})  -->  (:Ghost {id:"ghost-2"})  -->
          (:Entity:Person:Sanction {id:"risk-3"})``
    Run at ``sensitivity_khop_depth=2`` (indirect fixture) so the risk node IS within range by hop
    count — a naive k-hop that did not exclude ghosts on EVERY matched node along the path WOULD
    reach ``risk-3`` and flag ``near-3``. A correct Stage 2 excludes the ghost (``AND NOT n:Ghost``
    on every traversed node, terminate-at not through), so ``risk-3`` is unreachable and ``near-3``
    is NOT flagged. ``near-3``'s only non-ghost neighbourhood within ``k`` carries no risk.

    The depth-2 setting is what makes this assertion LOAD-BEARING (at the default k=1 a 2-hop risk
    node is out of range for any implementation, so the not-flag would not prove the ghost block).
    DENY E-GHOST if a path bridges through a ghost to flag the cluster.
    """
    assert khop_depth == 2, "T5c needs depth 2 so the ghost-bridged risk node is in hop range"
    clean_graph.execute_write(
        "CREATE (near:Entity:Person {id: $near}) "
        "CREATE (ghost:Ghost {id: $ghost}) "
        "CREATE (risk:Entity:Person:Sanction {id: $risk}) "
        "CREATE (near)-[:LINKED]->(ghost) "
        "CREATE (ghost)-[:LINKED]->(risk)",
        near="near-3",
        ghost="ghost-2",
        risk="risk-3",
    )
    # Fixture pin (1): the risk node IS reachable within 2 hops if the ghost is traversed —
    # so a ghost-bridging k-hop at depth 2 WOULD flag (this is what must NOT happen).
    reachable_via_ghost = clean_graph.execute_read(
        "MATCH (:Person {id:'near-3'})-[*1..2]-(:Sanction {id:'risk-3'}) RETURN count(*) AS n"
    )[0]["n"]
    assert reachable_via_ghost >= 1, (
        "fixture: risk-3 is within 2 hops of near-3 (through the ghost)"
    )
    # Fixture pin (2): EVERY such path runs through the ghost — there is no ghost-free bridge.
    ghost_free = clean_graph.execute_read(
        "MATCH p = (:Person {id:'near-3'})-[*1..2]-(:Sanction {id:'risk-3'}) "
        "WHERE NONE(n IN nodes(p) WHERE n:Ghost) RETURN count(p) AS n"
    )[0]["n"]
    assert ghost_free == 0, "fixture: every near-3 -> risk-3 path runs THROUGH the ghost"

    merge = _merge_pair("near-3")
    by_id = _by_id(merge)

    flagged, reason = needs_review(merge, by_id, neo4j=clean_graph)
    assert flagged is False, (
        "a risk node reachable ONLY through a :Ghost (at depth 2) must NOT flag — a ghost is not a "
        "risk-bridge (spec §3.3 terminate-at, never traverse-through); DENY E-GHOST"
    )
    assert reason == "", "no bridge through a ghost ⇒ no flag, no reason"


# --------------------------------------------------------------------------------------------
# T5d — a member not yet in the graph ⇒ clean no-flag (k-hop ordering, NOT an error).
# --------------------------------------------------------------------------------------------


def test_t5d_member_not_in_graph_is_clean_no_flag(
    clean_graph: Neo4jClient, khop_depth: int
) -> None:
    """T5d: a cluster whose member id is NOT a node in the graph returns ``flagged=False`` — a clean
    no-op, no error, no exception.

    ``needs_review`` runs BEFORE ``write_entities`` (VERIFIED_API.md "Gate E" k-hop ordering), so a
    member of the CURRENT batch is not yet a node; the k-hop MATCH finds nothing ⇒ ``count == 0`` ⇒
    no flag. The graph is wiped clean (``clean_graph``) and seeded with an UNRELATED risk node that
    is NOT connected to the cluster's member, so the only correct outcome is not-flagged.

    Pins the ordering contract: a not-yet-written member must be a clean Stage-2 no-op, never an
    error path. (Pre- and post-fix both not-flagged; this is the regression-pin that the k-hop never
    raises / never flags on an absent member.)
    """
    assert khop_depth == 1
    # An unrelated risk node exists from a prior batch — but the cluster member is NOT in the graph.
    clean_graph.execute_write("CREATE (:Entity:Person:Sanction {id: $risk})", risk="unrelated-risk")
    absent = clean_graph.execute_read("MATCH (n {id: 'absent-1'}) RETURN count(n) AS n")[0]["n"]
    assert absent == 0, "fixture: the cluster member must NOT yet be a node in the graph"

    merge = _merge_pair("absent-1")  # member set {absent-1, absent-1-dup}, neither in the graph
    by_id = _by_id(merge)

    flagged, reason = needs_review(merge, by_id, neo4j=clean_graph)
    assert flagged is False, (
        "a cluster whose member is not yet a graph node must be a clean Stage-2 no-op (count==0), "
        "not an error and not a flag — k-hop runs before write_entities (VERIFIED_API.md)"
    )
    assert reason == "", "an absent member yields no k-hop reason"


# --------------------------------------------------------------------------------------------
# T5e (slice-3, adapted) — the STRUCTURED probe's k-hop branch (replaces reason-marker coupling).
# --------------------------------------------------------------------------------------------


def test_t5e_has_nonexemptible_sensitivity_khop_branch(
    clean_graph: Neo4jClient, khop_depth: int
) -> None:
    """``guard.sensitivity.has_nonexemptible_sensitivity(merge, by_id, neo4j=clean_graph)`` is True
    for the T5a risk-adjacent cluster and False when no risk node is seeded.

    Spec §15.2/§16 (the slice-3 structured probe). slice-3 deletes ``is_nonexemptible_reason`` (and
    the marker constants) — the reason-string coupling this test used to pin — and replaces it with
    the structured probe, which evaluates the Stage-2 k-hop adjacency INDEPENDENTLY of
    ``needs_review``'s first-flag short-circuit and of the returned reason string (closing the
    masking fail-open, Finding B). This test exercises the probe's k-hop branch directly, which the
    pure unit file (``tests/unit/test_exemption_fence.py``) cannot (it has no graph).

    True case: ``near-1`` is topic-clean (no newly-broadened axis), the band is OFF (no Chow axis),
    so the probe's True provably comes from the k-hop branch — ``near-1`` one hop from the seeded
    ``:Sanction`` node ``risk-1``. False case: a topic-clean cluster whose members are NOT in the
    graph has no risk neighbour, no risk topic, and an out-of-band score ⇒ the probe is False (the
    "no wider" direction — the structured probe does not over-flag a benign cluster).

    RED pre-fix: ``has_nonexemptible_sensitivity`` does not exist on slice-2 (ImportError). DENY
    E-MASK if the probe's k-hop branch is absent.
    """
    from worldmonitor.guard.sensitivity import has_nonexemptible_sensitivity

    assert khop_depth == 1
    clean_graph.execute_write(
        "CREATE (risk:Entity:Person:Sanction {id: $risk}) "
        "CREATE (near:Entity:Person {id: $near}) "
        "CREATE (risk)-[:LINKED]->(near)",
        risk="risk-1",
        near="near-1",
    )
    # Non-vacuity: confirm the merge formed AND near-1 flags via Stage-2 k-hop (structural).
    merge = _merge_pair("near-1")
    by_id = _by_id(merge)
    flagged, reason = needs_review(merge, by_id, neo4j=clean_graph)
    assert flagged is True and "sensitive (PEP/sanctioned)" not in reason, (
        "fixture: near-1 must flag via Stage-2 k-hop (structural, not a topic) — else vacuous"
    )

    assert has_nonexemptible_sensitivity(merge, by_id, neo4j=clean_graph) is True, (
        "a member within k hops of a non-ghost risk node is NON-exemptible — the structured probe "
        "must catch the k-hop signal a stale approval could not have considered (spec §15.2/§16; "
        "DENY E-MASK)"
    )

    # No risk node seeded for this cluster: not graph-resolvable, topic-clean, out-of-band ⇒ False.
    benign = _merge_pair("lonely-1")
    benign_by_id = _by_id(benign)
    assert has_nonexemptible_sensitivity(benign, benign_by_id, neo4j=clean_graph) is False, (
        "a topic-clean cluster with NO risk neighbour has no non-exemptible signal — the probe "
        "must not over-flag it (the 'no wider' direction; preserves the approve→promote path)"
    )
