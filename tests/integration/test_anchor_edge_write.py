"""Gate CID-fix (ADR 0048) — THE edge-survival oracle whose ABSENCE hid a confirmed blocker.

THE BUG (reproduced on installed ``followthemoney==4.9.2``): ``pick_anchor`` mints the durable
canonical id as ``f"{tier.kind}:{value}"`` -> ``qid:Q42``. The colon is NOT in FtM's
entity-reference charset (``[A-Za-z0-9.-]``), so ``registry.entity.clean('qid:Q42') is None``. That
durable id is not only the graph node id -- referent rewriting (``referents.rewrite_referents`` ->
``pipeline.py``) REWRITES it into edge ENDPOINTS. FtM cleans an entity-typed property value through
``registry.entity``; an endpoint of ``qid:Q42`` cleans to ``None`` -> the value is dropped -> the
owning Ownership/Directorship edge is **silently dropped**. The re-keyed NODE still exists, but its
edge vanishes. Because an anchor-preferred id is minted for EVERY entity carrying a
``wikidataId``/``leiCode``/``registrationNumber``/``taxNumber`` (a large fraction of real entities),
this corrupts the product -- the resolved entity graph -- for those entities.

WHY THIS IS RED ON ``master`` (the RIGHT red): Gate B's green suite asserted the durable-id STRING
and the node, but never built an anchored merge, attached an edge, and read the edge back. This test
is that missing oracle. It builds a QID-anchored 2-member MERGE, attaches an Ownership edge whose
endpoint names a merged-away member, runs the REAL pipeline, and asserts the edge SURVIVES:

  * the merged company node IS written under the anchor-derived durable id (proves the merge wrote
    a node -- the failure is isolated to the EDGE, never a vacuous "nothing happened"),
  * a merged-away member id resolves (via the ledger) to that same durable id (proves the edge
    endpoint was rewritten onto the surviving canonical), and then
  * the Ownership edge from that durable node is NON-EMPTY (``get_neighbors`` finds the owner AND a
    direct Cypher edge count is >= 1) -- pre-fix the endpoint ``qid:Q42`` cleans to ``None`` so the
    edge is dropped and these assertions FAIL; once the id is FtM-clean (``wm-anchor-qid-Q42``,
    ADR 0048 §3) the edge persists and they pass.

The durable id is read straight back from ``canonical.pick_anchor`` so this oracle pins the
EDGE-SURVIVAL invariant independently of the exact id serialization -- it cannot be passed by
weakening the format. The format itself (and FtM-cleanliness) is additionally pinned at the end.

Real ER pipeline + writer against ephemeral Neo4j + Postgres (testcontainers); marked
``integration`` so it runs on the dedicated CI job (no Docker in the default run).
"""

from __future__ import annotations

import uuid

import pytest
from followthemoney import registry
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, ResolverJudgement
from worldmonitor.graph.constraints import ensure_constraints
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.queries import get_neighbors
from worldmonitor.graph.writer import resolve_node_id
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution import canonical
from worldmonitor.resolution.pipeline import resolve_pending

pytestmark = pytest.mark.integration

Q_ACME = "Q42"


def _company(entity_id: str) -> dict[str, object]:
    """A Company carrying its QID anchor as the FtM ``wikidataId`` identifier property.

    Two such records (same QID) earn a QID-anchored durable id when merged, so ``pick_anchor``
    governs the id and the merged node is re-keyed under the anchor-derived id (ADR 0044/0048).
    """
    return {
        "id": entity_id,
        "schema": "Company",
        "properties": {
            "name": ["Acme Corporation Ltd"],
            "jurisdiction": ["us"],
            "wikidataId": [Q_ACME],
        },
        "datasets": ["t"],
    }


def _ownership(edge_id: str, owner: str, asset: str) -> dict[str, object]:
    """An Ownership EDGE entity (owner OWNS asset). ``asset`` names a member that merges away, so
    referent rewriting redirects this endpoint onto the surviving anchor-derived durable id."""
    return {
        "id": edge_id,
        "schema": "Ownership",
        "properties": {"owner": [owner], "asset": [asset]},
        "datasets": ["t"],
    }


def _person(entity_id: str) -> dict[str, object]:
    return {
        "id": entity_id,
        "schema": "Person",
        "properties": {"name": ["Ivan Owner"], "nationality": ["ru"]},
        "datasets": ["t"],
    }


def _queue_item(data: dict[str, object], *, source: str) -> ErQueueItem:
    """An ER-queue row whose mapped entity carries provenance (so the edge gets prov_*)."""
    provenance = Provenance(
        source_id="opensanctions:test",
        retrieved_at="2026-06-25T00:00:00Z",
        reliability="A",
        source_record=f"s3://landing/{source}.json",
    )
    entity = stamp(make_entity(data), provenance)
    return ErQueueItem(
        id=str(uuid.uuid4()),
        connector_id="opensanctions",
        entity_id=entity.id,
        raw_entity=entity.to_dict(),
        source_record=provenance.source_record,
        status="pending",
    )


def _judgement(left: str, right: str) -> ResolverJudgement:
    """A durable POSITIVE sign-off so the two QID members merge deterministically (the channel a
    human sign-off uses; mirrors ``test_stable_id_graph`` / ``test_b6_resolve_incompat``)."""
    low, high = sorted((left, right))
    return ResolverJudgement(
        id=str(uuid.uuid4()), left_id=low, right_id=high, judgement="positive", source="signoff"
    )


def test_anchored_merge_edge_survives_durable_rekey(
    clean_graph: Neo4jClient, postgres_dsn: str
) -> None:
    """An Ownership edge into a QID-ANCHORED merge survives the durable re-key (G1: edge written).

    ``ivan`` owns ``acme-a``; ``acme-a`` and ``acme-b`` (both QID ``Q42``) merge to one
    anchor-derived durable node. The ownership endpoint ``acme-a`` is rewritten onto that durable
    id. Pre-fix the durable id is ``qid:Q42`` -> the rewritten endpoint cleans to ``None`` -> the
    edge is SILENTLY DROPPED (RED). Post-fix it is the FtM-clean ``wm-anchor-qid-Q42`` -> the edge
    persists (GREEN).
    """
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions: sessionmaker[Session] = session_factory(engine)
    ensure_constraints(clean_graph)

    rows = [
        (_person("ivan"), "ivan"),
        (_company("acme-a"), "acme-a"),
        (_company("acme-b"), "acme-b"),
        (_ownership("own-a", "ivan", "acme-a"), "own-a"),
    ]
    with sessions() as session:
        for data, source in rows:
            session.add(_queue_item(data, source=source))
        session.add(_judgement("acme-a", "acme-b"))
        session.commit()
    with sessions() as session:
        resolve_pending(session=session, neo4j=clean_graph)

    # The durable id the pipeline keyed the merge under -- read back from the SAME function the
    # pipeline used, so the edge-survival assertions hold regardless of the exact serialization.
    durable = canonical.pick_anchor(
        [make_entity(_company("acme-a")), make_entity(_company("acme-b"))]
    )
    assert durable is not None, "a single shared QID must yield an anchor-derived durable id"

    # (1) NON-VACUOUS: the merge actually wrote a node under the durable id (so any edge failure
    #     below is the silent-drop bug, not "nothing was written"). True pre- AND post-fix.
    node_count = clean_graph.execute_read(
        "MATCH (n:Entity {id: $durable}) RETURN count(n) AS c", durable=durable
    )[0]["c"]
    assert node_count == 1, "the anchored merge must be materialized as exactly one durable node"

    # (2) NON-VACUOUS: a merged-away member id resolves -- via the ledger -- to that durable id,
    #     i.e. referent rewriting pointed the ownership endpoint at the surviving canonical.
    with sessions() as session:
        assert resolve_node_id(session, "acme-a") == durable
        assert resolve_node_id(session, "acme-b") == durable

    # (3) THE ORACLE: the Ownership edge into the anchored merge is NON-EMPTY. Pre-fix the
    #     rewritten endpoint (``qid:Q42``) cleans to ``None`` -> the edge is dropped -> count 0.
    edge_count = clean_graph.execute_read(
        "MATCH (a:Entity {id: $durable})-[r]-(b:Entity) RETURN count(r) AS c", durable=durable
    )[0]["c"]
    assert edge_count >= 1, "the edge into the anchored merge must SURVIVE the durable re-key"

    owns = clean_graph.execute_read(
        "MATCH (o:Entity {id: 'ivan'})-[r:OWNS]->(c:Entity {id: $durable}) RETURN count(r) AS c",
        durable=durable,
    )[0]["c"]
    assert owns == 1, "ivan must OWN the durable anchored node (the rewritten edge materialized)"

    # (3b) The headline read the API exposes -- neighbour traversal -- finds the owner.
    neighbours = {n["id"] for n in get_neighbors(clean_graph, entity_id=durable)}
    assert neighbours == {"ivan"}, "get_neighbors of the anchored merge must reach the owner"

    # (4) ROOT CAUSE / format pin: the durable id is an FtM entity fixed point and the ADR-0048
    #     ``wm-anchor-qid-<QID>`` shape. Pre-fix ``clean('qid:Q42') is None`` -> this is RED, which
    #     is EXACTLY why the endpoint dropped and the edge above vanished.
    assert registry.entity.clean(durable) == durable, (
        "the durable id MUST be an FtM entity reference (else it drops as an edge endpoint)"
    )
    assert durable == f"wm-anchor-qid-{Q_ACME}"

    engine.dispose()
