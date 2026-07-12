"""Integration tests for Gate WPI-3 — single-writer ingest assert (ADR 0110).

``INV-SINGLE-WRITER``: at most one writer holds the SoR-spine promote transaction
at a time. A second concurrent writer that tries to enter a promote transaction
while another holds it is **refused (fail-closed)** with
``ConcurrentSpineWriterError`` — never allowed to interleave its
``seq``-assign/commit window with the holder's. Consequence: under this
discipline the committed ``StatementRecord.seq`` values the projector reads form
a **contiguous, gap-free** consumption order (ADR 0100 D1's named revisit
trigger, closed here).

All tests are RED on the current tree: the module-level import of
``acquire_spine_writer_lock`` / ``ConcurrentSpineWriterError`` from
``worldmonitor.resolution.spine_lock`` fails because that module does not exist
yet (mirrors the gate-import idiom in ``tests/integration/test_statement_spine.py``
/ ``tests/integration/test_projector.py``).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, make_url, select, text

from worldmonitor.db.engine import create_all, make_engine, session_factory

# ----- GATE IMPORTS — fail at collection until builder creates them (RED for right reason) -----
from worldmonitor.db.models import ErQueueItem, StatementRecord  # noqa: E402
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.spine_lock import (  # gate import: RED until builder lands it
    ConcurrentSpineWriterError,
    acquire_spine_writer_lock,
)

pytestmark = pytest.mark.integration


def _create_fresh_database(postgres_dsn: str) -> str:
    """Create a uniquely-named empty database on the test server; return its DSN.

    Mirrors ``tests/integration/test_migrations.py::_create_fresh_database``.
    Postgres advisory locks are scoped PER DATABASE (the lock tag embeds the
    database OID) — a fresh database isolates the lock key this test races on
    from any other test sharing the session-scoped ``postgres_dsn`` database.
    """
    url = make_url(postgres_dsn)
    name = f"single_writer_lock_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


# ---------------------------------------------------------------------------
# Test 1 (PRIMARY): a second concurrent writer is refused, fail-closed
# ---------------------------------------------------------------------------


def test_second_concurrent_writer_refused(postgres_dsn: str) -> None:
    """PRIMARY assertion (INV-SINGLE-WRITER).

    Connection A acquires ``pg_try_advisory_xact_lock(key)`` directly and keeps
    its transaction OPEN (uncommitted). While A holds it, writer B calling
    ``acquire_spine_writer_lock`` for the SAME key MUST be refused with
    ``ConcurrentSpineWriterError`` — never silently allowed to proceed and
    interleave a seq-assign/commit window with A's. After A releases (commit,
    which auto-releases a transaction-scoped advisory lock), B's retry in a
    FRESH transaction MUST succeed (no raise).

    RED today: ImportError — ``worldmonitor.resolution.spine_lock`` does not
    exist, so this module fails to collect at all.
    """
    key = 424242
    fresh_dsn = _create_fresh_database(postgres_dsn)
    engine = make_engine(fresh_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    conn_a = engine.connect()
    session_b = sessions()
    try:
        trans_a = conn_a.begin()
        lock_sql = text("SELECT pg_try_advisory_xact_lock(:k)")
        acquired = conn_a.execute(lock_sql, {"k": key}).scalar()
        assert acquired is True, (
            "setup precondition failed: connection A must acquire the advisory "
            "lock first (nothing else holds it on a fresh database)"
        )

        with pytest.raises(ConcurrentSpineWriterError):
            acquire_spine_writer_lock(session_b, key=key)

        # Release A — pg_try_advisory_xact_lock is TRANSACTION-scoped, so commit
        # auto-releases it (ADR 0110: no explicit unlock, no leak, no cross-batch
        # holding).
        trans_a.commit()

        # B's refused attempt above must not leave a poisoned session: start a
        # FRESH transaction before retrying (the spec's explicit "fresh
        # transaction" requirement).
        session_b.rollback()
        acquire_spine_writer_lock(session_b, key=key)  # must NOT raise now
        session_b.rollback()
    finally:
        session_b.close()
        conn_a.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Test 2: end-to-end — resolve_pending's per-batch lock composes with the
# real drain, leaving a contiguous (gap-free) committed seq sequence.
# ---------------------------------------------------------------------------


def _sw_candidates() -> list[dict[str, Any]]:
    """Three distinct singleton Companies — one promote transaction each when
    ``batch_size=1``, so ``resolve_pending`` drains >= 2 batches (>= 2 separate
    lock-acquire/commit windows) — the placement ADR 0110 requires (the lock at
    the top of EACH batch iteration, inside the transaction its
    ``session.commit()`` closes)."""
    return [
        {
            "id": f"single-writer-target-{i}",
            "schema": "Company",
            "properties": {"name": [f"Single Writer Target {i}"], "jurisdiction": ["us"]},
            "datasets": ["t"],
        }
        for i in range(3)
    ]


def _sw_queue_item(entity: dict[str, object]) -> ErQueueItem:
    """Build a stamped ErQueueItem from a raw entity dict (mirrors
    ``test_statement_spine.py::_queue_item``): every queued entity must carry
    ``prov_*`` so the writer does not fail closed (ADR 0060)."""
    source_record = f"s3://landing/{entity['id']}.json"
    stamped = stamp(
        make_entity(entity),
        Provenance(
            source_id="src:single-writer-lock-test",
            retrieved_at="2026-07-12T00:00:00Z",
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


def test_resolve_pending_holds_lock_no_seq_gap(
    clean_graph: Neo4jClient,
    postgres_dsn: str,
) -> None:
    """End-to-end: ``resolve_pending`` drains >= 2 batches (``batch_size=1``); the
    per-batch advisory lock composes with the real drain, and the resulting
    committed ``StatementRecord.seq`` values are CONTIGUOUS (no gap) — the
    consequence ``INV-SINGLE-WRITER`` guarantees under single-writer discipline.

    RED today: ImportError — ``worldmonitor.resolution.spine_lock`` does not
    exist, so this module fails to collect at all.
    """
    # A FRESH database isolates this global ``select(StatementRecord.seq)`` from
    # any row (or rolled-back IDENTITY burn -> gap) another test left in the
    # session-scoped ``postgres_dsn`` database; here seqs start at 1 and a
    # single-writer clean drain is deterministically contiguous.
    engine = make_engine(_create_fresh_database(postgres_dsn))
    create_all(engine)
    sessions = session_factory(engine)

    candidates = _sw_candidates()
    with sessions() as session:
        for candidate in candidates:
            session.add(_sw_queue_item(candidate))
        session.commit()

    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, guard_mode="block", batch_size=1
        )

    assert stats.batches >= 2, (
        f"expected >= 2 drained batches with batch_size=1 and {len(candidates)} pending "
        f"rows, got {stats.batches} — this test needs a REAL multi-batch drain to prove "
        "the per-batch lock composes with resolve_pending"
    )
    assert stats.promoted == len(candidates), (
        f"expected all {len(candidates)} distinct singletons promoted, got "
        f"{stats.promoted} (review={stats.review}, alerts={stats.alerts})"
    )

    with sessions() as session:
        seqs = sorted(session.execute(select(StatementRecord.seq)).scalars())

    assert seqs, "expected at least one committed StatementRecord row"
    expected = list(range(seqs[0], seqs[0] + len(seqs)))
    assert seqs == expected, (
        f"StatementRecord.seq values are NOT contiguous: {seqs} (expected an unbroken "
        f"run {expected}) — a gap here means a committed row was skipped by the "
        "incremental watermark, the exact silent-loss hazard ADR 0100 D1 named and "
        "ADR 0110 / INV-SINGLE-WRITER closes."
    )

    engine.dispose()
