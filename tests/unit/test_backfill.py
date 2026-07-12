"""Unit tests for Gate 2b — ``resolution/backfill.py``'s reader + dedup pre-filter (ADR 0113,
spec ``docs/reviews/GATE_2B_BACKFILL_SPEC.md`` §4). Docker-free: SQLite in-memory for the
Postgres-side pieces (mirrors ``tests/unit/test_erasure_scrub.py``'s ``_sqlite_sessions()`` +
JSONB shim) + a duck-typed Neo4j stub for ``backfill_spine``'s required ``neo4j`` kwarg.

API-CONTRACT NOTE (flagged for the builder — see ``tests/property/test_prop_backfill.py``'s
module docstring for the full rationale): the pinned ``backfill_spine(session, *, dry_run=False)``
signature omits a Neo4j client, but the mechanism must run the FROZEN
``scrub_stock(session, *, neo4j=...)`` internally (``resolution/erasure_scrub.py``, spec §8), so
``backfill_spine`` MUST itself accept one. Every call below passes
``backfill_spine(session, neo4j=<stub>, ...)``.

Covers:
  * ``load_erased_sources`` reads ``TaskRun.stats`` PYTHON-SIDE — ``kind="erase"`` AND
    ``status="ok"`` rows ONLY (a failed/running erase, or a different ``kind`` entirely, must
    never contribute); a duplicate ``TaskRun`` for the same source (an idempotent re-run)
    collapses to one entry (mirrors ``scrub_stock``'s own dedup-by-source_id, ADR 0107).
  * ``iter_backfill_members`` skips a ``{"erased": True}`` shell AND a member whose ``source_id``
    is in ``exclude_sources`` — proven as TWO INDEPENDENT mechanisms.
  * ``iter_backfill_members``'s ``canonical_id`` resolves BOTH a singleton self-row
    (``record_canonical`` only, no alias) and a merge alias (``record_alias`` to a DISTINCT
    survivor) — the same ``build_survivor_of`` resolver the projector's fold uses.
  * the ``statement_id`` dedup pre-filter (SF-2): a pre-existing ``StatementRecord`` carrying the
    EXACT ``statement_id`` the backfill would mint is skipped, never duplicated.

RED today: ``ImportError`` — ``worldmonitor.resolution.backfill`` does not exist yet (ADR 0113 /
Gate 2b).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, StatementRecord, TaskRun
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp

# ---- GATE IMPORT — does not exist yet (RED for the right reason, ADR 0113 / Gate 2b) ----
from worldmonitor.resolution.backfill import (
    backfill_spine,
    iter_backfill_members,
    load_erased_sources,
)
from worldmonitor.resolution.canonical import record_alias, record_canonical
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.statements import fuse_statement_rows

_RETRIEVED_AT = "2026-07-12T00:00:00Z"


# ---------------------------------------------------------------------------
# SQLite JSONB shim (idempotent if already registered by another test module) — this file
# must be self-contained (mirrors test_erasure_scrub.py's unit-test file exactly).
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _sqlite_sessions() -> tuple[Any, sessionmaker[Session]]:
    engine = make_engine("sqlite:///:memory:")
    create_all(engine)
    return engine, session_factory(engine)


def _member_entity(
    member_id: str, *, source_id: str, name: str = "Unit Backfill Corp"
) -> FtmEntity:
    entity = make_entity({"id": member_id, "schema": "Person", "properties": {"name": [name]}})
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{member_id}.json",
        ),
    )


def _queue_item(entity: FtmEntity, *, status: str = "resolved") -> ErQueueItem:
    assert entity.id is not None
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        entity_id=entity.id,
        raw_entity=entity.to_dict(),
        source_record=f"s3://landing/{entity.id}.json",
        status=status,
    )


class _StubNeo4j:
    """A duck-typed Neo4j-client stub (mirrors ``Neo4jClient.execute_read``/``execute_write``'s
    signature only, ``test_erasure_scrub.py``'s unit-test idiom) — no testcontainer needed. Every
    node "does not exist" (empty read), so ``scrub_stock``'s internal ``prune_live_to_fold`` (if
    it ever fires) is a safe no-op; write calls are merely recorded for inspection."""

    def __init__(self) -> None:
        self.write_calls: list[dict[str, Any]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        return []

    def execute_write(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.write_calls.append({"query": query, "params": params})
        return []


# ===========================================================================
# load_erased_sources — TaskRun.stats read Python-side (SQLite-safe)
# ===========================================================================


def test_load_erased_sources_reads_task_run_stats_python_side() -> None:
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        session.add(
            TaskRun(id=str(uuid.uuid4()), kind="erase", status="ok", stats={"source_id": "esrc:a"})
        )
        # A second "ok" erase run for the SAME source (an idempotent re-run) — must collapse.
        session.add(
            TaskRun(id=str(uuid.uuid4()), kind="erase", status="ok", stats={"source_id": "esrc:a"})
        )
        session.add(
            TaskRun(id=str(uuid.uuid4()), kind="erase", status="ok", stats={"source_id": "esrc:b"})
        )
        # A FAILED erase — never actually erased anything, must NOT be excluded.
        session.add(
            TaskRun(
                id=str(uuid.uuid4()),
                kind="erase",
                status="error",
                stats={"source_id": "esrc:failed"},
            )
        )
        # A different TaskRun kind entirely, carrying an unrelated "source_id"-shaped payload.
        session.add(
            TaskRun(
                id=str(uuid.uuid4()),
                kind="ingest",
                status="ok",
                stats={"source_id": "esrc:ingest-noise"},
            )
        )
        # A "running" (not yet "ok") erase — must NOT be excluded.
        session.add(
            TaskRun(
                id=str(uuid.uuid4()),
                kind="erase",
                status="running",
                stats={"source_id": "esrc:running"},
            )
        )
        session.commit()

        result = load_erased_sources(session)

    assert result == {"esrc:a", "esrc:b"}, (
        "load_erased_sources must return exactly the DISTINCT source_ids of kind='erase' "
        f"status='ok' TaskRun rows; got {result!r}"
    )
    engine.dispose()


# ===========================================================================
# iter_backfill_members — shell skip + exclusion-set skip, independently
# ===========================================================================


def test_iter_backfill_members_skips_redacted_shell_and_excluded_source_independently() -> None:
    engine, sessions = _sqlite_sessions()
    kept_src = "ksrc:iter-unit"
    excl_src = "exclsrc:iter-unit"
    shell_src = "shellsrc:iter-unit"
    m_kept = "iter-unit-kept"
    m_excl = "iter-unit-excl"
    m_shell = "iter-unit-shell"

    kept_entity = _member_entity(m_kept, source_id=kept_src)
    excl_entity = _member_entity(m_excl, source_id=excl_src)

    with sessions() as session:
        session.add(_queue_item(kept_entity))
        session.add(_queue_item(excl_entity))
        session.add(
            ErQueueItem(
                id=str(uuid.uuid4()),
                connector_id="opensanctions",
                entity_id=m_shell,
                raw_entity={"erased": True, "source_id": shell_src},
                source_record=f"s3://landing/{m_shell}.json",
                status="resolved",
            )
        )
        record_canonical(session, m_kept)
        record_canonical(session, m_excl)
        session.commit()

        yielded = list(iter_backfill_members(session, exclude_sources={excl_src}))

    yielded_ids = {entity.id for entity, _canonical_id in yielded}
    assert yielded_ids == {m_kept}, (
        "expected ONLY the kept, non-shell, non-excluded member to be yielded; got "
        f"{yielded_ids!r} (the shell {m_shell!r} and the excluded-source member {m_excl!r} must "
        "both be absent)"
    )
    engine.dispose()


# ===========================================================================
# iter_backfill_members — canonical_id resolution: singleton self-row + merge alias
# ===========================================================================


def test_iter_backfill_members_resolves_singleton_self_row_and_merge_alias() -> None:
    engine, sessions = _sqlite_sessions()
    singleton_id = "iter-unit-singleton"
    member_id = "iter-unit-merge-member"
    survivor_id = "iter-unit-merge-survivor"
    singleton_entity = _member_entity(singleton_id, source_id="src:iter-unit-singleton")
    member_entity = _member_entity(member_id, source_id="src:iter-unit-merge")

    with sessions() as session:
        session.add(_queue_item(singleton_entity))
        session.add(_queue_item(member_entity))
        # Singleton: self-row ONLY (canonical_alias == canonical_id == its own id).
        record_canonical(session, singleton_id)
        # Merge: the survivor's self-row + an alias row FROM the member TO the survivor.
        record_canonical(session, survivor_id)
        record_alias(session, survivor_id, member_id)
        session.commit()

        yielded = {
            entity.id: canonical_id
            for entity, canonical_id in iter_backfill_members(session, exclude_sources=set())
        }

    assert yielded[singleton_id] == singleton_id, (
        f"a singleton with only its own self-row must resolve to ITSELF, got "
        f"{yielded[singleton_id]!r}"
    )
    assert yielded[member_id] == survivor_id, (
        f"a merge-aliased member must resolve to its SURVIVOR ({survivor_id!r}), not its own id; "
        f"got {yielded[member_id]!r}"
    )
    engine.dispose()


# ===========================================================================
# The statement_id dedup pre-filter (SF-2)
# ===========================================================================


def test_backfill_spine_dedup_prefilter_skips_a_preexisting_statement_id() -> None:
    engine, sessions = _sqlite_sessions()
    member_id = "unit-dedup-m1"
    canonical_id = member_id  # a true singleton: canonical_id == its own id, no alias needed
    source_id = "src:unit-dedup"
    member = _member_entity(member_id, source_id=source_id, name="Dedup Prefilter Corp")

    expected_rows = fuse_statement_rows(
        ResolvedCluster(
            canonical_id=canonical_id, member_ids=(member_id,), entity=member, score=1.0
        ),
        {member_id: member},
    )
    assert expected_rows, "sanity: a prop-bearing singleton must yield >= 1 expected row"
    preexisting = expected_rows[0]

    with sessions() as session:
        session.add(_queue_item(member))
        record_canonical(session, canonical_id)
        # Pre-seed a StatementRecord carrying the EXACT statement_id the backfill would mint —
        # simulating "already dual-written" (post-2a) or a prior backfill run.
        session.add(
            StatementRecord(
                id=str(uuid.uuid4()),
                statement_id=preexisting.statement_id,
                canonical_id=canonical_id,
                entity_id=member_id,
                schema="Person",
                prop=preexisting.prop,
                value=preexisting.value,
                dataset=source_id,
                reliability="A",
                retrieved_at=_RETRIEVED_AT,
                raw_pointer=f"s3://landing/{member_id}.json",
                first_seen=_RETRIEVED_AT,
                last_seen=_RETRIEVED_AT,
                method=None,
                scope="default",
            )
        )
        session.commit()

    with sessions() as session:
        result = backfill_spine(session, neo4j=_StubNeo4j())  # type: ignore[arg-type]

    assert result.skipped_duplicate >= 1, (
        f"SF-2 dedup pre-filter VIOLATED: expected >= 1 skipped_duplicate, got "
        f"{result.skipped_duplicate}"
    )

    with sessions() as session:
        rows = list(
            session.execute(
                select(StatementRecord).where(
                    StatementRecord.statement_id == preexisting.statement_id
                )
            ).scalars()
        )
    assert len(rows) == 1, (
        "SF-2 dedup pre-filter VIOLATED: the pre-existing statement_id must NOT be duplicated, "
        f"got {len(rows)} row(s)"
    )
    engine.dispose()
