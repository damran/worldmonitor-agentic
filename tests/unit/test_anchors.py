"""Unit tests for canonical reference anchors.

Also covers the anchor-preferred durable-id WIRING at the merge boundary (Gate B-front / ADR
0044): the A10 grep-gate — an ANCHORED merged cluster, after the pipeline's durable-id derivation
hook (``resolve_durable_id`` + ``rekey_cluster``), is keyed on the anchor-prefixed durable id and is
NOT a ``wmc-`` hash; an UNANCHORED merge keeps ``wmc-`` (the ONLY path ``wmc-`` is the durable id).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS, get_anchors, set_anchor
from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution import canonical
from worldmonitor.resolution.merge import cluster_and_merge, rekey_cluster
from worldmonitor.resolution.referents import build_referent_map
from worldmonitor.resolution.splink_model import ScoredPair

Q_ACME = "Q42"


def _entity():
    return make_entity(
        {"id": "x", "schema": "Person", "properties": {"name": ["A"]}, "datasets": ["t"]}
    )


def test_set_and_get_anchors() -> None:
    entity = _entity()
    set_anchor(entity, "wikidata_id", "Q1")
    set_anchor(entity, "geonames_id", "123")
    assert get_anchors(entity) == {"wikidata_id": "Q1", "geonames_id": "123"}
    # Anchors ride in the context and survive the serialization round-trip.
    assert get_anchors(make_entity(entity.to_dict())) == {"wikidata_id": "Q1", "geonames_id": "123"}


def test_unknown_anchor_field_raises() -> None:
    with pytest.raises(ValueError, match="anchor field"):
        set_anchor(_entity(), "bogus_id", "x")


def test_no_anchors_returns_empty() -> None:
    assert get_anchors(_entity()) == {}


# ---------------------------------------------------------------------------------------------
# Gate B-front: anchor-preferred durable-id wiring at the merge boundary (ADR 0044).
# ---------------------------------------------------------------------------------------------


def _company(entity_id: str, *, wikidata_id: str | None = None) -> FtmEntity:
    props: dict[str, list[str]] = {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]}
    if wikidata_id is not None:
        props["wikidataId"] = [wikidata_id]
    return make_entity(
        {"id": entity_id, "schema": "Company", "properties": props, "datasets": ["t"]}
    )


@pytest.fixture
def ledger_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    canonical.CanonicalIdLedger.__table__.create(engine)
    with Session(engine) as session:
        yield session


def _rekeyed(session: Session, members: list[FtmEntity], pairs: list[ScoredPair]):
    """Mirror the pipeline hook: cluster, derive the durable id, re-key the merged cluster."""
    cluster = next(c for c in cluster_and_merge(members, pairs) if c.is_merge)
    durable = canonical.resolve_durable_id(session, members, fallback_id=cluster.canonical_id)
    return rekey_cluster(cluster, durable), cluster.canonical_id


def test_anchored_merge_is_not_keyed_wmc(ledger_session: Session) -> None:
    """A10/D1: an anchored merge is keyed on the durable QID, not the ``wmc-`` fingerprint."""
    members = [_company("m1", wikidata_id=Q_ACME), _company("m2", wikidata_id=Q_ACME)]
    rekeyed, prior = _rekeyed(ledger_session, members, [ScoredPair("m1", "m2", 0.99)])
    assert prior.startswith("wmc-")  # the cluster's idempotency fingerprint
    assert rekeyed.canonical_id == f"qid:{Q_ACME}"
    assert not rekeyed.canonical_id.startswith("wmc-")
    assert rekeyed.entity.id == f"qid:{Q_ACME}"


def test_referent_map_targets_durable_id(ledger_session: Session) -> None:
    """The referent map redirects each collapsed member onto the durable id (not ``wmc-``)."""
    members = [_company("m1", wikidata_id=Q_ACME), _company("m2", wikidata_id=Q_ACME)]
    rekeyed, _ = _rekeyed(ledger_session, members, [ScoredPair("m1", "m2", 0.99)])
    assert build_referent_map([rekeyed]) == {"m1": f"qid:{Q_ACME}", "m2": f"qid:{Q_ACME}"}


def test_unanchored_merge_keeps_wmc(ledger_session: Session) -> None:
    """An unanchored merge keeps ``wmc-`` as its durable id — the ONLY path ``wmc-`` is durable."""
    members = [_company("m1"), _company("m2")]
    rekeyed, prior = _rekeyed(ledger_session, members, [ScoredPair("m1", "m2", 0.99)])
    assert prior.startswith("wmc-")
    assert rekeyed.canonical_id == prior  # unchanged: no anchor, no re-key


def test_durable_precedence_is_separate_from_canonical_id_fields() -> None:
    """The durable precedence reads FtM identifier props / ``wm_anchor_*`` context — it does NOT
    reuse ``CANONICAL_ID_FIELDS`` (wrong storage keys, no regNo/taxNo, GeoNames is a place anchor).
    Pins that the existing anchor vocabulary is unchanged by the gate."""
    assert CANONICAL_ID_FIELDS == ("wikidata_id", "geonames_id", "lei", "opencorporates_id")
    # name + country only -> no durable anchor (geonames_id/opencorporates_id are not v0 anchors).
    assert canonical.pick_anchor([_company("a")]) is None
