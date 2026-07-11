"""Gate B-4a slice-2 — the cross-store erasure orchestrator (``erasure.py:erase_source``).

FAILING-FIRST oracle for cross-store GDPR source erasure (spec ``GATE_B4A_ERASURE_SPEC.md`` §4/§9,
ADR 0049). ``erase_source(*, neo4j, session, landing, source_id, authorized_by)`` removes ONE
source's contribution from all four stores — landing (MinIO prefix delete), ER queue
(``raw_entity`` redacted to a non-PII shell), dead-letter (``error`` redacted), and the Neo4j graph
(slice-1 prune) — idempotently, source-scoped, runtime-authorized, and audited via one
``TaskRun(kind="erase")`` row. It PRESERVES the ``canonical_id_ledger`` / ``ResolverJudgement`` /
``SignOff`` / ``MergeAudit`` rows (no un-merge, the one sanctioned exception to append-only).

This file holds BOTH:
  * Docker-free pure-logic tests (the ``source_id -> landing-prefix`` derivation and the
    ``authorized_by`` contract) — UNMARKED so they run in the default quality job, giving a local
    RED/GREEN signal for an otherwise all-integration gate.
  * ``integration``-marked cross-store tests (Neo4j + Postgres + MinIO testcontainers) that drive
    the real ingest runner + ER pipeline, then erase and pin the post-state of every store.

RED today: ``worldmonitor.erasure`` does not exist (imported lazily inside each test so the file
collects); ``LandingStore.delete`` / ``delete_prefix`` are not implemented yet.

Gate P2 (ADR 0107) additive extension, appended at the end of this file (``test_t9``/``test_t10``):
erase_source ALSO reaches the Gate-2a/P1 statement-log dual-write and gains additive
``TaskRun.stats`` scrub-count keys — added ON TOP of the existing, LOCKED ``_STATS_KEYS`` /
``_COUNT_KEYS`` below (untouched, unweakened) and the existing ``test_t3``..``test_t6`` (untouched,
including ``test_t4``'s idempotent zero-run assertion at what was originally line 372).
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Iterable, Iterator, Mapping
from typing import Any

import pytest
from sqlalchemy import select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import (
    CanonicalIdLedger,
    ErQueueItem,
    IngestDeadLetter,
    MergeAudit,
    ResolverJudgement,
    SignOff,
    StatementRecord,
    TaskRun,
)
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import get_entity_by_alias, resolve_node_id
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.plugins.base import Capability, Connector, Kind, Manifest, Mode, RawRecord
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.runner.ingest import run_ingest
from worldmonitor.storage.landing import LandingStore

# The per-store count keys the audit ``TaskRun.stats`` MUST carry (spec §4.4) — locked verbatim.
_COUNT_KEYS = (
    "nodes_deleted",
    "nodes_pruned",
    "props_retracted",
    "edges_deleted",
    "queue_rows_redacted",
    "landing_objects_deleted",
    "dead_letters_redacted",
)
_STATS_KEYS = ("source_id", "authorized_by", *_COUNT_KEYS)

# Gate P2 (ADR 0107) — the ADDITIVE per-lane scrub-count fields ErasureResult/TaskRun.stats gain
# ON TOP of the existing _COUNT_KEYS/_STATS_KEYS above (those stay byte-unchanged, locked by
# test_t3/test_t4/test_t5a/test_t6 below — this is a SEPARATE, additive contract this gate's
# builder must satisfy; it does not replace or narrow the original keys).
_SCRUB_COUNT_KEYS = (
    "statements_scrubbed",
    "context_claims_scrubbed",
    "decisions_redacted",
    "survivors_value_pruned",
)


# ================================================ Docker-free pure-logic tests (no mark)


def test_landing_prefix_mirrors_ingest_key_scheme() -> None:
    """``_landing_prefix(source_id)`` derives the source's landing object prefix by mirroring the
    ingest key scheme (``runner/ingest.py``) and REUSING its ``_safe_segment`` sanitizer."""
    from worldmonitor.erasure import _landing_prefix
    from worldmonitor.runner.ingest import _safe_segment

    assert (
        _landing_prefix("opensanctions:ie_unlawful_organizations")
        == "opensanctions/ie_unlawful_organizations/"
    )
    assert _landing_prefix("ofac:sdn") == "ofac/sdn/"
    # Reuses _safe_segment (a naive ``source_id.replace(":", "/") + "/"`` would yield "conn/a/b/").
    assert _landing_prefix("conn:a/b") == "conn/a_b/"

    # The derived prefix is a TRUE prefix of the real ingest key for the same source (single source
    # of truth) — so a prefix delete catches exactly that source's landed bytes.
    connector, dataset, record_key = "opensanctions", "ie_unlawful_organizations", "Q-123/x"
    key = "/".join(
        filter(
            None,
            [
                connector,
                _safe_segment(dataset) if dataset else "",
                f"{_safe_segment(record_key)}.json",
            ],
        )
    )
    assert key.startswith(_landing_prefix(f"{connector}:{dataset}"))


def test_landing_prefix_is_slash_terminated_and_collision_safe() -> None:
    """The prefix is ``/``-terminated, so erasing ``"ofac"`` can never sweep ``"ofac-eu"`` bytes
    (the B-3 prefix-collision guard) — in either direction."""
    from worldmonitor.erasure import _landing_prefix

    a = _landing_prefix("ofac:sdn")
    b = _landing_prefix("ofac-eu:sdn")
    assert a.endswith("/") and b.endswith("/")
    assert a != b
    assert not b.startswith(a), "erasing 'ofac:sdn' must not sweep 'ofac-eu:sdn'"
    assert not a.startswith(b), "erasing 'ofac-eu:sdn' must not sweep 'ofac:sdn'"


def test_t7_erase_source_requires_authorized_by() -> None:
    """T7 (authorization). ``authorized_by`` is a REQUIRED keyword-only argument (no default); a
    call omitting it is rejected before any store is touched (it can never run anonymously)."""
    from worldmonitor.erasure import erase_source

    sig = inspect.signature(erase_source)
    assert "authorized_by" in sig.parameters
    param = sig.parameters["authorized_by"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, "authorized_by must be keyword-only"
    assert param.default is inspect.Parameter.empty, "authorized_by must be REQUIRED (no default)"

    with pytest.raises(TypeError):
        erase_source(neo4j=None, session=None, landing=None, source_id="conn:ds")  # type: ignore[arg-type, call-arg]


# ========================================================= integration test scaffolding

_MANIFEST = Manifest(
    connector_id="testsrc",
    name="TestSrc",
    version="0",
    kind=Kind.CONNECTOR,
    mode=Mode.EXTERNAL_IMPORT,
    capability=Capability.PASSIVE,
)


def _record(key: str) -> RawRecord:
    return RawRecord(key=key, data=b'{"pii": 1}', retrieved_at="2026-06-25T00:00:00Z")


class _PiiConnector(Connector):
    """A scripted connector that maps each record to a Person carrying a PII name."""

    def __init__(
        self,
        records: list[RawRecord],
        *,
        fail_map_on: frozenset[str] = frozenset(),
        name_for: Mapping[str, str] | None = None,
    ) -> None:
        self._records = records
        self._fail_map_on = fail_map_on
        self._name_for = dict(name_for or {})

    @property
    def manifest(self) -> Manifest:
        return _MANIFEST

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        yield from self._records

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        if record.key in self._fail_map_on:
            raise ValueError(f"unmappable PII record {record.key}")
        entity = make_entity(
            {
                "id": record.key,
                "schema": "Person",
                "properties": {"name": [self._name_for[record.key]]},
                "datasets": [provenance.source_id],
            }
        )
        return [stamp(entity, provenance)]


def _landing(minio: tuple[str, str, str]) -> LandingStore:
    """A LandingStore on a per-test bucket (a shared MinIO would otherwise bleed across tests)."""
    endpoint, access_key, secret_key = minio
    store = LandingStore.connect(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=f"landing-{uuid.uuid4().hex[:8]}",
    )
    store.ensure_bucket()
    return store


def _sessions(postgres_dsn: str):
    engine = make_engine(postgres_dsn)
    create_all(engine)
    return engine, session_factory(engine)


def _node(client: Neo4jClient, node_id: str) -> dict[str, Any] | None:
    rows = client.execute_read(
        "MATCH (n:Entity {id: $id}) RETURN properties(n) AS props", id=node_id
    )
    return rows[0]["props"] if rows else None


# =========================================================================================== T3


@pytest.mark.integration
def test_t3_cross_store_erase_removes_pii_everywhere_and_audits(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """End-to-end through the REAL ingest runner: a good PII record + a map-stage dead-letter both
    land under the source prefix. After ``erase_source``: the landing prefix is empty, the ER row is
    a non-PII shell, the dead-letter ``error`` is redacted, the graph node + value are gone, and a
    ``TaskRun(kind="erase")`` row records the operator + non-zero per-store counts."""
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)
    ensure_constraints(clean_graph)

    pii = "Johnathan Q Testperson"
    connector = _PiiConnector(
        [_record("person-good"), _record("person-bad")],
        fail_map_on=frozenset({"person-bad"}),
        name_for={"person-good": pii},
    )
    with sessions() as session:
        stats = run_ingest(connector, {"dataset": "people"}, landing=landing, session=session)
    assert stats.landed == 2 and stats.queued == 1 and stats.dead_lettered == 1

    source_id = "testsrc:people"
    prefix = "testsrc/people/"

    # PRECONDITION — PII present in landing, ER queue, and dead-letter.
    assert len(landing.list_keys(prefix=prefix)) == 2, "good + dead-lettered raw bytes both landed"
    with sessions() as session:
        rows = session.execute(select(ErQueueItem)).scalars().all()
        assert len(rows) == 1
        assert rows[0].raw_entity["wm_prov_source_id"] == [source_id]
        assert pii in json.dumps(rows[0].raw_entity)
        dls = session.execute(select(IngestDeadLetter)).scalars().all()
        assert len(dls) == 1 and dls[0].stage == "map"
        assert dls[0].source_record is not None and dls[0].source_record.startswith(
            f"s3://{landing.bucket}/{prefix}"
        )
        assert dls[0].error, "the map-stage dead-letter carries a (PII-bearing) error fragment"

    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)
    assert _node(clean_graph, "person-good") is not None, "the resolved node must exist pre-erase"
    name_node = clean_graph.execute_read(
        "MATCH (n:Entity {id: 'person-good'}) RETURN n.name AS name"
    )[0]
    assert pii in json.dumps(name_node)

    # ERASE
    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=source_id,
            authorized_by="dpo@worldmonitor",
        )
        session.commit()

    # POSTCONDITION — every store is scrubbed of the source's PII.
    assert landing.list_keys(prefix=prefix) == [], "all of the source's landed bytes are deleted"
    with sessions() as session:
        rows = session.execute(select(ErQueueItem)).scalars().all()
        assert len(rows) == 1, "the queue row SHELL is kept for audit"
        assert rows[0].raw_entity == {"erased": True, "source_id": source_id}
        assert pii not in json.dumps(rows[0].raw_entity)
        dls = session.execute(select(IngestDeadLetter)).scalars().all()
        assert len(dls) == 1 and dls[0].error == "", "the dead-letter PII fragment is redacted"

        runs = session.execute(select(TaskRun).where(TaskRun.kind == "erase")).scalars().all()
        assert len(runs) == 1, "exactly one TaskRun(kind='erase') audit row per run"
        run = runs[0]
        assert run.status == "ok"
        assert run.stats is not None
        for key in _STATS_KEYS:
            assert key in run.stats, f"audit stats must carry {key!r} (spec §4.4)"
        assert run.stats["source_id"] == source_id
        assert run.stats["authorized_by"] == "dpo@worldmonitor"
        assert run.stats["landing_objects_deleted"] >= 2
        assert run.stats["queue_rows_redacted"] >= 1
        assert run.stats["dead_letters_redacted"] >= 1
        assert run.stats["nodes_deleted"] >= 1

    assert _node(clean_graph, "person-good") is None, "the sole-source graph node is gone"
    hits = clean_graph.execute_read(
        "MATCH (n) WHERE $v IN coalesce(n.name, []) RETURN count(n) AS n", v=pii
    )[0]["n"]
    assert hits == 0, "the personal-data value must be gone from the graph"
    engine.dispose()


# =========================================================================================== T4


@pytest.mark.integration
def test_t4_second_erase_is_zero_count_idempotent_noop(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """A second ``erase_source`` of the same source is a clean no-op: every store is byte-identical
    to the post-first-erase state, and the only delta is an appended all-zero-count audit row."""
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)
    ensure_constraints(clean_graph)

    connector = _PiiConnector(
        [_record("p1"), _record("p2")],
        fail_map_on=frozenset({"p2"}),
        name_for={"p1": "Repeatable Subject"},
    )
    with sessions() as session:
        run_ingest(connector, {"dataset": "people"}, landing=landing, session=session)
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    source_id = "testsrc:people"
    prefix = "testsrc/people/"
    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=source_id,
            authorized_by="op-1",
        )
        session.commit()

    # Snapshot the post-first-erase state.
    keys_1 = landing.list_keys(prefix=prefix)
    with sessions() as s:
        queue_1 = {r.id: r.raw_entity for r in s.execute(select(ErQueueItem)).scalars()}
        dead_1 = {r.id: r.error for r in s.execute(select(IngestDeadLetter)).scalars()}
    nodes_1 = sorted(r["id"] for r in clean_graph.execute_read("MATCH (n) RETURN n.id AS id"))

    # SECOND erase.
    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=source_id,
            authorized_by="op-2",
        )
        session.commit()

    # Every store is unchanged …
    assert landing.list_keys(prefix=prefix) == keys_1 == []
    with sessions() as s:
        queue_2 = {r.id: r.raw_entity for r in s.execute(select(ErQueueItem)).scalars()}
        dead_2 = {r.id: r.error for r in s.execute(select(IngestDeadLetter)).scalars()}
        assert queue_2 == queue_1, "the redacted ER shell is unchanged on a repeat erase"
        assert dead_2 == dead_1, "the redacted dead-letter is unchanged on a repeat erase"

        # … and exactly one of the two audit rows is an all-zero no-op (the 2nd request).
        runs = s.execute(select(TaskRun).where(TaskRun.kind == "erase")).scalars().all()
        assert len(runs) == 2, (
            "each erase appends its own audit row (the GDPR repeat-request trail)"
        )
        zero_runs = [
            r for r in runs if r.stats is not None and all(r.stats[k] == 0 for k in _COUNT_KEYS)
        ]
        assert len(zero_runs) == 1, "exactly one (the 2nd) erase has all-zero per-store counts"
        assert zero_runs[0].stats is not None
        assert zero_runs[0].stats["authorized_by"] == "op-2"
        assert zero_runs[0].stats["source_id"] == source_id

    nodes_2 = sorted(r["id"] for r in clean_graph.execute_read("MATCH (n) RETURN n.id AS id"))
    assert nodes_2 == nodes_1, "the graph is byte-identical after the repeat erase"
    engine.dispose()


# =========================================================================================== T5a


@pytest.mark.integration
def test_t5a_erasing_a_leaves_source_b_fully_intact(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """Source isolation. Two independent sources are ingested + resolved; erasing source A leaves
    source B's landing object, ER row, and graph node (with its value) completely untouched."""
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)
    ensure_constraints(clean_graph)

    name_a, name_b = "Alice OnlyA Person", "Bob OnlyB Person"
    with sessions() as session:
        run_ingest(
            _PiiConnector([_record("a-1")], name_for={"a-1": name_a}),
            {"dataset": "people-a"},
            landing=landing,
            session=session,
        )
        run_ingest(
            _PiiConnector([_record("b-1")], name_for={"b-1": name_b}),
            {"dataset": "people-b"},
            landing=landing,
            session=session,
        )
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    sid_a = "testsrc:people-a"
    # PRECONDITION — both sources fully present.
    assert len(landing.list_keys("testsrc/people-b/")) == 1
    assert _node(clean_graph, "b-1") is not None
    with sessions() as s:
        b_raw_before = dict(
            s.execute(select(ErQueueItem).where(ErQueueItem.entity_id == "b-1"))
            .scalar_one()
            .raw_entity
        )
    assert name_b in json.dumps(b_raw_before)

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=sid_a,
            authorized_by="op",
        )
        session.commit()

    # Source A is gone …
    assert landing.list_keys("testsrc/people-a/") == []
    assert _node(clean_graph, "a-1") is None
    # … source B is fully intact across every store.
    assert len(landing.list_keys("testsrc/people-b/")) == 1, (
        "source B's landing object is untouched"
    )
    assert _node(clean_graph, "b-1") is not None
    b_node_name = clean_graph.execute_read("MATCH (n:Entity {id: 'b-1'}) RETURN n.name AS name")[0]
    assert name_b in json.dumps(b_node_name)
    with sessions() as s:
        b_after = s.execute(select(ErQueueItem).where(ErQueueItem.entity_id == "b-1")).scalar_one()
        assert b_after.raw_entity == b_raw_before, "source B's ER row must be byte-identical"
        a_after = s.execute(select(ErQueueItem).where(ErQueueItem.entity_id == "a-1")).scalar_one()
        assert a_after.raw_entity == {"erased": True, "source_id": sid_a}, (
            "source A's row is redacted"
        )
    engine.dispose()


# =========================================================================================== T6


def _company_item(entity_id: str, dataset: str, *, qid: str, source_record: str) -> ErQueueItem:
    """An anchored Company ER-queue row whose lineage traces to ``testsrc:<dataset>``."""
    source_id = f"testsrc:{dataset}"
    provenance = Provenance(
        source_id=source_id,
        retrieved_at="2026-06-25T00:00:00Z",
        reliability="A",
        source_record=source_record,
    )
    entity = stamp(
        make_entity(
            {
                "id": entity_id,
                "schema": "Company",
                "properties": {
                    "name": ["Acme Holdings Ltd"],
                    "jurisdiction": ["us"],
                    "wikidataId": [qid],
                },
                "datasets": [source_id],
            }
        ),
        provenance,
    )
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="testsrc",
        entity_id=entity_id,
        raw_entity=entity.to_dict(),
        source_record=source_record,
        status="pending",
    )


@pytest.mark.integration
def test_t6_erase_preserves_ledger_judgements_signoff_and_audit(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """No resurrection / no un-merge. A 2-source anchored canonical is merged through the pipeline;
    erasing ONE source leaves the canonical alive (durable id intact, members not resurrected) and
    PRESERVES every durable human-decision row — the ``canonical_id_ledger`` (incl. the alias map),
    ``ResolverJudgement`` (incl. a forbidden-merge NEGATIVE), ``SignOff``, and ``MergeAudit``."""
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)
    ensure_constraints(clean_graph)

    qid = "Q888"
    durable = f"wm-anchor-qid-{qid}"
    with sessions() as session:
        session.add(
            _company_item(
                "mA", "ds-a", qid=qid, source_record=f"s3://{landing.bucket}/testsrc/ds-a/mA.json"
            )
        )
        session.add(
            _company_item(
                "mB", "ds-b", qid=qid, source_record=f"s3://{landing.bucket}/testsrc/ds-b/mB.json"
            )
        )
        # A positive sign-off judgement forces the mA~mB merge deterministically.
        lo, hi = sorted(("mA", "mB"))
        session.add(
            ResolverJudgement(
                id=str(uuid.uuid4()),
                left_id=lo,
                right_id=hi,
                judgement="positive",
                source="signoff",
            )
        )
        # A human NEGATIVE judgement that PREVENTS a forbidden merge — deleting it could resurrect
        # that merge, so it MUST survive erasure (spec §5 / §11 DENY).
        nlo, nhi = sorted(("forbidden-X", "forbidden-Y"))
        session.add(
            ResolverJudgement(
                id=str(uuid.uuid4()),
                left_id=nlo,
                right_id=nhi,
                judgement="negative",
                source="signoff",
            )
        )
        session.add(
            SignOff(
                id=str(uuid.uuid4()),
                canonical_id=durable,
                source_ids=["mA", "mB"],
                decision="approved",
                approver="reviewer@worldmonitor",
                reason="same entity",
            )
        )
        session.commit()
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)
    assert _node(clean_graph, durable) is not None, "the anchored merge must materialise"

    def _snapshot(s: Any) -> dict[str, list[Any]]:
        return {
            "ledger": sorted(
                (r.canonical_id, r.canonical_alias, r.anchor_kind, r.anchor_value)
                for r in s.execute(select(CanonicalIdLedger)).scalars()
            ),
            "judgements": sorted(
                (r.left_id, r.right_id, r.judgement)
                for r in s.execute(select(ResolverJudgement)).scalars()
            ),
            "signoffs": sorted(
                (r.canonical_id, r.decision, r.approver)
                for r in s.execute(select(SignOff)).scalars()
            ),
            "merge_audit": sorted(
                (r.canonical_id, r.decision) for r in s.execute(select(MergeAudit)).scalars()
            ),
        }

    with sessions() as s:
        before = _snapshot(s)
    assert before["ledger"], "the merge must have written canonical_id_ledger rows"
    assert ("forbidden-X", "forbidden-Y", "negative") in before["judgements"]
    assert any(decision == "merged" for (_, decision) in before["merge_audit"])

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id="testsrc:ds-a",
            authorized_by="dpo@worldmonitor",
        )
        session.commit()

    # The multi-source canonical SURVIVES and is NOT un-merged into its members.
    assert _node(clean_graph, durable) is not None, (
        "a multi-source canonical survives one-source erase"
    )
    companies = sorted(
        r["id"] for r in clean_graph.execute_read("MATCH (n:Company) RETURN n.id AS id")
    )
    assert companies == [durable], "erase must NOT resurrect / un-merge merged-away members"

    with sessions() as s:
        after = _snapshot(s)
        assert after["ledger"] == before["ledger"], "canonical_id_ledger must NEVER be modified"
        assert after["judgements"] == before["judgements"], "ResolverJudgements must all survive"
        assert ("forbidden-X", "forbidden-Y", "negative") in after["judgements"], (
            "the forbidden-merge NEGATIVE judgement must survive erasure"
        )
        assert after["signoffs"] == before["signoffs"], "human SignOff rows must survive"
        assert after["merge_audit"] == before["merge_audit"], "the merge audit trail must survive"
        # Alias-on-read still resolves a merged-away member id to the surviving canonical.
        assert resolve_node_id(s, "mA") == durable
        via_alias = get_entity_by_alias(clean_graph, s, entity_id="mA")
        assert via_alias is not None and via_alias["id"] == durable
    engine.dispose()


# ============================================================ Gate P2 (ADR 0107) — ADDITIVE T9/T10
#
# Everything above this line is UNCHANGED from the pre-P2 file (byte-identical assertions,
# including test_t4's idempotent zero-run over _COUNT_KEYS). The two tests below are NEW and
# ADDITIVE: they extend erase_source's oracle to the Gate-2a/P1 statement-log lane this gate
# wires the scrub into, and to the additive TaskRun.stats scrub-count keys (_SCRUB_COUNT_KEYS,
# defined above, alongside — never replacing — the original _STATS_KEYS/_COUNT_KEYS).


# =========================================================================================== T9


@pytest.mark.integration
def test_t9_erase_scrubs_the_statement_log_and_extends_stats_additively(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """Gate P2 (ADR 0107). ``resolve_pending`` already dual-writes ``StatementRecord`` rows for
    every promoted cluster (Gate 2a, ``pipeline.py:487`` — unconditional, including singletons).
    ``erase_source`` must ALSO scrub those rows for the erased source, and ``TaskRun.stats`` must
    carry the ADDITIVE ``_SCRUB_COUNT_KEYS`` on top of the existing, unmodified ``_STATS_KEYS``.

    RED today: no scrub runs at all — the statement row(s) survive erase_source, and
    ``TaskRun.stats`` carries NONE of ``_SCRUB_COUNT_KEYS`` (``KeyError`` on lookup).
    """
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)
    ensure_constraints(clean_graph)

    pii = "Scrub Lane Testperson"
    connector = _PiiConnector([_record("scrub-good")], name_for={"scrub-good": pii})
    with sessions() as session:
        run_ingest(connector, {"dataset": "scrub-lane"}, landing=landing, session=session)
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    source_id = "testsrc:scrub-lane"
    with sessions() as session:
        pre_rows = (
            session.execute(select(StatementRecord).where(StatementRecord.dataset == source_id))
            .scalars()
            .all()
        )
    assert len(pre_rows) >= 1, "precondition: resolve_pending must dual-write statement rows"

    from worldmonitor.erasure import erase_source

    with sessions() as session:
        erase_source(
            neo4j=clean_graph,
            session=session,
            landing=landing,
            source_id=source_id,
            authorized_by="dpo-p2@worldmonitor",
        )
        session.commit()

    with sessions() as session:
        post_rows = (
            session.execute(select(StatementRecord).where(StatementRecord.dataset == source_id))
            .scalars()
            .all()
        )
    assert post_rows == [], (
        f"Gate P2 INV-ERASE-3LANE: statement rows for {source_id!r} must be scrubbed by "
        f"erase_source, {len(post_rows)} survive"
    )

    with sessions() as session:
        runs = session.execute(select(TaskRun).where(TaskRun.kind == "erase")).scalars().all()
    assert len(runs) == 1
    run = runs[0]
    assert run.stats is not None
    for key in _STATS_KEYS:
        assert key in run.stats, f"the EXISTING (locked) stats key {key!r} must still be present"
    for key in _SCRUB_COUNT_KEYS:
        assert key in run.stats, f"Gate P2: the ADDITIVE scrub-count key {key!r} must be present"
    assert run.stats["statements_scrubbed"] >= 1, (
        "at least one statement row (the singleton's) must be counted as scrubbed"
    )
    engine.dispose()


# ========================================================================================== T10


@pytest.mark.integration
def test_t10_second_erase_scrub_counts_are_also_zero(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient
) -> None:
    """Gate P2 (ADR 0107). A SECOND ``erase_source`` on an already-erased source has ALL-ZERO
    ``_SCRUB_COUNT_KEYS`` too — extending ``test_t4``'s idempotent zero-run (over the ORIGINAL,
    unmodified ``_COUNT_KEYS``) to the new lanes, without touching ``test_t4`` itself.

    RED today: ``_SCRUB_COUNT_KEYS`` is simply absent from ``run.stats`` — ``KeyError`` on lookup.
    """
    landing = _landing(minio)
    engine, sessions = _sessions(postgres_dsn)
    ensure_constraints(clean_graph)

    connector = _PiiConnector([_record("scrub2-a")], name_for={"scrub2-a": "Scrub Two Subject"})
    with sessions() as session:
        run_ingest(connector, {"dataset": "scrub-lane-2"}, landing=landing, session=session)
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    source_id = "testsrc:scrub-lane-2"
    from worldmonitor.erasure import erase_source

    for authorized_by in ("dpo-first", "dpo-second"):
        with sessions() as session:
            erase_source(
                neo4j=clean_graph,
                session=session,
                landing=landing,
                source_id=source_id,
                authorized_by=authorized_by,
            )
            session.commit()

    with sessions() as session:
        runs = (
            session.execute(
                select(TaskRun).where(TaskRun.kind == "erase").order_by(TaskRun.started_at)
            )
            .scalars()
            .all()
        )
    assert len(runs) == 2
    second = runs[1]
    assert second.stats is not None
    for key in _SCRUB_COUNT_KEYS:
        assert key in second.stats, f"the ADDITIVE key {key!r} must be present on EVERY run"
        assert second.stats[key] == 0, (
            f"Gate P2 idempotent zero-run: the SECOND erase's {key!r} must be 0, got "
            f"{second.stats[key]!r}"
        )
    engine.dispose()
