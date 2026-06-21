"""Unit tests for canonical reference anchors."""

from __future__ import annotations

import pytest

from worldmonitor.ontology.anchors import get_anchors, set_anchor
from worldmonitor.ontology.ftm import make_entity


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
