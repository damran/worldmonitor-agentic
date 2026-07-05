"""Unit tests for Gate 2a — Statement spine, step 1 (ADR 0099).

Fast, example-based tests for ``worldmonitor.resolution.statements``.  SQLite sessions
are used to avoid the Docker dependency; JSONB columns on DecisionRecord are shimmed to
JSON for SQLite compatibility (same shim as test_prop_landing_gc_reference_safety.py).

The tests cover:
- ``fuse_statement_rows`` yields the expected rows for a hand-built 2-source Company
  cluster, including correct reliability enrichment, exclusion of the "id" pseudo-property,
  NULL method, and "default" scope.
- ``record_decision`` writes exactly ONE DecisionRecord for a merge cluster (kind="merge",
  decided_by="auto:resolver", evidence={"reason": ...} or None); it is a no-op for a
  singleton (is_merge=False).
- Gate P1 (ADR 0106): inserting a ``ContextClaimRecord`` in an SQLite session assigns a
  monotonic non-NULL ``seq`` — the reused ``_assign_sqlite_seq`` ``before_insert`` listener
  pinned at RUNTIME (the ADR-0100 SQLite-IDENTITY trap avoidance).

All tests are RED on the current tree: the module-level imports fail because
``worldmonitor.resolution.statements`` and the new db.models classes do not exist yet.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# ----- GATE IMPORTS — fail at collection until builder creates them (RED for right reason) -----
from worldmonitor.db.models import (  # noqa: E402
    Base,
    ContextClaimRecord,
    DecisionRecord,
    StatementRecord,
)
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import ResolvedCluster, cluster_and_merge
from worldmonitor.resolution.splink_model import ScoredPair

# This import fails until builder creates worldmonitor/resolution/statements.py
from worldmonitor.resolution.statements import (  # noqa: E402
    fuse_statement_rows,
    record_decision,
    record_statements,
)

# ---------------------------------------------------------------------------
# SQLite JSONB shim — same idiom as test_prop_landing_gc_reference_safety.py
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_session() -> Iterator[Session]:
    """An isolated in-memory SQLite session for fast unit tests."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Hand-built 2-source Company cluster
# ---------------------------------------------------------------------------

_SRC_A = "src-A"
_SRC_B = "src-B"
_RETRIEVED_AT = "2026-01-01T00:00:00Z"


def _stamped_company(entity_id: str, source_id: str, name: str) -> Any:
    """A Company entity stamped with a provenance Provenance."""
    entity = make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [name]},
            "datasets": ["t"],
        }
    )
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=_RETRIEVED_AT,
            reliability="B",
            source_record=f"s3://landing/{entity_id}.json",
        ),
    )


def _two_source_cluster() -> tuple[Any, dict[str, Any]]:
    """Return (merged_cluster, by_id) for a 2-source Company merge.

    e1 (src-A): name="AlphaCorpLtd"
    e2 (src-B): name="BetaCorpLtd"
    Both are connected with a 0.95-score pair so cluster_and_merge merges them.
    """
    e1 = _stamped_company("e1", _SRC_A, "AlphaCorpLtd")
    e2 = _stamped_company("e2", _SRC_B, "BetaCorpLtd")
    pairs = [ScoredPair("e1", "e2", 0.95)]
    clusters = cluster_and_merge([e1, e2], pairs)
    merged = next(c for c in clusters if c.is_merge)
    by_id: dict[str, Any] = {"e1": e1, "e2": e2}
    return merged, by_id


# ---------------------------------------------------------------------------
# fuse_statement_rows — structure and content
# ---------------------------------------------------------------------------


def test_fuse_statement_rows_yields_rows_for_each_member_claim() -> None:
    """fuse_statement_rows returns one StatementRecord per (member, prop, value) triple.

    The 2-source cluster has e1/name="AlphaCorpLtd" (src-A) and e2/name="BetaCorpLtd"
    (src-B).  The expected rows cover both members' name claims — no row is dropped, none
    invented.

    RED today: ImportError on fuse_statement_rows.
    """
    cluster, by_id = _two_source_cluster()
    rows = fuse_statement_rows(cluster, by_id)

    assert rows, "fuse_statement_rows must return at least one row for a 2-source cluster"

    # Each row must be a StatementRecord instance
    for row in rows:
        assert isinstance(row, StatementRecord), (
            f"fuse_statement_rows must yield StatementRecord instances, got {type(row).__name__}"
        )

    # canonical_id on every row must match the cluster's canonical_id
    for row in rows:
        assert row.canonical_id == cluster.canonical_id, (
            f"row.canonical_id={row.canonical_id!r} != "
            f"cluster.canonical_id={cluster.canonical_id!r}"
        )

    # The (entity_id, prop, value, dataset) tuples must cover both members
    actual = {(r.entity_id, r.prop, r.value, r.dataset) for r in rows}

    # FtM cleans "AlphaCorpLtd" / "BetaCorpLtd" via NameType; recover via entity.get("name")
    e1_names = {str(v) for v in by_id["e1"].get("name")}
    e2_names = {str(v) for v in by_id["e2"].get("name")}

    for v in e1_names:
        assert ("e1", "name", v, _SRC_A) in actual, (
            f"Row for (e1, name, {v!r}, src-A) missing from fuse_statement_rows output"
        )
    for v in e2_names:
        assert ("e2", "name", v, _SRC_B) in actual, (
            f"Row for (e2, name, {v!r}, src-B) missing from fuse_statement_rows output"
        )


def test_fuse_statement_rows_excludes_id_pseudoproperty() -> None:
    """fuse_statement_rows must NOT produce any row with prop == "id".

    The FtM "id" pseudo-statement carries the construction Dataset ("worldmonitor"),
    not a source dataset; it is explicitly excluded from the statement log (ADR 0099,
    provenance.model._ID_PSEUDO_PROP carve-out).

    RED today: ImportError on fuse_statement_rows.
    """
    cluster, by_id = _two_source_cluster()
    rows = fuse_statement_rows(cluster, by_id)

    id_rows = [r for r in rows if r.prop == "id"]
    assert not id_rows, (
        f"fuse_statement_rows produced {len(id_rows)} row(s) with prop='id' — "
        "the id pseudo-property MUST be excluded (ADR 0099 / G1)"
    )


def test_fuse_statement_rows_reliability_enriched_from_provenance() -> None:
    """Each row's reliability comes from the contributing member's Provenance (not invented).

    The oracle: both e1 and e2 were stamped with reliability="B" (the _provenance helper's
    constant in strategies.py, replicated in _stamped_company above). Every row in the output
    must carry reliability="B".

    RED today: ImportError on fuse_statement_rows.
    """
    cluster, by_id = _two_source_cluster()
    rows = fuse_statement_rows(cluster, by_id)

    for row in rows:
        assert row.reliability == "B", (
            f"row for (entity_id={row.entity_id!r}, prop={row.prop!r}) has "
            f"reliability={row.reliability!r}, expected 'B' (from member Provenance)"
        )


def test_fuse_statement_rows_method_is_none() -> None:
    """The method column must be NULL (None) — method is unmodelled in step 1 (ADR 0099 §table).

    "not modelled anywhere today → always NULL until a method field exists".

    RED today: ImportError on fuse_statement_rows.
    """
    cluster, by_id = _two_source_cluster()
    rows = fuse_statement_rows(cluster, by_id)
    assert rows, "cluster must produce at least one row"

    for row in rows:
        assert row.method is None, (
            f"row.method={row.method!r} — method is unmodelled in step 1, must be NULL (ADR 0099)"
        )


def test_fuse_statement_rows_scope_defaults_to_default() -> None:
    """The scope column must default to 'default' (server_default reserved, ADR 0099 Decision A).

    fuse_statement_rows builds StatementRecord instances; their scope must be 'default' (either
    set explicitly in the constructor or inherited from the server_default when persisted).
    Since we're testing the ORM-level default (not yet persisted), the row's scope attribute
    should be 'default'.

    RED today: ImportError on fuse_statement_rows.
    """
    cluster, by_id = _two_source_cluster()
    rows = fuse_statement_rows(cluster, by_id)
    assert rows, "cluster must produce at least one row"

    for row in rows:
        # The scope may be None before persistence (server_default only fires on INSERT)
        # but fuse_statement_rows should either set it or leave it as None/default.
        # The builder MUST set scope="default" in the row constructor (or the server_default
        # covers it on flush). We assert it is either None (will become "default" via
        # server_default) or "default" (explicitly set).
        assert row.scope in (None, "default"), (
            f"row.scope={row.scope!r} — scope must be 'default' or None "
            "(resolved to 'default' by server_default on INSERT, ADR 0099 Decision A)"
        )


def test_fuse_statement_rows_scope_persisted_as_default(sqlite_session: Session) -> None:
    """After a real INSERT, the scope column is 'default' (server_default fires on persistence).

    RED today: ImportError on record_statements.
    """
    cluster, by_id = _two_source_cluster()
    record_statements(sqlite_session, cluster, by_id)
    sqlite_session.flush()

    rows = sqlite_session.query(StatementRecord).all()
    assert rows, "at least one statement row must be written"

    for row in rows:
        assert row.scope == "default", (
            f"Persisted row has scope={row.scope!r}, expected 'default' after INSERT "
            "(server_default='default', ADR 0099 Decision A)"
        )


# ---------------------------------------------------------------------------
# record_decision — row structure and singleton guard
# ---------------------------------------------------------------------------


def test_record_decision_writes_one_row_for_merge(sqlite_session: Session) -> None:
    """record_decision writes exactly ONE DecisionRecord row for a merge cluster.

    RED today: ImportError on record_decision.
    """
    cluster, _ = _two_source_cluster()
    assert cluster.is_merge, "test pre-condition: cluster must be is_merge=True"

    record_decision(sqlite_session, cluster, reason="unit-test-reason")
    sqlite_session.flush()

    rows = sqlite_session.query(DecisionRecord).all()
    assert len(rows) == 1, (
        f"record_decision must write exactly 1 decision row for a merge cluster, got {len(rows)}"
    )


def test_record_decision_kind_is_merge(sqlite_session: Session) -> None:
    """The decision row written by record_decision has kind='merge' (only kind in step 1).

    RED today: ImportError on record_decision.
    """
    cluster, _ = _two_source_cluster()
    record_decision(sqlite_session, cluster, reason="unit-test")
    sqlite_session.flush()

    row = sqlite_session.query(DecisionRecord).one()
    assert row.kind == "merge", (
        f"row.kind={row.kind!r} — decision row must have kind='merge' (ADR 0099 §decision table)"
    )


def test_record_decision_decided_by_is_auto_resolver(sqlite_session: Session) -> None:
    """The decision row has decided_by='auto:resolver' (the automated-resolver identity).

    RED today: ImportError on record_decision.
    """
    cluster, _ = _two_source_cluster()
    record_decision(sqlite_session, cluster, reason="unit-test")
    sqlite_session.flush()

    row = sqlite_session.query(DecisionRecord).one()
    assert row.decided_by == "auto:resolver", (
        f"row.decided_by={row.decided_by!r} — must be 'auto:resolver' (ADR 0099)"
    )


def test_record_decision_evidence_with_reason(sqlite_session: Session) -> None:
    """When reason is non-empty, evidence == {"reason": reason}.

    RED today: ImportError on record_decision.
    """
    cluster, _ = _two_source_cluster()
    record_decision(sqlite_session, cluster, reason="sensitive-entity")
    sqlite_session.flush()

    row = sqlite_session.query(DecisionRecord).one()
    assert row.evidence == {"reason": "sensitive-entity"}, (
        f"row.evidence={row.evidence!r} — must be {{'reason': 'sensitive-entity'}} "
        "when reason is non-empty (ADR 0099)"
    )


def test_record_decision_evidence_is_none_when_reason_empty(sqlite_session: Session) -> None:
    """When reason is empty / falsy, evidence must be NULL (None) — not an empty dict.

    RED today: ImportError on record_decision.
    """
    cluster, _ = _two_source_cluster()
    record_decision(sqlite_session, cluster, reason="")
    sqlite_session.flush()

    row = sqlite_session.query(DecisionRecord).one()
    assert row.evidence is None, (
        f"row.evidence={row.evidence!r} — when reason is empty, evidence must be NULL "
        "(ADR 0099: '{{\"reason\": reason}} if reason else None')"
    )


def test_record_decision_member_ids_and_score(sqlite_session: Session) -> None:
    """The decision row carries the correct member_ids and score from the cluster.

    RED today: ImportError on record_decision.
    """
    cluster, _ = _two_source_cluster()
    record_decision(sqlite_session, cluster, reason="test")
    sqlite_session.flush()

    row = sqlite_session.query(DecisionRecord).one()
    assert sorted(row.member_ids) == sorted(cluster.member_ids), (
        f"row.member_ids={sorted(row.member_ids)} != "
        f"cluster.member_ids={sorted(cluster.member_ids)}"
    )
    assert row.canonical_id == cluster.canonical_id, (
        f"row.canonical_id={row.canonical_id!r} != cluster.canonical_id={cluster.canonical_id!r}"
    )
    assert row.score == cluster.score, f"row.score={row.score} != cluster.score={cluster.score}"


def test_record_decision_singleton_writes_nothing(sqlite_session: Session) -> None:
    """record_decision is a no-op when cluster.is_merge=False (the singleton guard).

    The pipeline calls record_decision only for is_merge=True, but record_decision itself
    must guard on cluster.is_merge so it is safe to call unconditionally.

    RED today: ImportError on record_decision.
    """
    entity = _stamped_company("solo", _SRC_A, "SoloCorp")
    singleton = ResolvedCluster(
        canonical_id="solo",
        member_ids=("solo",),
        entity=entity,
        score=1.0,
    )
    assert not singleton.is_merge, "test pre-condition: singleton must not be is_merge"

    record_decision(sqlite_session, singleton, reason="should-not-write")
    sqlite_session.flush()

    rows = sqlite_session.query(DecisionRecord).all()
    assert len(rows) == 0, (
        f"record_decision wrote {len(rows)} row(s) for a singleton — "
        "must be a no-op for is_merge=False (ADR 0099 / P-STMT-3b)"
    )


def test_record_decision_supersedes_and_superseded_by_are_null(sqlite_session: Session) -> None:
    """In step 1, supersedes and superseded_by are always NULL (reserved for Gate 3).

    RED today: ImportError on record_decision.
    """
    cluster, _ = _two_source_cluster()
    record_decision(sqlite_session, cluster, reason="test")
    sqlite_session.flush()

    row = sqlite_session.query(DecisionRecord).one()
    assert row.supersedes is None, (
        f"row.supersedes={row.supersedes!r} — must be NULL in step 1 (reserved for Gate 3)"
    )
    assert row.superseded_by is None, (
        f"row.superseded_by={row.superseded_by!r} — must be NULL in step 1 (reserved for Gate 3)"
    )


# ---------------------------------------------------------------------------
# Gate P1 (ADR 0106): SQLite ``seq`` listener runtime pin
# ---------------------------------------------------------------------------


def test_context_claim_seq_assigned_monotonically_in_sqlite(sqlite_session: Session) -> None:
    """Inserting ContextClaimRecords in an SQLite session assigns a monotonic non-NULL ``seq``.

    ``ContextClaimRecord.seq`` MUST reuse the existing, byte-unchanged ``_assign_sqlite_seq``
    ``before_insert`` listener (the ADR-0100 dialect-guarded SQLite-IDENTITY-fallback trap
    avoidance: Postgres IDENTITY is a no-op on SQLite, so a new lane's ``seq`` column needs the
    SAME listener registered, not a bespoke one) — pinned here at RUNTIME (no test today
    exercises any lane's listener actually firing on an INSERT).

    RED today: ImportError — ContextClaimRecord does not exist yet.
    """
    first = ContextClaimRecord(
        id=str(uuid.uuid4()),
        canonical_id="ctx-seq-test",
        entity_id="ctx-seq-test-member-1",
        key="wikidata_id",
        value="Q1",
        dataset="src-A",
        method="connector:map",
        retrieved_at="2026-01-01T00:00:00Z",
    )
    sqlite_session.add(first)
    sqlite_session.flush()

    assert first.seq is not None, (
        "ContextClaimRecord.seq is NULL after INSERT — the reused _assign_sqlite_seq "
        "before_insert listener must fire on SQLite (ADR 0106 / the ADR-0100 trap avoidance)"
    )

    second = ContextClaimRecord(
        id=str(uuid.uuid4()),
        canonical_id="ctx-seq-test",
        entity_id="ctx-seq-test-member-2",
        key="wikidata_id",
        value="Q2",
        dataset="src-B",
        method="connector:map",
        retrieved_at="2026-01-01T00:00:00Z",
    )
    sqlite_session.add(second)
    sqlite_session.flush()

    assert second.seq is not None, "second ContextClaimRecord.seq is NULL after INSERT"
    assert second.seq > first.seq, (
        f"ContextClaimRecord.seq must be MONOTONIC: first.seq={first.seq!r}, "
        f"second.seq={second.seq!r} — got second <= first"
    )
