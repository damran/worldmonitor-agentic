"""Feodo Tracker connector — the abuse.ch C2 IP blocklist, mapped to `wm:Indicator` (ADR 0118).

abuse.ch's Feodo Tracker publishes a free, unauthenticated JSON feed of C2 (command-and-control)
IP:port pairs used by well-known banking-trojan/malware families (Emotet, QakBot, ...). Each entry
becomes exactly one FtM ``Indicator`` (the first ``wm:`` L2 extension, ``ontology/schema/wm/
Indicator.yaml``): a non-matchable, deterministic-id-only node that converges on re-ingest by
``feodo-<sha1(ip:port)>`` rather than fuzzy resolution — CTI infrastructure indicators must never
enter the person/org merge path (CLAUDE.md catastrophic-merge guard).

``collect()`` streams the feed over the SSRF guard and yields ONE ``RawRecord`` per feed entry,
unfiltered (the feed carries no revoked/deprecated eligibility concept, unlike ``mitre_attack``);
``limit`` hard-caps the yield count in feed order. ``map()`` turns each entry into an ``Indicator``
with provenance; an entry with no usable ``ip_address`` (the identity field) is fail-soft dropped
(``[]``), never raising. No FtM ``topics``, no ``country`` — ASN geo is not an event location, so
Indicators stay off the dashboard globe by design. This is a passive ``EXTERNAL_IMPORT`` connector;
it never writes to the graph.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from importlib import resources
from typing import Any, cast

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

# Default feed (unauthenticated, free, abuse.ch fair-use — ADR 0118). Config-overridable via
# `url` (mirrors mitre_attack's pinned-default-with-override shape).
_DEFAULT_URL = "https://feodotracker.abuse.ch/downloads/ipblocklist.json"
_HTTP_TIMEOUT = 30.0
# Hostile-body bound: the feed is a few hundred KB in practice, so a response over this cap is
# refused (fail-closed) rather than read unbounded into memory (D2 spec: 16 MiB).
_MAX_FEED_BYTES = 16 * 1024 * 1024


class FeodoConnector(Connector):
    """Imports the Feodo Tracker C2 IP blocklist as `wm:Indicator` entities."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store an optional injected `transport` (`httpx.MockTransport` in tests).

        Production instantiation passes no transport (real HTTP via `guarded_stream`); tests
        inject an `httpx.MockTransport` so no live network call is ever made.
        """
        self._transport = transport

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="feodo",
            name="Feodo Tracker",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description=(
                "abuse.ch Feodo Tracker C2 IP blocklist; one wm:Indicator per malicious "
                "IP:port pair."
            ),
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Stream the feed over the SSRF guard and yield one `RawRecord` per feed entry.

        Fetched through :func:`guarded_stream` (SSRF-validated), checked with
        `raise_for_status` (a 4xx/5xx fails loud), and read under :data:`_MAX_FEED_BYTES` (an
        oversized body raises, fail-closed). The feed's JSON array has no eligibility concept —
        every entry (well-formed or not) is yielded; malformed-entry rejection is entirely
        `map()`'s job. `limit` (when set) hard-caps the number of records yielded, counted in
        feed order.
        """
        self.validate_config(config)
        url = str(config.get("url") or _DEFAULT_URL)
        limit = config.get("limit")
        retrieved_at = datetime.now(UTC).isoformat()

        with guarded_stream(
            "GET", url, timeout=_HTTP_TIMEOUT, transport=self._transport
        ) as response:
            response.raise_for_status()
            body = self._read_bounded(response)

        parsed: Any = json.loads(body)
        entries = cast("list[Any]", parsed) if isinstance(parsed, list) else []

        count = 0
        for raw_entry in entries:
            if not isinstance(raw_entry, dict):
                continue
            entry = cast("dict[str, Any]", raw_entry)
            ip_address = entry.get("ip_address")
            port = entry.get("port")
            yield RawRecord(
                key=f"{ip_address}:{port}",
                data=json.dumps(entry).encode("utf-8"),
                retrieved_at=retrieved_at,
                content_type="application/json",
            )
            count += 1
            if limit is not None and count >= int(limit):
                break

    @staticmethod
    def _read_bounded(response: httpx.Response) -> bytes:
        """Read the streaming body under :data:`_MAX_FEED_BYTES`, raising if it exceeds the cap.

        Iterates `iter_bytes` accumulating chunks (never `.read()`/`.text` unbounded). A body
        over the cap raises :class:`ValueError` (fail-closed against a hostile, oversized
        response) before it is parsed.
        """
        chunks = bytearray()
        for chunk in response.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > _MAX_FEED_BYTES:
                raise ValueError(
                    f"feodo feed body exceeded the {_MAX_FEED_BYTES}-byte cap (fail-closed)"
                )
        return bytes(chunks)

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Map one raw Feodo entry to an FtM `Indicator` with its deterministic id + provenance.

        An entry with a blank/missing `ip_address` (the identity field an Indicator cannot be
        built without) is dropped (`[]`, fail-soft) rather than raising and failing the batch.
        The entity id is `feodo-<sha1(ip:port)>`, so a re-ingest of the same IOC converges on the
        same node. `firstSeenAt`/`lastSeenAt` are added via `entity.add()` (not the initial
        `properties` payload) so FtM's own date-type cleaning normalizes the feed's
        space-separated `first_seen` timestamp to ISO-T and silently drops any junk value,
        without ever raising. No `topics`, no `country` — ASN geo is not an event location.
        """
        entry: Any = json.loads(record.data)
        if not isinstance(entry, dict):
            return []
        entry = cast("dict[str, Any]", entry)

        ip_address = entry.get("ip_address")
        if not isinstance(ip_address, str) or not ip_address.strip():
            logger.warning("feodo: dropping entry with a blank/missing ip_address")
            return []

        value = f"{ip_address}:{entry.get('port')}"
        entity_id = f"feodo-{hashlib.sha1(value.encode('utf-8')).hexdigest()}"

        properties: dict[str, list[str]] = {
            "name": [value],
            "indicatorValue": [value],
            "indicatorType": ["ipv4"],
        }
        malware = entry.get("malware")
        if isinstance(malware, str) and malware.strip():
            properties["malwareFamily"] = [malware]

        entity = validate_or_raise(
            {
                "id": entity_id,
                "schema": "Indicator",
                "properties": properties,
                "datasets": ["feodo"],
            }
        )
        # `entity.add()` (unlike the initial `properties` payload) cleans through the FtM date
        # type: a valid "YYYY-MM-DD HH:MM:SS"/"YYYY-MM-DD" value normalizes to ISO-T, and any
        # junk value is silently dropped rather than stored or raising.
        entity.add("firstSeenAt", entry.get("first_seen"))
        entity.add("lastSeenAt", entry.get("last_online"))

        return [stamp(entity, provenance)]
