"""Gate B-4b — backup / restore disaster recovery (``worldmonitor.backup``).

FAILING-FIRST oracle for cross-store DR (spec ``GATE_B4B_BACKUP_RESTORE_SPEC.md`` §6, ADR 0050).
``backup(*, neo4j, session, landing, dest)`` dumps all THREE stores — Postgres (every
``Base.metadata`` table + ``alembic_version``, via a SQLAlchemy logical dump), Neo4j (an online
Cypher logical export of nodes [labels + props incl. ``id``/``prov_*``/``prov_witnesses``] + edges
[type + endpoint ids + ``prov_*``]), and the MinIO landing bucket (object mirror) — to ``dest``,
and ``restore(*, neo4j, session, landing, src)`` rebuilds them by wipe + reload, halting LOUDLY on
any partial failure.

The reason this gate exists is NOT "the process exits 0": a restore that silently drops the
``resolver_judgement`` NEGATIVES re-enables the exact H-1 transitive re-merge ADR 0037 closes — now
reached via disaster recovery. So the correctness bar is **the human-reject guarantee and the
canonical-id ledger survive DR byte-for-byte**.

RED today: ``worldmonitor.backup`` does not exist (imported lazily inside each test, after the seed,
so the file still collects). The seed + baseline + WIPE-precondition run against real
testcontainers; the suite then fails at the ``from worldmonitor.backup import ...`` boundary — RED
for the right reason. GREEN once ``src/worldmonitor/backup.py`` is built.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text

from worldmonitor.db.engine import create_all, make_engine, migrate_to_head, session_factory
from worldmonitor.db.models import (
    Base,
    CanonicalIdLedger,
    ErQueueItem,
    MergeAudit,
    ResolverJudgement,
    SignOff,
)
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.writer import write_entities
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.pipeline import _load_judgements, resolve_pending
from worldmonitor.resolution.splink_model import ScoredPair
from worldmonitor.storage.landing import LandingStore

pytestmark = pytest.mark.integration

# The ftmg node label a ``topics:["sanction"]`` code projects to (sensitivity guard: sanction ->
# Sanction). Seeded so the round-trip proves a topic label survives DR (the guard still sees it).
_SANCTION_LABEL = "Sanction"
_PROBE_KEY = "backup-probe/dr.json"
_PROBE_BYTES = b'{"probe":"disaster-recovery","value":42}'


# ===================================================================== seed / snapshot helpers


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


def _db(postgres_dsn: str):
    """Engine + session factory; ``migrate_to_head`` so ``alembic_version`` exists at head.

    Backup must capture that row and restore must re-stamp it, so a post-restore ``migrate_to_head``
    is a no-op (ADR 0050 / spec §4.1).
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    migrate_to_head(engine)
    return engine, session_factory(engine)


def _company_item(entity_id: str, dataset: str, *, qid: str, source_record: str) -> ErQueueItem:
    """An anchored Company ER-queue row whose lineage traces to ``testsrc:<dataset>`` (test_t6)."""
    source_id = f"testsrc:{dataset}"
    provenance = Provenance(
        source_id=source_id,
        retrieved_at="2026-06-26T00:00:00Z",
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


def _person(entity_id: str) -> FtmEntity:
    """A Person used by the H-1 functional proof (mirrors test_resolution_negative_judgement)."""
    return make_entity(
        {
            "id": entity_id,
            "schema": "Person",
            "properties": {"name": ["Ivan Petrov"], "nationality": ["ru"]},
            "datasets": ["t"],
        }
    )


def _stamped(data: dict[str, object], provenance: Provenance) -> FtmEntity:
    return stamp(make_entity(data), provenance)


def _node(client: Neo4jClient, node_id: str) -> dict[str, Any] | None:
    rows = client.execute_read("MATCH (n {id: $id}) RETURN properties(n) AS props", id=node_id)
    return rows[0]["props"] if rows else None


def _node_ids(client: Neo4jClient) -> list[str]:
    return sorted(r["id"] for r in client.execute_read("MATCH (n) RETURN n.id AS id"))


def _edge_count(client: Neo4jClient) -> int:
    return client.execute_read("MATCH ()-[r]->() RETURN count(r) AS c")[0]["c"]


def _nodes_snapshot(client: Neo4jClient) -> dict[str, str]:
    """id -> canonical JSON of {labels, props} for every node (byte-identity oracle)."""
    rows = client.execute_read(
        "MATCH (n) RETURN n.id AS id, labels(n) AS labels, properties(n) AS props"
    )
    return {
        r["id"]: json.dumps({"labels": sorted(r["labels"]), "props": r["props"]}, sort_keys=True)
        for r in rows
    }


def _edges_snapshot(client: Neo4jClient) -> list[str]:
    """Sorted canonical-JSON of every edge (type + endpoint ids + props) — byte-identity oracle."""
    rows = client.execute_read(
        "MATCH (a)-[r]->(b) "
        "RETURN type(r) AS type, a.id AS start, b.id AS end, properties(r) AS props"
    )
    return sorted(
        json.dumps(
            {"type": r["type"], "start": r["start"], "end": r["end"], "props": r["props"]},
            sort_keys=True,
        )
        for r in rows
    )


def _node_labels(client: Neo4jClient, node_id: str) -> list[str]:
    rows = client.execute_read("MATCH (n {id: $id}) RETURN labels(n) AS labels", id=node_id)
    return sorted(rows[0]["labels"]) if rows else []


def _edge_prov(client: Neo4jClient, edge_id: str) -> dict[str, Any] | None:
    rows = client.execute_read(
        "MATCH ()-[r]->() WHERE r.id = $id "
        "RETURN r.prov_source_id AS source_id, r.prov_source_record AS source_record, "
        "r.prov_reliability AS reliability, r.prov_retrieved_at AS retrieved_at",
        id=edge_id,
    )
    return rows[0] if rows else None


def _pg_counts(session: Any) -> dict[str, int]:
    return {
        table.name: session.execute(select(func.count()).select_from(table)).scalar_one()
        for table in Base.metadata.sorted_tables
    }


def _ledger_snapshot(session: Any) -> list[tuple[str, str, str, str]]:
    return sorted(
        (r.canonical_id, r.canonical_alias, r.anchor_kind, r.anchor_value)
        for r in session.execute(select(CanonicalIdLedger)).scalars()
    )


def _judgements(session: Any) -> list[tuple[str, str, str]]:
    return sorted(
        (r.left_id, r.right_id, r.judgement)
        for r in session.execute(select(ResolverJudgement)).scalars()
    )


def _signoffs(session: Any) -> list[tuple[str, str, str]]:
    return sorted(
        (r.canonical_id, r.decision, r.approver) for r in session.execute(select(SignOff)).scalars()
    )


def _merge_audit(session: Any) -> list[tuple[str, str]]:
    return sorted(
        (r.canonical_id, r.decision) for r in session.execute(select(MergeAudit)).scalars()
    )


def _landing_map(landing: LandingStore) -> dict[str, bytes]:
    return {key: landing.get(key) for key in landing.list_keys("")}


def _wipe_postgres(engine: Any) -> None:
    """TRUNCATE every Base table AND drop the alembic_version row.

    Dropping ``alembic_version`` too makes the restore re-stamp non-vacuous — restore must put both
    the rows and the migration head back.
    """
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        conn.execute(text("DELETE FROM alembic_version"))


class _ExplodingLanding:
    """A landing stand-in whose every store-touch RAISES.

    Proves backup halts loud on any store-export failure, whichever store it reads first (R3).
    """

    bucket = "exploding-bucket"

    def ensure_bucket(self) -> None:
        raise RuntimeError("simulated landing export failure")

    def list_keys(self, prefix: str = "") -> list[str]:
        raise RuntimeError("simulated landing export failure")

    def get(self, key: str) -> bytes:
        raise RuntimeError("simulated landing export failure")

    def put(self, key: str, data: bytes, **kwargs: Any) -> str:
        raise RuntimeError("simulated landing export failure")

    def delete_prefix(self, prefix: str) -> int:
        raise RuntimeError("simulated landing export failure")


# ========================================================================= R1 — the round-trip


def test_backup_wipe_restore_preserves_h1_and_canonical_ids(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient, tmp_path: Any
) -> None:
    """THE gate. Seed a realistic state — an ANCHORED merge (``wm-anchor-qid-Q888`` + its ledger),
    an Ownership edge carrying ``prov_*``, a topic label, a human NEGATIVE judgement on ('a','c') +
    a SignOff, and a landing object — then backup -> WIPE all three stores -> restore, and prove:
    the human reject survived DR (row present AND a fresh resolve does NOT re-fuse 'a'/'c'), the
    canonical ids are byte-identical, ``prov_*`` survives on every node AND the edge, Postgres rows
    count-match, MinIO bytes are byte-identical, and the whole restored state equals the baseline.
    """
    landing = _landing(minio)
    engine, sessions = _db(postgres_dsn)
    ensure_constraints(clean_graph)
    dest = tmp_path / "dr-backup"

    qid = "Q888"
    durable = f"wm-anchor-qid-{qid}"

    # --- SEED the human-decision relational state + the anchored merge (test_t6 idiom) ---
    with sessions() as s:
        s.add(
            _company_item(
                "mA", "ds-a", qid=qid, source_record=f"s3://{landing.bucket}/testsrc/ds-a/mA.json"
            )
        )
        s.add(
            _company_item(
                "mB", "ds-b", qid=qid, source_record=f"s3://{landing.bucket}/testsrc/ds-b/mB.json"
            )
        )
        # a positive sign-off forces the mA~mB merge deterministically
        lo, hi = sorted(("mA", "mB"))
        s.add(
            ResolverJudgement(
                id=str(uuid.uuid4()),
                left_id=lo,
                right_id=hi,
                judgement="positive",
                source="signoff",
            )
        )
        # The H-1 oracle: a human NEGATIVE reject on ('a','c'). Losing this on restore would
        # re-open the transitive re-merge ADR 0037 closes — via disaster recovery.
        nlo, nhi = sorted(("a", "c"))
        s.add(
            ResolverJudgement(
                id=str(uuid.uuid4()),
                left_id=nlo,
                right_id=nhi,
                judgement="negative",
                source="signoff",
            )
        )
        s.add(
            SignOff(
                id=str(uuid.uuid4()),
                canonical_id=durable,
                source_ids=["mA", "mB"],
                decision="approved",
                approver="reviewer@worldmonitor",
                reason="same entity",
            )
        )
        s.commit()
    with sessions() as s:
        resolve_pending(session=s, neo4j=clean_graph)
    assert _node(clean_graph, durable) is not None, "the anchored merge must materialise pre-backup"

    # --- SEED an Ownership edge (G1-on-edge oracle) + a topic label, via the writer ---
    edge_prov = Provenance(
        source_id="opencorporates:ownership",
        retrieved_at="2026-06-26T01:00:00Z",
        reliability="B",
        source_record=f"s3://{landing.bucket}/testsrc/own/own-1.json",
    )
    asset_prov = Provenance(
        source_id="opensanctions:sdn",
        retrieved_at="2026-06-26T00:30:00Z",
        reliability="A",
        source_record=f"s3://{landing.bucket}/testsrc/sdn/second.json",
    )
    second_co = _stamped(
        {
            "id": "wm-second-co",
            "schema": "Company",
            "properties": {"name": ["Globex SA"], "topics": ["sanction"]},
            "datasets": ["opensanctions:sdn"],
        },
        asset_prov,
    )
    ownership = _stamped(
        {
            "id": "wm-own-edge-1",
            "schema": "Ownership",
            "properties": {"owner": [durable], "asset": ["wm-second-co"]},
            "datasets": ["opencorporates:ownership"],
        },
        edge_prov,
    )
    write_entities(clean_graph, [second_co, ownership])

    # --- SEED a landing object probe ---
    landing.put(_PROBE_KEY, _PROBE_BYTES)

    # --- BASELINE captured as Python objects (the byte-identity oracle) ---
    base_node_ids = _node_ids(clean_graph)
    assert durable in base_node_ids and "wm-second-co" in base_node_ids
    base_edge_count = _edge_count(clean_graph)
    assert base_edge_count >= 1, "the Ownership edge must materialise pre-backup"
    base_nodes = _nodes_snapshot(clean_graph)
    base_edges = _edges_snapshot(clean_graph)
    base_second_labels = _node_labels(clean_graph, "wm-second-co")
    assert _SANCTION_LABEL in base_second_labels, "the topic label must be applied at seed time"
    base_edge_prov = _edge_prov(clean_graph, "wm-own-edge-1")
    assert base_edge_prov is not None and base_edge_prov["source_id"] == edge_prov.source_id

    with sessions() as s:
        base_counts = _pg_counts(s)
        base_ledger = _ledger_snapshot(s)
        base_judgements = _judgements(s)
        base_signoffs = _signoffs(s)
        base_merge_audit = _merge_audit(s)
        base_alembic = s.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert base_ledger, "the anchored merge must have written canonical_id_ledger rows"
    assert ("a", "c", "negative") in base_judgements
    assert base_counts["resolver_judgement"] == 2
    assert base_counts["sign_off"] == 1
    assert base_counts["merge_audit"] >= 1
    base_landing = _landing_map(landing)
    assert base_landing.get(_PROBE_KEY) == _PROBE_BYTES

    # --- BACKUP (lazy import: RED today — no worldmonitor.backup module exists) ---
    from worldmonitor.backup import backup, restore

    with sessions() as s:
        backup(neo4j=clean_graph, session=s, landing=landing, dest=dest)
    assert (dest / "manifest.json").exists(), "backup must write a completion manifest"
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest.get("complete") is True, "a verified backup is marked complete:true"

    # --- WIPE all three stores; assert each is EMPTY (so restore really does the work) ---
    clean_graph.execute_write("MATCH (n) DETACH DELETE n")
    _wipe_postgres(engine)
    landing.delete_prefix("")
    assert _node_ids(clean_graph) == [], "graph must be empty after wipe"
    assert _edge_count(clean_graph) == 0
    assert landing.list_keys("") == [], "landing must be empty after wipe"
    with sessions() as s:
        assert all(c == 0 for c in _pg_counts(s).values()), "all Postgres tables empty after wipe"
        assert s.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 0

    # --- RESTORE ---
    with sessions() as s:
        restore(neo4j=clean_graph, session=s, landing=landing, src=dest)
        s.commit()

    # 1. H-1 PRESERVED — the load-bearing invariant.
    with sessions() as s:
        restored_judgements = _load_judgements(s)
    restored_pairs = {(j.left_id, j.right_id, j.judgement) for j in restored_judgements}
    assert ("a", "c", "negative") in restored_pairs, "the human NEGATIVE reject row must survive DR"
    # Functional proof: a post-DR resolve over the RESTORED judgements does NOT re-fuse 'a' and 'c',
    # even with a bridging a~b 0.99 / b~c 0.95 window (b joins its stronger side; c is left alone).
    clusters = cluster_and_merge(
        [_person("a"), _person("b"), _person("c")],
        [ScoredPair("a", "b", 0.99), ScoredPair("b", "c", 0.95)],
        judgements=restored_judgements,
    )
    a_cluster = next(c for c in clusters if "a" in c.member_ids)
    c_cluster = next(c for c in clusters if "c" in c.member_ids)
    assert "c" not in a_cluster.member_ids, (
        "a post-DR resolve must NOT re-fuse the human-rejected pair (H-1 survived restore)"
    )
    assert a_cluster is not c_cluster
    assert set(a_cluster.member_ids) == {"a", "b"}
    assert set(c_cluster.member_ids) == {"c"}

    # 2. CANONICAL-ID BYTE-IDENTITY.
    restored_ids = _node_ids(clean_graph)
    assert restored_ids == base_node_ids, (
        "the restored node-id set must equal baseline byte-for-byte"
    )
    assert durable in restored_ids, "the FtM-clean anchor id must round-trip exactly"
    assert _edge_count(clean_graph) == base_edge_count, "edge count must equal the baseline"
    with sessions() as s:
        assert _ledger_snapshot(s) == base_ledger, (
            "canonical_id_ledger must round-trip byte-for-byte"
        )

    # 3. G1 — provenance on every restored node AND edge; the topic label survives.
    restored_node_prov = clean_graph.execute_read(
        "MATCH (n) RETURN n.id AS id, n.prov_source_id AS sid"
    )
    assert restored_node_prov, "graph must be non-empty after restore"
    assert all(r["sid"] for r in restored_node_prov), "every restored node carries prov_source_id"
    restored_edge_prov_all = clean_graph.execute_read(
        "MATCH ()-[r]->() RETURN r.prov_source_id AS sid"
    )
    assert restored_edge_prov_all, "the restored graph must still carry its edge(s)"
    assert all(e["sid"] for e in restored_edge_prov_all), (
        "every restored edge carries prov_source_id"
    )
    assert _edge_prov(clean_graph, "wm-own-edge-1") == base_edge_prov, (
        "the seeded edge provenance must survive byte-identically (G1 on edges)"
    )
    assert _SANCTION_LABEL in _node_labels(clean_graph, "wm-second-co"), (
        "the topic label must survive restore (the guard still sees it)"
    )

    # 4. Postgres rows count-match; alembic_version re-stamped to head.
    with sessions() as s:
        assert _pg_counts(s) == base_counts, (
            "every Postgres table row-count must equal the baseline"
        )
        assert _judgements(s) == base_judgements
        assert _signoffs(s) == base_signoffs
        assert _merge_audit(s) == base_merge_audit
        assert (
            s.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == base_alembic
        )

    # 5. MinIO restored byte-identical.
    restored_landing = _landing_map(landing)
    assert restored_landing == base_landing, "the landing bucket must restore byte-identically"
    assert restored_landing[_PROBE_KEY] == _PROBE_BYTES

    # 6. Exact-state: the whole restored graph snapshot equals the pre-backup baseline.
    assert _nodes_snapshot(clean_graph) == base_nodes, (
        "every node (labels + props) restores exactly"
    )
    assert _edges_snapshot(clean_graph) == base_edges, "every edge restores exactly"

    engine.dispose()


# ======================================================================= R2 — halt-loud restore


def test_restore_aborts_on_incomplete_backup_without_touching_any_store(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient, tmp_path: Any
) -> None:
    """A missing / ``complete:false`` / missing-artifact backup makes ``restore()`` RAISE BEFORE any
    store is touched. Seed all three stores, attempt three bad restores, then assert every store is
    byte-identical to the seed (validate-before-wipe — A's a5bb7d5 #4)."""
    landing = _landing(minio)
    engine, sessions = _db(postgres_dsn)
    ensure_constraints(clean_graph)

    write_entities(
        clean_graph,
        [
            _stamped(
                {
                    "id": "seed-node",
                    "schema": "Company",
                    "properties": {"name": ["KeepMe Ltd"]},
                    "datasets": ["t"],
                },
                Provenance("seed:src", "2026-06-26T00:00:00Z", "A", "s3://seed/keep.json"),
            )
        ],
    )
    with sessions() as s:
        s.add(
            ResolverJudgement(
                id=str(uuid.uuid4()),
                left_id="keep-a",
                right_id="keep-b",
                judgement="negative",
                source="signoff",
            )
        )
        s.commit()
    landing.put("seed/keep.json", b"keep-me")

    base_nodes = _node_ids(clean_graph)
    with sessions() as s:
        base_counts = _pg_counts(s)
    base_keys = landing.list_keys("")

    from worldmonitor.backup import restore

    no_manifest = tmp_path / "no-manifest"
    no_manifest.mkdir()

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "manifest.json").write_text(json.dumps({"complete": False}))

    missing_artifact = tmp_path / "missing-artifact"
    missing_artifact.mkdir()
    (missing_artifact / "manifest.json").write_text(json.dumps({"complete": True}))  # but no dumps

    for bad_src in (no_manifest, incomplete, missing_artifact):
        # blind Exception is deliberate: the not-yet-built API may raise any type — the OUTCOME the
        # gate pins is "it raises before touching a store", not the exception class.
        with sessions() as s, pytest.raises(Exception):  # noqa: B017
            restore(neo4j=clean_graph, session=s, landing=landing, src=bad_src)

    # NOTHING was touched — every store is byte-identical to the seed.
    assert _node_ids(clean_graph) == base_nodes, "a bad restore must not wipe the graph"
    assert "seed-node" in _node_ids(clean_graph)
    with sessions() as s:
        assert _pg_counts(s) == base_counts, "a bad restore must not truncate Postgres"
        assert ("keep-a", "keep-b", "negative") in _judgements(s)
    assert landing.list_keys("") == base_keys, "a bad restore must not empty the landing bucket"
    assert landing.get("seed/keep.json") == b"keep-me"

    engine.dispose()


def test_restore_aborts_on_corrupt_backup_artifacts_without_touching_any_store(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient, tmp_path: Any
) -> None:
    """A backup that passes the manifest check but is CORRUPT in its CONTENTS — a landing object
    byte-file the index lists is missing (the canonical bit-rot / partial-copy DR input), or a Neo4j
    node carries a null id — must make ``restore()`` RAISE BEFORE any store is wiped. Pins
    validate-before-touch over artifact CONTENTS, not just presence: Neo4j + MinIO imports are
    non-transactional, so a wipe-then-discover-corrupt restore would destroy the live stores."""
    landing = _landing(minio)
    engine, sessions = _db(postgres_dsn)
    ensure_constraints(clean_graph)
    from worldmonitor.backup import backup, restore

    write_entities(
        clean_graph,
        [
            _stamped(
                {
                    "id": "seed-node",
                    "schema": "Company",
                    "properties": {"name": ["KeepMe Ltd"]},
                    "datasets": ["t"],
                },
                Provenance("seed:src", "2026-06-26T00:00:00Z", "A", "s3://seed/keep.json"),
            )
        ],
    )
    with sessions() as s:
        s.add(
            ResolverJudgement(
                id=str(uuid.uuid4()),
                left_id="keep-a",
                right_id="keep-b",
                judgement="negative",
                source="signoff",
            )
        )
        s.commit()
    landing.put("seed/keep.json", b"keep-me")

    base_nodes = _node_ids(clean_graph)
    with sessions() as s:
        base_counts = _pg_counts(s)
    base_keys = landing.list_keys("")

    def _assert_untouched() -> None:
        assert _node_ids(clean_graph) == base_nodes, "a corrupt restore must not wipe the graph"
        with sessions() as s:
            assert _pg_counts(s) == base_counts, "a corrupt restore must not truncate Postgres"
            assert ("keep-a", "keep-b", "negative") in _judgements(s)
        assert landing.list_keys("") == base_keys, "a corrupt restore must not empty the bucket"
        assert landing.get("seed/keep.json") == b"keep-me"

    # corruption A — a landing object byte-file the index lists is missing (bit-rot / partial copy).
    dest_a = tmp_path / "corrupt-landing"
    with sessions() as s:
        backup(neo4j=clean_graph, session=s, landing=landing, dest=dest_a)
    obj_files = [p for p in (dest_a / "landing").rglob("*") if p.is_file()]
    assert obj_files, "the backup must have mirrored at least one landing object"
    obj_files[0].unlink()  # the index still lists it; the byte-file is gone
    with sessions() as s, pytest.raises(Exception):  # noqa: B017
        restore(neo4j=clean_graph, session=s, landing=landing, src=dest_a)
    _assert_untouched()

    # corruption B — a Neo4j node with a null id (would raise mid-MERGE, AFTER the DETACH DELETE).
    dest_b = tmp_path / "corrupt-neo4j"
    with sessions() as s:
        backup(neo4j=clean_graph, session=s, landing=landing, dest=dest_b)
    neo_path = dest_b / "neo4j.json"
    neo = json.loads(neo_path.read_text())
    assert neo["nodes"], "the backup must have captured at least one node"
    neo["nodes"][0]["props"]["id"] = ""  # corrupt the id
    neo_path.write_text(json.dumps(neo))
    with sessions() as s, pytest.raises(Exception):  # noqa: B017
        restore(neo4j=clean_graph, session=s, landing=landing, src=dest_b)
    _assert_untouched()

    engine.dispose()


# ======================================================================== R3 — halt-loud backup


def test_backup_raises_on_store_export_failure_and_writes_no_complete_manifest(
    postgres_dsn: str, clean_graph: Neo4jClient, tmp_path: Any
) -> None:
    """A store-export failure propagates (no silent partial backup) and NO ``complete:true``
    manifest is written, so an operator can never ``down -v`` believing a broken backup succeeded
    (A's a5bb7d5 #2)."""
    engine, sessions = _db(postgres_dsn)
    ensure_constraints(clean_graph)
    dest = tmp_path / "partial"

    from worldmonitor.backup import backup

    # blind Exception is deliberate (see R2): the pinned OUTCOME is "a store-export failure
    # propagates", not the exception class.
    with sessions() as s, pytest.raises(Exception):  # noqa: B017
        backup(neo4j=clean_graph, session=s, landing=_ExplodingLanding(), dest=dest)

    manifest_path = dest / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        assert manifest.get("complete") is not True, (
            "a partial/failed backup must NEVER be marked complete:true"
        )

    engine.dispose()


# ========================================================================== R4 — empty-store DR


def test_backup_restore_of_empty_stores_roundtrips_with_explicit_empty_collections(
    minio: tuple[str, str, str], postgres_dsn: str, clean_graph: Neo4jClient, tmp_path: Any
) -> None:
    """Empty stores back up to EXPLICIT empty collections (never a missing artifact) and restore to
    empty stores — the a5bb7d5 empty-bucket bug guard: zero objects / zero nodes / zero edges are an
    explicit ``[]``, never a missing file that restore would read as corruption."""
    landing = _landing(minio)  # bucket created, zero objects
    engine, sessions = _db(postgres_dsn)
    ensure_constraints(clean_graph)
    dest = tmp_path / "empty-backup"

    assert _node_ids(clean_graph) == [], "graph must be empty for the empty-store case"
    assert landing.list_keys("") == [], "landing must be empty for the empty-store case"

    from worldmonitor.backup import backup, restore

    with sessions() as s:
        backup(neo4j=clean_graph, session=s, landing=landing, dest=dest)

    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest.get("complete") is True

    # zero objects -> an EXPLICIT empty list, not a missing artifact (the a5bb7d5 #3 bug).
    assert (dest / "landing_index.json").exists(), "an empty bucket must still write landing_index"
    assert json.loads((dest / "landing_index.json").read_text()) == [], (
        "zero landing objects must serialize as an explicit empty list"
    )
    # zero nodes / zero edges -> explicit empty collections, present keys.
    assert (dest / "neo4j.json").exists(), "the Neo4j export artifact must exist even when empty"
    neo4j_artifact = json.loads((dest / "neo4j.json").read_text())
    assert neo4j_artifact.get("nodes") == [], "zero nodes must serialize as an explicit empty list"
    assert neo4j_artifact.get("edges") == [], "zero edges must serialize as an explicit empty list"

    # restore of an empty backup succeeds and leaves empty stores (no missing-vs-empty confusion).
    with sessions() as s:
        restore(neo4j=clean_graph, session=s, landing=landing, src=dest)
        s.commit()
    assert _node_ids(clean_graph) == [], "restore of an empty backup leaves an empty graph"
    assert _edge_count(clean_graph) == 0
    assert landing.list_keys("") == [], "restore of an empty backup leaves an empty bucket"
    with sessions() as s:
        assert all(c == 0 for c in _pg_counts(s).values()), "restore leaves empty tables"

    engine.dispose()
