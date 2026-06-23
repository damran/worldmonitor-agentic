"""The mandatory ER tests: clear matches merge, clear non-matches don't, and a
high-sensitivity merge is held for review (the catastrophic-merge negative test)."""

from __future__ import annotations

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.merge import cluster_and_merge
from worldmonitor.resolution.review import needs_review
from worldmonitor.resolution.splink_model import score_pairs


def _company(entity_id: str, name: str, country: str) -> FtmEntity:
    return make_entity(
        {
            "id": entity_id,
            "schema": "Company",
            "properties": {"name": [name], "jurisdiction": [country]},
            "datasets": ["t"],
        }
    )


def _person(
    entity_id: str, name: str, country: str, dob: str, topics: list[str] | None = None
) -> FtmEntity:
    props: dict[str, list[str]] = {"name": [name], "nationality": [country], "birthDate": [dob]}
    if topics:
        props["topics"] = topics
    return make_entity(
        {"id": entity_id, "schema": "Person", "properties": props, "datasets": ["t"]}
    )


def test_clearly_matching_records_merge() -> None:
    a = _company("c1", "Acme Corporation Ltd", "us")
    b = _company("c2", "Acme Corporation Ltd", "us")
    clusters = cluster_and_merge([a, b], score_pairs([a, b]))
    merges = [c for c in clusters if c.is_merge]
    assert len(merges) == 1
    assert set(merges[0].member_ids) == {"c1", "c2"}
    assert "Acme Corporation Ltd" in merges[0].entity.get("name")


def test_clearly_different_records_do_not_merge() -> None:
    a = _company("c1", "Acme Corporation Ltd", "us")
    b = _company("c2", "Globex Incorporated", "gb")
    clusters = cluster_and_merge([a, b], score_pairs([a, b]))
    assert all(not c.is_merge for c in clusters)
    assert len(clusters) == 2


def test_non_sensitive_merge_auto_promotes() -> None:
    a = _company("c1", "Acme Corporation Ltd", "us")
    b = _company("c2", "Acme Corporation Ltd", "us")
    clusters = cluster_and_merge([a, b], score_pairs([a, b]))
    merge = next(c for c in clusters if c.is_merge)
    flagged, _ = needs_review(merge, {"c1": a, "c2": b})
    assert flagged is False


def test_sensitive_merge_goes_to_review() -> None:
    """Catastrophic-merge negative test: a merge touching a sanctioned entity is held."""
    a = _person("p1", "Vladimir Example", "ru", "1960-01-01", topics=["sanction"])
    b = _person("p2", "Vladimir Example", "ru", "1960-01-01")
    clusters = cluster_and_merge([a, b], score_pairs([a, b]))
    merges = [c for c in clusters if c.is_merge]
    assert len(merges) == 1, "the two records should cluster on identical name+country+dob"
    flagged, reason = needs_review(merges[0], {"p1": a, "p2": b})
    assert flagged is True
    assert "sensitive" in reason.lower()


def test_resolver_is_isolated_per_batch_no_cross_tenant_leak() -> None:
    """G4: one batch's merge must never influence another batch's resolution.

    cluster_and_merge resolves each batch on a private in-memory ledger. If it
    shared a persistent ledger, a source id common to two batches (e.g. two tenants
    ingesting the same record) would fuse their independent merges — one tenant's
    merge leaking into another's. Two batches sharing id "c1" must mint independent
    canonical ids, and the second must contain only its own members.
    """
    first = [
        _company("c1", "Acme Corporation Ltd", "us"),
        _company("c2", "Acme Corporation Ltd", "us"),
    ]
    first_canonical = next(
        c for c in cluster_and_merge(first, score_pairs(first)) if c.is_merge
    ).canonical_id

    second = [
        _company("c1", "Acme Corporation Ltd", "us"),
        _company("c3", "Acme Corporation Ltd", "us"),
    ]
    second_merge = next(c for c in cluster_and_merge(second, score_pairs(second)) if c.is_merge)

    # The real isolation proof is MEMBERSHIP: a shared resolver ledger would fuse c2 into the
    # second batch's cluster ({c1,c2,c3}). Canonical ids are now content-addressed (ADR 0036),
    # so the id-inequality below follows from the differing membership — it corroborates, but
    # membership is what actually catches a leak.
    assert set(second_merge.member_ids) == {"c1", "c3"}, (
        "second batch must contain only its own ids — a shared ledger would fuse in c2 (G4 leak)"
    )
    assert second_merge.canonical_id != first_canonical, (
        "distinct membership => distinct content-addressed id (corroborates isolation)"
    )
