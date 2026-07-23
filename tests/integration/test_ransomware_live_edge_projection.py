"""Gate S-4 slice 3 — the FIRST edge-emitting connector's tripwire (spec §9 Slice 3 / §10 / §12
item 2): drive ONE hermetic ``recentvictims`` record through the REAL
``run_ingest`` -> ``resolve_pending`` -> ``graph.writer.write_entities`` path (real Postgres +
MinIO + Neo4j via testcontainers, an injected ``httpx.MockTransport`` -- no live network) and prove
the ``UnknownLink`` claim edge lands in Neo4j as a real relationship carrying flat ``prov_*``
provenance, between the group Organization and victim Company NODES the same batch also writes.

Mirrors ``tests/integration/test_cti_pipeline.py`` (PR #212, the S-2/S-3 pipeline-proof pattern)
and ``tests/integration/test_edge_provenance.py`` / ``tests/test_abstract_edge.py`` (the ``prov_*``
edge-property convention + the ADR 0046 ``UnknownLink``-materializes precedent this test exercises
end-to-end for the first time via the FULL resolve pipeline, not a direct ``write_entities`` call).

**THIS IS THE TRIPWIRE.** If it fails because the writer/ftmg projection cannot emit the edge (or
drops/mis-shapes it somewhere in ``score_pairs`` / ``cluster_and_merge`` / ``needs_review`` before
ever reaching the writer), that is a REAL finding for the gate report -- STOP-AND-ESCALATE, do not
soften the assertion to make it pass (spec §12 item 2).
"""

from __future__ import annotations

import json

import httpx
import pytest

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.plugins.connectors.ransomware_live.connector import (
    RansomwareLiveConnector,
    _group_id,
    _victim_id,
)
from worldmonitor.resolution.pipeline import resolve_pending
from worldmonitor.runner.ingest import run_ingest
from worldmonitor.settings import Settings
from worldmonitor.storage.landing import LandingStore

pytestmark = pytest.mark.integration

# One hermetic recentvictims record shaped exactly per spec §3.2 -- every mapped field present so
# the resulting Company/Organization/UnknownLink triple exercises the full field map, not just the
# identity fields.
_PERMALINK = "https://api.ransomware.live/recentvictims/s4-edge-projection-smoke"
_GROUP_RAW = "BrainCipher"
_VICTIM_RECORD = {
    "victim": "S4 Edge Projection Smoke Co",
    "domain": "s4-smoke.example",
    "country": "US",
    "activity": "Manufacturing",
    "group": _GROUP_RAW,
    "attackdate": "2026-07-01T00:00:00+00:00",
    "claim_url": "http://exampleonionaddress.onion/leak/s4-smoke",
    "description": "Stolen data allegedly posted to the group's leak site.",
    "url": _PERMALINK,
}

_GROUP_ID = _group_id(_GROUP_RAW)
_VICTIM_ID = _victim_id(_PERMALINK)


def _transport() -> httpx.MockTransport:
    body = json.dumps([_VICTIM_RECORD]).encode("utf-8")
    return httpx.MockTransport(lambda _req: httpx.Response(200, content=body))


def _stores(minio: tuple[str, str, str], postgres_dsn: str):
    endpoint, access_key, secret_key = minio
    landing = LandingStore.connect(
        endpoint=endpoint, access_key=access_key, secret_key=secret_key, bucket="landing"
    )
    engine = make_engine(postgres_dsn)
    create_all(engine)
    return landing, engine, session_factory(engine)


def test_ransomware_live_claim_edge_projects_to_neo4j(
    minio: tuple[str, str, str],
    postgres_dsn: str,
    clean_graph: Neo4jClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One hermetic victim record -> run_ingest (reliability='E') -> resolve_pending (guard
    'block') -> the UnknownLink claim edge exists in Neo4j between the group Organization and
    victim Company nodes, carrying flat prov_* with reliability='E' -- the disclaimed-allegation
    invariant (spec §1: "the allegation lives on the edge ... stamped reliability 'E'")."""
    import worldmonitor.net.ssrf as ssrf
    import worldmonitor.resolution.pipeline as pipeline_mod

    # Scope the DNS bypass to the SSRF guard ONLY (mirrors test_cti_pipeline.py) -- the transport
    # is a hermetic httpx.MockTransport; the guard's own resolution logic is unit-covered
    # elsewhere (tests/unit/test_ssrf_guard.py).
    monkeypatch.setattr(ssrf, "assert_public_host", lambda *_a, **_k: None)
    # Pin the enforcement profile explicitly (the local-.env-off footgun) so the merge guard's
    # "block" mode is the one actually exercised here, matching CI regardless of a dev .env.
    monkeypatch.setattr(
        pipeline_mod, "get_settings", lambda: Settings(enforcement_profile="strict")
    )

    ensure_constraints(clean_graph)
    landing, engine, sessions = _stores(minio, postgres_dsn)
    try:
        connector = RansomwareLiveConnector(transport=_transport())
        config = {"dataset": "recentvictims"}

        with sessions() as session:
            stats = run_ingest(connector, config, landing=landing, session=session, reliability="E")
        assert stats.queued == 3, (
            "one victim record must map to exactly THREE ER-queue candidates (victim Company, "
            f"thin group Organization, UnknownLink claim edge) -- got queued={stats.queued} "
            f"({stats!r})"
        )

        with sessions() as session:
            resolve_stats = resolve_pending(session=session, neo4j=clean_graph, guard_mode="block")
        assert resolve_stats.promoted == 3, (
            f"all three candidates must promote (no park expected -- singletons are never "
            f"flagged by the sensitivity guard, spec §9/needs_review): {resolve_stats!r}"
        )

        # --- Node-level sanity: both endpoints landed as their own correct-schema nodes -------
        victim_rows = clean_graph.execute_read(
            "MATCH (n:Company {id: $id}) RETURN n.id AS id, head(n.name) AS name, "
            "n.prov_source_id AS src, n.prov_reliability AS reliability, "
            "labels(n) AS labels",
            id=_VICTIM_ID,
        )
        assert len(victim_rows) == 1, (
            f"expected exactly one victim Company node {_VICTIM_ID!r}, got {victim_rows!r}"
        )
        victim = victim_rows[0]
        assert victim["name"] == "S4 Edge Projection Smoke Co"
        assert "CrimeCyber" not in victim["labels"], (
            "the victim node must carry no allegation topic label (S-4 §1) -- got "
            f"labels={victim['labels']!r}"
        )
        assert victim["reliability"] == "E"

        group_rows = clean_graph.execute_read(
            "MATCH (n:Organization {id: $id}) RETURN n.id AS id, head(n.name) AS name, "
            "n.prov_reliability AS reliability, labels(n) AS labels",
            id=_GROUP_ID,
        )
        assert len(group_rows) == 1, (
            f"expected exactly one group Organization node {_GROUP_ID!r}, got {group_rows!r}"
        )
        assert group_rows[0]["name"] == _GROUP_RAW
        assert group_rows[0]["reliability"] == "E"
        # ftmg projects a risk topic as a NODE LABEL (PascalCase, "crime.cyber" -> "CrimeCyber"),
        # never a "topics" property -- see guard/sensitivity.py::_risk_labels.
        assert "CrimeCyber" in group_rows[0]["labels"], (
            "the group Organization must carry the CrimeCyber risk label (deliberately "
            f"sensitive -- group merges park for human review), got labels="
            f"{group_rows[0]['labels']!r}"
        )

        # --- THE TRIPWIRE: the UnknownLink claim edge exists as a real relationship ------------
        edge_rows = clean_graph.execute_read(
            "MATCH (g:Organization {id: $group_id})-[r]->(v:Company {id: $victim_id}) "
            "RETURN type(r) AS rel_type, r.prov_source_id AS source_id, "
            "r.prov_retrieved_at AS retrieved_at, r.prov_reliability AS reliability, "
            "r.prov_source_record AS source_record, head(r.role) AS role",
            group_id=_GROUP_ID,
            victim_id=_VICTIM_ID,
        )
        assert len(edge_rows) == 1, (
            "STOP-AND-ESCALATE (spec §10/§12 item 2): the ransomware_live UnknownLink claim "
            "edge did NOT materialize as a (group)-[r]->(victim) relationship in Neo4j -- the "
            "writer/ftmg projection cannot yet emit this connector's first edge-emitting map() "
            f"output through the FULL run_ingest->resolve_pending pipeline. Got: {edge_rows!r}"
        )
        edge = edge_rows[0]
        assert edge["source_id"] == "ransomware_live:recentvictims", (
            f"edge provenance source_id must trace to the recentvictims dataset, got "
            f"{edge['source_id']!r}"
        )
        assert edge["reliability"] == "E", (
            "the disclaimed allegation must carry Admiralty reliability 'E' on the EDGE "
            f"(spec §1/§5) -- got {edge['reliability']!r}"
        )
        assert isinstance(edge["source_record"], str) and edge["source_record"].startswith(
            "s3://"
        ), f"edge must carry a landing-zone pointer, got {edge['source_record']!r}"
        assert edge["role"] == "ransomware victim (claimed by group)"

        # Nothing dead-lettered / left pending along the way.
        from sqlalchemy import func, select

        with sessions() as session:
            pending = session.execute(
                select(func.count())
                .select_from(ErQueueItem)
                .where(ErQueueItem.status.in_(("pending", "error")))
            ).scalar_one()
        assert pending == 0, "no candidate may be left pending/errored"
    finally:
        engine.dispose()
