"""Unit tests for Gate P2 — ``resolution/erasure_scrub.py`` + ``graph/ops.py::set_node_values``
(ADR 0107, spec §4). Docker-free where possible (SQLite in-memory for the Postgres-side pieces;
a duck-typed stub for the Neo4j-side piece — no testcontainer needed for either).

Covers:
  * the ``erased_member_ids`` derivation ordering — COMPUTE BEFORE DELETE (surprise #2: a
    delete-first bug returns an EMPTY set, since the SELECT would then find nothing).
  * the ``(dataset == source_id) OR (entity_id IN erased_member_ids)`` reach predicate (SF-1
    fallback-keyed residual closure) — a member with BOTH a ``dataset``-keyed row AND a
    ``member.id``-keyed fallback row (the P1-writer ``dataset = source_id or member.id or ""``
    residual) has BOTH rows reached.
  * decision-row redaction REASSIGNS the JSONB list — an in-place ``.remove()`` does NOT persist
    (a plain JSONB column with no ``MutableList``/``as_mutable`` — SQLAlchemy change-detection
    never sees it); a standalone failing-idiom probe demonstrates the underlying gotcha directly
    (independent of ``scrub_log_lanes``, using only the EXISTING ``DecisionRecord`` model — this
    probe test alone is NOT import-RED, but the whole file still collection-errors on the
    module-level import below, per the gate's named import-RED exception list).
  * ``set_node_values`` reads the node's CURRENT full props before merging — a stubbed node
    retains ``prov_*``/``id``/``caption`` untouched by the compared-prop/anchor write (HIGH-1),
    the write is a SINGLE full-dict ``SET`` (never a partial-map / dynamic-property write), and
    anchor removal is REMOVE-only (an anchor key present pre-write and NOT re-affirmed by
    ``remove_anchor_keys`` is absent post-write; no NEW anchor value is ever introduced,
    HIGH-2).

RED today: ``ImportError`` — neither ``worldmonitor.resolution.erasure_scrub`` nor
``worldmonitor.graph.ops.set_node_values`` exists yet. This whole file is one of the gate's
NAMED import-RED exceptions (it necessarily exercises the new surface throughout).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ContextClaimRecord, DecisionRecord, StatementRecord
from worldmonitor.graph.ops import set_node_values  # NEW surface — does not exist yet
from worldmonitor.resolution.erasure_scrub import (
    scrub_log_lanes,
)  # NEW surface — does not exist yet

_RETRIEVED_AT = "2026-07-11T00:00:00Z"


# ---------------------------------------------------------------------------
# SQLite JSONB shim (idempotent if already registered by another test module) — this file
# must be self-contained: it fails standalone without it (other JSONB-bearing tables reachable
# from Base.metadata, e.g. er_queue_item.raw_entity/task_run.stats, don't compile to SQLite).
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _sqlite_sessions() -> tuple[Any, sessionmaker[Session]]:
    """A fresh, Docker-free in-memory SQLite engine + session factory (SQLite gets the
    ``_assign_sqlite_seq`` ``before_insert`` treatment already wired on ``StatementRecord``/
    ``ContextClaimRecord``/``DecisionRecord``, so ``session.add`` + ``session.commit`` works the
    same shape as the Postgres path for these tables)."""
    engine = make_engine("sqlite:///:memory:")
    create_all(engine)
    return engine, session_factory(engine)


def _stmt(
    canonical_id: str, entity_id: str, prop: str, value: str, dataset: str
) -> StatementRecord:
    return StatementRecord(
        id=str(uuid.uuid4()),
        statement_id=str(uuid.uuid4()),
        canonical_id=canonical_id,
        entity_id=entity_id,
        schema="Person",
        prop=prop,
        value=value,
        dataset=dataset,
        reliability="A",
        retrieved_at=_RETRIEVED_AT,
        raw_pointer=f"s3://landing/{dataset}/{entity_id}.json",
        first_seen=_RETRIEVED_AT,
        last_seen=_RETRIEVED_AT,
        method=None,
        scope="default",
    )


# ===========================================================================
# erased_member_ids — compute-before-delete ordering (surprise #2)
# ===========================================================================


def test_erased_member_ids_is_computed_before_the_delete() -> None:
    """A delete-first implementation bug returns an EMPTY ``erased_member_ids`` (the SELECT
    finds nothing once the rows are already gone) — this directly probes the ordering, not just
    the end-state row count."""
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        session.add(_stmt("surv-1", "m1", "name", "Ordering Probe", "esrc:ordering"))
        session.commit()

        result = scrub_log_lanes(session, "esrc:ordering")
        session.commit()

    assert "m1" in result.erased_member_ids, (
        "erased_member_ids must be computed BEFORE the delete — got "
        f"{result.erased_member_ids!r} (empty/missing 'm1' means the implementation deleted "
        "first, then computed against an already-empty table)"
    )
    engine.dispose()


# ===========================================================================
# The (dataset == source_id) OR (entity_id IN erased_member_ids) reach predicate (SF-1)
# ===========================================================================


def test_reach_predicate_sweeps_the_fallback_keyed_residual_row_too() -> None:
    """A member with ONE ``dataset``-keyed row (reachable directly) AND ONE ``member.id``-keyed
    fallback row (the P1-writer ``dataset = source_id or member.id or ""`` residual,
    ``statements.py:196``) must have BOTH rows swept — the fallback row is reached via
    ``entity_id IN erased_member_ids``, not via ``dataset`` (its dataset is the bare member id,
    never equal to the erased ``source_id``)."""
    engine, sessions = _sqlite_sessions()
    erased_src = "esrc:fallback"
    with sessions() as session:
        # The reachable row (dataset == source_id).
        session.add(_stmt("surv-2", "m-fallback", "name", "Reachable", erased_src))
        # The fallback-keyed row: dataset == the member's OWN id (statements.py:196's `or
        # member.id` branch), NOT the source_id — unreachable by a bare `dataset == source_id`.
        session.add(_stmt("surv-2", "m-fallback", "alias", "FallbackKeyed", "m-fallback"))
        # An UNRELATED row that must survive (non-vacuity).
        session.add(_stmt("surv-3", "m-other", "name", "Untouched", "ksrc:other"))
        session.commit()

        scrub_log_lanes(session, erased_src)
        session.commit()

    with sessions() as session:
        remaining = {
            (row.entity_id, row.dataset)
            for row in session.execute(select(StatementRecord)).scalars()
        }
    assert ("m-fallback", erased_src) not in remaining
    assert ("m-fallback", "m-fallback") not in remaining, (
        "the fallback-keyed row (dataset == member.id) must ALSO be swept via "
        "entity_id IN erased_member_ids, not just the dataset-matched row"
    )
    assert ("m-other", "ksrc:other") in remaining, "an unrelated member's row must survive"
    engine.dispose()


def test_reach_predicate_also_sweeps_context_claim_rows() -> None:
    """The SAME ``(dataset OR entity_id)`` reach predicate applies to the ``context_claim`` lane
    (SF-1: ``statement`` and ``context_claim`` reached identically)."""
    engine, sessions = _sqlite_sessions()
    erased_src = "esrc:ctx-reach"
    with sessions() as session:
        session.add(
            ContextClaimRecord(
                id=str(uuid.uuid4()),
                canonical_id="surv-ctx",
                entity_id="m-ctx",
                key="wikidata_id",
                value="Q4242",
                dataset=erased_src,
                method="connector:map",
                retrieved_at=_RETRIEVED_AT,
                scope="default",
            )
        )
        session.commit()

        scrub_log_lanes(session, erased_src)
        session.commit()

    with sessions() as session:
        remaining = list(session.execute(select(ContextClaimRecord)).scalars())
    assert remaining == [], (
        f"context_claim row(s) with dataset={erased_src!r} must be swept, got {remaining!r}"
    )
    engine.dispose()


# ===========================================================================
# Decision-row redaction — REASSIGN, never in-place .remove()
# ===========================================================================


def test_inplace_remove_on_jsonb_list_does_not_persist_the_underlying_gotcha() -> None:
    """The failing-idiom PROBE (independent of ``scrub_log_lanes``): ``DecisionRecord.member_ids``
    is a plain JSONB column with no ``MutableList``/``as_mutable`` wrapper, so an in-place
    ``list.remove()`` mutation is invisible to SQLAlchemy's change-detection and is silently
    LOST on commit — proving WHY the redaction must reassign a new list."""
    engine, sessions = _sqlite_sessions()
    with sessions() as session:
        session.add(
            DecisionRecord(
                id="probe-decision",
                canonical_id="surv-probe",
                kind="merge",
                member_ids=["m1", "m2"],
                score=0.5,
                decided_by="auto:resolver",
                evidence=None,
                supersedes=None,
                superseded_by=None,
                scope="default",
            )
        )
        session.commit()

    with sessions() as session:
        row = session.execute(
            select(DecisionRecord).where(DecisionRecord.id == "probe-decision")
        ).scalar_one()
        row.member_ids.remove("m1")  # THE FAILING IDIOM — in-place mutation, never reassigned
        session.commit()

    with sessions() as fresh_session:
        reread = fresh_session.execute(
            select(DecisionRecord).where(DecisionRecord.id == "probe-decision")
        ).scalar_one()
    assert reread.member_ids == ["m1", "m2"], (
        "SQLAlchemy change-detection did NOT persist the in-place .remove() — "
        f"got {reread.member_ids!r} (if this list is now ['m2'], SQLAlchemy's JSONB tracking "
        "changed and the underlying gotcha this probe demonstrates no longer holds — the "
        "builder's redaction must still explicitly reassign, never rely on in-place mutation)"
    )
    engine.dispose()


def test_scrub_log_lanes_redaction_reassigns_and_persists() -> None:
    """The REAL redaction (via ``scrub_log_lanes``) must actually persist across a fresh
    session — proving it reassigns (or ``flag_modified``s) rather than relying on the
    in-place-mutation idiom the sibling probe test shows is silently lost."""
    engine, sessions = _sqlite_sessions()
    erased_src = "esrc:redact"
    with sessions() as session:
        session.add(_stmt("surv-redact", "m-redact", "name", "Redact Me", erased_src))
        session.add(
            DecisionRecord(
                id="redact-decision",
                canonical_id="surv-redact",
                kind="merge",
                member_ids=["m-redact", "m-keep"],
                score=0.6,
                decided_by="auto:resolver",
                evidence=None,
                supersedes=None,
                superseded_by=None,
                scope="default",
            )
        )
        session.commit()

        scrub_log_lanes(session, erased_src)
        session.commit()

    with sessions() as fresh_session:
        reread = fresh_session.execute(
            select(DecisionRecord).where(DecisionRecord.id == "redact-decision")
        ).scalar_one()
    assert reread.member_ids == ["m-keep"], (
        f"the redaction must PERSIST across a fresh session (reassign, not in-place .remove()); "
        f"got member_ids={reread.member_ids!r}"
    )
    engine.dispose()


# ===========================================================================
# set_node_values — read-current-props-then-merge (HIGH-1) + REMOVE-only anchors (HIGH-2)
# ===========================================================================


class _StubNeo4j:
    """A duck-typed Neo4j-client stub (mirrors ``Neo4jClient.execute_read``/``execute_write``'s
    signature only) — no testcontainer needed for this write-shape unit test."""

    def __init__(self, node_props: dict[str, Any]) -> None:
        self._node_props = dict(node_props)
        self.write_calls: list[dict[str, Any]] = []

    def execute_read(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        return [{"props": dict(self._node_props)}]

    def execute_write(self, query: str, /, **params: Any) -> list[dict[str, Any]]:
        self.write_calls.append({"query": query, "params": params})
        return []


def _sole_dict_param(params: dict[str, Any]) -> dict[str, Any]:
    """The one dict-valued kwarg among an ``execute_write`` call's params — the full-prop-replace
    payload — without hardcoding the builder's exact kwarg NAME (only that there is EXACTLY one,
    proving a single full-map write, never a Cypher call built from multiple scalar params that
    would imply a dynamic/partial SET)."""
    dicts = [v for v in params.values() if isinstance(v, dict)]
    assert len(dicts) == 1, (
        f"expected exactly one dict-valued execute_write param (the full-prop-replace map), "
        f"got {params!r}"
    )
    return dicts[0]


def test_set_node_values_reads_current_props_and_preserves_provenance() -> None:
    """HIGH-1: the write MUST read the node's CURRENT full props first and merge — a bare
    ``SET n = <partial map>`` built ONLY from ``compared_props``/anchors would wipe
    ``prov_source_id``/``prov_witnesses``/``id``/``caption`` (G1). A stubbed node with those
    fields set must retain them, byte-unchanged, in the write payload."""
    stub = _StubNeo4j(
        {
            "id": "surv-stub",
            "caption": "Stub Survivor",
            "prov_source_id": "keep-src",
            "prov_witnesses": '{"name": ["keep-src"]}',
            "datasets": ["keep-src"],
            "name": ["Stub Survivor"],
            "alias": ["OnlyFromErased", "OnlyFromKept"],
            "wikidata_id": "Q555555",
        }
    )

    set_node_values(
        stub,  # type: ignore[arg-type]
        "surv-stub",
        compared_props={"alias": ["OnlyFromKept"]},
        remove_anchor_keys=["wikidata_id"],
    )

    assert len(stub.write_calls) == 1, "exactly one Neo4j write for one node"
    props = _sole_dict_param(stub.write_calls[0]["params"])

    assert props.get("id") == "surv-stub", "HIGH-1 VIOLATED: id must be preserved"
    assert props.get("caption") == "Stub Survivor", "HIGH-1 VIOLATED: caption must be preserved"
    assert props.get("prov_source_id") == "keep-src", (
        "HIGH-1 VIOLATED: prov_source_id must be preserved (G1)"
    )
    assert props.get("prov_witnesses") == '{"name": ["keep-src"]}', (
        "HIGH-1 VIOLATED: prov_witnesses must be preserved (G1)"
    )
    assert props.get("datasets") == ["keep-src"], "HIGH-1 VIOLATED: datasets must be preserved"
    assert props.get("alias") == ["OnlyFromKept"], (
        "the compared prop must be updated to the fold's row-granular result"
    )
    assert "wikidata_id" not in props, "HIGH-2: the erased-source anchor must be REMOVEd"


def test_set_node_values_anchor_removal_is_remove_only_never_sets_a_new_value() -> None:
    """HIGH-2: ``remove_anchor_keys`` may only REMOVE an existing anchor — it must never
    introduce/replace it with a NEW value (which would risk a UNIQUE-constraint collision,
    ``graph/constraints.py:24-30``). A ``compared_props`` write that does NOT mention an anchor
    key at all must leave that anchor untouched (no gratuitous rebuild)."""
    stub = _StubNeo4j(
        {
            "id": "surv-stub-2",
            "prov_source_id": "keep-src",
            "wikidata_id": "Q777777",
            "geonames_id": "999",
            "name": ["Untouched Name"],
        }
    )

    set_node_values(
        stub,  # type: ignore[arg-type]
        "surv-stub-2",
        compared_props={},
        remove_anchor_keys=["geonames_id"],
    )

    props = _sole_dict_param(stub.write_calls[0]["params"])
    assert props.get("wikidata_id") == "Q777777", (
        "an anchor NOT named in remove_anchor_keys must be left untouched"
    )
    assert "geonames_id" not in props, "the named anchor must be REMOVEd"
    assert props.get("name") == ["Untouched Name"], "an unrelated prop must be preserved"


def test_set_node_values_empty_compared_prop_removes_it_from_the_node() -> None:
    """A ``compared_props`` entry with an EMPTY value list means the fold's row-granular result
    for that prop is now empty (every value was erased-source-only) — the prop must be REMOVEd
    from the node entirely, not left as a stale non-empty list."""
    stub = _StubNeo4j(
        {
            "id": "surv-stub-3",
            "prov_source_id": "keep-src",
            "passportNumber": ["P-9-SECRET"],
        }
    )

    set_node_values(
        stub,  # type: ignore[arg-type]
        "surv-stub-3",
        compared_props={"passportNumber": []},
        remove_anchor_keys=[],
    )

    props = _sole_dict_param(stub.write_calls[0]["params"])
    assert "passportNumber" not in props, (
        "an emptied compared prop must be REMOVEd from the node, not left stale"
    )
    assert props.get("prov_source_id") == "keep-src"
