"""Integration tests for Gate 3a-i — the fold engine (ADR 0100).

Exercises the full ``project()`` function end-to-end against real Postgres +
real Neo4j (testcontainers).  Reuses the ``_candidates()`` / ``_queue_item()``
seed shape from ``tests/integration/test_statement_spine.py`` (same fixture, same
source, same entities — so the direct-write and fold-reconstructed graphs can be
compared byte-for-byte).

Tests
-----
IT-PROJ-1  End-to-end: resolve_pending populates the log; wipe graph; project();
           every PROMOTED cluster canonical_id has a node carrying prov_* +
           prov_witnesses; the PARKED (sanctioned) cluster has NO node.
           Checkpoint invariant: a ProjectionCheckpoint row with last_statement_seq > 0
           must exist after project() (ADR 0100 D1 watermark invariant).

IT-PROJ-2  MANDATORY single-batch fold-vs-direct-write EQUIVALENCE (the correctness
           anchor, NOT optional):  on the _candidates() corpus (single batch, ONE
           shared source 'src:statement-spine-test', no wm_anchor_* → divergence
           classes E1/E2/E3 are all NULL) the projector's graph_signature equals the
           direct writer's EXACTLY.

           WHY E1/E2/E3 are null on this corpus:
           E1 (cross-batch referent): single batch, so the live writer's
             within-batch rewrite_referents and the projector's global survivor_of
             produce the same result — all referents resolved in one pass.
           E2 (anchors/enrichment): _candidates() entities carry no wikidataId,
             leiCode, registrationNumber, taxNumber, and no enricher runs in tests,
             so wm_anchor_* is absent on both sides.
           E3 (prov_* representative): _candidates() uses ONE source
             ('src:statement-spine-test'), so min(entity_id) picks the same
             representative that the direct merge path uses — they coincide.
           This is the null-divergence base case.  Cross-batch / enriched parity
           is 3a-ii's P-FOLD-2, NOT tested here.

IT-PROJ-3  Edge-bearing fixture: an Ownership edge between two Companies with one
           endpoint a merged-away source id → the projected edge exists between the
           SURVIVOR nodes and carries prov_* (G1 on the edge); the referent was
           globally rewritten via the ledger.

All tests are RED at collection time: the module-level imports of ``project``,
``ProjectionResult`` from ``worldmonitor.resolution.projector`` and
``ProjectionCheckpoint`` from ``worldmonitor.db.models`` fail with ``ImportError``
because those symbols do not exist until the builder creates them.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    ErQueueItem,
    MergeAudit,
    ProjectionCheckpoint,  # gate import: RED until builder adds it to models
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import ProjectionResult, project  # gate: RED until built

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# graph_signature — shared with test_prop_fold_engine.py (kept in sync manually)
# ---------------------------------------------------------------------------


def _stable_val(v: Any) -> str:
    """Stable string representation of a Neo4j property value (list-safe)."""
    if isinstance(v, list):
        return json.dumps(sorted(str(x) for x in v), ensure_ascii=False)
    return json.dumps(v, default=str, ensure_ascii=False, sort_keys=True)


def graph_signature(
    client: Neo4jClient,
    exclude_node_props: frozenset[str] = frozenset(),
) -> tuple[tuple, tuple]:
    """Byte-comparable canonical fingerprint of the full graph in ``client``.

    Captures every node (non-null id only) and every relationship with ALL
    properties including ``prov_*``, ``prov_witnesses``, and ``wm_anchor_*``.
    Sorting is applied at every level so the signature is permutation-stable.
    """
    node_rows = client.execute_read(
        "MATCH (n) WHERE n.id IS NOT NULL "
        "RETURN n.id AS nid, labels(n) AS lbls, properties(n) AS props "
        "ORDER BY n.id"
    )
    edge_rows = client.execute_read(
        "MATCH (a)-[r]->(b) "
        "RETURN type(r) AS rtype, a.id AS src, b.id AS dst, properties(r) AS rprops "
        "ORDER BY type(r), a.id, b.id"
    )

    node_sigs = tuple(
        sorted(
            (
                str(row["nid"]),
                tuple(sorted(str(lbl) for lbl in (row["lbls"] or []))),
                tuple(
                    sorted(
                        (_stable_val(k), _stable_val(v))
                        for k, v in (row["props"] or {}).items()
                        if k not in exclude_node_props
                    )
                ),
            )
            for row in node_rows
            if row["nid"] is not None
        )
    )

    edge_sigs = tuple(
        sorted(
            (
                str(row["rtype"] or ""),
                str(row["src"] or ""),
                str(row["dst"] or ""),
                tuple(
                    sorted(
                        (_stable_val(k), _stable_val(v)) for k, v in (row["rprops"] or {}).items()
                    )
                ),
            )
            for row in edge_rows
        )
    )

    return (node_sigs, edge_sigs)


# ---------------------------------------------------------------------------
# Seed helpers — mirror test_statement_spine.py exactly so the _candidates()
# corpus is a known baseline and the direct vs fold comparison is clean.
# ---------------------------------------------------------------------------


def _queue_item(entity: dict[str, object]) -> ErQueueItem:
    """Build a stamped ErQueueItem from a raw entity dict.

    Identical to _queue_item in test_statement_spine.py — same source_id, same
    reliability, same timestamp so the fold-reconstructed prov_* matches the
    direct-written prov_* byte-for-byte (the null-divergence base case, IT-PROJ-2).
    """
    source_record = f"s3://landing/{entity['id']}.json"
    stamped = stamp(
        make_entity(entity),
        Provenance(
            source_id="src:statement-spine-test",
            retrieved_at="2026-06-21T00:00:00Z",
            reliability="A",
            source_record=source_record,
        ),
    )
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity=stamped.to_dict(),
        source_record=source_record,
        status="pending",
    )


def _candidates() -> list[dict[str, object]]:
    """Canonical fixture: clear dup pair + distinct entity + sanctioned dup pair.

    Identical to _candidates() in test_statement_spine.py — SINGLE batch, ONE
    shared source 'src:statement-spine-test', NO wm_anchor_* on any entity.
    All three divergence classes E1/E2/E3 (ADR 0100 D2) are null on this corpus:
    E1 — single batch, referent rewrites are identical in both paths.
    E2 — no anchors/enrichment in the corpus; wm_anchor_* absent on both sides.
    E3 — single source, so min(entity_id) representative coincides with the
         direct merge path's representative.
    This makes IT-PROJ-2's fold == direct comparison byte-identical and clean.
    """
    return [
        {
            "id": "c1",
            "schema": "Company",
            "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "c2",
            "schema": "Company",
            "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "c3",
            "schema": "Company",
            "properties": {"name": ["Globex Incorporated"], "jurisdiction": ["gb"]},
            "datasets": ["t"],
        },
        {
            "id": "p1",
            "schema": "Person",
            "properties": {
                "name": ["Vladimir Example"],
                "nationality": ["ru"],
                "birthDate": ["1960-01-01"],
                "topics": ["sanction"],
            },
            "datasets": ["t"],
        },
        {
            "id": "p2",
            "schema": "Person",
            "properties": {
                "name": ["Vladimir Example"],
                "nationality": ["ru"],
                "birthDate": ["1960-01-01"],
            },
            "datasets": ["t"],
        },
    ]


# ===========================================================================
# IT-PROJ-1: End-to-end — promoted clusters have nodes; parked cluster has none
# ===========================================================================


def test_end_to_end_project_promoted_clusters_have_nodes(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """IT-PROJ-1: resolve_pending populates the log → wipe graph → project() →
    every promoted canonical has a node with prov_* + prov_witnesses; the PARKED
    (sanctioned p1+p2) cluster has NO node (it wrote no statements — ADR 0099 /
    pipeline 'continue' gate, proven by test_statement_spine.py IT-STMT-3).

    G1 invariants checked on every projected node:
    - prov_source_id is non-null
    - prov_witnesses is present (witness map serialised to JSON string)

    Checkpoint invariant (ADR 0100 D1):
    - A ProjectionCheckpoint row with last_statement_seq > 0 exists after project()
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    assert stats.promoted == 2, (
        f"IT-PROJ-1: expected 2 promoted clusters, got {stats.promoted} — "
        "regression against test_resolution_pipeline.py baseline"
    )
    assert stats.review == 1, f"IT-PROJ-1: expected 1 parked cluster, got {stats.review}"

    # Find parked canonical_id from the audit table
    with sessions() as session:
        parked_audit = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        parked_canonical_id = parked_audit.canonical_id

        promoted_audits = list(
            session.execute(select(MergeAudit).where(MergeAudit.decision == "merged")).scalars()
        )
    promoted_canonical_ids = [a.canonical_id for a in promoted_audits]

    # Wipe the live graph — projector is the only writer from here on
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    with sessions() as session:
        result = project(session, clean_graph, full_rebuild=True)

    assert isinstance(result, ProjectionResult), (
        f"IT-PROJ-1: project() must return a ProjectionResult, got {type(result).__name__!r}"
    )
    assert result.entities_written >= 2, (
        f"IT-PROJ-1: expected >= 2 entities written, got {result.entities_written}"
    )

    # --- Checkpoint invariant: ADR 0100 D1 — watermark must be persisted after fold ---
    with sessions() as session:
        checkpoints = list(session.execute(select(ProjectionCheckpoint)).scalars())
    assert len(checkpoints) >= 1, (
        "IT-PROJ-1: no ProjectionCheckpoint row found after project() — "
        "the projector MUST checkpoint its last_statement_seq watermark in the "
        "'projection_checkpoint' table (ADR 0100 D1: checkpoint-before-watermark-commit)."
    )
    assert checkpoints[0].last_statement_seq > 0, (
        f"IT-PROJ-1: ProjectionCheckpoint.last_statement_seq="
        f"{checkpoints[0].last_statement_seq} — must be > 0 after projecting a non-empty "
        "log (ADR 0100 D1: the watermark advances with each statement batch read)."
    )

    # --- Every promoted canonical has a node with G1 provenance ---
    for canonical_id in promoted_canonical_ids:
        rows = clean_graph.execute_read(
            "MATCH (n {id: $cid}) RETURN properties(n) AS props",
            cid=canonical_id,
        )
        assert len(rows) == 1, (
            f"IT-PROJ-1: promoted canonical_id={canonical_id!r} has {len(rows)} node(s) "
            "in the projected graph — expected exactly 1 (G1 node for every promoted cluster)"
        )
        node_props = rows[0]["props"] or {}

        assert node_props.get("prov_source_id"), (
            f"IT-PROJ-1: G1 VIOLATED — node for canonical_id={canonical_id!r} has no "
            "'prov_source_id'. Every projected node must carry prov_* (ADR 0100 D3 / ADR 0060)."
        )
        assert "prov_witnesses" in node_props, (
            f"IT-PROJ-1: Tier-1 WITNESS MAP MISSING — node for canonical_id={canonical_id!r} "
            "has no 'prov_witnesses'. The fold must reconstruct the witness map from statement "
            "rows and stamp it on every projected entity (ADR 0100 D3 / ADR 0045)."
        )

    # --- Parked (sanctioned) cluster has NO node ---
    parked_rows = clean_graph.execute_read(
        "MATCH (n {id: $cid}) RETURN count(n) AS cnt",
        cid=parked_canonical_id,
    )
    parked_count = int(parked_rows[0]["cnt"]) if parked_rows else 0
    assert parked_count == 0, (
        f"IT-PROJ-1: PARKED INVARIANT VIOLATED — parked canonical_id={parked_canonical_id!r} "
        f"has {parked_count} node(s) in the projected graph. A pending_review cluster wrote NO "
        "statement rows (ADR 0099 pipeline 'continue' gate), so the projector must produce NO "
        "node for it (person_affecting: no node = no data in the graph, ADR 0100)."
    )

    engine.dispose()


# ===========================================================================
# IT-PROJ-2: MANDATORY single-batch fold-vs-direct-write EQUIVALENCE
# ===========================================================================


def test_single_batch_fold_vs_direct_write_equivalence(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """IT-PROJ-2 (MANDATORY): fold graph == direct graph, byte-identical, on null-divergence corpus.

    On the _candidates() corpus (single batch, ONE shared source 'src:statement-spine-test',
    no wm_anchor_* → divergence classes E1/E2/E3 are all NULL) the projector's
    graph_signature must equal the direct writer's EXACTLY.

    WHY E1/E2/E3/E4 are either null or legitimately excluded on this corpus:
    - E1 (cross-batch referent resolution): SINGLE BATCH — the live per-batch
      rewrite_referents and the projector's global survivor_of apply the same
      referent map (all resolutions happen in one pass; no cross-batch dangling refs).
    - E2 (anchors / enrichment): _candidates() entities carry no canonical anchor
      properties (wikidataId, leiCode, registrationNumber, taxNumber); no enricher
      runs in tests; so wm_anchor_* is absent on BOTH sides of the comparison.
    - E3 (prov_* representative): ONE shared source_id ('src:statement-spine-test')
      so the direct merge path's prov_* representative (merge_order[0] = 'c1') and
      the projector's representative (min(entity_id) = 'c1') COINCIDE — same quad.
    - E4 (FtM 'datasets' label — connector ingest metadata): the raw entity's
      datasets field (e.g. ["t"]) is written by resolve_pending as a Neo4j
      node property, but it is NOT a per-claim statement in the log — the statement
      spine stores Provenance.source_id ("src:statement-spine-test") in the
      dataset column.  The fold therefore reconstructs datasets with the
      source_id, while the direct path carries the original FtM Dataset label.
      LEGITIMATELY EXCLUDED: datasets is not reconstructable from the log alone
      (analogous to E2 anchors).  The comparison uses
      exclude_node_props=frozenset({"datasets"}) for both sides; every OTHER node
      and edge property must still be byte-identical.

    This is the null-divergence BASE CASE (modulo E4).  Cross-batch / enriched corpus
    parity (P-FOLD-2) is 3a-ii's job, NOT tested here.

    Steps:
    1. resolve_pending writes the direct graph; capture direct = graph_signature(exclude datasets).
    2. Wipe graph.
    3. project(full_rebuild=True); capture fold = graph_signature(exclude datasets).
    4. Assert fold == direct (byte-identical on all properties except datasets).
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        for candidate in _candidates():
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    assert stats.promoted == 2, (
        f"IT-PROJ-2 pre-condition: expected 2 promoted clusters, got {stats.promoted}"
    )

    # (1) Capture the DIRECT graph (written by resolve_pending)
    # Exclude E4 ('datasets'): FtM Dataset label = connector ingest metadata, not in the
    # statement log; legitimately absent from the fold reconstruction (documented ADR 0100).
    _EXCL = frozenset({"datasets"})
    direct = graph_signature(clean_graph, exclude_node_props=_EXCL)

    assert len(direct[0]) >= 2, (
        f"IT-PROJ-2: direct graph has only {len(direct[0])} node(s) — "
        "expected at least 2 (the c1+c2 canonical and c3)"
    )

    # (2) Wipe the graph
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    # (3) project(full_rebuild=True) — reads the statement log, folds, writes
    with sessions() as session:
        result = project(session, clean_graph, full_rebuild=True)

    fold = graph_signature(clean_graph, exclude_node_props=_EXCL)

    # (4) BYTE-IDENTICAL assertion (excluding 'datasets' / E4) — the fold IS the direct
    # write on the null-divergence corpus for every reconstructable property
    assert fold == direct, (
        "IT-PROJ-2 MANDATORY EQUIVALENCE VIOLATED: fold graph != direct graph on the "
        "_candidates() null-divergence corpus.\n\n"
        "On a SINGLE BATCH with ONE shared source and NO anchors/enrichment, the fold "
        "engine MUST reproduce the direct-write graph byte-identically (ADR 0100 D2 / D3).\n\n"
        f"  direct: {len(direct[0])} nodes, {len(direct[1])} edges\n"
        f"  fold:   {len(fold[0])} nodes, {len(fold[1])} edges\n\n"
        "Divergences that should be ABSENT here:\n"
        "  E1 (cross-batch referent): absent — single batch\n"
        "  E2 (anchor/enrichment):    absent — no wm_anchor_* in corpus\n"
        "  E3 (prov_* representative): absent — one shared source, c1=min(entity_id)=base\n\n"
        "If fold != direct, the fold algorithm has a bug in value reconstruction, "
        "provenance stamping, or witness map derivation (ADR 0100 D3)."
    )

    # Sanity: ProjectionResult counts are consistent
    assert result.entities_written >= 2, (
        f"IT-PROJ-2: ProjectionResult.entities_written={result.entities_written} — "
        "must be >= 2 (c1+c2 canonical + c3 singleton)"
    )
    assert result.last_statement_seq > 0, (
        f"IT-PROJ-2: ProjectionResult.last_statement_seq={result.last_statement_seq} — "
        "must be > 0 (statements were read from the log)"
    )

    engine.dispose()


# ===========================================================================
# IT-PROJ-3: Edge-bearing fixture — G1 on edges, global referent rewrite
# ===========================================================================


def test_edge_bearing_ownership_global_referent_rewrite(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """IT-PROJ-3: an Ownership edge with a merged-away endpoint is globally rewritten.

    Sets up:
    - co1 + co2 (two Companies with same name) → merge into canonical co12
    - co3 (distinct Company, singleton) → canonical co3
    - own-A (Ownership: owner='co1', asset='co3') — owner is a merged-away SOURCE id

    After project(full_rebuild=True):
    - The Ownership relationship exists between 'co12' (the survivor of co1) and 'co3'.
    - The relationship carries prov_* (G1 on the edge: ADR 0055 / ADR 0100 D3).
    - No relationship endpoint references 'co1' (the merged-away id was globally rewritten).

    This verifies ADR 0100 D2 (global-fold-is-truth): the projector applies the GLOBAL
    referent rewrite (all entity-typed props via survivor_of over the full ledger),
    not just within-batch rewrites like the live writer's rewrite_referents.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    # Fixture: co1 + co2 merge candidate + co3 singleton + Ownership(owner=co1, asset=co3)
    candidates = [
        {
            "id": "co1",
            "schema": "Company",
            "properties": {"name": ["FoldEdge Corp"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "co2",
            "schema": "Company",
            "properties": {"name": ["FoldEdge Corp"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        },
        {
            "id": "co3",
            "schema": "Company",
            "properties": {"name": ["Asset Corp"], "jurisdiction": ["gb"]},
            "datasets": ["t"],
        },
        {
            "id": "own-A",
            "schema": "Ownership",
            "properties": {
                "owner": ["co1"],  # entity-typed; 'co1' is a merged-away SOURCE id
                "asset": ["co3"],  # entity-typed; 'co3' is a valid canonical id (singleton)
            },
            "datasets": ["t"],
        },
    ]

    with sessions() as session:
        for candidate in candidates:
            session.add(_queue_item(candidate))
        session.commit()

    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")

    # Find co1+co2's canonical_id from the merge audit (they should merge)
    with sessions() as session:
        all_audits = list(
            session.execute(select(MergeAudit).where(MergeAudit.decision == "merged")).scalars()
        )

    # Identify the canonical_id for the co1+co2 cluster (source_ids contains both co1 and co2)
    co12_audit = next(
        (a for a in all_audits if "co1" in a.source_ids and "co2" in a.source_ids),
        None,
    )
    # co1+co2 (same name + same jurisdiction) MUST merge: the Splink score is deterministic
    # for identical name/jurisdiction, so a non-merge is a real fold/pipeline regression, not a
    # flake.  Skipping would self-nullify the edge-rewrite + edge-G1 oracle.
    # PRE-FIX skip: the original code used pytest.skip, silently dropping the assertion if
    # Splink drifted — turning a potential regression into a vacuous "pass".
    assert co12_audit is not None, (
        "IT-PROJ-3: co1+co2 (identical name='FoldEdge Corp', jurisdiction='us') did NOT merge. "
        "This is a real regression — co1+co2 must ALWAYS merge when Splink scores identical "
        "name+jurisdiction pairs. A non-merge means the Splink model, ER pipeline, or MergeAudit "
        "writing logic has regressed. Investigate, do NOT convert this back to a pytest.skip."
    )

    co12_canonical = co12_audit.canonical_id

    # Wipe the live graph; project from the log
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)

    # --- Assert: Ownership relationship exists between the SURVIVOR (co12) and co3 ---
    ownership_rows = clean_graph.execute_read(
        """
        MATCH (owner {id: $owner_id})-[r:OWNS]->(asset {id: $asset_id})
        RETURN properties(r) AS rprops
        """,
        owner_id=co12_canonical,
        asset_id="co3",
    )
    assert len(ownership_rows) >= 1, (
        f"IT-PROJ-3: G1 EDGE MISSING — expected an OWNS relationship from "
        f"owner={co12_canonical!r} (survivor of co1) to asset='co3', but found none.\n"
        "The projector must globally rewrite entity-typed Ownership.owner='co1' "
        f"→ '{co12_canonical}' via survivor_of (ADR 0100 D2: global-fold-is-truth)."
    )

    # --- G1 on the edge: prov_* must be present on the relationship ---
    rel_props = ownership_rows[0]["rprops"] or {}
    assert rel_props.get("prov_source_id"), (
        f"IT-PROJ-3: G1 VIOLATED on edge — the OWNS relationship from "
        f"{co12_canonical!r} → 'co3' has no 'prov_source_id'. Every projected edge "
        "must carry the asserting entity's prov_* (ADR 0055 / ADR 0100 D3)."
    )

    # --- No edge from the merged-away source id 'co1' ---
    stale_edges = clean_graph.execute_read("MATCH (n {id: 'co1'})-[r]->(m) RETURN count(r) AS cnt")
    stale_count = int(stale_edges[0]["cnt"]) if stale_edges else 0
    assert stale_count == 0, (
        f"IT-PROJ-3: STALE REFERENT — found {stale_count} relationship(s) from node id='co1' "
        "(a merged-away source id). After global referent rewrite, no edge should reference "
        "the superseded source id 'co1' — all should reference the survivor canonical "
        f"'{co12_canonical}' (ADR 0100 D2)."
    )

    # Sanity: the projected graph has at least co12 and co3 as nodes
    co12_node = clean_graph.execute_read(
        "MATCH (n {id: $cid}) RETURN count(n) AS cnt", cid=co12_canonical
    )
    assert int(co12_node[0]["cnt"]) == 1, (
        f"IT-PROJ-3: node for co12 canonical_id={co12_canonical!r} not found in projected graph"
    )
    co3_node = clean_graph.execute_read("MATCH (n {id: 'co3'}) RETURN count(n) AS cnt")
    assert int(co3_node[0]["cnt"]) == 1, "IT-PROJ-3: node for co3 not found in projected graph"

    engine.dispose()
