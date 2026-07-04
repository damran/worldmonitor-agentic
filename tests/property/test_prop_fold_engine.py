"""Property/metamorphic tests for Gate 3a-i — the fold engine (ADR 0100).

Three mandatory ``@given`` property invariants (CLAUDE.md build-discipline):

P-FOLD-1  DETERMINISM — projecting the SAME fixed log into a fresh isolated target
          twice yields byte-identical canonical graph signatures (node set + labels +
          ALL node props incl. prov_*/prov_witnesses/anchors; edge set + edge props).

P-FOLD-3  IDEMPOTENT RE-DELIVERY — projecting the same log twice into the SAME target
          (no wipe between runs) leaves the graph unchanged after the second run (the
          idempotent MERGE is a no-op for duplicate delivery).  Also verifies that an
          incremental ``project()`` with a current watermark reads 0 new rows and leaves
          the graph unchanged.

P-FOLD-4  DEDUP / SUPERSESSION CONVERGENCE — a log containing (a) DUPLICATE
          ``statement_id`` rows (same statement_id, distinct UUID PKs) and (b) a ledger
          re-canonicalisation (old canonical X aliased to survivor Y, statements under
          both) projects in a full_rebuild into EXACTLY ONE node under Y and NO node
          under X — the ADR-0095 fold-under-re-canonicalisation guard.

All three tests are RED at collection time because the module-level import of
``project`` from ``worldmonitor.resolution.projector`` fails with ``ImportError``
— that module does not exist until the builder creates it.  That is the correct,
intended TDD failure mode.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import strategies as wm
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select, text

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import Base, ErQueueItem, StatementRecord
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.resolution.canonical import record_alias, record_canonical
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import project  # gate import: RED until builder lands

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Hypothesis settings — container round-trips are slow; deadline=None required
# ---------------------------------------------------------------------------

_SETTINGS = settings(
    max_examples=20,
    deadline=None,
    # function_scoped_fixture: clean_graph is function-scoped but each example explicitly
    # resets both Postgres (via _cleanup_postgres) and Neo4j (via execute_write DETACH DELETE)
    # at the top of every example — so inter-example state bleed is prevented manually.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


# ---------------------------------------------------------------------------
# graph_signature — canonical byte-comparable fingerprint of a Neo4j graph
#
# Captures: sorted (node_id, sorted_labels, sorted_node_props) + sorted
# (edge_type, src_id, dst_id, sorted_edge_props) including prov_*/prov_witnesses/anchors.
# All values are JSON-serialised for stable cross-run comparison.
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
# Helpers
# ---------------------------------------------------------------------------


def _cleanup_postgres(postgres_dsn: str) -> None:
    """Truncate ALL relational tables to isolate hypothesis examples.

    The Postgres container is session-scoped; ``_isolate_postgres`` (autouse) only
    truncates at the test-function level, not between hypothesis examples.  After each
    example (which calls ``resolve_pending`` and commits), we must manually truncate so
    the next example starts fresh.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    engine.dispose()


def _er_queue_item(entity: FtmEntity) -> ErQueueItem:
    """Wrap a stamped FtmEntity as an ErQueueItem for the ER queue."""
    eid = entity.id
    source_record = f"s3://landing/{eid or 'noid'}.json"
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="test-fold-engine",
        entity_id=eid,
        raw_entity=entity.to_dict(),
        source_record=source_record,
        status="pending",
    )


# ===========================================================================
# P-FOLD-1: DETERMINISM — same log → byte-identical graph (projected twice)
# ===========================================================================


@given(
    entities=st.lists(
        wm.source_tagged_entity(schema="Company"),
        min_size=1,
        max_size=4,
        unique_by=lambda e: e.id,
    )
)
@_SETTINGS
def test_p_fold_1_determinism(
    entities: list[FtmEntity], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """P-FOLD-1: projecting the same fixed log into a fresh target twice produces
    byte-identical graph signatures (ADR 0100 fold-determinism guarantee).

    Steps:
    1. Seed the ER queue from a drawn corpus and call resolve_pending → log is populated.
    2. Wipe the live graph so only the projector writes Neo4j from here on.
    3. project(full_rebuild=True) → sig1 = graph_signature.
    4. Wipe graph.
    5. project(full_rebuild=True) → sig2 = graph_signature.
    6. Assert sig1 == sig2 (byte-identical).
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    # Seed the log via resolve_pending
    with sessions() as session:
        for e in entities:
            if e.id:
                session.add(_er_queue_item(e))
        session.commit()

    with sessions() as session:
        try:
            resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
        except Exception:
            engine.dispose()
            return  # degenerate corpus (failed scoring / validation); skip

    # Check that at least some statement rows were written (non-empty corpus)
    with sessions() as session:
        stmt_count = session.execute(select(func.count()).select_from(StatementRecord)).scalar_one()
    if stmt_count == 0:
        engine.dispose()
        return  # all entities dead-lettered; skip

    # Wipe graph — from here on, the projector is the ONLY writer
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    # First projection
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)
    sig1 = graph_signature(clean_graph)

    # Wipe and project again into a fresh target
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)
    sig2 = graph_signature(clean_graph)

    assert sig1 == sig2, (
        "P-FOLD-1 DETERMINISM VIOLATED: projecting the same fixed log twice produced "
        "non-identical graph signatures.\n"
        f"  sig1 node count: {len(sig1[0])}, edge count: {len(sig1[1])}\n"
        f"  sig2 node count: {len(sig2[0])}, edge count: {len(sig2[1])}\n"
        "The fold must be a deterministic function of the log (ADR 0100 D3)."
    )

    engine.dispose()


# ===========================================================================
# P-FOLD-3: IDEMPOTENT RE-DELIVERY — re-projection of same log is a no-op
# ===========================================================================


@given(
    entities=st.lists(
        wm.source_tagged_entity(schema="Company"),
        min_size=1,
        max_size=4,
        unique_by=lambda e: e.id,
    )
)
@_SETTINGS
def test_p_fold_3_idempotent_redelivery(
    entities: list[FtmEntity], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """P-FOLD-3: projecting the same log twice into the SAME target leaves the graph unchanged.

    The idempotent MERGE in write_entities guarantees that re-delivery is a no-op.
    Also verifies that a second incremental project() with a current watermark reads
    0 new rows and leaves the graph byte-identical.
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    # Seed
    with sessions() as session:
        for e in entities:
            if e.id:
                session.add(_er_queue_item(e))
        session.commit()

    with sessions() as session:
        try:
            resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
        except Exception:
            engine.dispose()
            return

    with sessions() as session:
        stmt_count = session.execute(select(func.count()).select_from(StatementRecord)).scalar_one()
    if stmt_count == 0:
        engine.dispose()
        return

    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    # ---- First full_rebuild projection ----
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)
    sig1 = graph_signature(clean_graph)

    # ---- Second full_rebuild projection — SAME target, no wipe → must be idempotent ----
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)
    sig2 = graph_signature(clean_graph)

    assert sig1 == sig2, (
        "P-FOLD-3 IDEMPOTENCY VIOLATED: projecting the same log twice into the SAME target "
        "produced a different graph after the second run.\n"
        f"  sig1 node count: {len(sig1[0])}, edge count: {len(sig1[1])}\n"
        f"  sig2 node count: {len(sig2[0])}, edge count: {len(sig2[1])}\n"
        "The idempotent MERGE (write_entities + projector dedup) must guarantee that "
        "re-delivery of the same log is a no-op (ADR 0100 D3 / D4)."
    )

    # ---- Incremental projection with a current watermark → reads 0 new rows ----
    # After two full_rebuild runs, the checkpoint is at max_seq.
    # project(full_rebuild=False) with an unchanged log must find no new rows.
    with sessions() as session:
        result3 = project(session, clean_graph, full_rebuild=False)
    sig3 = graph_signature(clean_graph)

    assert sig3 == sig1, (
        "P-FOLD-3 INCREMENTAL IDEMPOTENCY VIOLATED: incremental project() with a current "
        "watermark changed the graph.\n"
        "  expected sig1 == sig3, but they differ.\n"
        "An incremental run with no new rows must be a no-op (at-least-once watermark "
        "semantics, ADR 0100 D3)."
    )
    assert result3.statements_read == 0, (
        f"P-FOLD-3: incremental project() with current watermark read {result3.statements_read} "
        "statement rows — expected 0 (no new rows since the checkpoint was advanced by the "
        "preceding full_rebuild run, ADR 0100 D3)."
    )

    engine.dispose()


# ===========================================================================
# P-FOLD-4: DEDUP / SUPERSESSION CONVERGENCE
# ===========================================================================


@given(
    entities=st.lists(
        wm.source_tagged_entity(schema="Company"),
        min_size=1,
        max_size=3,
        unique_by=lambda e: e.id,
    )
)
@_SETTINGS
def test_p_fold_4_dedup_supersession_convergence(
    entities: list[FtmEntity], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """P-FOLD-4: dedup + supersession convergence (ADR 0100 D3 / D4).

    Seeds the log with:
    (a) DUPLICATE ``statement_id`` rows (same content hash, distinct UUID PKs)
    (b) Ledger re-canonicalisation: statements under canonical X (manually inserted,
        no ledger self-row) AND under canonical Y (from resolve_pending, has a
        self-row); record_alias(session, Y, X) aliases X → Y.

    A full_rebuild projection into a FRESH target must produce:
    - EXACTLY ONE node under survivor Y (absorbing both X's and Y's statements)
    - NO node under superseded X (the fold-under-re-canonicalisation guard)
    - ProjectionResult.statements_deduped >= 1 (the duplicate statement_id was removed)
    """
    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    # Seed the ER queue and run resolve_pending → canonical Y gets a self-row + statements
    with sessions() as session:
        for e in entities:
            if e.id:
                session.add(_er_queue_item(e))
        session.commit()

    with sessions() as session:
        try:
            resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
        except Exception:
            engine.dispose()
            return

    # Get one promoted canonical_id to serve as Y (the survivor)
    with sessions() as session:
        canonical_y = session.execute(
            select(StatementRecord.canonical_id).limit(1)
        ).scalar_one_or_none()

    if canonical_y is None:
        engine.dispose()
        return  # no statements written (all dead-lettered); skip

    # X is a "prior" canonical that will receive BOTH a self-row AND a supersession alias
    # to Y — the hardened F2 scenario (fold-under-re-canonicalisation DETERMINISM guard).
    #
    # PRE-FIX REGRESSION (old unordered alias_map): the ledger would hold both
    #   (canonical_id=X, canonical_alias=X)  ← self-row
    #   (canonical_id=Y, canonical_alias=X)  ← supersession row
    # When the alias map was built without filtering self-rows (canonical_id == canonical_alias),
    # a simple first()-lookup could return X itself as survivor_of(X), producing an orphan
    # node under X in the projected graph — the assertion x_count == 0 would then FAIL.
    #
    # FIXED: project() now builds its alias map from supersession rows ONLY
    # (canonical_id != canonical_alias), with deterministic ORDER BY, so the self-row
    # is excluded and survivor_of(X) always resolves to Y.
    canonical_x = f"fake-prior-canonical-p-fold-4-{uuid.uuid4().hex[:8]}"

    # (a) Insert statement rows under X + a DUPLICATE of one (same statement_id, new PK)
    x_stmt_id = f"stmt-id-p-fold-4-{uuid.uuid4().hex}"
    with sessions() as session:
        x_row = StatementRecord(
            id=str(uuid.uuid4()),
            statement_id=x_stmt_id,
            canonical_id=canonical_x,
            entity_id="src-member-x",
            schema="Company",
            prop="name",
            value="PriorCompanyX",
            dataset="src-fold4-x-test",
            reliability="B",
            retrieved_at="2026-01-01T00:00:00Z",
        )
        # DUPLICATE: same statement_id, distinct UUID PK — tests the dedup invariant
        x_dup_row = StatementRecord(
            id=str(uuid.uuid4()),
            statement_id=x_stmt_id,  # identical statement_id (same content hash)
            canonical_id=canonical_x,
            entity_id="src-member-x",
            schema="Company",
            prop="name",
            value="PriorCompanyX",
            dataset="src-fold4-x-test",
            reliability="B",
            retrieved_at="2026-01-01T00:00:00Z",
        )
        session.add(x_row)
        session.add(x_dup_row)
        session.commit()

    # (b) Give X a self-row THEN alias X → Y.
    # The ledger now holds both (X→X self-row) and (Y→X supersession row) for the alias X.
    # Against the old projector, survivor_of(X) could match the self-row and return X
    # (unordered first() on two rows with canonical_alias=X).  The fixed projector filters
    # self-rows from the alias map before lookup, so X→Y is the only resolution.
    with sessions() as session:
        record_canonical(session, canonical_x)  # adds (canonical_id=X, canonical_alias=X)
        session.commit()
    with sessions() as session:
        record_alias(session, canonical_y, canonical_x)  # adds (canonical_id=Y, canonical_alias=X)
        session.commit()

    # Wipe graph, then project fresh (full_rebuild=True reads ALL rows including X rows)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        result = project(session, clean_graph, full_rebuild=True)

    # --- Assert: NO node under X (superseded), EXACTLY ONE node under Y (survivor) ---
    nodes_x = clean_graph.execute_read(
        "MATCH (n {id: $nid}) RETURN count(n) AS cnt", nid=canonical_x
    )
    nodes_y = clean_graph.execute_read(
        "MATCH (n {id: $nid}) RETURN count(n) AS cnt", nid=canonical_y
    )
    x_count = int(nodes_x[0]["cnt"]) if nodes_x else 0
    y_count = int(nodes_y[0]["cnt"]) if nodes_y else 0

    assert x_count == 0, (
        f"P-FOLD-4 SUPERSESSION GUARD VIOLATED: superseded canonical X={canonical_x!r} "
        f"has {x_count} node(s) in the projected graph. After record_alias(Y, X), the fold "
        "MUST produce NO node under X — the ADR-0095 fold-under-re-canonicalisation invariant "
        "(a superseded canonical_id must never appear as a node in the projected graph)."
    )
    assert y_count == 1, (
        f"P-FOLD-4: survivor canonical Y={canonical_y!r} must have EXACTLY 1 node, "
        f"got {y_count}. Duplicate statement_id rows must dedup (no duplicate node), "
        "and X's statements must fold under Y without multiplying the node count."
    )

    # --- Verify dedup was actually exercised (ProjectionResult reports it) ---
    assert result.statements_deduped >= 1, (
        f"P-FOLD-4: ProjectionResult.statements_deduped={result.statements_deduped} — "
        "must be >= 1 since we inserted a duplicate statement_id row (same statement_id, "
        "distinct UUID PK). The projector must report deduplication (ADR 0100 D3 step 1)."
    )

    engine.dispose()
