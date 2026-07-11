"""Integration tests for Gate P2 — right-to-forget reaches the SoR (ADR 0107, spec §4).

Real Postgres + Neo4j (+ MinIO where ``erase_source`` needs it) — Docker IS available locally;
run this suite locally, not CI-only (memory: docker-available-run-integration-locally).

Covers spec §4's integration items:

IT-ERASE-flow          A full corpus (multi-source survivor with a co-witnessed erased-only
                        value + an erased-source anchor + a sole-source node + a decision
                        referencing an erased member) → ``erase_source(...)`` → BOTH surfaces
                        (SF-6) → a fresh ``project(full_rebuild=True)`` contains nothing of the
                        erased source. RED today (assertion-RED, EXISTING entry points only).

IT-ERASE-stock          The one-off retroactive scrub over the dual-write window:
                        ``TaskRun(kind="erase")`` rows whose log rows are STILL present →
                        ``scrub_stock(...)`` scrubs each distinct source once, idempotently.
                        RED today: ``ImportError`` (local import — the ONLY test in this file
                        besides IT-ERASE-appendonly-(b) allowed to be import-RED, per the gate's
                        RED-evidence contract).

IT-ERASE-signoff-lane   A P3 sign-off-approved survivor whose members include an erased source
                        → the scrub reaches its sign-off statement/context rows AND redacts its
                        ``decided_by="operator:…"`` decision row. RED today (assertion-RED,
                        EXISTING entry points: ``resolve_pending`` + ``signoff.approve`` +
                        ``erase_source``).

IT-ERASE-idempotent     (a) a plain second ``erase_source`` is a precise no-op (reached-row
                        count stays 0). RED today (assertion-RED — EXISTING entry points only).
                        (b) an injected post-Neo4j/pre-Postgres-commit failure leaves the live
                        graph pruned but the log un-scrubbed → a retry converges
                        (resurrection-then-recovery PROVEN). RED today: ``ImportError`` (local
                        import — necessarily exercises the new scrub functions directly).

IT-ERASE-appendonly     POSITIVE confinement (INV-ERASE-APPENDONLY-CARVEOUT), split in two:
                        (a) the FULL normal pipeline (seed → resolve_pending → signoff.approve →
                        project) issues ZERO DELETE/UPDATE against statement/context_claim/
                        decision (table-qualified detector, the P1 idiom extended to the two SoR
                        lanes + decision). Runs GREEN today (there IS no scrub yet to violate
                        this — a legitimate regression guard, like the P1 detector itself,
                        matching the docstring's "not the trivially-green P1 detector" framing:
                        its VALUE is in combination with (b)). (b) ``scrub_log_lanes(...)`` DOES
                        emit exactly those DELETE/UPDATEs. RED today: ``ImportError`` (local
                        import — necessarily exercises the new scrub function directly).

All ``ImportError``-RED tests import ``worldmonitor.resolution.erasure_scrub`` /
``worldmonitor.graph.ops.set_node_values`` LOCALLY (inside the test function only), so the
OTHER, genuinely-runnable tests in this file still collect and execute (per the gate's import-
guarding contract — ``pytest.importorskip`` is FORBIDDEN, a missing module must surface as a
real collection/runtime error, never a silent skip).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    ContextClaimRecord,
    DecisionRecord,
    ErQueueItem,
    MergeAudit,
    StatementRecord,
    TaskRun,
)
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import _merge_entities  # pyright: ignore[reportPrivateUsage]
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.resolution.projector import project
from worldmonitor.resolution.signoff import approve
from worldmonitor.storage.landing import LandingStore

pytestmark = pytest.mark.integration

_RETRIEVED_AT = "2026-07-11T00:00:00Z"
_FOLD_NEO4J_PW = "testpw-p2-it-flow-diff"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# Shared, file-local helpers (duplicated across suites per house convention — see
# test_prop_signoff_spine.py's docstring on per-file self-containment).
# ---------------------------------------------------------------------------


def _landing(minio: tuple[str, str, str]) -> LandingStore:
    endpoint, access_key, secret_key = minio
    store = LandingStore.connect(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=f"landing-p2-it-{uuid.uuid4().hex[:8]}",
    )
    store.ensure_bucket()
    return store


def _sessions(postgres_dsn: str):
    engine = make_engine(postgres_dsn)
    create_all(engine)
    return engine, session_factory(engine)


def _read_node(client: Neo4jClient, node_id: str) -> dict[str, Any] | None:
    rows = client.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN properties(n) AS props", id=node_id
    )
    return dict(rows[0]["props"]) if rows else None


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


def _person(entity_id: str, source_id: str, props: dict[str, list[str]]) -> FtmEntity:
    entity = make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": [source_id]}
    )
    return stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at=_RETRIEVED_AT,
            reliability="A",
            source_record=f"s3://landing/{source_id}/{entity_id}.json",
        ),
    )


# =========================================================================================
# IT-ERASE-flow
# =========================================================================================


def _seed_flow_corpus(session: Any, neo4j: Neo4jClient, tag: str) -> dict[str, str]:
    """One deterministic (non-Hypothesis) instance of the P-ERASE-1 corpus shape: a sole-source
    node, a multi-source survivor with a co-witnessed erased-only value, an erased-source anchor,
    and a decision row referencing the erased member."""
    erased_src = f"esrc-flow:{tag}"
    keep_src = f"ksrc-flow:{tag}"
    sole_id = f"sole-flow-{tag}"
    survivor_id = f"surv-flow-{tag}"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    anchor_value = f"Q{700000 + (sum(ord(c) for c in tag) % 90000)}"

    session.add(_stmt(sole_id, sole_id, "name", "Sole Flow PII", erased_src))
    session.add(_stmt(survivor_id, m1, "name", "Flow Shared Name", erased_src))
    session.add(_stmt(survivor_id, m2, "name", "Flow Shared Name", keep_src))
    session.add(_stmt(survivor_id, m1, "alias", "FlowOnlyFromErased", erased_src))
    session.add(_stmt(survivor_id, m2, "alias", "FlowOnlyFromKept", keep_src))
    session.add(
        ContextClaimRecord(
            id=str(uuid.uuid4()),
            canonical_id=survivor_id,
            entity_id=m1,
            key="wikidata_id",
            value=anchor_value,
            dataset=erased_src,
            method="connector:map",
            retrieved_at=_RETRIEVED_AT,
            scope="default",
        )
    )
    session.add(
        DecisionRecord(
            id=str(uuid.uuid4()),
            canonical_id=survivor_id,
            kind="merge",
            member_ids=[m1, m2],
            score=0.93,
            decided_by="auto:resolver",
            evidence={"reason": "it-erase-flow"},
            supersedes=None,
            superseded_by=None,
            scope="default",
        )
    )
    session.commit()

    ensure_constraints(neo4j)
    sole_entity = _person(sole_id, erased_src, {"name": ["Sole Flow PII"]})
    by_id = {
        m1: _person(
            m1, erased_src, {"name": ["Flow Shared Name"], "alias": ["FlowOnlyFromErased"]}
        ),
        m2: _person(m2, keep_src, {"name": ["Flow Shared Name"], "alias": ["FlowOnlyFromKept"]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2), by_id)
    assert dropped == ()
    set_anchor(merged, "wikidata_id", anchor_value)
    write_entities(neo4j, [sole_entity, merged])

    return {
        "erased_src": erased_src,
        "keep_src": keep_src,
        "sole_id": sole_id,
        "survivor_id": survivor_id,
        "m1": m1,
        "m2": m2,
        "anchor_value": anchor_value,
    }


def test_it_erase_flow_both_surfaces_and_fresh_rebuild(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-flow / INV-ERASE-3LANE + INV-ERASE-DECISION-REDACT + INV-ERASE-LIVE-VALUE +
    INV-ERASE-PROV-PRESERVED + INV-ERASE-NONRESURRECT + INV-ERASE-BOTH-SURFACES.

    RED today (assertion-RED): none of the log-scrub / live value-prune ever runs against
    master, so every assertion below (except the pre-existing sole-source DETACH DELETE) fails.
    """
    from testcontainers.neo4j import Neo4jContainer

    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)

    with sessions() as session:
        corpus = _seed_flow_corpus(session, clean_graph, "flow1")

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=corpus["erased_src"],
            authorized_by="it-erase-flow-op",
        )
        session.commit()

    # ---- Log surface ----
    with sessions() as session:
        stmt_reached = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == corpus["erased_src"])
        ).scalar_one()
        ctx_reached = session.execute(
            select(func.count())
            .select_from(ContextClaimRecord)
            .where(ContextClaimRecord.dataset == corpus["erased_src"])
        ).scalar_one()
        decision = session.execute(
            select(DecisionRecord).where(DecisionRecord.canonical_id == corpus["survivor_id"])
        ).scalar_one()
    assert stmt_reached == 0, (
        f"IT-ERASE-flow INV-ERASE-3LANE VIOLATED: {stmt_reached} statement row(s) with "
        f"dataset={corpus['erased_src']!r} survive erase_source"
    )
    assert ctx_reached == 0, (
        f"IT-ERASE-flow INV-ERASE-3LANE VIOLATED: {ctx_reached} context_claim row(s) with "
        f"dataset={corpus['erased_src']!r} survive erase_source"
    )
    assert corpus["m1"] not in decision.member_ids, (
        "IT-ERASE-flow INV-ERASE-DECISION-REDACT VIOLATED: "
        f"decision.member_ids={decision.member_ids!r} still references the erased member"
    )
    assert corpus["m2"] in decision.member_ids

    # ---- Live surface: co-witnessed erased-only value gone, erased anchor gone (DIRECT read),
    # G1 provenance preserved (INV-ERASE-PROV-PRESERVED) ----
    assert _read_node(clean_graph, corpus["sole_id"]) is None
    live_survivor = _read_node(clean_graph, corpus["survivor_id"])
    assert live_survivor is not None
    live_alias = list(live_survivor.get("alias") or [])
    assert "FlowOnlyFromErased" not in live_alias, (
        "IT-ERASE-flow INV-ERASE-LIVE-VALUE VIOLATED: the co-witnessed erased-source-only value "
        f"survives on the live node: alias={live_alias!r}"
    )
    assert "FlowOnlyFromKept" in live_alias
    live_anchor = clean_graph.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN n.wikidata_id AS wid", id=corpus["survivor_id"]
    )[0]["wid"]
    assert live_anchor is None, (
        "IT-ERASE-flow INV-ERASE-LIVE-VALUE VIOLATED (anchor-oracle, DIRECT read): the "
        f"erased-source-only anchor {corpus['anchor_value']!r} still lives on the node"
    )
    assert live_survivor.get("prov_source_id"), (
        "IT-ERASE-flow INV-ERASE-PROV-PRESERVED VIOLATED: prov_source_id missing/empty"
    )

    # ---- Fresh isolated rebuild contains nothing of the erased source ----
    with Neo4jContainer("neo4j:2026.05.0-community", password=_FOLD_NEO4J_PW) as fold_c:
        fold = Neo4jClient.connect(
            uri=fold_c.get_connection_url(), user="neo4j", password=_FOLD_NEO4J_PW
        )
        fold.verify()
        try:
            with sessions() as session:
                project(session, fold, full_rebuild=True, checkpoint_id="it-erase-flow-diff")
            assert _read_node(fold, corpus["sole_id"]) is None, (
                "IT-ERASE-flow INV-ERASE-NONRESURRECT VIOLATED: sole-source node resurrects"
            )
            fold_survivor = _read_node(fold, corpus["survivor_id"])
            assert fold_survivor is not None
            fold_alias = list(fold_survivor.get("alias") or [])
            assert "FlowOnlyFromErased" not in fold_alias, (
                "IT-ERASE-flow INV-ERASE-NONRESURRECT VIOLATED: erased-source-only value "
                f"resurrects on a fresh full_rebuild: alias={fold_alias!r}"
            )
            assert fold_survivor.get("wikidata_id") is None, (
                "IT-ERASE-flow INV-ERASE-NONRESURRECT VIOLATED: erased-source anchor resurrects"
            )
        finally:
            fold.close()

    engine.dispose()


# =========================================================================================
# IT-ERASE-stock
# =========================================================================================


def test_it_erase_stock_scrubs_every_dual_write_window_source_once_idempotently(
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-stock / INV-ERASE-STOCK.

    Simulates the dual-write window: TWO ``TaskRun(kind="erase")`` audit rows for source A (one
    real erase + one idempotent re-run — dedup-by-source_id must collapse them), ONE for source
    B, and a THIRD, never-erased source C whose rows must stay untouched. Their statement/
    context_claim rows are still present (as they would be pre-scrub). ``scrub_stock`` must
    reach A and B exactly once each and leave C alone; a second call is a no-op; a fresh
    full_rebuild afterwards holds nothing of A or B.

    RED today: ``ImportError`` — ``worldmonitor.resolution.erasure_scrub.scrub_stock`` does not
    exist yet (local import — this test necessarily exercises the new surface).
    """
    engine, sessions = _sessions(postgres_dsn)

    src_a, src_b, src_c = "stockA:ds", "stockB:ds", "stockC:ds"
    with sessions() as session:
        session.add(_stmt("surv-a", "surv-a-m1", "name", "Stock A Name", src_a))
        session.add(_stmt("surv-b", "surv-b-m1", "name", "Stock B Name", src_b))
        session.add(_stmt("surv-c", "surv-c-m1", "name", "Stock C Name", src_c))
        # TWO audit rows referencing source A (a real erase + its idempotent re-run) — the stock
        # driver must dedup by distinct source_id, not by TaskRun row count.
        for src, _run_idx in ((src_a, 1), (src_a, 2), (src_b, 1)):
            session.add(
                TaskRun(
                    id=str(uuid.uuid4()),
                    kind="erase",
                    status="ok",
                    stats={
                        "source_id": src,
                        "authorized_by": "dpo",
                        "nodes_deleted": 0,
                        "nodes_pruned": 0,
                        "props_retracted": 0,
                        "edges_deleted": 0,
                        "queue_rows_redacted": 0,
                        "landing_objects_deleted": 0,
                        "dead_letters_redacted": 0,
                    },
                )
            )
        session.commit()

    ensure_constraints(clean_graph)
    write_entities(
        clean_graph,
        [
            _person("surv-a", src_a, {"name": ["Stock A Name"]}),
            _person("surv-b", src_b, {"name": ["Stock B Name"]}),
            _person("surv-c", src_c, {"name": ["Stock C Name"]}),
        ],
    )

    # ---- GATE IMPORT — does not exist yet (RED for the right reason) ----
    from worldmonitor.resolution.erasure_scrub import scrub_stock

    with sessions() as session:
        results_1 = scrub_stock(session, neo4j=clean_graph)
        session.commit()

    scrubbed_sources = {r.source_id for r in results_1} if results_1 else set()  # type: ignore[attr-defined]
    assert src_a in scrubbed_sources, "IT-ERASE-STOCK: source A must be scrubbed"
    assert src_b in scrubbed_sources, "IT-ERASE-STOCK: source B must be scrubbed"
    assert src_c not in scrubbed_sources, "IT-ERASE-STOCK: an un-erased source must be left alone"
    a_count = sum(1 for r in results_1 if getattr(r, "source_id", None) == src_a)
    assert a_count == 1, (
        f"IT-ERASE-STOCK: source A must be scrubbed EXACTLY ONCE despite 2 TaskRun rows, got "
        f"{a_count} LogScrubResult(s)"
    )

    with sessions() as session:
        c_rows = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == src_c)
        ).scalar_one()
    assert c_rows == 1, "IT-ERASE-STOCK: an un-erased source's rows must be untouched"

    # ---- idempotent second call: nothing left to scrub ----
    with sessions() as session:
        results_2 = scrub_stock(session, neo4j=clean_graph)
        session.commit()
    assert all(getattr(r, "statements_scrubbed", 0) == 0 for r in results_2), (
        "IT-ERASE-STOCK: a second scrub_stock() call must be a no-op"
    )

    # ---- verification: rebuild contains no erased source ----
    with sessions() as session:
        project(session, clean_graph, full_rebuild=True, checkpoint_id="it-erase-stock-diff")
    with sessions() as session:
        remaining_a = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == src_a)
        ).scalar_one()
        remaining_b = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == src_b)
        ).scalar_one()
    assert remaining_a == 0 and remaining_b == 0

    engine.dispose()


# =========================================================================================
# IT-ERASE-signoff-lane
# =========================================================================================


def _sanctioned_person(entity_id: str, source_id: str, *, sanction: bool) -> FtmEntity:
    props: dict[str, list[str]] = {
        "name": ["Signoff Lane Example"],
        "nationality": ["ru"],
        "birthDate": ["1971-02-02"],
    }
    if sanction:
        props["topics"] = ["sanction"]
    return _person(entity_id, source_id, props)


def _queue_item(entity: FtmEntity) -> ErQueueItem:
    assert entity.id is not None
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="it-erase-signoff-lane",
        entity_id=entity.id,
        raw_entity=entity.to_dict(),
        source_record=f"s3://landing/{entity.id}.json",
        status="pending",
    )


def test_it_erase_signoff_lane_reaches_operator_approved_rows(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-signoff-lane / P2 layers uniformly on P3 (ADR 0108).

    A parked (block-mode) merge is approved via ``signoff.approve()`` (P3 co-commits statement +
    context_claim + a ``decided_by="operator:…"`` decision row). One member's source is later
    erased: the scrub must reach ITS statement/context rows and redact the operator decision row
    while preserving ``decided_by``.

    RED today (assertion-RED, EXISTING entry points only: resolve_pending + signoff.approve +
    erase_source).
    """
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)

    erased_src = "signoff-lane-erased:ds"
    keep_src = "signoff-lane-keep:ds"
    m1, m2 = "sl-m1", "sl-m2"
    anchor_value = "Q9182736"

    e1 = _sanctioned_person(m1, erased_src, sanction=True)
    set_anchor(e1, "wikidata_id", anchor_value)
    e2 = _sanctioned_person(m2, keep_src, sanction=False)

    with sessions() as session:
        session.add(_queue_item(e1))
        session.add(_queue_item(e2))
        session.commit()
        stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
    assert stats.review == 1, f"expected exactly 1 parked cluster, got {stats}"

    with sessions() as session:
        parked = session.execute(
            select(MergeAudit).where(MergeAudit.decision == "pending_review")
        ).scalar_one()
        canonical_id = parked.canonical_id
        assert set(parked.source_ids) == {m1, m2}

    approver = "it-erase-signoff-op"
    with sessions() as session:
        result = approve(
            session, clean_graph, canonical_id=canonical_id, approver=approver, reason="it-p2"
        )
    assert result.decision == "approved"

    # Precondition: the sign-off co-commit really did write the lanes.
    with sessions() as session:
        pre_stmt = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == erased_src)
        ).scalar_one()
        pre_ctx = session.execute(
            select(func.count())
            .select_from(ContextClaimRecord)
            .where(ContextClaimRecord.dataset == erased_src)
        ).scalar_one()
        pre_decision = session.execute(
            select(DecisionRecord).where(DecisionRecord.canonical_id == canonical_id)
        ).scalar_one()
    assert pre_stmt > 0, "precondition: signoff.approve() must have written statement rows"
    assert pre_ctx > 0, "precondition: signoff.approve() must have written a context_claim row"
    assert pre_decision.decided_by == f"operator:{approver}"
    assert m1 in pre_decision.member_ids

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=erased_src,
            authorized_by="it-erase-signoff-dpo",
        )
        session.commit()

    with sessions() as session:
        post_stmt = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == erased_src)
        ).scalar_one()
        post_ctx = session.execute(
            select(func.count())
            .select_from(ContextClaimRecord)
            .where(ContextClaimRecord.dataset == erased_src)
        ).scalar_one()
        post_decision = session.execute(
            select(DecisionRecord).where(DecisionRecord.canonical_id == canonical_id)
        ).scalar_one()

    assert post_stmt == 0, (
        f"IT-ERASE-signoff-lane INV-ERASE-3LANE VIOLATED: {post_stmt} sign-off statement row(s) "
        "for the erased member survive erase_source"
    )
    assert post_ctx == 0, (
        f"IT-ERASE-signoff-lane INV-ERASE-3LANE VIOLATED: {post_ctx} sign-off context_claim "
        "row(s) for the erased member survive erase_source"
    )
    assert post_decision.decided_by == f"operator:{approver}", (
        "IT-ERASE-signoff-lane: the operator attribution must be PRESERVED by redaction"
    )
    assert m1 not in post_decision.member_ids, (
        "IT-ERASE-signoff-lane INV-ERASE-DECISION-REDACT VIOLATED: "
        f"member_ids={post_decision.member_ids!r} still references the erased member {m1!r}"
    )
    assert m2 in post_decision.member_ids

    engine.dispose()


# =========================================================================================
# IT-ERASE-idempotent
# =========================================================================================


def test_it_erase_idempotent_a_plain_second_run_is_precise_noop(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-idempotent (a) / INV-ERASE-CROSS-STORE-RECOVER (plain half).

    A second ``erase_source`` on an already-erased source is a precise no-op: the reached-row
    count stays 0 across both lanes.

    RED today (assertion-RED, EXISTING entry points only): the log is never scrubbed on the
    FIRST call, so the reached-row count after the SECOND call is still > 0.
    """
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)

    with sessions() as session:
        corpus = _seed_flow_corpus(session, clean_graph, "idemA")

    from worldmonitor.erasure import erase_source

    for authorized_by in ("it-erase-idemA-op1", "it-erase-idemA-op2"):
        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=corpus["erased_src"],
                authorized_by=authorized_by,
            )
            session.commit()

    with sessions() as session:
        stmt_reached = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == corpus["erased_src"])
        ).scalar_one()
        ctx_reached = session.execute(
            select(func.count())
            .select_from(ContextClaimRecord)
            .where(ContextClaimRecord.dataset == corpus["erased_src"])
        ).scalar_one()
    assert stmt_reached == 0, (
        "IT-ERASE-idempotent (a) VIOLATED: a SECOND erase_source must leave ZERO reached "
        f"statement rows, got {stmt_reached}"
    )
    assert ctx_reached == 0, (
        "IT-ERASE-idempotent (a) VIOLATED: a SECOND erase_source must leave ZERO reached "
        f"context_claim rows, got {ctx_reached}"
    )

    engine.dispose()


def test_it_erase_idempotent_b_cross_store_crash_recovery_converges(
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-idempotent (b) / INV-ERASE-CROSS-STORE-RECOVER (crash-recovery half).

    ``prune_live_to_fold`` writes Neo4j immediately; ``scrub_log_lanes`` stages Postgres for the
    caller's commit. A failure BETWEEN the two (simulated: commit the Neo4j-affecting call, then
    abandon the session BEFORE committing the Postgres delete) leaves the live graph pruned but
    the log un-scrubbed — a full_rebuild taken in that window still carries the erased-only value
    (momentary resurrection risk); a RETRY (re-scrub + re-prune, this time committed) converges.

    RED today: ``ImportError`` — this test necessarily exercises ``scrub_log_lanes`` +
    ``prune_live_to_fold`` directly (local import). Gate-P2-fix-round NOTE: once the module
    imports cleanly, this test also exercises ``prune_live_to_fold``'s NEW signature (accepts
    the whole ``LogScrubResult``, not a bare ``touched_survivors`` iterable, fix design SS1) —
    its two call sites below pass ``crash_result``/``retry_result`` directly (not
    ``.touched_survivors``), matching the target interface.
    """
    engine, sessions = _sessions(postgres_dsn)

    with sessions() as session:
        corpus = _seed_flow_corpus(session, clean_graph, "idemB")

    # ---- GATE IMPORT — does not exist yet (RED for the right reason) ----
    from worldmonitor.resolution.erasure_scrub import prune_live_to_fold, scrub_log_lanes

    # Simulate the crash window: the live prune (Neo4j, immediate) + the log scrub (Postgres,
    # STAGED) both run, but the session is abandoned (never committed) before the caller's
    # session.commit() — the same crash window erasure.py's docstring already documents.
    crash_engine, crash_sessions = _sessions(postgres_dsn)
    with crash_sessions() as crash_session:
        crash_result = scrub_log_lanes(crash_session, corpus["erased_src"])
        prune_live_to_fold(crash_session, clean_graph, crash_result)
        # NO commit — simulates the crash between the Neo4j write and the Postgres commit.
    crash_engine.dispose()

    # ---- Momentary state: live graph is ALREADY pruned, log is STILL un-scrubbed ----
    live_survivor = _read_node(clean_graph, corpus["survivor_id"])
    assert live_survivor is not None
    assert "FlowOnlyFromErased" not in list(live_survivor.get("alias") or []), (
        "IT-ERASE-idempotent (b) precondition: the live prune must have committed to Neo4j "
        "despite the abandoned Postgres session"
    )
    with sessions() as session:
        still_present = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == corpus["erased_src"])
        ).scalar_one()
    assert still_present > 0, (
        "IT-ERASE-idempotent (b) precondition: the log scrub must NOT have persisted (the "
        "abandoned session's DELETE was rolled back) — this is the momentary un-scrubbed window"
    )

    # ---- RETRY: re-scrub + re-prune, this time committed ----
    with sessions() as session:
        retry_result = scrub_log_lanes(session, corpus["erased_src"])
        prune_live_to_fold(session, clean_graph, retry_result)
        session.commit()

    with sessions() as session:
        converged = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == corpus["erased_src"])
        ).scalar_one()
    assert converged == 0, (
        "IT-ERASE-idempotent (b) VIOLATED: a retry after the crash window must converge the log "
        f"to zero reached rows, got {converged}"
    )

    with sessions() as session:
        project(session, clean_graph, full_rebuild=True, checkpoint_id="it-erase-idem-b-diff")
    fold_survivor = _read_node(clean_graph, corpus["survivor_id"])
    assert fold_survivor is not None
    assert "FlowOnlyFromErased" not in list(fold_survivor.get("alias") or []), (
        "IT-ERASE-idempotent (b) VIOLATED: a full_rebuild after the retry must be erased-free "
        "(resurrection-then-recovery)"
    )

    engine.dispose()


# =========================================================================================
# IT-ERASE-appendonly (POSITIVE confinement, split a/b — INV-ERASE-APPENDONLY-CARVEOUT)
# =========================================================================================


def _sanction_candidates() -> list[FtmEntity]:
    """A tiny clean corpus: one promotable duplicate pair (merge → decision row) + one
    singleton, run through resolve_pending + signoff.approve to exercise every normal writer
    path against the three lanes this gate's scrub touches."""
    a = _person("ao1", "src:appendonly", {"name": ["Appendonly Example"], "nationality": ["ru"]})
    b = _person("ao2", "src:appendonly", {"name": ["Appendonly Example"], "nationality": ["ru"]})
    c = _person("ao3", "src:appendonly", {"name": ["Singleton Appendonly"], "nationality": ["us"]})
    return [a, b, c]


def test_it_erase_appendonly_a_normal_pipeline_never_writes_the_three_lanes(
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-appendonly (a) / INV-ERASE-APPENDONLY-CARVEOUT (positive half).

    The FULL normal pipeline (seed → resolve_pending → project) issues ZERO DELETE/UPDATE
    against statement/context_claim/decision — a DB-level proof via a real
    ``before_cursor_execute`` listener, table-qualified (never a bare substring match — the P1
    false-positive trap: ``projection_checkpoint.last_context_claim_seq``/``last_decision_seq``
    are legitimate UPDATE targets whose COLUMN names contain the table names as substrings).

    Runs GREEN today (nothing in this gate has wired a destructive write into these lanes yet —
    a legitimate confinement guard, like the P1 detector: its evidentiary weight comes from
    combination with part (b), which proves ``scrub_log_lanes`` DOES emit exactly these writes).
    """
    import re

    from sqlalchemy import create_engine, event

    write_re = re.compile(
        r'\b(?:update|delete\s+from)\s+"?(?:statement|context_claim|decision)"?\b', re.IGNORECASE
    )

    engine = create_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    captured: list[str] = []

    def _capture(_conn, _cursor, statement: str, _params, _context, _executemany) -> None:  # type: ignore[no-untyped-def]
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        for entity in _sanction_candidates():
            with sessions() as session:
                session.add(_queue_item(entity))
                session.commit()
        with sessions() as session:
            resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
        with sessions() as session:
            project(session, clean_graph, full_rebuild=True, checkpoint_id="it-erase-ao-a")
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        engine.dispose()

    forbidden = [stmt for stmt in captured if write_re.search(stmt)]
    assert not forbidden, (
        "IT-ERASE-appendonly (a) APPEND-ONLY VIOLATED AT THE DB LEVEL: "
        f"{len(forbidden)} UPDATE/DELETE statement(s) against statement/context_claim/decision "
        "from the NORMAL pipeline:\n" + "\n".join(forbidden)
    )


def test_it_erase_appendonly_b_scrub_log_lanes_emits_exactly_those_writes(
    postgres_dsn: str,
) -> None:
    """IT-ERASE-appendonly (b) / INV-ERASE-APPENDONLY-CARVEOUT (negative-control half).

    ``scrub_log_lanes(...)`` DOES emit DELETE/UPDATE against statement/context_claim/decision —
    proving (a)'s silence is a genuine confinement fact, not an artefact of the detector never
    seeing a real write of this shape (the non-vacuity fence for the P1-detector-idiom reuse).

    RED today: ``ImportError`` — this test necessarily exercises ``scrub_log_lanes`` directly
    (local import).
    """
    import re

    from sqlalchemy import create_engine, event

    write_re = re.compile(
        r'\b(?:update|delete\s+from)\s+"?(?:statement|context_claim|decision)"?\b', re.IGNORECASE
    )

    engine = create_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    with sessions() as session:
        erased_src = "src:appendonly-b"
        session.add(_stmt("ao-b-surv", "ao-b-m1", "name", "Appendonly B", erased_src))
        session.add(
            DecisionRecord(
                id=str(uuid.uuid4()),
                canonical_id="ao-b-surv",
                kind="merge",
                member_ids=["ao-b-m1", "ao-b-m2"],
                score=0.8,
                decided_by="auto:resolver",
                evidence=None,
                supersedes=None,
                superseded_by=None,
                scope="default",
            )
        )
        session.commit()

    # ---- GATE IMPORT — does not exist yet (RED for the right reason) ----
    from worldmonitor.resolution.erasure_scrub import scrub_log_lanes

    captured: list[str] = []

    def _capture(_conn, _cursor, statement: str, _params, _context, _executemany) -> None:  # type: ignore[no-untyped-def]
        captured.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        with sessions() as session:
            scrub_log_lanes(session, erased_src)
            session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
        engine.dispose()

    hits = [stmt for stmt in captured if write_re.search(stmt)]
    assert hits, (
        "IT-ERASE-appendonly (b) NON-VACUITY VIOLATED: scrub_log_lanes() must emit at least one "
        "DELETE/UPDATE against statement/context_claim/decision — none captured"
    )


# =========================================================================================
# IT-ERASE-flow-legacy-data (CRITICAL bug, fix design SS1 — positive-attribution gate)
# =========================================================================================


def _seed_flow_legacy_corpus(session: Any, neo4j: Neo4jClient, tag: str) -> dict[str, str]:
    """The P-ERASE-5 CRITICAL-bug shape at integration scale: a survivor with (a) an
    erased-source-only STATEMENT-LOGGED value (``alias``, the SF-4 case) that MUST be removed,
    and (b) a DIFFERENT prop (``profession``) AND anchor (``lei``) contributed by a
    NEVER-erased source with ZERO backing log row at all (legacy/pre-dual-write live data)."""
    erased_src = f"esrc-legacy:{tag}"
    keep_src = f"ksrc-legacy:{tag}"
    legacy_src = f"lsrc-legacy:{tag}"
    survivor_id = f"surv-legacy-{tag}"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    m3 = f"{survivor_id}-m3"
    legacy_prop_value = f"LegacyProfession-{tag}"
    legacy_anchor_value = f"LEILEGACYIT{tag.upper()}"

    session.add(_stmt(survivor_id, m1, "name", "Legacy Flow Name", erased_src))
    session.add(_stmt(survivor_id, m2, "name", "Legacy Flow Name", keep_src))
    session.add(_stmt(survivor_id, m1, "alias", "LegacyFlowOnlyFromErased", erased_src))
    # NOTE: m3's `profession` + `lei` anchor are DELIBERATELY never logged (legacy data) — no
    # `_stmt(...)` call, no `ContextClaimRecord` for m3 anywhere.
    session.commit()

    ensure_constraints(neo4j)
    by_id = {
        m1: _person(
            m1, erased_src, {"name": ["Legacy Flow Name"], "alias": ["LegacyFlowOnlyFromErased"]}
        ),
        m2: _person(m2, keep_src, {"name": ["Legacy Flow Name"]}),
        m3: _person(m3, legacy_src, {"profession": [legacy_prop_value]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2, m3), by_id)
    assert dropped == ()
    set_anchor(merged, "lei", legacy_anchor_value)
    write_entities(neo4j, [merged])

    return {
        "erased_src": erased_src,
        "keep_src": keep_src,
        "legacy_src": legacy_src,
        "survivor_id": survivor_id,
        "m1": m1,
        "m2": m2,
        "m3": m3,
        "legacy_prop_value": legacy_prop_value,
        "legacy_anchor_value": legacy_anchor_value,
    }


def test_it_erase_flow_legacy_data_survives_unrelated_erasure(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-flow-legacy-data — the CRITICAL bug at integration scale (fix design SS1).

    Sibling of P-ERASE-5 (property suite) at deterministic integration scale. RED today:
    ``profession``/``lei`` from a NEVER-erased legacy source (with zero backing log rows) are
    WIPED by ``prune_live_to_fold``'s "no fold evidence == erased-source-only" mis-inference —
    proven against TODAY's EXISTING ``erase_source`` entry point (no new symbol required).
    """
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)

    with sessions() as session:
        corpus = _seed_flow_legacy_corpus(session, clean_graph, "legacyflow1")

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=corpus["erased_src"],
            authorized_by="it-erase-legacy-op",
        )
        session.commit()

    after = _read_node(clean_graph, corpus["survivor_id"])
    assert after is not None, "the multi-source survivor must SURVIVE a partial erase"

    alias = list(after.get("alias") or [])
    assert "LegacyFlowOnlyFromErased" not in alias, (
        "IT-ERASE-flow-legacy-data (a) VIOLATED: the erased-source-only statement-logged alias "
        f"value survives: alias={alias!r}"
    )

    assert after.get("profession") == [corpus["legacy_prop_value"]], (
        "IT-ERASE-flow-legacy-data (b) CRITICAL VIOLATED: the legacy (never statement-logged) "
        f"profession value was wiped: got {after.get('profession')!r}, expected "
        f"[{corpus['legacy_prop_value']!r}]"
    )
    live_anchor = clean_graph.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN n.lei AS lei", id=corpus["survivor_id"]
    )[0]["lei"]
    assert live_anchor == corpus["legacy_anchor_value"], (
        "IT-ERASE-flow-legacy-data (b) CRITICAL VIOLATED: the legacy (never context-claim-"
        f"logged) lei anchor was wiped: got {live_anchor!r}, expected "
        f"{corpus['legacy_anchor_value']!r}"
    )

    engine.dispose()


# =========================================================================================
# IT-ERASE-caption-staleness (fix design SS2 — the caption half of the HIGH)
# =========================================================================================


def test_it_erase_caption_recomputed_not_stale(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-caption-staleness / fix design SS2.

    A survivor whose live ``caption`` was picked from an erased-source-only ``name`` value
    while a surviving source contributes a DIFFERENT name value: after ``erase_source``,
    ``n.caption`` must be CORRECTLY RECOMPUTED off the post-scrub fold (never left stale, and
    never corrupted into a 1-element list — ``caption`` is a Neo4j SCALAR string, not an FtM
    multi-valued prop, ``graph/ftmg_fork/transform.py:103``). The independent oracle is the
    SAME frozen ``reconstruct_entities`` :func:`prune_live_to_fold` itself uses — not a second,
    hand-rolled expectation.

    RED today: ``caption`` is ``_excluded`` (same predicate as ``datasets``) so
    ``prune_live_to_fold`` never touches it at all — the stale erased-source-only caption
    survives the erase unchanged.
    """
    erased_src = "esrc-caption:it"
    keep_src = "ksrc-caption:it"
    survivor_id = "surv-caption-it"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"

    engine, sessions = _sessions(postgres_dsn)
    with sessions() as session:
        session.add(_stmt(survivor_id, m1, "name", "Erased Caption Name", erased_src))
        session.add(_stmt(survivor_id, m2, "name", "Kept Caption Name", keep_src))
        session.commit()

    ensure_constraints(clean_graph)
    by_id = {
        m1: _person(m1, erased_src, {"name": ["Erased Caption Name"]}),
        m2: _person(m2, keep_src, {"name": ["Kept Caption Name"]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2), by_id)
    assert dropped == ()
    write_entities(clean_graph, [merged])
    # Force the PRE-erasure live caption to be the erased-source-only value (a deterministic
    # precondition — FtM's own caption-pick order among the two name values is not the thing
    # under test here; "gets correctly recomputed" is).
    clean_graph.execute_write(
        "MATCH (n:Entity {id: $id}) SET n.caption = $caption",
        id=survivor_id,
        caption="Erased Caption Name",
    )
    pre = _read_node(clean_graph, survivor_id)
    assert pre is not None and pre.get("caption") == "Erased Caption Name"

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=_landing(minio),
            source_id=erased_src,
            authorized_by="it-erase-caption-op",
        )
        session.commit()

    # Independent oracle: the FROZEN pure reconstruct over the POST-scrub remaining rows (the
    # SAME single source of truth prune_live_to_fold itself must use, per the fix design).
    from worldmonitor.resolution.projector import build_survivor_of, reconstruct_entities

    with sessions() as session:
        survivor_of = build_survivor_of(session)
        remaining_stmt_rows = list(session.execute(select(StatementRecord)).scalars())
        fold_entities = {
            e.id: e
            for e in reconstruct_entities(remaining_stmt_rows, survivor_of)
            if e.id is not None
        }
    expected_caption = fold_entities[survivor_id].caption

    after = _read_node(clean_graph, survivor_id)
    assert after is not None
    assert isinstance(after.get("caption"), str), (
        "IT-ERASE-caption VIOLATED: caption must stay a SCALAR string, never list-wrapped "
        f"(got {after.get('caption')!r} of type {type(after.get('caption'))})"
    )
    assert after.get("caption") == expected_caption, (
        f"IT-ERASE-caption VIOLATED: n.caption={after.get('caption')!r} does not match the "
        f"post-scrub fold's caption {expected_caption!r} (stale/uncomputed caption)"
    )
    assert after.get("caption") != "Erased Caption Name", (
        "non-vacuity: the caption must actually CHANGE off the erased-source-only stale value"
    )

    engine.dispose()


# =========================================================================================
# IT-ERASE-datasets-staleness (fix design SS3 — the datasets half of the HIGH)
# =========================================================================================


def test_it_erase_datasets_list_strips_erased_source_id(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-datasets-staleness / fix design SS3.

    ``datasets`` is a plain Neo4j list-of-strings property holding contributing source ids
    (``graph/ftmg_fork/transform.py:104``). After ``erase_source``, the erased ``source_id``
    must be stripped from a touched survivor's live ``datasets`` list — no fold-based
    inference needed (the caller already knows exactly which ``source_id`` was erased).

    RED today: ``datasets`` is ``_excluded`` (same predicate as ``caption``) so
    ``prune_live_to_fold`` never touches it — the erased source_id survives in the list
    unchanged (``erase_source_graph`` doesn't touch ``datasets`` either — ``graph/ops.py``).
    """
    erased_src = "esrc-datasets:it"
    keep_src = "ksrc-datasets:it"
    survivor_id = "surv-datasets-it"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"

    engine, sessions = _sessions(postgres_dsn)
    with sessions() as session:
        session.add(_stmt(survivor_id, m1, "name", "Datasets Shared Name", erased_src))
        session.add(_stmt(survivor_id, m2, "name", "Datasets Shared Name", keep_src))
        session.commit()

    ensure_constraints(clean_graph)
    by_id = {
        m1: _person(m1, erased_src, {"name": ["Datasets Shared Name"]}),
        m2: _person(m2, keep_src, {"name": ["Datasets Shared Name"]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2), by_id)
    assert dropped == ()
    write_entities(clean_graph, [merged])

    pre = _read_node(clean_graph, survivor_id)
    assert pre is not None
    pre_datasets = set(pre.get("datasets") or [])
    assert erased_src in pre_datasets, "precondition: erased_src must be a contributing dataset"
    assert keep_src in pre_datasets, "precondition: keep_src must be a contributing dataset"

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=_landing(minio),
            source_id=erased_src,
            authorized_by="it-erase-datasets-op",
        )
        session.commit()

    after = _read_node(clean_graph, survivor_id)
    assert after is not None
    after_datasets = list(after.get("datasets") or [])
    assert erased_src not in after_datasets, (
        "IT-ERASE-datasets VIOLATED: the erased source_id survives in the live datasets list: "
        f"{after_datasets!r}"
    )
    assert keep_src in after_datasets, "non-vacuity: the KEPT source must survive in datasets"

    engine.dispose()


# =========================================================================================
# IT-ERASE-multi-anchor (regression-lock, test-gap #1, spec SS5 — GREEN before AND after)
# =========================================================================================


def test_it_erase_multi_anchor_together_both_removed(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-multi-anchor — regression-lock (test-gap #1, spec SS5): a survivor with TWO
    erased-source-only anchors (``wikidata_id`` AND ``lei``, both anchor-claimed by the erased
    source via TWO separate ``ContextClaimRecord`` rows with different ``key`` values) must
    have BOTH removed together in one scrub. GREEN both BEFORE and AFTER the CRITICAL/HIGH fix
    (current code is already correct here, per the adversarial review) — a pure regression
    lock, not RED-driving.
    """
    erased_src = "esrc-multianchor:it"
    keep_src = "ksrc-multianchor:it"
    survivor_id = "surv-multianchor-it"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    wikidata_value = "Q8172635"
    lei_value = "LEIMULTIANCHOR000001"

    engine, sessions = _sessions(postgres_dsn)
    with sessions() as session:
        session.add(_stmt(survivor_id, m1, "name", "Multi Anchor Name", erased_src))
        session.add(_stmt(survivor_id, m2, "name", "Multi Anchor Name", keep_src))
        session.add(
            ContextClaimRecord(
                id=str(uuid.uuid4()),
                canonical_id=survivor_id,
                entity_id=m1,
                key="wikidata_id",
                value=wikidata_value,
                dataset=erased_src,
                method="connector:map",
                retrieved_at=_RETRIEVED_AT,
                scope="default",
            )
        )
        session.add(
            ContextClaimRecord(
                id=str(uuid.uuid4()),
                canonical_id=survivor_id,
                entity_id=m1,
                key="lei",
                value=lei_value,
                dataset=erased_src,
                method="connector:map",
                retrieved_at=_RETRIEVED_AT,
                scope="default",
            )
        )
        session.commit()

    ensure_constraints(clean_graph)
    by_id = {
        m1: _person(m1, erased_src, {"name": ["Multi Anchor Name"]}),
        m2: _person(m2, keep_src, {"name": ["Multi Anchor Name"]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2), by_id)
    assert dropped == ()
    set_anchor(merged, "wikidata_id", wikidata_value)
    set_anchor(merged, "lei", lei_value)
    write_entities(clean_graph, [merged])

    pre_anchors = clean_graph.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN n.wikidata_id AS wid, n.lei AS lei", id=survivor_id
    )[0]
    assert pre_anchors["wid"] == wikidata_value and pre_anchors["lei"] == lei_value

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=_landing(minio),
            source_id=erased_src,
            authorized_by="it-erase-multianchor-op",
        )
        session.commit()

    post_anchors = clean_graph.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN n.wikidata_id AS wid, n.lei AS lei", id=survivor_id
    )[0]
    assert post_anchors["wid"] is None, (
        f"IT-ERASE-multi-anchor VIOLATED: wikidata_id survives: {post_anchors['wid']!r}"
    )
    assert post_anchors["lei"] is None, (
        f"IT-ERASE-multi-anchor VIOLATED: lei survives: {post_anchors['lei']!r}"
    )

    engine.dispose()


# =========================================================================================
# IT-ERASE-stock-malformed-stats (regression-lock, test-gap #2, spec SS5 — GREEN before AND
# after)
# =========================================================================================


def test_it_erase_stock_skips_malformed_stats_rows_gracefully(
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-stock-malformed-stats — regression-lock (test-gap #2, spec SS5): a
    ``TaskRun(kind="erase", status="ok")`` row with ``stats=None`` and another with
    ``stats={}`` (missing ``"source_id"``) interleaved among otherwise-valid rows must be
    skipped gracefully (no crash, no phantom empty-string source scrubbed) while the valid
    rows are still correctly processed. GREEN both BEFORE and AFTER the CRITICAL/HIGH fix
    (current code is already correct here, per the adversarial review) — a pure regression
    lock, not RED-driving.
    """
    engine, sessions = _sessions(postgres_dsn)

    src_valid = "stockvalid:ds"
    with sessions() as session:
        session.add(_stmt("surv-malformed", "surv-malformed-m1", "name", "Valid Stock", src_valid))
        session.add(TaskRun(id=str(uuid.uuid4()), kind="erase", status="ok", stats=None))
        session.add(TaskRun(id=str(uuid.uuid4()), kind="erase", status="ok", stats={}))
        session.add(
            TaskRun(
                id=str(uuid.uuid4()),
                kind="erase",
                status="ok",
                stats={"source_id": src_valid, "authorized_by": "dpo"},
            )
        )
        session.commit()

    ensure_constraints(clean_graph)
    write_entities(
        clean_graph, [_person("surv-malformed-m1", src_valid, {"name": ["Valid Stock"]})]
    )

    from worldmonitor.resolution.erasure_scrub import scrub_stock

    with sessions() as session:
        results = scrub_stock(session, neo4j=clean_graph)
        session.commit()

    scrubbed_sources = {r.source_id for r in results}
    assert scrubbed_sources == {src_valid}, (
        f"IT-ERASE-stock-malformed-stats VIOLATED: expected exactly {{src_valid!r}} scrubbed, "
        f"got {scrubbed_sources!r} (a malformed-stats row must never be scrubbed as a phantom "
        "empty-string source)"
    )
    assert "" not in scrubbed_sources, (
        "IT-ERASE-stock-malformed-stats VIOLATED: a phantom empty-string source_id was scrubbed"
    )

    with sessions() as session:
        remaining_valid = session.execute(
            select(func.count())
            .select_from(StatementRecord)
            .where(StatementRecord.dataset == src_valid)
        ).scalar_one()
    assert remaining_valid == 0, "the valid row must still be correctly scrubbed"

    engine.dispose()


# =========================================================================================
# IT-ERASE-caption-sole-witness (fix-round NEW-1, HIGH — caption recompute must not be gated
# on compared_props being non-empty)
# =========================================================================================


def test_it_erase_caption_recomputes_when_compared_props_ends_up_empty(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-caption-sole-witness / fix-round NEW-1 (HIGH).

    A survivor whose ``name`` (the caption-relevant prop) is SOLE-witnessed by the erased
    source: ``erase_source_graph`` (frozen, runs FIRST in ``erase_source``'s call sequence)
    already pops ``name`` from the live node's props wholesale before ``prune_live_to_fold``
    ever runs, so ``name`` never enters the per-prop loop at all. The survivor's OTHER prop
    (``alias``) was never touched by the erased source either, so ``compared_props`` ends up
    completely EMPTY.

    RED today: today's caption gate (``if compared_props and fold_entity is not None:``) never
    fires when ``compared_props`` is empty — the stale erased-source-only ``caption`` survives
    the erase unchanged, even though the fold has a perfectly good new pick (``alias``).
    """
    erased_src = "esrc-caption-sole:it"
    keep_src = "ksrc-caption-sole:it"
    survivor_id = "surv-caption-sole-it"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    name_value = "Sole Witnessed Caption Name"
    alias_value = "Unrelated Kept Alias"

    engine, sessions = _sessions(postgres_dsn)
    with sessions() as session:
        session.add(_stmt(survivor_id, m1, "name", name_value, erased_src))
        session.add(_stmt(survivor_id, m2, "alias", alias_value, keep_src))
        session.commit()

    ensure_constraints(clean_graph)
    by_id = {
        m1: _person(m1, erased_src, {"name": [name_value]}),
        m2: _person(m2, keep_src, {"alias": [alias_value]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2), by_id)
    assert dropped == ()
    write_entities(clean_graph, [merged])
    clean_graph.execute_write(
        "MATCH (n:Entity {id: $id}) SET n.caption = $caption", id=survivor_id, caption=name_value
    )
    pre = _read_node(clean_graph, survivor_id)
    assert pre is not None and pre.get("caption") == name_value

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=_landing(minio),
            source_id=erased_src,
            authorized_by="it-erase-caption-sole-op",
        )
        session.commit()

    # Independent oracle: the SAME frozen reconstruct_entities prune_live_to_fold itself must
    # use, over the POST-scrub remaining rows.
    from worldmonitor.resolution.projector import build_survivor_of, reconstruct_entities

    with sessions() as session:
        survivor_of = build_survivor_of(session)
        remaining_rows = list(session.execute(select(StatementRecord)).scalars())
        fold_entities = {
            e.id: e for e in reconstruct_entities(remaining_rows, survivor_of) if e.id is not None
        }
    expected_caption = fold_entities[survivor_id].caption

    after = _read_node(clean_graph, survivor_id)
    assert after is not None
    assert "name" not in after, (
        "precondition sanity: erase_source_graph must already have popped the sole-witnessed "
        f"name prop, got name={after.get('name')!r}"
    )
    assert after.get("caption") == expected_caption, (
        f"IT-ERASE-caption-sole-witness VIOLATED: n.caption={after.get('caption')!r} does not "
        f"match the post-scrub fold's caption {expected_caption!r} (stale/uncomputed caption "
        "when compared_props ends up empty)"
    )
    assert after.get("caption") != name_value, (
        "non-vacuity: the caption must actually CHANGE off the sole-witnessed erased name"
    )

    engine.dispose()


# =========================================================================================
# IT-ERASE-shared-multivalued-prop (fix-round NEW-2, MEDIUM — value-level positive
# attribution for a multi-valued prop)
# =========================================================================================


def test_it_erase_legacy_value_survives_sharing_a_multivalued_prop_with_an_erased_value(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
) -> None:
    """IT-ERASE-shared-multivalued-prop / fix-round NEW-2 (MEDIUM).

    ONE multi-valued prop (``alias``) holds THREE live values: erased-source-logged, kept-
    source-logged, and a legacy (never statement-logged) value from a THIRD, never-erased
    source. ``erase_source_graph``'s witness map is prop-granular, so the whole value list
    survives it untouched (other witnesses remain); ``prune_live_to_fold``'s
    positive-attribution gate is (today) also only prop-granular, wiping every value it
    couldn't positively re-derive from the fold's reconstruction — including the innocent
    legacy value.

    RED today: ``compared_props["alias"]`` is set to the fold's reconstructed value set
    (``[kept_alias_value]`` only) — the legacy value is wiped alongside the genuinely-erased
    one.
    """
    erased_src = "esrc-shared:it"
    keep_src = "ksrc-shared:it"
    legacy_src = "lsrc-shared:it"
    survivor_id = "surv-shared-it"
    m1 = f"{survivor_id}-m1"
    m2 = f"{survivor_id}-m2"
    m3 = f"{survivor_id}-m3"
    shared_name = "Shared Multivalued Name"
    erased_alias_value = "IT Erased Alias"
    kept_alias_value = "IT Kept Alias"
    legacy_alias_value = "IT Legacy Alias"

    engine, sessions = _sessions(postgres_dsn)
    with sessions() as session:
        session.add(_stmt(survivor_id, m1, "name", shared_name, erased_src))
        session.add(_stmt(survivor_id, m2, "name", shared_name, keep_src))
        session.add(_stmt(survivor_id, m1, "alias", erased_alias_value, erased_src))
        session.add(_stmt(survivor_id, m2, "alias", kept_alias_value, keep_src))
        # m3's alias is DELIBERATELY never logged (legacy/pre-dual-write data) — no
        # `_stmt(...)` call for it anywhere.
        session.commit()

    ensure_constraints(clean_graph)
    by_id = {
        m1: _person(m1, erased_src, {"name": [shared_name], "alias": [erased_alias_value]}),
        m2: _person(m2, keep_src, {"name": [shared_name], "alias": [kept_alias_value]}),
        m3: _person(m3, legacy_src, {"alias": [legacy_alias_value]}),
    }
    merged, dropped = _merge_entities(survivor_id, (m1, m2, m3), by_id)
    assert dropped == ()
    write_entities(clean_graph, [merged])

    pre = _read_node(clean_graph, survivor_id)
    assert pre is not None
    pre_alias = set(pre.get("alias") or [])
    assert pre_alias == {erased_alias_value, kept_alias_value, legacy_alias_value}, (
        f"precondition: all three alias values must be live pre-erasure, got {pre_alias!r}"
    )

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=_landing(minio),
            source_id=erased_src,
            authorized_by="it-erase-shared-op",
        )
        session.commit()

    after = _read_node(clean_graph, survivor_id)
    assert after is not None
    after_alias = set(after.get("alias") or [])
    assert erased_alias_value not in after_alias, (
        f"IT-ERASE-shared (a) VIOLATED: the erased-source-logged value survives: {after_alias!r}"
    )
    assert kept_alias_value in after_alias, "non-vacuity: the kept, still-logged value must survive"
    assert legacy_alias_value in after_alias, (
        "IT-ERASE-shared (b) NEW-2 VIOLATED: the legacy (never statement-logged) alias value "
        f"from an UN-erased source was wiped: alias={after_alias!r}"
    )
    assert after_alias == {kept_alias_value, legacy_alias_value}, (
        f"IT-ERASE-shared VIOLATED: expected EXACTLY the kept + legacy values, got {after_alias!r}"
    )

    engine.dispose()
