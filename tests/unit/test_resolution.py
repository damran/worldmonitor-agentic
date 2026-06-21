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
