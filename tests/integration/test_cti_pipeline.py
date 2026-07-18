"""CTI lane end-to-end over REAL stores (S-2/S-3 closure): the gap the gate checkers named.

The S-2 checker recorded that no test writes a ``wm:Indicator`` through the REAL pipeline into a
REAL Neo4j (the writer estate merely stayed green). If ftmg's schema handling balked at the
injected wm schema, the operator's first boot would dead-letter every Feodo record — this test
would catch it before that boot. Hermetic upstreams (injected ``httpx.MockTransport`` — no live
network), real Postgres + MinIO + Neo4j:

CTI-1  feodo ingest → resolve → a provenance-stamped ``Indicator`` NODE exists in Neo4j (the
       fail-closed writer ACCEPTED the injected schema), with the shared ``ioc-<sha1>`` id and
       no topics/country.
CTI-2  mitre_attack ingest → resolve → the intrusion set lands as an Organization carrying the
       ``mitre_gid`` node property (guard evidence + the ``entity_mitre_gid`` uniqueness
       constraint are live). A SINGLETON keeps its connector id (``mitre-G0034``) BY DESIGN —
       the anchor-preferred ``wm-anchor-gid-*`` durable id is assigned at the MERGE boundary
       (``resolve_durable_id``/``rekey_cluster``) when a second source ever merges in, which
       the S-3 gate's checker proved at that boundary on a real ledger.
CTI-3  Re-running ingest + resolve is idempotent: node counts stable (deterministic ids + the
       dedup/converge machinery hold over the real stores).
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.plugins.connectors.feodo import FeodoConnector
from worldmonitor.plugins.connectors.mitre_attack import MitreAttackConnector
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.runner.ingest import run_ingest
from worldmonitor.storage.landing import LandingStore

pytestmark = pytest.mark.integration

_FEODO_ENTRIES = [
    {
        "ip_address": "203.0.113.7",
        "port": 443,
        "status": "online",
        "hostname": None,
        "as_number": 64500,
        "as_name": "EXAMPLE-AS",
        "country": "US",
        "first_seen": "2026-01-02 03:04:05",
        "last_online": "2026-07-01",
        "malware": "Pikabot",
    },
    {
        "ip_address": "203.0.113.9",
        "port": 8080,
        "status": "offline",
        "hostname": None,
        "as_number": 64501,
        "as_name": "EXAMPLE-AS-2",
        "country": "DE",
        "first_seen": "2026-02-03 04:05:06",
        "last_online": "2026-06-30",
    },
]

_ATTACK_BUNDLE = {
    "type": "bundle",
    "id": "bundle--test",
    "objects": [
        {
            "type": "intrusion-set",
            "id": "intrusion-set--x1",
            "name": "Sandworm Team",
            "aliases": ["Sandworm Team", "IRIDIUM", "Voodoo Bear"],
            "external_references": [
                {"source_name": "mitre-attack", "external_id": "G0034"},
            ],
        },
    ],
}


def _transport(payload: object) -> httpx.MockTransport:
    body = json.dumps(payload).encode("utf-8")
    return httpx.MockTransport(lambda _req: httpx.Response(200, content=body))


def _stores(minio: tuple[str, str, str], postgres_dsn: str):
    endpoint, access_key, secret_key = minio
    landing = LandingStore.connect(
        endpoint=endpoint, access_key=access_key, secret_key=secret_key, bucket="landing"
    )
    engine = make_engine(postgres_dsn)
    create_all(engine)
    return landing, engine, session_factory(engine)


def _ingest_and_resolve(connector, config, landing, sessions, neo4j) -> None:
    with sessions() as session:
        run_ingest(connector, config, landing=landing, session=session)
    with sessions() as session:
        resolve_pending(session=session, neo4j=neo4j, guard_mode="block")


def test_cti_pipeline_end_to_end(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scope the DNS bypass to the SSRF guard ONLY (a global getaddrinfo stub would hijack the
    # psycopg/neo4j testcontainer connections): the transports are hermetic mocks, and the
    # guard's own resolution logic is unit-covered in tests/unit/test_ssrf_guard.py.
    import worldmonitor.net.ssrf as ssrf

    monkeypatch.setattr(ssrf, "assert_public_host", lambda *_a, **_k: None)
    ensure_constraints(clean_graph)
    landing, engine, sessions = _stores(minio, postgres_dsn)

    # --- CTI-1: feodo → Indicator node in the real graph -------------------------------------
    feodo = FeodoConnector(transport=_transport(_FEODO_ENTRIES))
    _ingest_and_resolve(feodo, {}, landing, sessions, clean_graph)

    ioc_id = "ioc-" + hashlib.sha1(b"203.0.113.7:443").hexdigest()
    rows = clean_graph.execute_read(
        "MATCH (n:Indicator) RETURN n.id AS id, head(n.indicatorValue) AS value, "
        "head(n.malwareFamily) AS family, n.prov_source_id AS src, "
        "head(n.topics) AS topics, head(n.country) AS country ORDER BY id"
    )
    assert len(rows) == 2, f"expected 2 Indicator nodes, got {rows}"
    pika = next(r for r in rows if r["id"] == ioc_id)
    assert pika["value"] == "203.0.113.7:443"
    assert pika["family"] == "Pikabot"
    assert pika["src"], "provenance must be stamped on the node (fail-closed writer)"
    assert pika["topics"] is None and pika["country"] is None

    # --- CTI-2: mitre_attack → gid-anchored Organization --------------------------------------
    attack = MitreAttackConnector(transport=_transport(_ATTACK_BUNDLE))
    _ingest_and_resolve(attack, {}, landing, sessions, clean_graph)

    orgs = clean_graph.execute_read(
        "MATCH (n:Organization) WHERE n.mitre_gid IS NOT NULL "
        "RETURN n.id AS id, n.mitre_gid AS gid, head(n.name) AS name"
    )
    assert len(orgs) == 1, f"expected the intrusion set as one gid-anchored org, got {orgs}"
    assert orgs[0]["gid"] == "G0034"
    # A never-merged singleton keeps its connector id; the wm-anchor-gid-* durable id belongs
    # to the MERGE boundary (see module docstring). What matters here: the gid rode onto the
    # node as guard evidence under the live uniqueness constraint.
    assert orgs[0]["id"] == "mitre-G0034"
    assert orgs[0]["name"] == "Sandworm Team"

    # --- CTI-3: idempotent re-ingest + re-resolve ----------------------------------------------
    before = clean_graph.execute_read("MATCH (n:Entity) RETURN count(n) AS n")[0]["n"]
    _ingest_and_resolve(
        FeodoConnector(transport=_transport(_FEODO_ENTRIES)), {}, landing, sessions, clean_graph
    )
    _ingest_and_resolve(
        MitreAttackConnector(transport=_transport(_ATTACK_BUNDLE)),
        {},
        landing,
        sessions,
        clean_graph,
    )
    after = clean_graph.execute_read("MATCH (n:Entity) RETURN count(n) AS n")[0]["n"]
    assert after == before, f"re-ingest must be idempotent (before={before}, after={after})"

    # Nothing dead-lettered along the way: the injected wm schema was accepted end-to-end.
    with sessions() as session:
        pending = session.execute(
            select(func.count())
            .select_from(ErQueueItem)
            .where(ErQueueItem.status.in_(("pending", "error")))
        ).scalar_one()
    assert pending == 0, "no candidate may be left pending/errored"

    engine.dispose()
