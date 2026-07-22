"""ThreatFox connector — the abuse.ch multi-type IOC feed, mapped to `wm:Indicator` (ADR 0119).

abuse.ch's ThreatFox publishes a free, legacy-unauthenticated JSON export of recently-seen IOCs
spanning several types (C2 `ip:port` pairs, malicious `domain`/`url` values, payload file hashes).
Each entry becomes exactly one FtM ``Indicator`` (``ontology/schema/wm/Indicator.yaml``): a
non-matchable, deterministic-id-only node that converges on re-ingest — from THIS connector or
any sibling (``feodo``, ``urlhaus``, ``sslbl``) — via the shared ``ioc-<sha1(value)>`` scheme
(``worldmonitor.ontology.ioc.indicator_id``, S-2b), never by fuzzy resolution. CTI infrastructure
indicators must never enter the person/org merge path (CLAUDE.md catastrophic-merge guard); the
same real-world IOC value from ThreatFox and Feodo converges on ONE node by identity alone.

``collect()`` streams the export over the SSRF guard (bounded to ``_MAX_FEED_BYTES``, feodo
``_read_bounded`` idiom) and traverses its ``{numeric_id: [record, ...]}`` shape, yielding ONE
``RawRecord`` per inner record; a top-level value that is not a list, or a list element that is
not a dict, is skipped rather than raising (hostile-body tolerance — the feed carries no
eligibility concept of its own). ``limit`` hard-caps the yield count in traversal order. An
optional ``auth_key`` config value rides as the ``Auth-Key`` HTTP header (never the URL, never
logged) — abuse.ch's per-endpoint auth scheme; a 401/403 response is NOT swallowed as an empty
feed, it raises loud with an actionable message pointing at ``auth.abuse.ch`` (the researched
deprecation risk of the legacy anonymous endpoint).

``map()`` turns each entry into an ``Indicator`` with provenance: ``indicatorType`` follows the
shared ``ioc_type`` vocabulary (spec §3 — ``ip:port`` MUST map to ``ipv4`` exactly like Feodo; an
unrecognized member passes through lower-cased rather than being dropped, since an unknown label
is a taxonomy gap, not a reason to discard real evidence). ``malwareFamily`` is populated from
``malware_printable`` except when the feed's own ``"unknown"``/``"Unknown"`` placeholder is the
only attribution available (a REAL, if vague, family like ``unknown_stealer`` is kept). An entry
with no usable ``ioc_value`` (the identity field) is fail-soft dropped (``[]``), never raising.
No FtM ``topics``, no ``country``, no ``indicates`` edge — attribution (family → threat actor) is
the designated S-2 phase 3 enricher, out of scope here. This is a passive ``EXTERNAL_IMPORT``
connector; it never writes to the graph.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from importlib import resources
from typing import Any, cast

import httpx

from worldmonitor.net.ssrf import guarded_stream
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.ioc import indicator_id
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

# Default feed (legacy unauthenticated, free, abuse.ch fair-use — spec §5). Config-overridable via
# `url` (feodo/mitre_attack precedent).
_DEFAULT_URL = "https://threatfox.abuse.ch/export/json/recent/"
_HTTP_TIMEOUT = 30.0
# Hostile-body bound: the recent export is a few MB in practice, so a response over this cap is
# refused (fail-closed) rather than read unbounded into memory (D2 spec: 16 MiB, feodo precedent).
_MAX_FEED_BYTES = 16 * 1024 * 1024

# The shared ioc_type -> indicatorType vocabulary (spec §3, load-bearing cross-connector
# consistency — `ip:port` MUST equal Feodo's own `"ipv4"` exactly). Keys are matched lower-cased;
# an unrecognized member passes through as `lower(raw ioc_type)` rather than being dropped.
_IOC_TYPE_MAP: dict[str, str] = {
    "ip:port": "ipv4",
    "domain": "domain",
    "url": "url",
    "md5_hash": "md5",
    "sha1_hash": "sha1",
    "sha256_hash": "sha256",
}


class ThreatFoxAuthError(RuntimeError):
    """Raised when ThreatFox responds 401/403 — the legacy anonymous export appears gated.

    Deliberately NOT swallowed as an empty feed (an empty-but-200 body is fine and expected on a
    quiet 48 h window; a 401/403 is a distinct, actionable operator condition).
    """


class ThreatFoxConnector(Connector):
    """Imports the abuse.ch ThreatFox recent-IOC export as `wm:Indicator` entities."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store an optional injected `transport` (`httpx.MockTransport` in tests).

        Production instantiation passes no transport (real HTTP via `guarded_stream`); tests
        inject an `httpx.MockTransport` so no live network call is ever made.
        """
        self._transport = transport

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="threatfox",
            name="ThreatFox",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description=(
                "abuse.ch ThreatFox recent IOC export; one wm:Indicator per malicious "
                "ip:port/domain/url/hash value."
            ),
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Stream the export over the SSRF guard and yield one `RawRecord` per inner IOC record.

        Fetched through :func:`guarded_stream` (SSRF-validated, `Auth-Key` header attached only
        when `auth_key` is configured), checked for a 401/403 BEFORE the generic
        `raise_for_status` (so the raised message is actionable rather than a bare status line),
        then read under :data:`_MAX_FEED_BYTES` (an oversized body raises, fail-closed). The body
        is a JSON object keyed by numeric IOC id, each value a list of record dicts (usually one
        element); a value that is not a list, or a list element that is not a dict, is skipped —
        never raised (hostile-body tolerance is entirely `collect()`'s job, per-row rejection is
        `map()`'s job). `limit` (when set) hard-caps the number of records yielded, counted in
        traversal order.
        """
        self.validate_config(config)
        url = str(config.get("url") or _DEFAULT_URL)
        limit = config.get("limit")
        auth_key = config.get("auth_key")
        headers: dict[str, str] | None = (
            {"Auth-Key": str(auth_key)} if isinstance(auth_key, str) and auth_key.strip() else None
        )
        retrieved_at = datetime.now(UTC).isoformat()

        with guarded_stream(
            "GET", url, timeout=_HTTP_TIMEOUT, transport=self._transport, headers=headers
        ) as response:
            if response.status_code in (401, 403):
                raise ThreatFoxAuthError(
                    f"threatfox: {response.status_code} — the legacy export appears gated; "
                    "register a free Auth-Key at auth.abuse.ch and set the connector's `auth_key`"
                )
            response.raise_for_status()
            body = self._read_bounded(response)

        parsed: Any = json.loads(body)
        if not isinstance(parsed, dict):
            return
        entries = cast("dict[str, Any]", parsed)

        count = 0
        for record_id, value in entries.items():
            if not isinstance(value, list):
                continue
            for raw_item in cast("list[Any]", value):
                if not isinstance(raw_item, dict):
                    continue
                item = cast("dict[str, Any]", raw_item)
                ioc_value = item.get("ioc_value")
                key = (
                    ioc_value
                    if isinstance(ioc_value, str) and ioc_value.strip()
                    else str(record_id)
                )
                yield RawRecord(
                    key=key,
                    data=json.dumps(item).encode("utf-8"),
                    retrieved_at=retrieved_at,
                    content_type="application/json",
                )
                count += 1
                if limit is not None and count >= int(limit):
                    return

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
                    f"threatfox feed body exceeded the {_MAX_FEED_BYTES}-byte cap (fail-closed)"
                )
        return bytes(chunks)

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Map one raw ThreatFox entry to an FtM `Indicator` with its deterministic id + provenance.

        An entry with a blank/missing `ioc_value` (the identity field an Indicator cannot be
        built without) is dropped (`[]`, fail-soft) rather than raising and failing the batch.
        The entity id comes from the SHARED connector-independent scheme
        (`worldmonitor.ontology.ioc.indicator_id`), so a re-ingest of the same IOC — from THIS or
        any sibling connector — converges on the same node by identity. `indicatorType` follows
        the shared vocabulary (`_IOC_TYPE_MAP`, spec §3); an unrecognized `ioc_type` passes
        through lower-cased rather than being dropped, and a missing `ioc_type` key emits no
        `indicatorType` at all rather than raising. `malwareFamily` is `malware_printable` unless
        the feed's own `"unknown"`/`"Unknown"` placeholder is all that's on offer — a real, if
        vague, family (e.g. `unknown_stealer`/"Unknown Stealer") is kept. `firstSeenAt`/
        `lastSeenAt` are added via `entity.add()` (not the initial `properties` payload) so FtM's
        own date-type cleaning normalizes the feed's space-separated UTC timestamps to ISO-T and
        silently drops any junk/null value, without ever raising. No `topics`, no `country`, no
        `indicates` edge — attribution is S-2 phase 3, out of scope for this gate.
        """
        entry: Any = json.loads(record.data)
        if not isinstance(entry, dict):
            return []
        entry = cast("dict[str, Any]", entry)

        value = entry.get("ioc_value")
        if not isinstance(value, str) or not value.strip():
            logger.warning("threatfox: dropping entry with a blank/missing ioc_value")
            return []

        entity_id = indicator_id(value)

        properties: dict[str, list[str]] = {
            "name": [value],
            "indicatorValue": [value],
        }

        ioc_type = entry.get("ioc_type")
        if isinstance(ioc_type, str) and ioc_type.strip():
            normalized_type = ioc_type.strip().lower()
            properties["indicatorType"] = [_IOC_TYPE_MAP.get(normalized_type, normalized_type)]

        malware = entry.get("malware")
        malware_printable = entry.get("malware_printable")
        if (
            isinstance(malware_printable, str)
            and malware_printable.strip()
            and malware != "unknown"
            and malware_printable != "Unknown"
        ):
            properties["malwareFamily"] = [malware_printable]

        entity = validate_or_raise(
            {
                "id": entity_id,
                "schema": "Indicator",
                "properties": properties,
                "datasets": ["threatfox"],
            }
        )
        # `entity.add()` (unlike the initial `properties` payload) cleans through the FtM date
        # type: a valid "YYYY-MM-DD HH:MM:SS" value normalizes to ISO-T, and any junk/null value
        # (e.g. a nullable `last_seen_utc`) is silently dropped rather than stored or raising.
        entity.add("firstSeenAt", entry.get("first_seen_utc"))
        entity.add("lastSeenAt", entry.get("last_seen_utc"))

        return [stamp(entity, provenance)]
