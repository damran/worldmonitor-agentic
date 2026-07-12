"""Integration tests for Gate WPI-1 — zero-prop-entity disposition (ADR 0112).

Real Postgres + real Neo4j (testcontainers) — Docker IS available locally; run this suite
locally (memory: docker-available-run-integration-locally).

``INV-ZEROPROP-DISPOSITION``: a promoted entity with ZERO FtM properties (these fixtures never
set an anchor, so always the zero-anchor sub-case too) has a decided, tested disposition at BOTH
promote points (pipeline ``_resolve_batch`` + sign-off ``approve``/``reject``): it leaves >= 1
``wm:exists`` existence-claim ``StatementRecord`` so a ``full_rebuild`` reproduces its node
BYTE-EQUIVALENTLY to the direct write (``datasets`` excluded, standing E4 carve-out).

Tests
-----
IT-ZEROPROP-1   BYTE-EQUIVALENCE (the load-bearing anchor, IT-PROJ-2 style): a zero-prop
                SINGLETON promoted via the pipeline — direct-write graph_signature ==
                full_rebuild graph_signature (excluding 'datasets'); the bare node carries id +
                schema label + prov_*, no prov_witnesses, no bare CANONICAL_ID_FIELDS key.
IT-ZEROPROP-2   Both promote points: the SAME disposition emitted at the SIGN-OFF promote point
                — signoff.approve() of a parked zero-prop MERGE leaves >= 1 WM_EXISTS row and its
                survivor materialises on full_rebuild. Seeded by hand-writing a pending_review
                MergeAudit + 2 pending_review ErQueueItem rows directly (a propertyless pair has
                nothing for Splink to block/score on, so it never naturally trips
                guard.needs_review — see _seed_parked_zeroprop_merge below).
IT-ZEROPROP-3   WPI-2 INTERLOCK (must-have): the SAME seeded zero-prop MERGE, approved. TODAY
                (pre-fix), the aliased survivor has ZERO statement rows folding into it, so
                project(full_rebuild=True) RAISES IncompleteAliasedSurvivorError
                (resolution/spine_integrity.py, UNCHANGED by this gate) — this test's steady
                state targets the POST-FIX behaviour (no raise + bare node materialises), so it
                is RED today via an UNHANDLED IncompleteAliasedSurvivorError propagating out of
                the plain (non-pytest.raises) project() call.
rider-3         INV-SINGLE-WRITER: signoff.approve()/reject() must take
                acquire_spine_writer_lock(session) before their spine writes. Proven by holding
                the Postgres advisory lock from a SECOND connection and asserting the sign-off
                call is refused with ConcurrentSpineWriterError (mirrors
                tests/integration/test_single_writer_lock.py's two-connection pattern).

All tests are RED today (assertion-adjacent / a genuinely-raised exception, NOT an ImportError —
this gate adds no new symbol to db/models.py, FROZEN, no schema change): ``fuse_statement_rows``
returns [] for a zero-prop cluster, so every statement-row-presence assertion below currently
fails, and IT-ZEROPROP-3's plain project() call currently raises IncompleteAliasedSurvivorError
unhandled. rider-3's tests are RED because signoff.py does not yet call
acquire_spine_writer_lock at all, so the concurrent sign-off call is never refused.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, func, make_url, select, text
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    CanonicalIdLedger,
    ErQueueItem,
    MergeAudit,
    StatementRecord,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import signoff
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import project
from worldmonitor.resolution.spine_integrity import find_incomplete_aliased_survivors
from worldmonitor.resolution.spine_lock import ConcurrentSpineWriterError
from worldmonitor.settings import get_settings

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# graph_signature — duplicated from tests/integration/test_projector.py (kept in sync
# manually, per that file's own convention) so this file is self-contained.
# ---------------------------------------------------------------------------


def _stable_val(v: object) -> str:
    import json

    if isinstance(v, list):
        return json.dumps(sorted(str(x) for x in v), ensure_ascii=False)
    return json.dumps(v, default=str, ensure_ascii=False, sort_keys=True)


def graph_signature(
    client: Neo4jClient, exclude_node_props: frozenset[str] = frozenset()
) -> tuple[tuple, tuple]:
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
# Seed helpers
# ---------------------------------------------------------------------------


def _zeroprop_entity(
    member_id: str, *, schema: str = "Person", source_id: str = "src:zp"
) -> FtmEntity:
    """A zero-prop AND zero-anchor stamped FtM entity (no anchor is ever set)."""
    entity = make_entity({"id": member_id, "schema": schema, "properties": {}})
    stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at="2026-07-12T00:00:00Z",
            reliability="A",
            source_record=f"s3://landing/{member_id}.json",
        ),
    )
    return entity


def _zeroprop_queue_item(member_id: str, *, source_id: str, status: str = "pending") -> ErQueueItem:
    entity = _zeroprop_entity(member_id, schema="Person", source_id=source_id)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        raw_entity=entity.to_dict(),
        source_record=f"s3://landing/{member_id}.json",
        status=status,
    )


def _seed_parked_zeroprop_merge(
    sessions: sessionmaker[Session], prefix: str
) -> tuple[str, str, str]:
    """Directly seed a PARKED zero-prop MERGE, bypassing resolve_pending entirely.

    A propertyless pair has nothing for Splink to block/score on (no name, no shared
    attribute), so it never naturally trips guard.needs_review through the real ER pipeline —
    the gate spec's explicit seeding note. Hand-writes the MergeAudit
    (decision='pending_review') + two ErQueueItem rows (status='pending_review') directly,
    mirroring tests/integration/test_b1_signoff_idempotency.py's `_seed_parked` shape minus the
    resolve_pending step a zero-prop pair cannot reach.
    """
    m1, m2 = f"{prefix}-m1", f"{prefix}-m2"
    canonical_id = f"wmc-{prefix}"
    with sessions() as session:
        session.add(
            MergeAudit(
                id=str(uuid.uuid4()),
                canonical_id=canonical_id,
                source_ids=[m1, m2],
                score=1.0,
                decision="pending_review",
                reason="zeroprop-test-seed",
            )
        )
        session.add(_zeroprop_queue_item(m1, source_id=f"src:{m1}", status="pending_review"))
        session.add(_zeroprop_queue_item(m2, source_id=f"src:{m2}", status="pending_review"))
        session.commit()
    return canonical_id, m1, m2


def _create_fresh_database(postgres_dsn: str) -> str:
    """Mirrors tests/integration/test_single_writer_lock.py::_create_fresh_database — a fresh,
    uniquely-named database isolates this test's advisory-lock key (Postgres advisory locks are
    scoped PER DATABASE) from any other test sharing the session-scoped ``postgres_dsn`` database.
    """
    url = make_url(postgres_dsn)
    name = f"zeroprop_rider3_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


# ===========================================================================
# IT-ZEROPROP-1 — MANDATORY byte-equivalence, zero-prop SINGLETON
# ===========================================================================


def test_it_zeroprop_1_singleton_byte_equivalence(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    member_id = "itzp1-singleton"
    entity = _zeroprop_entity(member_id, schema="Person", source_id="src:itzp1")
    with sessions() as session:
        session.add(
            ErQueueItem(
                id=str(uuid.uuid4()),
                connector_id="opensanctions",
                raw_entity=entity.to_dict(),
                source_record=f"s3://landing/{member_id}.json",
                status="pending",
            )
        )
        session.commit()

    with sessions() as session:
        stats = resolve_pending(
            session=session, neo4j=clean_graph, guard_mode="block", batch_size=1
        )
    assert stats.promoted == 1, (
        f"IT-ZEROPROP-1 precondition: expected 1 promoted singleton, got {stats.promoted}"
    )

    with sessions() as session:
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == member_id)
        ).scalar_one()
    assert stmt_count >= 1, (
        f"IT-ZEROPROP-1 VIOLATED: zero-prop singleton {member_id!r} left NO statement row after "
        "the pipeline promote point (INV-ZEROPROP-DISPOSITION: expected >= 1 WM_EXISTS row)"
    )

    _EXCL = frozenset({"datasets"})
    direct = graph_signature(clean_graph, exclude_node_props=_EXCL)
    assert len(direct[0]) >= 1, "precondition: the direct write must have produced >= 1 node"

    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)
    fold = graph_signature(clean_graph, exclude_node_props=_EXCL)

    assert fold == direct, (
        "IT-ZEROPROP-1 MANDATORY BYTE-EQUIVALENCE VIOLATED: fold graph != direct graph for the "
        f"zero-prop singleton {member_id!r}.\n"
        f"  direct: {len(direct[0])} nodes, {len(direct[1])} edges\n"
        f"  fold:   {len(fold[0])} nodes, {len(fold[1])} edges\n"
        "A zero-prop entity that leaves NO statement row folds to NO node at all, diverging "
        "from the direct write (ADR 0112's exact gap)."
    )

    node_rows = clean_graph.execute_read(
        "MATCH (n {id: $id}) RETURN properties(n) AS props, labels(n) AS lbls", id=member_id
    )
    assert len(node_rows) == 1, f"IT-ZEROPROP-1: expected exactly 1 bare node for {member_id!r}"
    props = node_rows[0]["props"] or {}
    labels = node_rows[0]["lbls"] or []
    assert "Person" in labels, f"expected the Person schema label, got {labels}"
    assert props.get("prov_source_id") == "src:itzp1"
    assert "prov_witnesses" not in props, "a bare zero-prop node must have NO prov_witnesses"
    assert not (set(props) & set(CANONICAL_ID_FIELDS)), (
        "a bare zero-prop-zero-anchor node must carry NO bare CANONICAL_ID_FIELDS key"
    )

    engine.dispose()


# ===========================================================================
# IT-ZEROPROP-2 — both promote points: sign-off approve() of a parked zero-prop MERGE
# ===========================================================================


def test_it_zeroprop_2_signoff_approve_promote_point(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    canonical_id, m1, m2 = _seed_parked_zeroprop_merge(sessions, "itzp2")

    with sessions() as session:
        result = signoff.approve(
            session, clean_graph, canonical_id=canonical_id, approver="itzp2-op", reason="itzp2"
        )
    assert result.decision == "approved"

    with sessions() as session:
        stmt_count = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.canonical_id == canonical_id)
        ).scalar_one()
    assert stmt_count >= 1, (
        "IT-ZEROPROP-2 VIOLATED: the zero-prop MERGE approved at the SIGN-OFF promote point "
        f"left NO statement row for survivor {canonical_id!r} — INV-ZEROPROP-DISPOSITION "
        "requires >= 1 WM_EXISTS row at BOTH promote points, not just pipeline._resolve_batch"
    )

    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)

    node_rows = clean_graph.execute_read(
        "MATCH (n {id: $id}) RETURN count(n) AS n", id=canonical_id
    )
    assert node_rows[0]["n"] == 1, (
        "IT-ZEROPROP-2: full_rebuild must materialise a bare node for the sign-off-approved "
        f"zero-prop survivor {canonical_id!r}"
    )
    engine.dispose()


# ===========================================================================
# IT-ZEROPROP-3 — WPI-2 interlock: a zero-prop MERGE (aliased survivor)
# ===========================================================================


def test_it_zeroprop_3_wpi2_interlock_zero_prop_merge_no_longer_raises(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """The zero-prop-MERGE-approve scenario: approve() co-commits a CanonicalIdLedger
    supersession alias (member -> survivor) in the SAME transaction as its (pre-fix: zero)
    statement rows. This is EXACTLY the WPI-2 (ADR 0111) aliased-survivor-with-no-content-row
    shape (see tests/integration/test_alias_cocommit.py's IT-ALIAS-1), reproduced here with a
    REAL zero-prop merge instead of a hand-injected ghost alias.

    Steady-state target (POST-FIX): project(full_rebuild=True) does NOT raise, and the
    survivor materialises a bare node.

    RED TODAY: fuse_statement_rows returns [] for a zero-prop cluster (pre-fix), so the aliased
    survivor has ZERO statement rows folding into it. WPI-2's find_incomplete_aliased_survivors
    (resolution/spine_integrity.py, UNCHANGED by this gate) correctly flags it incomplete, and
    the plain project() call below raises IncompleteAliasedSurvivorError, UNHANDLED — this test
    errors out with that exception today, which IS the RED evidence this gate closes.
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    canonical_id, m1, m2 = _seed_parked_zeroprop_merge(sessions, "itzp3")

    with sessions() as session:
        result = signoff.approve(
            session, clean_graph, canonical_id=canonical_id, approver="itzp3-op", reason="itzp3"
        )
    assert result.decision == "approved"

    with sessions() as session:
        alias_count = session.execute(
            select(func.count())
            .select_from(CanonicalIdLedger)
            .where(
                CanonicalIdLedger.canonical_id == canonical_id,
                CanonicalIdLedger.canonical_alias != canonical_id,
            )
        ).scalar_one()
    assert alias_count >= 1, (
        "IT-ZEROPROP-3 precondition: approve() must co-commit >= 1 CanonicalIdLedger "
        f"supersession alias for survivor {canonical_id!r} (member ids collapsed onto it) "
        "regardless of the zero-prop disposition — this is the WPI-2 aliased-survivor scenario"
    )

    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    # POST-FIX TARGET BEHAVIOUR — plain call, no pytest.raises: once WPI-1's existence-claim
    # disposition gives the zero-prop merge survivor >= 1 statement row, this must NOT raise.
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True)

    node_rows = clean_graph.execute_read(
        "MATCH (n {id: $id}) RETURN count(n) AS n", id=canonical_id
    )
    assert node_rows[0]["n"] == 1, (
        f"IT-ZEROPROP-3 VIOLATED: full_rebuild must materialise a bare node for the zero-prop "
        f"MERGE survivor {canonical_id!r} once the interlock closes"
    )

    # find_incomplete_aliased_survivors must count the sentinel row as coverage, with NO change
    # to spine_integrity.py: re-derive the alias map exactly as projector._load_alias_map does.
    with sessions() as session:
        ledger_rows = session.execute(
            select(CanonicalIdLedger.canonical_alias, CanonicalIdLedger.canonical_id)
        ).all()
        alias_map = {str(a): str(c) for a, c in ledger_rows if str(a) != str(c)}
        stmt_rows = list(session.execute(select(StatementRecord)).scalars())
    incomplete = find_incomplete_aliased_survivors(alias_map, stmt_rows)
    assert canonical_id not in incomplete, (
        f"IT-ZEROPROP-3 VIOLATED: find_incomplete_aliased_survivors must count the WM_EXISTS "
        f"sentinel row(s) as coverage for {canonical_id!r} — NO change to spine_integrity.py "
        "required (the sentinel IS a statement row)"
    )
    engine.dispose()


# ===========================================================================
# rider-3 — INV-SINGLE-WRITER: signoff.approve()/reject() hold the spine writer lock
# ===========================================================================


def test_rider3_signoff_approve_refused_by_concurrent_writer(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """A second concurrent writer holding the advisory lock refuses signoff.approve()'s spine
    writes with ConcurrentSpineWriterError; after release, a fresh-transaction retry succeeds.

    RED today: signoff.py does not call acquire_spine_writer_lock at all, so approve() proceeds
    unrefused — `pytest.raises(ConcurrentSpineWriterError)` fails with "DID NOT RAISE".
    """
    fresh_dsn = _create_fresh_database(postgres_dsn)
    engine = make_engine(fresh_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    canonical_id, m1, m2 = _seed_parked_zeroprop_merge(sessions, "itr3appr")
    lock_key = get_settings().spine_writer_lock_key

    conn_a = engine.connect()
    session_b = sessions()
    try:
        trans_a = conn_a.begin()
        acquired = conn_a.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
        ).scalar()
        assert acquired is True, (
            "setup precondition failed: connection A must acquire the advisory lock first "
            "(nothing else holds it on a fresh database)"
        )

        with pytest.raises(ConcurrentSpineWriterError):
            signoff.approve(
                session_b, clean_graph, canonical_id=canonical_id, approver="itr3-appr-op"
            )

        # Release A — pg_try_advisory_xact_lock is TRANSACTION-scoped, so commit auto-releases
        # it (ADR 0110: no explicit unlock, no leak, no cross-batch holding).
        trans_a.commit()

        # B's refused attempt above must not leave a poisoned session: start a FRESH
        # transaction before retrying.
        session_b.rollback()
        result = signoff.approve(
            session_b, clean_graph, canonical_id=canonical_id, approver="itr3-appr-op-2"
        )
        assert result.decision == "approved"
    finally:
        session_b.close()
        conn_a.close()
        engine.dispose()


def test_rider3_signoff_reject_refused_by_concurrent_writer(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """Same as above, for signoff.reject().

    RED today: signoff.py does not call acquire_spine_writer_lock at all, so reject() proceeds
    unrefused — `pytest.raises(ConcurrentSpineWriterError)` fails with "DID NOT RAISE".
    """
    fresh_dsn = _create_fresh_database(postgres_dsn)
    engine = make_engine(fresh_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    canonical_id, m1, m2 = _seed_parked_zeroprop_merge(sessions, "itr3rej")
    lock_key = get_settings().spine_writer_lock_key

    conn_a = engine.connect()
    session_b = sessions()
    try:
        trans_a = conn_a.begin()
        acquired = conn_a.execute(
            text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key}
        ).scalar()
        assert acquired is True, (
            "setup precondition failed: connection A must acquire the advisory lock first "
            "(nothing else holds it on a fresh database)"
        )

        with pytest.raises(ConcurrentSpineWriterError):
            signoff.reject(
                session_b, clean_graph, canonical_id=canonical_id, approver="itr3-rej-op"
            )

        trans_a.commit()
        session_b.rollback()
        result = signoff.reject(
            session_b, clean_graph, canonical_id=canonical_id, approver="itr3-rej-op-2"
        )
        assert result.decision == "rejected"
    finally:
        session_b.close()
        conn_a.close()
        engine.dispose()
