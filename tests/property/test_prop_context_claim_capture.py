"""Property/metamorphic tests for Gate P1 — the context-claim capture lane (ADR 0106).

Six mandatory ``@given`` invariants (CLAUDE.md build-discipline / spec §3), split across the
gate's two slices:

P-CTX-1  LOSSLESS anchor capture (Slice P1-a; real-Postgres round-trip) — the persisted
         ``context_claim`` rows equal, tuple-for-tuple, the independently-derived per-member
         anchor projection. MANDATORY non-vacuity (spec §3, adversarial-verify HIGH): the
         generator forces (i) members with DISTINCT ``dataset``s and (ii) a cross-member
         CONFLICTING anchor pair (same key, different values) — per-member capture must write
         BOTH conflicting rows; a merged-entity capture (whose ``merge_context`` union makes
         ``get_anchors`` omit the key) would write ZERO and is caught here.

P-CTX-2  NON-MUTATION fence (Slice P1-a) — the writers mutate no FtM entity and
         ``session.add`` only ``ContextClaimRecord`` (never a Statement/Decision/MergeAudit row
         or a raw entity).

P-CTX-3  PROVENANCE-COMPLETE + APPEND-ONLY + skip-unstamped (Slice P1-a) — every written row
         has non-NULL ``method`` AND ``retrieved_at``; the writer issues only ``session.add``
         (never UPDATE/DELETE/``session.delete``); an unstamped / no-``retrieved_at`` member's
         anchors are skipped (never written naked); a no-anchor member yields zero rows.

P-CTX-4  ANCHOR ROUND-TRIP FIDELITY (Slice P1-b; pure) — on a single-batch, no-conflict
         anchored corpus, ``reconstruct_entities`` + ``get_anchors`` on the fold entity equals
         ``get_anchors`` on the directly-merged entity.

P-CTX-5  OMIT-ON-CONFLICT PARITY (Slice P1-b; pure) — a survivor whose context rows hold >1
         distinct value for a key has that key OMITTED by the fold's ``get_anchors`` —
         identical to the merged-entity path; a co-existing single-value key stays present
         (guards against omit-everything).

P-CTX-6  INCREMENTAL == FULL-REBUILD WITH ANCHORS (Slice P1-b; extends P-FOLD-2, real DB +
         Neo4j) — folding a multi-batch anchored log incrementally (INCLUDING a
         context-claim-ONLY delta for an already statement-bearing survivor) reproduces the
         SAME node anchors as one ``full_rebuild``. Non-vacuity: directly asserts the
         incrementally-folded node actually carries the bare anchor key (an
         implementation that never reconstructs anchors would otherwise satisfy
         ``incr == full`` VACUOUSLY — both sides anchor-empty).

All P-CTX-1..3 tests are RED at collection time: the module-level imports of
``ContextClaimRecord`` (``worldmonitor.db.models``) and ``fuse_context_claim_rows`` /
``record_context_claims`` (``worldmonitor.resolution.statements``) fail with ``ImportError`` —
those symbols do not exist until the builder creates them (Gate 2a / test_prop_statement_spine.py
precedent). P-CTX-4/5/6 additionally call ``reconstruct_entities(..., context_claim_rows=...)`` —
an additive keyword ``reconstruct_entities`` does not accept yet — but the file already fails at
collection via the imports above, so this is moot until Slice P1-a lands; once P1-a is built
alone (before P1-b), P-CTX-4/5/6 fail with ``TypeError: unexpected keyword argument
'context_claim_rows'`` (assertion-adjacent RED, the correct failure once the imports resolve).

Container-heavy examples wrap their per-example engine in ``try/finally: engine.dispose()``
(memory: given-red-tests-leak-connections — a per-example engine + end-only dispose exhausts
Postgres connections and masks the real assertion on the RED path).
"""

from __future__ import annotations

import copy
import json
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
import strategies as wm
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from worldmonitor.db.engine import create_all, make_engine, session_factory

# ----- GATE IMPORTS — fail at collection until builder creates them (RED for right reason) -----
from worldmonitor.db.models import (  # noqa: E402
    Base,
    ContextClaimRecord,
    DecisionRecord,
    MergeAudit,
    StatementRecord,
)
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS, get_anchors, set_anchor
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, get_provenance, stamp
from worldmonitor.resolution.projector import project, reconstruct_entities

# This import fails until builder creates fuse_context_claim_rows/record_context_claims
from worldmonitor.resolution.statements import (  # noqa: E402
    fuse_context_claim_rows,
    record_context_claims,
)

_SETTINGS = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

_ALNUM = "ABCDEFGHIJ0123456789"


# ---------------------------------------------------------------------------
# Shared graph_signature (kept in sync manually with test_prop_fold_engine.py /
# tests/integration/test_projector.py — P-CTX-6 is a real-DB + real-Neo4j property).
# ---------------------------------------------------------------------------


def _stable_val(v: Any) -> str:
    if isinstance(v, list):
        return json.dumps(sorted(str(x) for x in v), ensure_ascii=False)
    return json.dumps(v, default=str, ensure_ascii=False, sort_keys=True)


def graph_signature(client: Neo4jClient) -> tuple[tuple, tuple]:
    """Byte-comparable canonical fingerprint of the full graph in ``client`` (anchors INCLUDED —
    P-CTX-6 compares node anchors, so no exclusion is applied here)."""
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
                        (_stable_val(k), _stable_val(v)) for k, v in (row["props"] or {}).items()
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


def _cleanup_postgres(postgres_dsn: str) -> None:
    """Truncate ALL relational tables between hypothesis examples (P-FOLD-2 idiom)."""
    engine = make_engine(postgres_dsn)
    create_all(engine)
    with engine.begin() as conn:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    engine.dispose()


# ===========================================================================
# P-CTX-1: LOSSLESS anchor capture (real-Postgres round-trip)
# ===========================================================================


@dataclass(frozen=True)
class _ContextConflictScenario:
    """A member set forcing P-CTX-1's MANDATORY non-vacuity coverage (spec §3)."""

    canonical_id: str
    members: tuple[Any, ...]
    conflict_field: str


@st.composite
def _anchor_conflict_scenario(draw: st.DrawFn) -> _ContextConflictScenario:
    """2..4 members: pairwise-DISTINCT ``dataset``s (kills merged-entity capture's dataset
    loss) + a FORCED cross-member CONFLICTING anchor pair (same key, distinct values) + 0..k
    other (non-conflicting) anchors on the remaining members, all stamped with real
    provenance."""
    n = draw(st.integers(min_value=2, max_value=4))
    ids = list(wm.ID_POOL[:n])
    sources = draw(st.permutations(list(wm.SOURCE_POOL)))[:n]

    members: list[Any] = []
    for i, (eid, src) in enumerate(zip(ids, sources, strict=True)):
        entity = draw(wm.ftm_entity(entity_id=eid, schema="Company"))
        retrieved_at = f"2026-02-{i + 1:02d}T00:00:00Z"
        stamp(
            entity,
            Provenance(
                source_id=src,
                retrieved_at=retrieved_at,
                reliability="B",
                source_record=f"s3://landing/{eid}.json",
            ),
        )
        members.append(entity)

    conflict_field = draw(st.sampled_from(CANONICAL_ID_FIELDS))
    set_anchor(members[0], conflict_field, "CONFLICT-VALUE-A")
    set_anchor(members[1], conflict_field, "CONFLICT-VALUE-B")

    other_fields = [f for f in CANONICAL_ID_FIELDS if f != conflict_field]
    for i in range(2, n):
        if other_fields and draw(st.booleans()):
            field = draw(st.sampled_from(other_fields))
            value = draw(st.text(alphabet=_ALNUM, min_size=1, max_size=6))
            set_anchor(members[i], field, value)

    canonical_id = "ctx-test-" + "-".join(ids)
    return _ContextConflictScenario(
        canonical_id=canonical_id, members=tuple(members), conflict_field=conflict_field
    )


def _oracle_dataset(member: Any) -> str:
    """Independent reproduction of the ADR-0106 §2.a.4 fallback WITHOUT calling the writer."""
    prov = get_provenance(member)
    if prov is not None and prov.source_id:
        return prov.source_id
    return member.id or ""


def _oracle_context_claim_tuples(
    canonical_id: str, members: Sequence[Any]
) -> set[tuple[str, str, str, str, str, str, str]]:
    """P-CTX-1's INDEPENDENT oracle — NEVER calls fuse_context_claim_rows."""
    expected: set[tuple[str, str, str, str, str, str, str]] = set()
    for m in members:
        prov = get_provenance(m)
        if prov is None or not prov.retrieved_at:
            continue  # skip-and-log: unprovenanceable anchor, never written naked
        dataset = _oracle_dataset(m)
        entity_id = m.id or canonical_id
        for field, value in get_anchors(m).items():
            expected.add(
                (canonical_id, entity_id, field, value, dataset, "connector:map", prov.retrieved_at)
            )
    return expected


@given(scenario=_anchor_conflict_scenario())
@_SETTINGS
def test_p_ctx_1_lossless_anchor_capture(
    scenario: _ContextConflictScenario, postgres_dsn: str
) -> None:
    """P-CTX-1: persisted context_claim rows == the independent per-member anchor projection.

    RED today: ImportError — ContextClaimRecord, record_context_claims do not exist yet.
    """
    engine = make_engine(postgres_dsn)
    try:
        Base.metadata.create_all(engine)  # idempotent; adds context_claim once the builder lands it

        with Session(engine) as session:
            record_context_claims(session, scenario.canonical_id, scenario.members)
            session.flush()  # visible in this transaction, NEVER committed

            rows = list(
                session.execute(
                    select(ContextClaimRecord).where(
                        ContextClaimRecord.canonical_id == scenario.canonical_id
                    )
                ).scalars()
            )

            expected = _oracle_context_claim_tuples(scenario.canonical_id, scenario.members)
            actual = {
                (r.canonical_id, r.entity_id, r.key, r.value, r.dataset, r.method, r.retrieved_at)
                for r in rows
            }

            assert actual == expected, (
                f"P-CTX-1 LOSSLESS CAPTURE VIOLATED for canonical_id={scenario.canonical_id!r}\n"
                f"  expected {len(expected)} claim tuple(s), got {len(actual)}\n"
                f"  invented (in actual, not oracle): {actual - expected}\n"
                f"  dropped  (in oracle, not actual):  {expected - actual}"
            )

            # --- MANDATORY non-vacuity (spec §3): the conflict pair yields BOTH rows ---
            conflict_rows = [r for r in rows if r.key == scenario.conflict_field]
            assert len(conflict_rows) >= 2, (
                "P-CTX-1 NON-VACUITY VIOLATED: cross-member conflicting anchor field "
                f"{scenario.conflict_field!r} produced {len(conflict_rows)} row(s) — expected "
                ">= 2. Per-member capture must write BOTH conflicting members' rows; a "
                "merged-entity capture (whose merge_context union makes get_anchors OMIT the "
                "field) would silently write ZERO for it."
            )
            assert actual, "P-CTX-1 NON-VACUITY: expected >= 1 anchored row overall"

            for row in rows:
                assert row.method is not None, f"row for {row.entity_id!r} has NULL method"
                assert row.retrieved_at is not None, (
                    f"row for {row.entity_id!r} has NULL retrieved_at"
                )

            session.rollback()  # ALWAYS rollback — never commit to the shared container
    finally:
        engine.dispose()


# ===========================================================================
# P-CTX-2: NON-MUTATION fence
# ===========================================================================


@st.composite
def _members_with_optional_anchors(draw: st.DrawFn) -> list[Any]:
    n = draw(st.integers(min_value=1, max_value=4))
    ids = list(wm.ID_POOL[:n])
    members: list[Any] = []
    for eid in ids:
        entity = draw(wm.source_tagged_entity(entity_id=eid, schema="Company"))
        if draw(st.booleans()):
            field = draw(st.sampled_from(CANONICAL_ID_FIELDS))
            value = draw(st.text(alphabet=_ALNUM, min_size=1, max_size=6))
            set_anchor(entity, field, value)
        members.append(entity)
    return members


@given(members=_members_with_optional_anchors())
@_SETTINGS
def test_p_ctx_2_non_mutation_fence(members: list[Any]) -> None:
    """P-CTX-2: fuse_context_claim_rows / record_context_claims mutate NOTHING.

    Non-vacuity: an impl that ``set_anchor``s onto a member (mutating context) fails; one that
    also emits a Statement/Decision/MergeAudit row fails.

    RED today: ImportError — fuse_context_claim_rows, record_context_claims, ContextClaimRecord.
    """
    canonical_id = "ctx-test-nonmut-" + "-".join(m.id or "" for m in members)
    snapshots_before = {m.id: copy.deepcopy(m.to_dict()) for m in members if m.id}

    mock_session = MagicMock()
    _ = fuse_context_claim_rows(canonical_id, members)
    record_context_claims(mock_session, canonical_id, members)

    for m in members:
        if not m.id:
            continue
        after = m.to_dict()
        assert after == snapshots_before[m.id], (
            f"P-CTX-2 NON-MUTATION: member {m.id!r}.to_dict() changed after writer calls.\n"
            f"  before: {snapshots_before[m.id]}\n  after:  {after}"
        )

    added_args = [c.args[0] for c in mock_session.add.call_args_list]
    for arg in added_args:
        assert isinstance(arg, ContextClaimRecord), (
            "P-CTX-2 NON-MUTATION: session.add() received unexpected type "
            f"{type(arg).__name__!r}; the context-claim writer must ONLY add ContextClaimRecord"
        )
        assert not isinstance(arg, (StatementRecord, DecisionRecord, MergeAudit)), (
            "P-CTX-2 NON-MUTATION: session.add() received a StatementRecord/DecisionRecord/"
            "MergeAudit row — record_context_claims must never write to another lane or the "
            "audit trail"
        )


# ===========================================================================
# P-CTX-3: PROVENANCE-COMPLETE · APPEND-ONLY · skip-unstamped / no-anchor writes nothing
# ===========================================================================


@dataclass(frozen=True)
class _MemberSpec:
    entity: Any
    expected_rows: int


@st.composite
def _p_ctx_3_scenario(draw: st.DrawFn) -> list[_MemberSpec]:
    n = draw(st.integers(min_value=1, max_value=4))
    ids = list(wm.ID_POOL[:n])
    specs: list[_MemberSpec] = []
    for eid in ids:
        entity = draw(wm.ftm_entity(entity_id=eid, schema="Company"))
        has_anchor = draw(st.booleans())
        if has_anchor:
            field = draw(st.sampled_from(CANONICAL_ID_FIELDS))
            value = draw(st.text(alphabet=_ALNUM, min_size=1, max_size=6))
            set_anchor(entity, field, value)

        prov_state = draw(st.sampled_from(("full", "unstamped", "empty_retrieved_at")))
        if prov_state == "full":
            stamp(
                entity,
                Provenance(
                    source_id=f"src-{eid}",
                    retrieved_at="2026-03-01T00:00:00Z",
                    reliability="B",
                    source_record=f"s3://landing/{eid}.json",
                ),
            )
            expected = 1 if has_anchor else 0
        elif prov_state == "empty_retrieved_at":
            stamp(
                entity,
                Provenance(
                    source_id=f"src-{eid}",
                    retrieved_at="",
                    reliability="B",
                    source_record=f"s3://landing/{eid}.json",
                ),
            )
            expected = 0  # skip-and-log: no retrieved_at, never written naked
        else:
            expected = 0  # skip-and-log: unstamped member, no provenance at all

        specs.append(_MemberSpec(entity=entity, expected_rows=expected))
    return specs


@given(specs=_p_ctx_3_scenario())
@_SETTINGS
def test_p_ctx_3_provenance_complete_append_only_and_skip_unstamped(
    specs: list[_MemberSpec],
) -> None:
    """P-CTX-3: every written row is provenance-complete; the writer is append-only; an
    unstamped / no-retrieved_at member's anchors are SKIPPED (never written naked); a
    no-anchor member yields zero rows.

    Non-vacuity: an impl writing a naked (NULL-retrieved_at) row fails (the per-row NOT-NULL
    loop below); an in-place UPDATE/DELETE fails (the spy-session method check).

    RED today: ImportError — fuse_context_claim_rows, record_context_claims, ContextClaimRecord.
    """
    members = [s.entity for s in specs]
    canonical_id = "ctx-test-prov-" + "-".join(m.id or "" for m in members)

    rows = fuse_context_claim_rows(canonical_id, members)

    # --- INV-CTX-PROV: every written row has non-NULL method AND retrieved_at ---
    for row in rows:
        assert row.method is not None, f"row for entity_id={row.entity_id!r} has NULL method"
        assert row.retrieved_at is not None, (
            f"row for entity_id={row.entity_id!r} has NULL retrieved_at"
        )
        assert row.method == "connector:map", f"row.method={row.method!r} != 'connector:map'"

    # --- Per-member expected row count (skip-and-log invariant) ---
    rows_by_entity: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        rows_by_entity[row.entity_id].append(row)

    for spec in specs:
        eid = spec.entity.id or ""
        got = len(rows_by_entity.get(eid, []))
        assert got == spec.expected_rows, (
            f"member {eid!r}: expected {spec.expected_rows} row(s), got {got} — skip-and-log "
            "for unstamped/no-retrieved_at members must yield ZERO rows (ADR 0106 §3)"
        )

    # --- INV-CTX-APPENDONLY: only session.add is ever issued (spy session) ---
    mock_session = MagicMock()
    record_context_claims(mock_session, canonical_id, members)
    assert mock_session.delete.call_count == 0, (
        "P-CTX-3 APPEND-ONLY VIOLATED: record_context_claims called session.delete()"
    )
    assert mock_session.execute.call_count == 0, (
        "P-CTX-3 APPEND-ONLY VIOLATED: record_context_claims issued a raw session.execute() — "
        "expected ONLY session.add calls"
    )
    for call in mock_session.method_calls:
        assert call[0] == "add", (
            f"P-CTX-3 APPEND-ONLY VIOLATED: unexpected session method called: {call[0]!r} — "
            "the writer must issue ONLY session.add (INSERT-only; no UPDATE/DELETE)"
        )


# ===========================================================================
# P-CTX-4: ANCHOR ROUND-TRIP FIDELITY (pure, Slice P1-b)
# ===========================================================================


@st.composite
def _p_ctx_4_scenario(draw: st.DrawFn) -> tuple[str, dict[str, str]]:
    """Draw (survivor, {field: value}) — 1..len(CANONICAL_ID_FIELDS) anchor fields, each a
    SINGLE distinct value (no conflict)."""
    survivor = "pctx4-" + draw(st.text(alphabet="abcdefghij0123456789", min_size=3, max_size=8))
    fields = draw(
        st.lists(
            st.sampled_from(CANONICAL_ID_FIELDS),
            min_size=1,
            max_size=len(CANONICAL_ID_FIELDS),
            unique=True,
        )
    )
    values = {field: draw(st.text(alphabet=_ALNUM, min_size=1, max_size=8)) for field in fields}
    return survivor, values


@given(scenario=_p_ctx_4_scenario())
@_SETTINGS
def test_p_ctx_4_anchor_round_trip_fidelity(scenario: tuple[str, dict[str, str]]) -> None:
    """P-CTX-4: reconstruct_entities + get_anchors on the fold entity == get_anchors on the
    directly-merged entity, on a single-batch no-conflict anchored corpus.

    Non-vacuity: a fold that never sets the context (today) fails; the generator always
    produces >= 1 anchor.

    RED today: ImportError (ContextClaimRecord) — and, once P1-a alone lands, TypeError
    (reconstruct_entities has no context_claim_rows parameter yet).
    """
    survivor, values = scenario
    member_id = f"{survivor}-member"
    dataset = "pctx4-ds"
    retrieved_at = "2026-05-01T00:00:00Z"

    stmt_row = StatementRecord(
        id=str(uuid.uuid4()),
        statement_id=str(uuid.uuid4()),
        canonical_id=survivor,
        entity_id=member_id,
        schema="Company",
        prop="name",
        value="Acme",
        dataset=dataset,
        reliability="B",
        retrieved_at=retrieved_at,
    )
    ctx_rows = [
        ContextClaimRecord(
            id=str(uuid.uuid4()),
            canonical_id=survivor,
            entity_id=member_id,
            key=field,
            value=value,
            dataset=dataset,
            method="connector:map",
            retrieved_at=retrieved_at,
            scope="default",
        )
        for field, value in values.items()
    ]

    entities = reconstruct_entities([stmt_row], lambda cid: cid, context_claim_rows=ctx_rows)
    fold_entity = next(e for e in entities if e.id == survivor)

    direct = make_entity({"id": survivor, "schema": "Company", "properties": {"name": ["Acme"]}})
    for field, value in values.items():
        set_anchor(direct, field, value)

    fold_anchors = get_anchors(fold_entity)
    assert fold_anchors == get_anchors(direct), (
        f"P-CTX-4 ROUND-TRIP FIDELITY VIOLATED: fold get_anchors()={fold_anchors!r} != "
        f"direct-merge get_anchors()={get_anchors(direct)!r} for survivor={survivor!r}"
    )
    assert fold_anchors, "P-CTX-4 NON-VACUITY: the fold entity must carry >= 1 anchor"


# ===========================================================================
# P-CTX-5: OMIT-ON-CONFLICT PARITY (pure, Slice P1-b)
# ===========================================================================


def _merge_members_direct(canonical_id: str, members: Sequence[Any]) -> Any:
    """Mirror ``resolution.signoff._merge_members``'s FtM-merge recipe (duplicated locally so
    this oracle never imports from resolution.signoff)."""
    ordered = sorted(members, key=lambda e: e.id or "")
    merged = make_entity({**ordered[0].to_dict(), "id": canonical_id})
    for member in ordered:
        merged.merge(member)
    return merged


@st.composite
def _p_ctx_5_scenario(draw: st.DrawFn) -> tuple[str, str, list[str], str, str]:
    survivor = "pctx5-" + draw(st.text(alphabet="abcdefghij0123456789", min_size=3, max_size=8))
    conflict_field = draw(st.sampled_from(CANONICAL_ID_FIELDS))
    n_conflict = draw(st.integers(min_value=2, max_value=3))
    conflict_values = draw(
        st.lists(
            st.text(alphabet=_ALNUM, min_size=1, max_size=6),
            min_size=n_conflict,
            max_size=n_conflict,
            unique=True,
        )
    )
    clean_field = draw(st.sampled_from([f for f in CANONICAL_ID_FIELDS if f != conflict_field]))
    clean_value = draw(st.text(alphabet=_ALNUM, min_size=1, max_size=6))
    return survivor, conflict_field, conflict_values, clean_field, clean_value


@given(scenario=_p_ctx_5_scenario())
@_SETTINGS
def test_p_ctx_5_omit_on_conflict_parity(
    scenario: tuple[str, str, list[str], str, str],
) -> None:
    """P-CTX-5: >1 distinct claim value for a key -> the fold's get_anchors OMITS that key,
    identical to the direct merged-entity path; a co-existing single-value key stays present.

    Non-vacuity: a fold that picks an arbitrary [0] winner fails; a single-value key must
    still be present (guards against omit-everything).

    RED today: ImportError (ContextClaimRecord) — and, once P1-a alone lands, TypeError
    (reconstruct_entities has no context_claim_rows parameter yet).
    """
    survivor, conflict_field, conflict_values, clean_field, clean_value = scenario
    dataset = "pctx5-ds"
    retrieved_at = "2026-04-01T00:00:00Z"

    stmt_row = StatementRecord(
        id=str(uuid.uuid4()),
        statement_id=str(uuid.uuid4()),
        canonical_id=survivor,
        entity_id=f"{survivor}-member",
        schema="Company",
        prop="name",
        value="Acme",
        dataset=dataset,
        reliability="B",
        retrieved_at=retrieved_at,
    )

    ctx_rows = [
        ContextClaimRecord(
            id=str(uuid.uuid4()),
            canonical_id=survivor,
            entity_id=f"{survivor}-member-{i}",
            key=conflict_field,
            value=value,
            dataset=dataset,
            method="connector:map",
            retrieved_at=retrieved_at,
            scope="default",
        )
        for i, value in enumerate(conflict_values)
    ]
    ctx_rows.append(
        ContextClaimRecord(
            id=str(uuid.uuid4()),
            canonical_id=survivor,
            entity_id=f"{survivor}-member-0",
            key=clean_field,
            value=clean_value,
            dataset=dataset,
            method="connector:map",
            retrieved_at=retrieved_at,
            scope="default",
        )
    )

    entities = reconstruct_entities([stmt_row], lambda cid: cid, context_claim_rows=ctx_rows)
    fold_entity = next(e for e in entities if e.id == survivor)
    fold_anchors = get_anchors(fold_entity)

    assert conflict_field not in fold_anchors, (
        "P-CTX-5 OMIT-ON-CONFLICT VIOLATED: fold projected a bare key for "
        f"{conflict_field!r} despite {len(conflict_values)} distinct claim values "
        f"({conflict_values!r}) — the fold must OMIT a conflicting anchor key (mirrors "
        "get_anchors' refusal to pick an arbitrary winner, Gate B-5 / ADR 0040 Finding 1)."
    )
    assert fold_anchors.get(clean_field) == clean_value, (
        "P-CTX-5 NON-VACUITY VIOLATED (guards against omit-everything): the single-value key "
        f"{clean_field!r} must still be present on the fold entity; got "
        f"{fold_anchors.get(clean_field)!r}"
    )

    # --- Parity with the direct merged-entity path (the SAME omit-on-conflict rule) ---
    members = [
        make_entity(
            {"id": f"{survivor}-m{i}", "schema": "Company", "properties": {"name": ["Acme"]}}
        )
        for i in range(len(conflict_values))
    ]
    for i, value in enumerate(conflict_values):
        set_anchor(members[i], conflict_field, value)
    set_anchor(members[0], clean_field, clean_value)
    direct = _merge_members_direct(survivor, members)

    assert fold_anchors == get_anchors(direct), (
        f"P-CTX-5: fold get_anchors()={fold_anchors!r} != direct-merge "
        f"get_anchors()={get_anchors(direct)!r} — the fold must reproduce the SAME "
        "omit-on-conflict projection the live merged-entity path applies."
    )


# ===========================================================================
# P-CTX-6: INCREMENTAL == FULL-REBUILD WITH ANCHORS (real DB + Neo4j, extends P-FOLD-2)
# ===========================================================================


@st.composite
def _p_ctx_6_scenario(draw: st.DrawFn) -> tuple[int, dict[str, dict[str, Any]]]:
    """Draw (n_batches, plan). ``plan[survivor]`` holds a ``stmt_batch`` (the ONE batch that
    writes the survivor's single StatementRecord) and a STRICTLY LATER ``ctx_batch`` (a
    context-claim-ONLY delta for an already statement-bearing survivor — the mandated
    scenario, spec §3) plus a single non-conflicting ``(field, value)``.

    No CanonicalIdLedger row is ever written, so survivor_of is the identity map — the
    supersession-monotonic bound (ADR 0101 A2) holds trivially (P-FOLD-2's recipe)."""
    n_batches = draw(st.integers(min_value=2, max_value=4))
    n_survivors = draw(st.integers(min_value=1, max_value=3))
    plan: dict[str, dict[str, Any]] = {}
    for i in range(n_survivors):
        survivor = f"pctx6-surv-{i}"
        stmt_batch = draw(st.integers(min_value=0, max_value=n_batches - 2))
        ctx_batch = draw(st.integers(min_value=stmt_batch + 1, max_value=n_batches - 1))
        field = draw(st.sampled_from(CANONICAL_ID_FIELDS))
        # Survivor-ordinal prefix: two survivors must never draw the same anchor value —
        # the graph's canonical-ID uniqueness constraints (shared container) reject the
        # cross-survivor duplicate, which is a generator collision, not a fold property.
        value = f"s{i}" + draw(st.text(alphabet=_ALNUM, min_size=1, max_size=8))
        plan[survivor] = {
            "stmt_batch": stmt_batch,
            "ctx_batch": ctx_batch,
            "field": field,
            "value": value,
        }
    return n_batches, plan


@pytest.mark.integration
@given(scenario=_p_ctx_6_scenario())
@_SETTINGS
def test_p_ctx_6_incremental_equals_full_rebuild_with_anchors(
    scenario: tuple[int, dict[str, dict[str, Any]]],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """P-CTX-6: incremental fold (INCLUDING a context-claim-ONLY delta for an already
    statement-bearing survivor) == full_rebuild, on node ANCHORS (extends P-FOLD-2).

    Non-vacuity: directly asserts the incrementally-folded node carries the bare anchor key —
    a fold that never reconstructs anchors would otherwise satisfy incr == full VACUOUSLY
    (both sides anchor-empty).

    RED today: ImportError — ContextClaimRecord does not exist yet.
    """
    n_batches, plan = scenario

    _cleanup_postgres(postgres_dsn)
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")

    engine = make_engine(postgres_dsn)
    try:
        create_all(engine)
        sessions = session_factory(engine)

        for batch_index in range(n_batches):
            with sessions() as session:
                for survivor, spec in plan.items():
                    if spec["stmt_batch"] == batch_index:
                        session.add(
                            StatementRecord(
                                id=str(uuid.uuid4()),
                                statement_id=str(uuid.uuid4()),
                                canonical_id=survivor,
                                entity_id=f"{survivor}-member",
                                schema="Company",
                                prop="name",
                                value=f"{survivor}-name",
                                dataset=f"pctx6-ds-batch-{batch_index}",
                                reliability="B",
                                retrieved_at=f"2026-01-{batch_index + 1:02d}T00:00:00Z",
                            )
                        )
                    if spec["ctx_batch"] == batch_index:
                        session.add(
                            ContextClaimRecord(
                                id=str(uuid.uuid4()),
                                canonical_id=survivor,
                                entity_id=f"{survivor}-member",
                                key=spec["field"],
                                value=spec["value"],
                                dataset=f"pctx6-ds-batch-{batch_index}",
                                method="connector:map",
                                retrieved_at=f"2026-01-{batch_index + 1:02d}T00:00:00Z",
                                scope="default",
                            )
                        )
                session.commit()

            with sessions() as session:
                project(session, clean_graph, full_rebuild=False)

        # --- Non-vacuity: every survivor's node must ACTUALLY carry its bare anchor key ---
        for survivor, spec in plan.items():
            rows = clean_graph.execute_read(
                "MATCH (n {id: $sid}) RETURN properties(n) AS props", sid=survivor
            )
            assert len(rows) == 1, (
                f"P-CTX-6: expected exactly 1 node for survivor={survivor!r}, got {len(rows)}"
            )
            props = rows[0]["props"] or {}
            assert props.get(spec["field"]) == spec["value"], (
                f"P-CTX-6 NON-VACUITY VIOLATED: survivor={survivor!r}'s node is missing bare "
                f"anchor key {spec['field']!r}={spec['value']!r} after the INCREMENTAL fold "
                f"(got {props.get(spec['field'])!r}) — the context-claim-ONLY delta for this "
                "already statement-bearing survivor was dropped (ADR 0106 §2.b.2's UNION "
                "touched-set requirement)."
            )

        sig_incr = graph_signature(clean_graph)

        clean_graph.execute_write("MATCH (n) DETACH DELETE n")
        with sessions() as session:
            project(session, clean_graph, full_rebuild=True)
        sig_full = graph_signature(clean_graph)

        assert sig_incr == sig_full, (
            "P-CTX-6 VIOLATED: incremental fold (per-batch project(full_rebuild=False), "
            "including a context-claim-ONLY delta for an already statement-bearing survivor) "
            "!= one project(full_rebuild=True) over the whole log, comparing node ANCHORS.\n"
            f"  sig_incr: {len(sig_incr[0])} nodes, {len(sig_incr[1])} edges\n"
            f"  sig_full: {len(sig_full[0])} nodes, {len(sig_full[1])} edges\n"
            f"  plan: {plan!r}\n"
            "The incremental touched set must be the UNION of the statement delta AND the "
            "context-claim delta (ADR 0106 §2.b.2)."
        )
    finally:
        engine.dispose()
