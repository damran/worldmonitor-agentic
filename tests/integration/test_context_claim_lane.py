"""Integration tests for Gate P1 — the context-claim capture lane (ADR 0106, spec §4).

Real Postgres (+ Neo4j where ``project()``/``signoff.approve()`` need it) — Docker IS
available locally; run this suite locally, not CI-only (memory: docker-available-run-
integration-locally).

Covers spec §4's integration items:

(a) LOSSLESS per-member rows from a real ``resolve_pending`` on an anchored corpus (the
    pipeline capture point) AND a real ``signoff.approve()`` (the sign-off capture point);
    every row's ``method``/``retrieved_at`` is NOT NULL. Also pins INV-CTX-PARKED-NOTHING: the
    sanctioned (parked) pair writes ZERO context_claim rows until approved.
(b) ``projection_checkpoint.last_context_claim_seq`` exists and ADVANCES to the max
    ``context_claim.seq`` consumed after ``project()``.
(c) Append-only AT THE DB LEVEL: an SQLAlchemy ``before_cursor_execute`` listener captures
    every SQL statement issued across seed + resolve_pending + signoff.approve() + project();
    none is an UPDATE/DELETE against ``context_claim``.
(d) ``test_migrations.py``'s drift guard (``test_no_autogenerate_drift`` / the create_all-vs-
    alembic-head snapshot equality) stays green UNCHANGED — no new test needed here: once the
    builder's model (``db/models.py``) and migration ``0012`` agree byte-for-byte, that
    EXISTING guard exercises the new table + column automatically (ADR 0030). This file adds
    no assertion for (d); it is a note, not a test.
(e) The ``server_default`` pin (adversarial-verify MEDIUM — neither ``alembic check`` nor the
    snapshot guard compares server defaults): the ``0012`` upgrade against a
    ``projection_checkpoint`` PRE-SEEDED with a row at the prior head (``0011_llm_egress_audit``,
    ADR 0106 — next migration = 0012) must succeed and backfill ``last_context_claim_seq=0``
    (the 0008 precedent: a missing ``server_default`` on a NOT NULL add-column fails exactly
    here on a non-empty table).
(f) CONTEXT-ONLY-SURVIVOR NO-OP: after ``signoff.approve()`` (context claims banked, ZERO
    statement rows in P1), ``project(full_rebuild=True)`` completes WITHOUT error and produces
    NO node for that survivor (dormant-until-P3, ADR 0106 §2.b.2).

All tests are RED at collection time: the module-level import of ``ContextClaimRecord`` from
``worldmonitor.db.models`` fails with ``ImportError`` — that symbol does not exist yet.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from alembic import command
from sqlalchemy import create_engine, event, func, inspect, make_url, select, text
from sqlalchemy.orm import Session

from worldmonitor.db.engine import (
    _alembic_config,
    create_all,
    make_engine,
    migrate_to_head,
    session_factory,
)

# ----- GATE IMPORT — fails at collection until builder creates it (RED for right reason) -----
from worldmonitor.db.models import (  # noqa: E402
    ContextClaimRecord,
    ErQueueItem,
    MergeAudit,
    ProjectionCheckpoint,
    StatementRecord,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import signoff
from worldmonitor.resolution.pipeline import ResolveStats, resolve_pending
from worldmonitor.resolution.projector import project

pytestmark = pytest.mark.integration

_SRC = "src:ctx-lane-test"
_RETRIEVED_AT = "2026-07-06T00:00:00Z"


# ---------------------------------------------------------------------------
# Seed helpers — an ANCHORED variant of test_statement_spine.py / test_projector.py's
# _candidates() shape: clear dup pair (anchored, non-conflicting) + distinct singleton
# (anchored) + a sanctioned dup pair (anchored, PARKED — exercises the sign-off capture
# point + INV-CTX-PARKED-NOTHING).
# ---------------------------------------------------------------------------


def _queue_item(entity: dict[str, Any], anchors: dict[str, str]) -> ErQueueItem:
    stamped = stamp(
        make_entity(entity),
        Provenance(
            source_id=_SRC,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{entity['id']}.json",
        ),
    )
    for field, value in anchors.items():
        set_anchor(stamped, field, value)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="ctx-lane-test",
        entity_id=stamped.id,
        raw_entity=stamped.to_dict(),
        source_record=f"s3://landing/{entity['id']}.json",
        status="pending",
    )


def _anchored_candidates() -> list[tuple[dict[str, Any], dict[str, str]]]:
    return [
        (
            {
                "id": "cc1",
                "schema": "Company",
                "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
                "datasets": ["t"],
            },
            {"geonames_id": "2643743"},
        ),
        (
            {
                "id": "cc2",
                "schema": "Company",
                "properties": {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]},
                "datasets": ["t"],
            },
            {"geonames_id": "2643743"},
        ),
        (
            {
                "id": "cc3",
                "schema": "Company",
                "properties": {"name": ["Globex Incorporated"], "jurisdiction": ["gb"]},
                "datasets": ["t"],
            },
            {"opencorporates_id": "gb/98765"},
        ),
        (
            {
                "id": "pp1",
                "schema": "Person",
                "properties": {
                    "name": ["Vladimir Example"],
                    "nationality": ["ru"],
                    "birthDate": ["1960-01-01"],
                    "topics": ["sanction"],
                },
                "datasets": ["t"],
            },
            {"wikidata_id": "Q999999"},
        ),
        (
            {
                "id": "pp2",
                "schema": "Person",
                "properties": {
                    "name": ["Vladimir Example"],
                    "nationality": ["ru"],
                    "birthDate": ["1960-01-01"],
                },
                "datasets": ["t"],
            },
            {"wikidata_id": "Q999999"},
        ),
    ]


def _seed_and_resolve(session: Session, neo4j: Neo4jClient) -> ResolveStats:
    for entity, anchors in _anchored_candidates():
        session.add(_queue_item(entity, anchors))
    session.commit()
    return resolve_pending(session=session, neo4j=neo4j, guard_mode="block")


def _create_fresh_database(postgres_dsn: str) -> str:
    """Create a uniquely-named empty database on the test server; return its DSN.

    Duplicated locally from test_migrations.py's helper of the same shape (small,
    test-only, kept in sync manually — this file must not import a sibling test module)."""
    url = make_url(postgres_dsn)
    name = f"ctx_claim_lane_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


# ===========================================================================
# (a) LOSSLESS per-member rows: pipeline capture + sign-off capture
# ===========================================================================


def test_lossless_context_claims_from_resolve_pending_and_signoff(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """(a): resolve_pending captures anchors for BOTH promoted clusters (cc1+cc2 merge, cc3
    singleton); the PARKED pp1+pp2 pair writes ZERO context_claim rows (INV-CTX-PARKED-
    NOTHING) until signoff.approve() banks them (the sign-off capture point).

    RED today: ImportError — ContextClaimRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        stats = _seed_and_resolve(session, clean_graph)
    assert stats.promoted == 2, f"expected 2 promoted clusters, got {stats.promoted}"
    assert stats.review == 1, f"expected 1 parked cluster, got {stats.review}"

    with sessions() as session:
        promoted_audits = list(
            session.execute(select(MergeAudit).where(MergeAudit.decision == "merged")).scalars()
        )
        parked_audit = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()

    cc12_audit = next(a for a in promoted_audits if sorted(a.source_ids) == ["cc1", "cc2"])
    cc3_audit = next(a for a in promoted_audits if sorted(a.source_ids) == ["cc3"])
    cc12_canonical = cc12_audit.canonical_id
    cc3_canonical = cc3_audit.canonical_id
    parked_canonical = parked_audit.canonical_id
    assert sorted(parked_audit.source_ids) == ["pp1", "pp2"]

    with sessions() as session:
        rows_after_resolve = list(session.execute(select(ContextClaimRecord)).scalars())

    actual_after_resolve = {
        (r.canonical_id, r.entity_id, r.key, r.value, r.dataset, r.method, r.retrieved_at)
        for r in rows_after_resolve
    }
    expected_after_resolve = {
        (cc12_canonical, "cc1", "geonames_id", "2643743", _SRC, "connector:map", _RETRIEVED_AT),
        (cc12_canonical, "cc2", "geonames_id", "2643743", _SRC, "connector:map", _RETRIEVED_AT),
        (
            cc3_canonical,
            "cc3",
            "opencorporates_id",
            "gb/98765",
            _SRC,
            "connector:map",
            _RETRIEVED_AT,
        ),
    }
    assert actual_after_resolve == expected_after_resolve, (
        "(a) PIPELINE CAPTURE LOSSLESS VIOLATED\n"
        f"  expected: {expected_after_resolve}\n"
        f"  actual:   {actual_after_resolve}\n"
        f"  invented: {actual_after_resolve - expected_after_resolve}\n"
        f"  dropped:  {expected_after_resolve - actual_after_resolve}"
    )

    # INV-CTX-PARKED-NOTHING: the parked (sanctioned) pair writes NOTHING until approved.
    parked_rows = [r for r in rows_after_resolve if r.canonical_id == parked_canonical]
    assert not parked_rows, (
        f"(a) PARKED INVARIANT VIOLATED: {len(parked_rows)} context_claim row(s) found for "
        f"parked cluster canonical_id={parked_canonical!r} — a pending_review cluster must "
        "write NO context_claim rows (ADR 0106 §3 / pipeline 'continue' gate)"
    )

    for row in rows_after_resolve:
        assert row.method is not None, f"row for {row.entity_id!r} has NULL method"
        assert row.retrieved_at is not None, f"row for {row.entity_id!r} has NULL retrieved_at"

    # --- Sign-off capture point ---
    with sessions() as session:
        result = signoff.approve(
            session,
            clean_graph,
            canonical_id=parked_canonical,
            approver="ctx-lane-test-operator",
            reason="test signoff capture",
        )
    assert result.decision == "approved"

    with sessions() as session:
        rows_after_signoff = list(session.execute(select(ContextClaimRecord)).scalars())

    actual_after_signoff = {
        (r.canonical_id, r.entity_id, r.key, r.value, r.dataset, r.method, r.retrieved_at)
        for r in rows_after_signoff
    }
    expected_after_signoff = expected_after_resolve | {
        (parked_canonical, "pp1", "wikidata_id", "Q999999", _SRC, "connector:map", _RETRIEVED_AT),
        (parked_canonical, "pp2", "wikidata_id", "Q999999", _SRC, "connector:map", _RETRIEVED_AT),
    }
    assert actual_after_signoff == expected_after_signoff, (
        "(a) SIGN-OFF CAPTURE LOSSLESS VIOLATED\n"
        f"  expected: {expected_after_signoff}\n"
        f"  actual:   {actual_after_signoff}\n"
        f"  invented: {actual_after_signoff - expected_after_signoff}\n"
        f"  dropped:  {expected_after_signoff - actual_after_signoff}"
    )
    for row in rows_after_signoff:
        assert row.method is not None
        assert row.retrieved_at is not None

    engine.dispose()


# ===========================================================================
# (b) last_context_claim_seq column exists + advances after project()
# ===========================================================================


def test_last_context_claim_seq_column_exists_and_advances_after_project(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """(b): ProjectionCheckpoint.last_context_claim_seq exists and advances to the max
    context_claim.seq consumed after project(full_rebuild=True).

    RED today: ImportError — ContextClaimRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        _seed_and_resolve(session, clean_graph)

    with sessions() as session:
        max_seq = session.execute(select(func.max(ContextClaimRecord.seq))).scalar_one()
    assert max_seq is not None and max_seq > 0, (
        "(b) precondition: context_claim must be non-empty after resolve_pending on an "
        "anchored corpus"
    )

    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)

    with sessions() as session:
        checkpoint = session.execute(
            select(ProjectionCheckpoint).where(ProjectionCheckpoint.id == "neo4j")
        ).scalar_one()

    assert hasattr(checkpoint, "last_context_claim_seq"), (
        "ProjectionCheckpoint has no last_context_claim_seq attribute/column"
    )
    assert checkpoint.last_context_claim_seq == max_seq, (
        f"(b) checkpoint.last_context_claim_seq={checkpoint.last_context_claim_seq} != "
        f"max(context_claim.seq)={max_seq} after project(full_rebuild=True) — the watermark "
        "must advance to the max seq consumed (ADR 0106 §2)"
    )

    engine.dispose()


# ===========================================================================
# (c) Append-only AT THE DB LEVEL
# ===========================================================================


def test_context_claim_writes_are_append_only_at_db_level(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """(c): across seed + resolve_pending + signoff.approve() + project(), no UPDATE/DELETE is
    EVER issued against the context_claim table (captured via a real ``before_cursor_execute``
    listener on the engine — a DB-level proof, not a mock).

    RED today: ImportError — ContextClaimRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    captured_sql: list[str] = []

    def _capture(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        captured_sql.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        with sessions() as session:
            _seed_and_resolve(session, clean_graph)

        with sessions() as session:
            parked_audit = session.execute(
                select(MergeAudit).where(MergeAudit.decision == "pending_review")
            ).scalar_one()
            parked_canonical = parked_audit.canonical_id

        with sessions() as session:
            signoff.approve(
                session,
                clean_graph,
                canonical_id=parked_canonical,
                approver="ctx-lane-append-only-test",
                reason="append-only probe",
            )

        with sessions() as session:
            project(session, clean_graph, full_rebuild=True)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        engine.dispose()

    forbidden = [
        stmt
        for stmt in captured_sql
        if "context_claim" in stmt.lower()
        and (" update " in f" {stmt.lower()} " or " delete " in f" {stmt.lower()} ")
    ]
    assert not forbidden, (
        f"(c) APPEND-ONLY VIOLATED AT THE DB LEVEL: {len(forbidden)} UPDATE/DELETE statement(s) "
        "issued against context_claim:\n" + "\n".join(forbidden)
    )


# ===========================================================================
# (e) server_default pin — migration 0012 against a PRE-SEEDED projection_checkpoint
# ===========================================================================


def test_migration_0012_upgrade_backfills_preseeded_checkpoint_default(
    postgres_dsn: str,
) -> None:
    """(e) INV-CKPT-DEFAULT (adversarial-verify MEDIUM — neither alembic check nor the
    snapshot guard compares server defaults): the 0012 upgrade against a
    projection_checkpoint that ALREADY HOLDS a row (pre-seeded at the prior head,
    "0011_llm_egress_audit" per ADR 0106 — next migration = 0012) must succeed and backfill
    last_context_claim_seq=0 (the 0008 precedent: a missing server_default on a NOT NULL
    add-column fails exactly here on a non-empty table).

    RED today: revision "0012_context_claim_lane" does not exist, so `migrate_to_head`
    cannot reach it and the later SELECT of last_context_claim_seq fails (column absent).
    """
    fresh_dsn = _create_fresh_database(postgres_dsn)
    engine = make_engine(fresh_dsn)
    try:
        cfg = _alembic_config(engine)
        command.upgrade(cfg, "0011_llm_egress_audit")  # the prior head (ADR 0106)

        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO projection_checkpoint (id, last_statement_seq, last_decision_seq) "
                    "VALUES ('neo4j', 5, 3)"
                )
            )

        migrate_to_head(engine)  # must reach 0012 and NOT error on the pre-seeded row

        with engine.connect() as conn:
            value = conn.execute(
                text("SELECT last_context_claim_seq FROM projection_checkpoint WHERE id = 'neo4j'")
            ).scalar_one()
        assert value == 0, (
            f"(e) pre-seeded projection_checkpoint row's last_context_claim_seq={value!r} — "
            "expected 0 (the server_default backfill, ADR 0106 §4(e))"
        )

        inspector = inspect(engine)
        columns = {c["name"]: c for c in inspector.get_columns("projection_checkpoint")}
        assert "last_context_claim_seq" in columns, (
            "(e) projection_checkpoint.last_context_claim_seq column missing after "
            "migrating to head"
        )
        default = str(columns["last_context_claim_seq"].get("default") or "")
        assert "0" in default, (
            f"(e) last_context_claim_seq server_default={default!r} — expected a '0' default "
            "(INV-CKPT-DEFAULT)"
        )
    finally:
        engine.dispose()


# ===========================================================================
# (f) CONTEXT-ONLY-SURVIVOR NO-OP after signoff.approve()
# ===========================================================================


def test_context_only_survivor_after_signoff_approve_is_projector_no_op(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """(f): after signoff.approve() (context claims banked, ZERO statement rows — P1 does not
    close the sign-off statement/decision gap, that is Gate P3), project(full_rebuild=True)
    completes WITHOUT error and produces NO node for that survivor (dormant-until-P3, ADR
    0106 §2.b.2 — a graceful no-op, never a crash).

    RED today: ImportError — ContextClaimRecord does not exist yet.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        _seed_and_resolve(session, clean_graph)

    with sessions() as session:
        parked_audit = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        parked_canonical = parked_audit.canonical_id

    with sessions() as session:
        signoff.approve(
            session,
            clean_graph,
            canonical_id=parked_canonical,
            approver="ctx-lane-no-op-test",
            reason="context-only no-op probe",
        )

    with sessions() as session:
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == parked_canonical)
        ).scalar_one()
    assert stmt_count == 0, (
        f"(f) precondition: expected 0 statement rows for signoff-approved canonical_id="
        f"{parked_canonical!r} in P1 (statement/decision routing at signoff is Gate P3), "
        f"got {stmt_count}"
    )

    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)  # must complete WITHOUT raising

    rows = clean_graph.execute_read(
        "MATCH (n {id: $cid}) RETURN count(n) AS cnt", cid=parked_canonical
    )
    count = int(rows[0]["cnt"]) if rows else 0
    assert count == 0, (
        f"(f) CONTEXT-ONLY-SURVIVOR NO-OP VIOLATED: {parked_canonical!r} produced a node after "
        "project() despite having ZERO statement rows — expected a graceful no-op (ADR 0106 "
        "§2.b.2: reconstruct_entities groups by statement rows, so a context-only survivor "
        "yields no entity and no anchors until P3 wires the sign-off statement/decision spine)"
    )

    engine.dispose()
