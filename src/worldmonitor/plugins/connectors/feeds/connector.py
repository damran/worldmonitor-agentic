"""FeedConnector — a generic RSS/Atom feed mapped to FtM ``Article`` (ADR 0066).

A single generic ``EXTERNAL_IMPORT`` / ``PASSIVE`` connector: any feed is just a ``feed_url``. It
fetches the feed XML over the SSRF guard, parses it with ``feedparser`` (which normalizes RSS 2.0 /
RSS 1.0 / Atom 1.0 into one structure AND — critically for hostile input — does not resolve external
XML entities), and yields one ``RawRecord`` per entry. ``map()`` turns each entry into an FtM-native
``Article`` (metadata only; full-text ``bodyText`` is a deferred Phase-4 enricher) carrying
provenance. It never writes to the graph — raw lands in the landing zone and candidates go to the ER
queue (L3 owns resolution).

Safety (the locked invariants):

* **Every fetch goes through** :func:`worldmonitor.net.ssrf.guarded_stream` — never a bare ``httpx``
  call to an attacker-influenced host (a private-resolving feed host is blocked before any request).
* **Collection is hard-bounded** by ``max_items`` — a feed with more entries than the cap stops at
  the cap.
* **The body is read under** :data:`_MAX_FEED_BYTES` — a hostile, oversized feed raises
  (fail-closed) instead of being read unbounded into memory.
* **XXE / entity-expansion safe** — ``feedparser`` does not resolve external entities (no SSRF /
  file-read via a crafted ``<!DOCTYPE>``) and does not network-fetch on parse.
"""

# feedparser ships no type stubs (its ``FeedParserDict`` is dynamically populated), so it is
# imported only here and its untyped surface is narrowed at the call site (``parsed: Any``).
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import feedparser
import httpx

from worldmonitor.net.ssrf import guarded_stream
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.plugins.base import (
    Capability,
    Connector,
    Kind,
    Manifest,
    Mode,
    RawRecord,
    Status,
)
from worldmonitor.provenance.model import Provenance, stamp

logger = logging.getLogger(__name__)

# Default HTTP timeout when a config omits ``timeout`` (the schema has no default here).
_HTTP_TIMEOUT = 30.0
# Default entry cap when a config omits ``max_items`` (the schema default is the real source of
# truth; jsonschema does not inject defaults, so the connector falls back defensively).
_DEFAULT_MAX_ITEMS = 100
# Hostile-body bound: a feed body over this many bytes is refused (fail-closed) rather than read
# unbounded into memory. 8 MiB comfortably exceeds a normal RSS/Atom document.
_MAX_FEED_BYTES = 8 * 1024 * 1024

# Normalized entry field -> FtM Article property (set only when the source value is present and
# non-empty; ``validate_or_raise`` enforces FtM validity). ``publishedAt`` and ``date`` both carry
# the entry's (normalized) date; ``publisher`` is the feed-level title.
_PROPERTY_MAP = {
    "title": "title",
    "author": "author",
    "published": "publishedAt",
    "published_date": "date",
    "link": "sourceUrl",
    "summary": "summary",
    "feed_title": "publisher",
    "language": "language",
}


def _entry_date(entry: Mapping[str, Any]) -> str | None:
    """Return the entry's publication date as an ISO-8601 string, or ``None``.

    feedparser exposes a parsed ``time.struct_time`` (``published_parsed`` / ``updated_parsed``)
    for the date-format zoo (RSS RFC822, Atom RFC3339, …); normalizing it here to ISO-8601 yields a
    clean FtM date downstream (ADR 0066 §4). Falls back to the raw ``published`` / ``updated``
    string when feedparser could not parse the date.
    """
    for parsed_key, raw_key in (("published_parsed", "published"), ("updated_parsed", "updated")):
        struct = entry.get(parsed_key)
        if struct:
            return datetime(*struct[:6], tzinfo=UTC).isoformat()
        raw = entry.get(raw_key)
        if raw:
            return str(raw)
    return None


class FeedConnector(Connector):
    """Imports one RSS/Atom feed (FtM Article, metadata only) with provenance."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store an optional injected ``transport`` (``httpx.MockTransport`` in tests).

        Production instantiation passes no transport (real HTTP via ``guarded_stream``); tests
        inject an ``httpx.MockTransport`` so no live network call is ever made.
        """
        self._transport = transport

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="feeds",
            name="Feeds",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description="Generic RSS/Atom feed; imports each entry as an FtM Article.",
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Fetch the feed over the SSRF guard and yield one raw record per entry (capped).

        The feed is fetched through :func:`guarded_stream` (SSRF-validated), checked with
        ``raise_for_status`` (a 4xx/5xx fails loud), and read under :data:`_MAX_FEED_BYTES` (an
        oversized body raises). ``feedparser`` parses the bytes (XXE-safe: external entities are not
        resolved) and the first ``max_items`` entries are normalized to JSON ``RawRecord``\\ s keyed
        by the entry's id/guid (or link). Pagination does not apply to a single feed; the
        ``max_items`` slice is the hard bound.
        """
        self.validate_config(config)
        retrieved_at = datetime.now(UTC).isoformat()
        max_items = int(config.get("max_items", _DEFAULT_MAX_ITEMS))
        timeout = float(config.get("timeout", _HTTP_TIMEOUT))

        with guarded_stream(
            "GET", str(config["feed_url"]), timeout=timeout, transport=self._transport
        ) as response:
            response.raise_for_status()
            body = self._read_bounded(response)

        # feedparser normalizes the feed variants AND does not resolve external entities (no SSRF /
        # file-read from a hostile ``<!DOCTYPE>``); it never makes a network call on parse. Its
        # dynamic ``FeedParserDict`` is narrowed to ``Any`` here (it has no usable static type).
        parsed: Any = feedparser.parse(body)
        feed_title = parsed.feed.get("title")
        language = parsed.feed.get("language")

        for entry in parsed.entries[:max_items]:
            link = entry.get("link")
            entry_id = entry.get("id") or link
            published = _entry_date(entry)
            normalized: dict[str, Any] = {
                "title": entry.get("title"),
                "link": link,
                "id": entry_id,
                "author": entry.get("author"),
                "published": published,
                "summary": entry.get("summary"),
                "feed_title": feed_title,
                "language": language,
            }
            yield RawRecord(
                key=str(entry_id or link or ""),
                data=json.dumps(normalized).encode("utf-8"),
                retrieved_at=retrieved_at,
                content_type="application/json",
            )

    @staticmethod
    def _read_bounded(response: httpx.Response) -> bytes:
        """Read the streaming body under :data:`_MAX_FEED_BYTES`, raising if it exceeds the cap.

        Iterates ``iter_bytes`` accumulating chunks (never ``.read()``/``.text`` unbounded). A body
        over the cap raises :class:`ValueError` (fail-closed against a hostile, oversized response)
        before it is parsed.
        """
        chunks = bytearray()
        for chunk in response.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > _MAX_FEED_BYTES:
                raise ValueError(f"feed body exceeded the {_MAX_FEED_BYTES}-byte cap (fail-closed)")
        return bytes(chunks)

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Map one normalized feed entry to an FtM Article with provenance.

        An entry with NEITHER a link NOR a title is identity-less and dropped (``[]``, fail-soft on
        a single entry) rather than raising and failing the batch. The entity id is derived
        deterministically from the entry's id/guid (or link) so a re-ingest enqueues idempotently.
        """
        entry = json.loads(record.data)
        link = str(entry.get("link") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not link and not title:
            return []

        published = entry.get("published")
        source_values: dict[str, Any] = {
            "title": entry.get("title"),
            "author": entry.get("author"),
            "published": published,
            "published_date": published,
            "link": entry.get("link"),
            "summary": entry.get("summary"),
            "feed_title": entry.get("feed_title"),
            "language": entry.get("language"),
        }
        properties: dict[str, list[str]] = {}
        for source_field, ftm_property in _PROPERTY_MAP.items():
            value = source_values.get(source_field)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                properties[ftm_property] = [text]

        stable = str(entry.get("id") or link or title)
        entity_id = f"feed-{hashlib.sha1(stable.encode('utf-8')).hexdigest()}"
        entity = validate_or_raise(
            {
                "id": entity_id,
                "schema": "Article",
                "properties": properties,
                "datasets": ["feeds"],
            }
        )
        return [stamp(entity, provenance)]
