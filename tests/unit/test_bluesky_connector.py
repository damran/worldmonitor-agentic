"""Primary invariant tests (RED) for the Bluesky Jetstream StreamConnector (ADR 0070).

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/bluesky/`` (a ``StreamConnector`` subclass mirroring the Feed
connector's map -> FtM ``Article`` + provenance model):

* MANIFEST: ``connector_id="bluesky"``, ``kind=CONNECTOR``, ``mode=STREAM`` (so the driver keeps it
  warm via ``next_run=now``), ``capability=PASSIVE`` (refuses ACTIVE), ``status=IMPLEMENTED``.
* CONFIG SCHEMA: ``wanted_collections`` (array, default ``["app.bsky.feed.post"]``),
  ``window_seconds`` (integer ``>=1``, default 20), ``max_events`` (integer ``>=1``, default 500);
  ``validate_config`` accepts a valid config (incl. the all-default ``{}``) and rejects an
  out-of-bounds bound.
* ``collect()`` over an INJECTED fake ``event_source`` (NO live WS) yields one RawRecord per post
  event — its key carries the ``did``/``rkey`` and its cursor is ``str(time_us)`` — and resumes from
  ``config["_cursor"]``.
* ``map()``: a post-create commit -> ONE FtM ``Article`` (id ``bluesky-{did}-{rkey}``,
  ``title``/``bodyText`` = the post text, ``author`` = did, ``publishedAt`` = createdAt,
  ``sourceUrl`` = the ``at://`` URI) whose provenance round-trips; a non-post / malformed event ->
  ``[]`` (fail-soft).

RED today: ``worldmonitor.plugins.connectors.bluesky`` does not exist, so the top-level import
raises ``ModuleNotFoundError`` and the whole module errors at collection (the correct RED: "bluesky
missing"). GREEN once the builder lands the connector + its ``config.schema.json``.

Note on ``mode``: ``Manifest.mode`` is single-valued. The ADR describes the connector as a STREAM
*external import*, but the value that GATES the driver's keep-warm scheduling is ``Mode.STREAM`` —
that is what is asserted here (and what the driver integration test pins via ``next_run=now``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest

from worldmonitor.plugins.base import Capability, Kind, Mode, RawRecord, Status

# Top-level import of the not-yet-built connector — ModuleNotFoundError today (the correct RED).
from worldmonitor.plugins.connectors.bluesky import BlueskyConnector
from worldmonitor.provenance.model import Provenance, get_provenance

_DID = "did:plc:abc"
_RKEY = "3kabc123xyz"
_TIME_US = 1234567890

_PROV = Provenance(
    source_id="bluesky:",
    retrieved_at="2026-06-28T00:00:00Z",
    reliability="C",
    source_record="s3://landing/bluesky/did_plc_abc-3kabc123xyz.json",
)


def _post_event(
    *,
    text: str = "hello world",
    created: str = "2026-06-28T00:00:00Z",
    time_us: int = _TIME_US,
    did: str = _DID,
    rkey: str = _RKEY,
) -> dict[str, Any]:
    """A Jetstream ``app.bsky.feed.post`` create commit, shaped exactly as the ADR fixture."""
    return {
        "did": did,
        "time_us": time_us,
        "kind": "commit",
        "commit": {
            "collection": "app.bsky.feed.post",
            "operation": "create",
            "rkey": rkey,
            "record": {
                "$type": "app.bsky.feed.post",
                "text": text,
                "createdAt": created,
            },
        },
    }


def _record(event: dict[str, Any]) -> RawRecord:
    """Wrap a Jetstream event as the JSON ``RawRecord`` that ``map()`` consumes."""
    return RawRecord(
        key=f"{event['did']}-{event['commit']['rkey']}",
        data=json.dumps(event).encode("utf-8"),
        retrieved_at=_PROV.retrieved_at,
        content_type="application/json",
    )


def _source_of(events: list[dict[str, Any]]) -> Any:
    """An ``event_source`` seam returning ``events`` verbatim (NO live WebSocket)."""

    def _source(
        cursor: str | None, window_seconds: int, max_events: int
    ) -> Iterator[dict[str, Any]]:
        return iter(events)

    return _source


# --------------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------------


def test_manifest_is_stream_passive_implemented() -> None:
    """A PASSIVE STREAM CONNECTOR named ``bluesky`` (driver keeps STREAM warm, refuses ACTIVE)."""
    manifest = BlueskyConnector().manifest
    assert manifest.connector_id == "bluesky"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.STREAM
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — wanted_collections / window_seconds / max_events with defaults + bounds
# --------------------------------------------------------------------------------------------------


def test_config_schema_defaults_and_bounds() -> None:
    """The schema declares the documented defaults + bounds; validate_config enforces them."""
    connector = BlueskyConnector()
    schema = connector.config_schema
    props = schema["properties"]

    assert props["wanted_collections"]["type"] == "array"
    assert props["wanted_collections"].get("default") == ["app.bsky.feed.post"]

    assert props["window_seconds"]["type"] == "integer"
    assert props["window_seconds"]["minimum"] >= 1
    assert props["window_seconds"].get("default") == 20

    assert props["max_events"]["type"] == "integer"
    assert props["max_events"]["minimum"] >= 1
    assert props["max_events"].get("default") == 500

    # A fully-specified config and the all-default empty config both pass (public firehose, no key).
    connector.validate_config(
        {"wanted_collections": ["app.bsky.feed.post"], "window_seconds": 20, "max_events": 500}
    )
    connector.validate_config({})

    # Out-of-bounds bounds are rejected (the window can't be zero / negative events).
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"window_seconds": 0})
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"max_events": 0})


# --------------------------------------------------------------------------------------------------
# collect() — over a fake event_source; key carries did/rkey, cursor = str(time_us), resumes
# --------------------------------------------------------------------------------------------------


def test_collect_yields_records_keyed_by_did_rkey_with_time_us_cursor() -> None:
    """Each post event becomes a RawRecord keyed by did/rkey and carrying cursor==str(time_us)."""
    events = [
        _post_event(rkey="3kaaa", time_us=1000),
        _post_event(rkey="3kbbb", time_us=2000),
        _post_event(rkey="3kccc", time_us=3000),
    ]
    connector = BlueskyConnector(event_source=_source_of(events))

    records = list(connector.collect({"window_seconds": 20, "max_events": 500}))

    assert len(records) == 3
    assert _DID in records[0].key and "3kaaa" in records[0].key
    assert [r.cursor for r in records] == ["1000", "2000", "3000"]


def test_collect_resumes_from_config_cursor() -> None:
    """collect() forwards ``config['_cursor']`` to the event_source (G8 resume)."""
    seen: list[str | None] = []

    def _source(
        cursor: str | None, window_seconds: int, max_events: int
    ) -> Iterator[dict[str, Any]]:
        seen.append(cursor)
        return iter([_post_event()])

    connector = BlueskyConnector(event_source=_source)
    list(connector.collect({"_cursor": "1234500000", "window_seconds": 20, "max_events": 500}))

    assert seen == ["1234500000"]


# --------------------------------------------------------------------------------------------------
# map() — FtM Article + provenance for a post-create; [] for a non-post / malformed event
# --------------------------------------------------------------------------------------------------


def test_map_post_create_emits_one_article_with_provenance() -> None:
    """A post-create commit maps to ONE FtM Article (title/bodyText/author/publishedAt/url)."""
    entities = list(BlueskyConnector().map(_record(_post_event()), provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]

    # Native FtM Article (NO wm: extension), id derived from did + rkey.
    assert entity.schema.name == "Article"
    assert entity.id == f"bluesky-{_DID}-{_RKEY}"

    assert entity.get("title") == ["hello world"]
    assert entity.get("bodyText") == ["hello world"]
    assert entity.get("author") == [_DID]
    assert entity.get("sourceUrl") == [f"at://{_DID}/app.bsky.feed.post/{_RKEY}"]
    published = entity.get("publishedAt")
    assert published and "2026-06-28" in published[0]

    # Provenance round-trips intact (the non-negotiable invariant: every mapped entity is stamped).
    prov = get_provenance(entity)
    assert prov is not None
    assert prov == _PROV
    assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])


def test_map_skips_non_post_and_malformed_events() -> None:
    """A delete, a non-post collection, and malformed bytes each map to ``[]`` (fail-soft)."""
    connector = BlueskyConnector()

    delete_event = _post_event()
    delete_event["commit"]["operation"] = "delete"
    assert list(connector.map(_record(delete_event), provenance=_PROV)) == []

    like_event = _post_event()
    like_event["commit"]["collection"] = "app.bsky.feed.like"
    assert list(connector.map(_record(like_event), provenance=_PROV)) == []

    bad = RawRecord(key="bad", data=b"not json at all", retrieved_at=_PROV.retrieved_at)
    assert list(connector.map(bad, provenance=_PROV)) == []
