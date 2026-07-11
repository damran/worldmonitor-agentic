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
    none is an UPDATE/DELETE against ``context_claim``. The detector is a TABLE-QUALIFIED
    token match (``update context_claim`` / ``delete from context_claim``, case-insensitive) —
    NOT a bare ``"context_claim" in stmt`` substring check, because
    ``projection_checkpoint.last_context_claim_seq`` (a real, legitimate UPDATE target of
    ``project()``'s checkpoint upsert) contains the literal substring ``"context_claim"`` in
    its COLUMN name; a bare substring match would false-positive on that benign statement the
    moment a second ``project()`` call in this file emits an UPDATE against
    ``projection_checkpoint`` (test (b) already does — this file previously only called
    ``project()`` once per test, so the false positive was LATENT, not triggered; the
    tightened detector below is the fix pinned for equal-or-greater strength).
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

import re
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

# (c) TABLE-QUALIFIED write-detector for the append-only-at-DB-level assertion.
#
# A bare ``"context_claim" in stmt.lower()`` substring check (the pre-tightening shape) also
# matches ``projection_checkpoint.last_context_claim_seq`` — a real COLUMN name that contains
# the literal substring "context_claim" — so an UPDATE against ``projection_checkpoint`` whose
# SET clause merely touches that column (exactly what ``project()``'s checkpoint upsert emits,
# ``resolution/projector.py``'s ``checkpoint.last_context_claim_seq = max_ctx_seq`` branch)
# would be wrongly flagged as a write against the ``context_claim`` TABLE. This regex instead
# requires the SQL verb to be immediately followed by the TABLE token itself (optionally
# double-quoted): "update context_claim" / "delete from context_claim". Underscore is a `\w`
# character, so this also naturally cannot match a substring embedded inside a longer
# identifier like ``last_context_claim_seq`` (the verb + whitespace must precede the table name
# directly — "set last_context_claim_seq" never satisfies "update\s+context_claim").
_CONTEXT_CLAIM_WRITE_RE = re.compile(
    r'\b(?:update|delete\s+from)\s+"?context_claim"?\b', re.IGNORECASE
)


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


def test_context_claim_write_detector_ignores_checkpoint_column_false_positive() -> None:
    """(c) detector self-check: the tightened, TABLE-QUALIFIED regex must NOT flag an UPDATE
    against ``projection_checkpoint`` whose SET clause merely touches the
    ``last_context_claim_seq`` COLUMN (the exact false positive a bare
    ``"context_claim" in stmt`` substring match would trip on — this is what
    ``project()``'s checkpoint-upsert branch, ``checkpoint.last_context_claim_seq =
    max_ctx_seq``, actually emits at the SQL level), while STILL catching a genuine
    UPDATE/DELETE against the ``context_claim`` TABLE itself. Pure/no-DB; pins the detector
    logic in isolation from the (Docker-backed) DB-level test below.
    """
    false_positive_sql = (
        "UPDATE projection_checkpoint SET last_statement_seq=%(last_statement_seq)s, "
        "last_decision_seq=%(last_decision_seq)s, "
        "last_context_claim_seq=%(last_context_claim_seq)s, updated_at=%(updated_at)s "
        "WHERE projection_checkpoint.id = %(projection_checkpoint_id)s"
    )
    assert not _CONTEXT_CLAIM_WRITE_RE.search(false_positive_sql), (
        "(c) DETECTOR REGRESSION: the tightened context_claim write-detector must NOT flag an "
        "UPDATE against projection_checkpoint whose SET clause merely touches the "
        "last_context_claim_seq COLUMN — this is the exact false positive a bare "
        "'context_claim' substring match would trip on the moment a second project() call "
        "emits this (legitimate) checkpoint UPDATE."
    )

    genuine_update_sql = "UPDATE context_claim SET value=%(value)s WHERE context_claim.id = %(id)s"
    genuine_update_quoted_sql = (
        'UPDATE "context_claim" SET value=%(value)s WHERE "context_claim".id = %(id)s'
    )
    genuine_delete_sql = "DELETE FROM context_claim WHERE context_claim.id = %(id)s"
    for label, sql in (
        ("bare UPDATE", genuine_update_sql),
        ("quoted UPDATE", genuine_update_quoted_sql),
        ("DELETE FROM", genuine_delete_sql),
    ):
        assert _CONTEXT_CLAIM_WRITE_RE.search(sql), (
            f"(c) DETECTOR REGRESSION: a genuine {label} against the context_claim TABLE must "
            f"still be caught by the tightened detector; sql={sql!r}"
        )

    # A benign INSERT (the only writer op the lane permits) must never be flagged either.
    benign_insert_sql = (
        "INSERT INTO context_claim (id, canonical_id, entity_id, key, value, dataset, method, "
        "retrieved_at, scope) VALUES (%(id)s, %(canonical_id)s, %(entity_id)s, %(key)s, "
        "%(value)s, %(dataset)s, %(method)s, %(retrieved_at)s, %(scope)s)"
    )
    assert not _CONTEXT_CLAIM_WRITE_RE.search(benign_insert_sql), (
        "(c) DETECTOR REGRESSION: a benign INSERT against context_claim must never be flagged "
        "as a forbidden write"
    )


def test_context_claim_writes_are_append_only_at_db_level(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """(c): across seed + resolve_pending + signoff.approve() + project(), no UPDATE/DELETE is
    EVER issued against the context_claim table (captured via a real ``before_cursor_execute``
    listener on the engine — a DB-level proof, not a mock).

    The forbidden-statement filter uses the TABLE-QUALIFIED ``_CONTEXT_CLAIM_WRITE_RE``
    (module-level, self-checked by the sibling
    ``test_context_claim_write_detector_ignores_checkpoint_column_false_positive`` test) rather
    than a bare ``"context_claim" in stmt`` substring match, so a ``project()``-issued UPDATE
    against ``projection_checkpoint.last_context_claim_seq`` can never trip this assertion —
    equal-or-greater strength than the substring check for the writers' actual INSERT-only
    intent, with the false-positive risk on the checkpoint column removed.

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

        # Advance the context watermark between the two project() calls: SQLAlchemy emits
        # only CHANGED columns in an UPDATE SET clause, so a 0-delta re-run would write
        # ``SET updated_at = ...`` alone and the precondition below would never see a
        # checkpoint UPDATE referencing ``last_context_claim_seq`` at all.
        with sessions() as session:
            session.add(
                ContextClaimRecord(
                    id=str(uuid.uuid4()),
                    canonical_id=parked_canonical,
                    entity_id=f"{parked_canonical}-watermark-member",
                    key="lei",
                    value="CTXLANEWATERMARK0001",
                    dataset=_SRC,
                    method="connector:map",
                    retrieved_at=_RETRIEVED_AT,
                    scope="default",
                )
            )
            session.commit()

        # A SECOND project() call — deliberately exercises the checkpoint-upsert UPDATE path
        # (the checkpoint row now exists and the context watermark has genuinely advanced, so
        # this call takes the ``checkpoint.last_context_claim_seq = max_ctx_seq`` UPDATE
        # branch with the column actually present in the SET clause, not the session.add
        # INSERT branch) — the exact statement shape the tightened detector must NOT confuse
        # with a write against the context_claim TABLE.
        with sessions() as session:
            project(session, clean_graph, full_rebuild=True)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        engine.dispose()

    checkpoint_updates = [
        stmt
        for stmt in captured_sql
        if "last_context_claim_seq" in stmt.lower()
        and re.search(r"\bupdate\b", stmt, re.IGNORECASE)
    ]
    assert checkpoint_updates, (
        "(c) precondition: expected >= 1 captured UPDATE statement referencing "
        "last_context_claim_seq (the second project() call's checkpoint-upsert UPDATE branch, "
        "checkpoint.last_context_claim_seq = max_ctx_seq) — the exact false-positive shape "
        "this test guards against was never exercised; without this, a regression back to "
        "the bare substring match would go undetected here"
    )

    forbidden = [stmt for stmt in captured_sql if _CONTEXT_CLAIM_WRITE_RE.search(stmt)]
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


def test_context_only_survivor_without_statements_is_projector_no_op(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """(f), FLIPPED (Gate P3 / ADR 0108): ``signoff.approve()`` now co-commits statement rows
    for the survivor (P3 un-dormants a sign-off-approved survivor — see
    ``tests/integration/test_signoff_spine.py``'s ``IT-SIGN-approve``), so the P1-era
    "approve() -> zero statement rows -> no fold node" precondition this test used to pin no
    longer holds through ``signoff.approve()``. The GENUINE projector no-op this test protects
    — a context-claim-only survivor (zero statement rows) yields NO fold node, a graceful
    no-op, never a crash (ADR 0106 §2.b.2: ``reconstruct_entities`` groups by statement rows)
    — is repointed here to a SYNTHETIC statement-less survivor: a bare ``ContextClaimRecord``
    with NO corresponding ``StatementRecord``, inserted directly (no signoff/pipeline call), so
    this pin is now INDEPENDENT of whichever write path happens to route through the statement
    spine. The sign-off-approve UN-DORMANTING itself is asserted by
    ``tests/integration/test_signoff_spine.py``'s ``IT-SIGN-approve``, not here.

    RED today: ImportError — ContextClaimRecord does not exist yet [Gate P1 precondition,
    unchanged by this flip].
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    canonical_id = "p3-flip-context-only-survivor"
    with sessions() as session:
        session.add(
            ContextClaimRecord(
                id=str(uuid.uuid4()),
                canonical_id=canonical_id,
                entity_id=f"{canonical_id}-member",
                key="wikidata_id",
                value="Q3FLIP0001",
                dataset=_SRC,
                method="connector:map",
                retrieved_at=_RETRIEVED_AT,
                scope="default",
            )
        )
        session.commit()

    with sessions() as session:
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == canonical_id)
        ).scalar_one()
    assert stmt_count == 0, (
        f"(f) FLIPPED precondition: expected 0 statement rows for the SYNTHETIC statement-less "
        f"survivor {canonical_id!r}, got {stmt_count}"
    )

    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)  # must complete WITHOUT raising

    rows = clean_graph.execute_read("MATCH (n {id: $cid}) RETURN count(n) AS cnt", cid=canonical_id)
    count = int(rows[0]["cnt"]) if rows else 0
    assert count == 0, (
        f"(f) FLIPPED CONTEXT-ONLY-SURVIVOR NO-OP VIOLATED: {canonical_id!r} produced a node "
        "after project() despite having ZERO statement rows — expected a graceful no-op (ADR "
        "0106 §2.b.2: reconstruct_entities groups by statement rows, so a context-claim-only "
        "survivor yields no entity and no anchors). Gate P3 (ADR 0108) un-dormants a sign-off-"
        "APPROVED survivor because approve() now writes statement rows for it — this pin is "
        "repointed to a SYNTHETIC statement-less survivor so it stays independent of that "
        "write path (the un-dormanting itself is asserted by "
        "tests/integration/test_signoff_spine.py's IT-SIGN-approve)."
    )

    engine.dispose()
