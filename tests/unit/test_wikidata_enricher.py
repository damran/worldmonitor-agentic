"""Unit tests for the Wikidata enricher (no network)."""

from __future__ import annotations

from worldmonitor.ontology.anchors import get_anchors
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.plugins.enrichers.wikidata import WikidataEnricher, _normalize_qid


def _org(props: dict[str, list[str]]):
    return make_entity(
        {"id": "x", "schema": "Organization", "properties": props, "datasets": ["t"]}
    )


def test_extracts_existing_wikidata_id() -> None:
    entity = _org({"name": ["Acme"], "wikidataId": ["Q42"]})
    WikidataEnricher(lookup=False).enrich(entity)
    assert get_anchors(entity)["wikidata_id"] == "Q42"


def test_no_lookup_no_anchor() -> None:
    entity = _org({"name": ["Some Org With No Wikidata Property"]})
    WikidataEnricher(lookup=False).enrich(entity)
    assert "wikidata_id" not in get_anchors(entity)


def test_normalize_qid() -> None:
    assert _normalize_qid("http://www.wikidata.org/entity/Q1065") == "Q1065"
    assert _normalize_qid("Q42") == "Q42"
    assert _normalize_qid("not-a-qid") is None
