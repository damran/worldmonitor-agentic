"""Primary invariant tests (RED) for the FeedConnector (RSS/Atom -> FtM Article) — ADR 0066.

These pin the contract the builder must satisfy for
``src/worldmonitor/plugins/connectors/feeds/`` (a plain ``Connector`` subclass mirroring GeoNames):

* MANIFEST: ``connector_id="feeds"``, ``kind=CONNECTOR``, ``mode=EXTERNAL_IMPORT``,
  ``capability=PASSIVE`` (the driver refuses ``ACTIVE``), ``status=IMPLEMENTED``.
* CONFIG SCHEMA: ``feed_url`` required (string); ``max_items`` integer ``>=1`` (default 100);
  ``additionalProperties: false``. ``validate_config`` rejects a missing ``feed_url`` / a
  ``max_items`` below the bound, and accepts a valid config.
* ``map()``: over an RSS-2.0 entry dict AND an Atom-1.0 entry dict (shaped exactly as ``collect()``
  emits) maps to ONE FtM ``Article`` with ``title`` + ``sourceUrl`` (= the link) +
  ``publishedAt``/``date`` present, and PROVENANCE that round-trips via ``get_provenance``. The
  entity id is deterministic and ``feed-`` prefixed. An entry with NEITHER link NOR title -> ``[]``.
* ``collect()``: over ``httpx.MockTransport`` serving the local rss2.xml / atom.xml fixtures (NO
  live HTTP) yields ONE ``RawRecord`` per entry (content-type ``application/json``, key=guid/link)
  HARD-bounded by ``max_items``, and the body read is bounded by ``_MAX_FEED_BYTES`` (oversized ->
  ``ValueError``).
* SSRF: every fetch goes through ``net.ssrf.guarded_stream`` — a host resolving to a private address
  is blocked BEFORE any request leaves.
* XXE / HOSTILE XML: feeding xxe_payload.xml (a ``<!DOCTYPE ... ENTITY SYSTEM "file:///etc/passwd">``
  + billion-laughs-lite payload) reads NO local file (no ``root:`` sentinel reaches any record /
  entity), makes NO extra network call (``getaddrinfo`` is hit only for the feed host), completes
  promptly, and does not raise a file-read error — feedparser must not resolve external entities.

RED today: ``worldmonitor.plugins.connectors.feeds`` does not exist (and ``feedparser`` is not yet a
declared dependency), so the top-level import raises ``ModuleNotFoundError`` and the whole module
errors at collection — the correct RED. GREEN once the builder lands the connector + the dep.

No live network: ``httpx.MockTransport`` is injected through the connector ``transport=`` ctor
kwarg (forwarded to ``guarded_stream``), and ``socket.getaddrinfo`` is monkeypatched so the SSRF
host check runs with no real DNS — the pattern from ``test_opencorporates_connector.py``.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest

from worldmonitor.net.ssrf import BlockedAddressError
from worldmonitor.plugins.base import Capability, Kind, Mode, RawRecord, Status

# Top-level import of the not-yet-built connector — ModuleNotFoundError today (correct RED).
from worldmonitor.plugins.connectors.feeds import FeedConnector
from worldmonitor.plugins.connectors.feeds.connector import _MAX_FEED_BYTES
from worldmonitor.provenance.model import Provenance, get_provenance

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "feeds"

_PROV = Provenance(
    source_id="feeds:example",
    retrieved_at="2026-06-28T00:00:00Z",
    reliability="B",
    source_record="s3://landing/feeds/urn-example-quake-1.json",
)

# Entry dicts shaped EXACTLY as ``collect()`` emits into ``RawRecord.data`` (the locked field names:
# title, link, id, author, published, updated, summary, feed_title, language). ``published`` is
# given ISO here so the map() unit asserts the property mapping precisely, independent of how the
# builder normalizes RSS RFC822 dates (the real-fixture normalization is locked end-to-end below).
_RSS_ENTRY: dict[str, Any] = {
    "title": "Magnitude 5 quake hits border region",
    "link": "https://news.example.com/articles/quake",
    "id": "urn:example:quake-1",
    "author": "Alice Reporter",
    "published": "2026-06-23T09:00:00Z",
    "updated": "2026-06-23T09:00:00Z",
    "summary": "A magnitude 5 earthquake struck the border region early Monday.",
    "feed_title": "Example News",
    "language": "en-us",
}
_ATOM_ENTRY: dict[str, Any] = {
    "title": "Cyber advisory released for critical sector",
    "link": "https://feeds.example.org/atom/cyber",
    "id": "tag:example.org,2026:atom-cyber-1",
    "author": "Dana Atomwriter",
    "published": "2026-06-20T10:00:00Z",
    "updated": "2026-06-20T11:00:00Z",
    "summary": "A new advisory describes active exploitation in a critical sector.",
    "feed_title": "Example Atom Feed",
    "language": "en",
}


# --------------------------------------------------------------------------------------------------
# Hermetic-HTTP helpers (mirror tests/unit/test_opencorporates_connector.py): a fake getaddrinfo so
# guard's assert_public_host resolves the feed host to a chosen IP with NO real DNS, plus a
# MockTransport that serves a feed fixture verbatim (NO real HTTP).
# --------------------------------------------------------------------------------------------------


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in resolving EVERY host to ``ip`` (one IPv4 5-tuple)."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def _fixture_transport(
    name: str, calls: list[httpx.Request], *, content_type: str = "application/xml"
) -> httpx.MockTransport:
    """Serve ``tests/fixtures/feeds/<name>`` verbatim for any request; record every request."""
    body = (_FIXTURES / name).read_bytes()

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    return httpx.MockTransport(_handler)


def _make_record(entry: dict[str, Any]) -> RawRecord:
    """Wrap a collect-shaped entry dict as the JSON ``RawRecord`` that ``map()`` consumes."""
    return RawRecord(
        key=str(entry.get("id") or entry.get("link") or ""),
        data=json.dumps(entry).encode("utf-8"),
        retrieved_at=_PROV.retrieved_at,
        content_type="application/json",
    )


# --------------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------------


def test_manifest_is_external_import_and_passive() -> None:
    """A PASSIVE EXTERNAL_IMPORT CONNECTOR named ``feeds`` (the driver refuses ACTIVE)."""
    manifest = FeedConnector().manifest
    assert manifest.connector_id == "feeds"
    assert manifest.kind is Kind.CONNECTOR
    assert manifest.mode is Mode.EXTERNAL_IMPORT
    assert manifest.capability is Capability.PASSIVE
    assert manifest.capability is not Capability.ACTIVE
    assert manifest.status is Status.IMPLEMENTED


# --------------------------------------------------------------------------------------------------
# Config schema — feed_url required + max_items bounded + closed
# --------------------------------------------------------------------------------------------------


def test_config_schema_requires_feed_url_and_bounds_max_items() -> None:
    """feed_url is required; max_items is a bounded integer (>=1, default 100); schema is closed."""
    connector = FeedConnector()
    schema = connector.config_schema
    props = schema["properties"]

    # feed_url: a required string.
    assert props["feed_url"]["type"] == "string"
    assert "feed_url" in schema["required"]

    # max_items: a bounded integer with the documented default.
    assert props["max_items"]["type"] == "integer"
    assert props["max_items"]["minimum"] >= 1
    assert props["max_items"].get("default") == 100

    # An optional numeric timeout, when present, is a number (no secret in this connector).
    if "timeout" in props:
        assert props["timeout"]["type"] == "number"

    # Closed schema — no smuggled extra keys.
    assert schema["additionalProperties"] is False

    # validate_config: a complete config passes; a missing feed_url / out-of-bounds max_items / an
    # extra key are all rejected.
    connector.validate_config({"feed_url": "https://news.example.com/rss"})  # must not raise
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"max_items": 10})  # feed_url missing
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"feed_url": "https://news.example.com/rss", "max_items": 0})
    with pytest.raises(jsonschema.ValidationError):
        connector.validate_config({"feed_url": "https://news.example.com/rss", "bogus": 1})


# --------------------------------------------------------------------------------------------------
# map() — FtM Article + provenance round-trip (RSS and Atom)
# --------------------------------------------------------------------------------------------------


def test_map_rss_entry_emits_article_with_provenance() -> None:
    """An RSS entry maps to ONE FtM Article (title/sourceUrl/publishedAt/date) with provenance."""
    record = _make_record(_RSS_ENTRY)
    connector = FeedConnector()

    entities = list(connector.map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]

    # Native FtM Article (NO wm: extension), deterministically ``feed-`` prefixed id.
    assert entity.schema.name == "Article"
    assert entity.id.startswith("feed-")

    # The mapped business properties — title + sourceUrl(=link) are the identity-bearing pair.
    assert entity.get("title") == ["Magnitude 5 quake hits border region"]
    assert entity.get("sourceUrl") == ["https://news.example.com/articles/quake"]
    assert entity.get("author") == ["Alice Reporter"]
    assert entity.get("publisher") == ["Example News"]  # publisher = feed_title

    # publishedAt(=published) and date are both present and carry the entry's date.
    published_at = entity.get("publishedAt")
    assert published_at and "2026-06-23" in published_at[0]
    assert entity.get("date")  # the generic FtM date is also set

    # Provenance round-trips intact (the non-negotiable invariant: every mapped entity is stamped).
    prov = get_provenance(entity)
    assert prov is not None
    assert prov == _PROV
    assert all([prov.source_id, prov.retrieved_at, prov.reliability, prov.source_record])

    # The id is DETERMINISTIC (same entry -> same id), so a re-ingest enqueues idempotently.
    again = list(connector.map(record, provenance=_PROV))
    assert again[0].id == entity.id


def test_map_atom_entry_emits_article() -> None:
    """An Atom entry maps to ONE FtM Article (title/sourceUrl/publishedAt) with provenance."""
    record = _make_record(_ATOM_ENTRY)

    entities = list(FeedConnector().map(record, provenance=_PROV))

    assert len(entities) == 1
    entity = entities[0]
    assert entity.schema.name == "Article"
    assert entity.id.startswith("feed-")
    assert entity.get("title") == ["Cyber advisory released for critical sector"]
    assert entity.get("sourceUrl") == ["https://feeds.example.org/atom/cyber"]
    published_at = entity.get("publishedAt")
    assert published_at and "2026-06-20" in published_at[0]

    prov = get_provenance(entity)
    assert prov == _PROV


def test_map_skips_entry_without_link_or_title() -> None:
    """An entry with NEITHER a link NOR a title is dropped (fail-soft on one entry), not raised."""
    orphan = {
        "id": "urn:example:orphan",
        "summary": "no title and no link, so nothing identity-bearing to anchor an Article on",
        "published": "2026-06-20T10:00:00Z",
        "feed_title": "Example News",
    }
    assert list(FeedConnector().map(_make_record(orphan), provenance=_PROV)) == []


# --------------------------------------------------------------------------------------------------
# collect() — one RawRecord per entry over MockTransport, bounded
# --------------------------------------------------------------------------------------------------


def test_collect_yields_one_record_per_entry_rss(monkeypatch: pytest.MonkeyPatch) -> None:
    """collect() over rss2.xml yields one RawRecord per <item> (3), keyed by guid, JSON bytes."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = FeedConnector(transport=_fixture_transport("rss2.xml", calls))

    records = list(
        connector.collect({"feed_url": "https://news.example.com/rss", "max_items": 100})
    )

    assert calls, "collect() never issued a request through the transport"
    assert len(records) == 3
    assert {r.key for r in records} == {
        "urn:example:quake-1",
        "urn:example:election-2",
        "urn:example:markets-3",
    }
    assert all(r.content_type == "application/json" for r in records)

    # Each record's bytes are the normalized entry JSON (locked field names title/link/feed_title).
    by_key = {r.key: json.loads(r.data) for r in records}
    quake = by_key["urn:example:quake-1"]
    assert quake["title"] == "Magnitude 5 quake hits border region"
    assert quake["link"] == "https://news.example.com/articles/quake"
    assert quake["feed_title"] == "Example News"


def test_collect_yields_one_record_per_entry_atom(monkeypatch: pytest.MonkeyPatch) -> None:
    """collect() over atom.xml yields one RawRecord per <entry> (2), keyed by id, JSON bytes."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = FeedConnector(transport=_fixture_transport("atom.xml", calls))

    records = list(
        connector.collect({"feed_url": "https://feeds.example.org/atom", "max_items": 100})
    )

    assert len(records) == 2
    assert {r.key for r in records} == {
        "tag:example.org,2026:atom-cyber-1",
        "tag:example.org,2026:atom-sanctions-2",
    }
    assert all(r.content_type == "application/json" for r in records)
    by_key = {r.key: json.loads(r.data) for r in records}
    cyber = by_key["tag:example.org,2026:atom-cyber-1"]
    assert cyber["title"] == "Cyber advisory released for critical sector"
    assert cyber["link"] == "https://feeds.example.org/atom/cyber"


def test_collect_then_map_rss_articles_carry_publishedat_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end (real fixture -> collect -> map): every RSS Article carries title + sourceUrl +
    a NON-EMPTY publishedAt + provenance. This forces the builder to normalize the RSS RFC822
    ``pubDate`` into an FtM date SOMEWHERE in the collect->map pipeline (the ADR §86 invariant)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = FeedConnector(transport=_fixture_transport("rss2.xml", calls))

    records = list(
        connector.collect({"feed_url": "https://news.example.com/rss", "max_items": 100})
    )
    assert len(records) == 3

    articles = []
    for rec in records:
        articles.extend(connector.map(rec, provenance=_PROV))

    assert len(articles) == 3
    for art in articles:
        assert art.schema.name == "Article"
        assert art.get("title"), "Article emitted with no title"
        assert art.get("sourceUrl"), "Article emitted with no sourceUrl"
        assert art.get("publishedAt"), "Article emitted with no publishedAt (date not normalized)"
        assert get_provenance(art) == _PROV


def test_collect_is_hard_bounded_by_max_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """A feed with 3 entries but max_items=2 yields EXACTLY 2 RawRecords — the HARD bound."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[httpx.Request] = []
    connector = FeedConnector(transport=_fixture_transport("rss2.xml", calls))

    records = list(connector.collect({"feed_url": "https://news.example.com/rss", "max_items": 2}))

    assert len(records) == 2, f"max_items cap not enforced (saw {len(records)} records)"


def test_collect_fails_closed_on_oversized_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A body larger than ``_MAX_FEED_BYTES`` raises ValueError — never an unbounded read."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    oversized = b"A" * (_MAX_FEED_BYTES + 4096)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"content-type": "application/xml"})

    connector = FeedConnector(transport=httpx.MockTransport(_handler))

    with pytest.raises(ValueError):
        list(connector.collect({"feed_url": "https://news.example.com/rss", "max_items": 100}))


# --------------------------------------------------------------------------------------------------
# SSRF — every fetch goes through guarded_stream; a private-resolving host is blocked
# --------------------------------------------------------------------------------------------------


def test_collect_uses_guarded_stream_and_blocks_private_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fetch path goes through net.ssrf.guarded_stream: a host resolving to a private address is
    refused BEFORE any request leaves (a bare-httpx connector would NOT block)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("10.0.0.1"))
    calls: list[httpx.Request] = []
    connector = FeedConnector(transport=_fixture_transport("rss2.xml", calls))

    with pytest.raises(BlockedAddressError):
        list(connector.collect({"feed_url": "https://news.example.com/rss", "max_items": 100}))

    assert calls == [], "collect() connected to a blocked host — SSRF guard was bypassed"


# --------------------------------------------------------------------------------------------------
# XXE / HOSTILE XML — feedparser must not resolve external entities (no file read, no extra network)
# --------------------------------------------------------------------------------------------------


def test_collect_xxe_payload_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """A feed with ``<!DOCTYPE ... ENTITY SYSTEM "file:///etc/passwd">`` + billion-laughs-lite does
    NOT read a local file (no ``root:`` sentinel reaches any record/entity), makes NO network call
    beyond the feed host, completes promptly, and raises nothing worse than a clean parse rejection.
    """
    seen_hosts: list[str] = []

    def _recording_getaddrinfo(host: str, *_a: object, **_k: object) -> list[tuple[object, ...]]:
        seen_hosts.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _recording_getaddrinfo)
    calls: list[httpx.Request] = []
    connector = FeedConnector(transport=_fixture_transport("xxe_payload.xml", calls))
    feed_url = "https://feeds.example.org/hostile"

    try:
        records = list(connector.collect({"feed_url": feed_url, "max_items": 100}))
        raised: Exception | None = None
    except Exception as exc:  # a clean parse-rejection is acceptable; a file-read attempt is NOT
        records = []
        raised = exc

    if raised is not None:
        assert not isinstance(raised, OSError), f"collect tried to touch the filesystem: {raised!r}"
        assert "root:" not in str(raised)
        assert "/etc/passwd" not in str(raised)

    # The external entity was NOT resolved: /etc/passwd contents (the ``root:`` sentinel) appear in
    # NO collected record and NO mapped entity.
    for rec in records:
        assert b"root:" not in rec.data, "XXE entity resolved — /etc/passwd leaked into a RawRecord"
        for entity in connector.map(rec, provenance=_PROV):
            blob = json.dumps(entity.to_dict())
            assert "root:" not in blob, "XXE entity resolved — /etc/passwd leaked into an Article"

    # No network call beyond the feed host (a SYSTEM entity fetch would resolve a different host).
    assert seen_hosts, "the feed was never fetched — the XXE assertion would be vacuous"
    assert set(seen_hosts) == {"feeds.example.org"}, (
        f"parsing the hostile feed resolved unexpected hosts: {sorted(set(seen_hosts))}"
    )
