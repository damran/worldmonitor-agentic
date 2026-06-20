"""Unit tests for FtM schema validation — valid passes, invalid raises loudly."""

from __future__ import annotations

import pytest

from worldmonitor.ontology.validation import InvalidEntity, validate_or_raise


def test_valid_entity_passes() -> None:
    entity = validate_or_raise(
        {
            "id": "ofac-123",
            "schema": "Person",
            "properties": {"name": ["Jane Doe"], "nationality": ["us"]},
            "datasets": ["test"],
        }
    )
    assert entity.id == "ofac-123"
    assert entity.schema.name == "Person"
    assert "Jane Doe" in entity.get("name")


def test_unknown_schema_raises() -> None:
    with pytest.raises(InvalidEntity):
        validate_or_raise({"id": "x", "schema": "NotASchema", "properties": {}})


def test_missing_id_raises() -> None:
    with pytest.raises(InvalidEntity, match="id"):
        validate_or_raise({"schema": "Person", "properties": {"name": ["x"]}})


def test_empty_id_raises() -> None:
    with pytest.raises(InvalidEntity, match="id"):
        validate_or_raise({"id": "", "schema": "Person", "properties": {}})


def test_missing_schema_raises() -> None:
    with pytest.raises(InvalidEntity, match="schema"):
        validate_or_raise({"id": "x", "properties": {}})
