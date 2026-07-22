"""URLhaus connector — the abuse.ch malicious-URL feed, mapped to `wm:Indicator` (ADR 0119).

abuse.ch's URLhaus publishes a free, anonymous JSON export of recently-seen malicious URLs (a
30-day rolling window). Each entry becomes exactly one FtM ``Indicator``
(``ontology/schema/wm/Indicator.yaml``): a non-matchable, deterministic-id-only node that
converges on re-ingest — from THIS connector or any sibling (``feodo``, ``threatfox``,
``sslbl``) — via the shared ``ioc-<sha1(value)>`` scheme
(``worldmonitor.ontology.ioc.indicator_id``, S-2b), never by fuzzy resolution. CTI infrastructure
indicators must never enter the person/org merge path (CLAUDE.md catastrophic-merge guard).

``collect()`` streams the export over the SSRF guard (bounded to ``_MAX_FEED_BYTES``, feodo/
threatfox ``_read_bounded`` idiom) and traverses the same ``{numeric_id: [record, ...]}`` shape
as ``threatfox``, yielding ONE ``RawRecord`` per inner record; a top-level value that is not a
list, or a list element that is not a dict, is skipped rather than raising. ``limit`` hard-caps
the yield count in traversal order. An optional ``auth_key`` config value rides as the
``Auth-Key`` HTTP header (never the URL, never logged); a 401/403 response is NOT swallowed as an
empty feed, it raises loud with an actionable message pointing at ``auth.abuse.ch`` (spec §6/§8).

``map()`` turns each entry into an ``Indicator`` with provenance. Every URLhaus record is, by
construction, a URL IOC, so ``indicatorType`` is unconditionally ``["url"]`` (no per-record type
inference needed, unlike ``threatfox``). ``firstSeenAt``/``lastSeenAt`` come from ``dateadded``/
``last_online``: the real ``json_recent`` export carries a trailing `` UTC`` suffix on both
fields. Spec §6 calls for stripping that suffix (case-insensitively, plus surrounding whitespace)
before handing the value to ``entity.add()``. At the pinned FollowTheMoney 4.9.2 / prefixdate
0.5.0 versions this strip is empirically DEFENSIVE rather than load-bearing — FtM's own date
cleaning already normalizes a `` UTC``-suffixed value correctly — but it is kept anyway as
insurance against a future FollowTheMoney/prefixdate version reintroducing stricter parsing that
would otherwise silently drop the value (see the spec's own build-time verification note, §11).

``malwareFamily`` is **never** emitted by this connector (a locked decision, spec §6): URLhaus's
``tags`` mix real family labels with unrelated format/architecture tags (e.g. ``"elf"``,
``"mips"``, ``"32-bit"`` alongside a genuine family token like ``"Mozi"``) with no reliable way
to distinguish them, and ``threat`` is a delivery *category* (e.g. ``malware_download``), not a
family — inventing a family from either would be a guess, and CLAUDE.md's "leads, not verdicts"
principle means we never guess an attribution-adjacent property. An entry with no usable ``url``
(the identity field) is fail-soft dropped (``[]``), never raising. No FtM ``topics``, no
``country``, no ``indicates`` edge — attribution is the designated S-2 phase 3 enricher, out of
scope here. This is a passive ``EXTERNAL_IMPORT`` connector; it never writes to the graph.
"""

from __future__ import annotations

import json
import logging
import re
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

# Default feed (anonymous, free, abuse.ch fair-use — spec §6). Config-overridable via `url`
# (feodo/threatfox precedent).
_DEFAULT_URL = "https://urlhaus.abuse.ch/downloads/json_recent/"
_HTTP_TIMEOUT = 30.0
# Hostile-body bound: the real json_recent export is ~10.8 MB in practice, comfortably under this
# cap — but a hostile/corrupted response must still be refused (fail-closed) rather than read
# unbounded into memory (spec §6 explicitly calls out asserting this cap; feodo/threatfox
# `_read_bounded` idiom, 16 MiB).
_MAX_FEED_BYTES = 16 * 1024 * 1024

# Matches a trailing " UTC" (any case) plus any preceding whitespace, e.g. "...20 UTC" or
# "...20  utc". Spec §6's defensive strip — see the module docstring's AMBIGUITY NOTED #2
# discussion for why this is belt-and-suspenders rather than load-bearing at the pinned FtM
# version.
_TRAILING_UTC_RE = re.compile(r"\s*UTC\s*$", re.IGNORECASE)


def _strip_trailing_utc(value: Any) -> Any:
    """Strip a trailing `` UTC`` suffix (case-insensitive) + whitespace from a string value.

    Non-string values (``None``, in particular a null `last_online`) pass through unchanged so
    the caller's `entity.add()` no-ops on them exactly as it would on the raw value.
    """
    if not isinstance(value, str):
        return value
    return _TRAILING_UTC_RE.sub("", value).strip()


class UrlhausAuthError(RuntimeError):
    """Raised when URLhaus responds 401/403 — the anonymous export appears gated.

    Deliberately NOT swallowed as an empty feed (an empty-but-200 body is fine and expected on a
    quiet window; a 401/403 is a distinct, actionable operator condition).
    """


class UrlhausConnector(Connector):
    """Imports the abuse.ch URLhaus recent-URL export as `wm:Indicator` entities."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store an optional injected `transport` (`httpx.MockTransport` in tests).

        Production instantiation passes no transport (real HTTP via `guarded_stream`); tests
        inject an `httpx.MockTransport` so no live network call is ever made.
        """
        self._transport = transport

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="urlhaus",
            name="URLhaus",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description=(
                "abuse.ch URLhaus recent malicious-URL export; one wm:Indicator per URL IOC."
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
        is a JSON object keyed by numeric urlhaus id, each value a list of record dicts (usually
        one element); a value that is not a list, or a list element that is not a dict, is
        skipped — never raised (hostile-body tolerance is entirely `collect()`'s job, per-row
        rejection is `map()`'s job). `limit` (when set) hard-caps the number of records yielded,
        counted in traversal order.
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
                raise UrlhausAuthError(
                    f"urlhaus: {response.status_code} — the anonymous export appears gated; "
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
                url_value = item.get("url")
                key = (
                    url_value
                    if isinstance(url_value, str) and url_value.strip()
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
                    f"urlhaus feed body exceeded the {_MAX_FEED_BYTES}-byte cap (fail-closed)"
                )
        return bytes(chunks)

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Map one raw URLhaus entry to an FtM `Indicator` with its deterministic id + provenance.

        An entry with a blank/missing `url` (the identity field an Indicator cannot be built
        without) is dropped (`[]`, fail-soft) rather than raising and failing the batch. The
        entity id comes from the SHARED connector-independent scheme
        (`worldmonitor.ontology.ioc.indicator_id`), so a re-ingest of the same URL — from THIS or
        any sibling connector — converges on the same node by identity. `indicatorType` is
        unconditionally `["url"]` (every URLhaus record IS a URL IOC — spec §6). `malwareFamily`
        is NEVER emitted (locked decision; see the module docstring for why `tags`/`threat` are
        not a reliable family signal). `firstSeenAt`/`lastSeenAt` are added via `entity.add()`
        (not the initial `properties` payload) so FtM's own date-type cleaning normalizes the
        feed's timestamps to ISO-T and silently drops any junk/null value, without ever raising;
        the raw `dateadded`/`last_online` values are defensively stripped of a trailing ` UTC`
        suffix first (spec §6 — see the module docstring for why this is belt-and-suspenders at
        the pinned FtM version rather than load-bearing). No `topics`, no `country`, no
        `indicates` edge — attribution is S-2 phase 3, out of scope for this gate.
        """
        entry: Any = json.loads(record.data)
        if not isinstance(entry, dict):
            return []
        entry = cast("dict[str, Any]", entry)

        value = entry.get("url")
        if not isinstance(value, str) or not value.strip():
            logger.warning("urlhaus: dropping entry with a blank/missing url")
            return []

        entity_id = indicator_id(value)

        properties: dict[str, list[str]] = {
            "name": [value],
            "indicatorValue": [value],
            "indicatorType": ["url"],
        }

        entity = validate_or_raise(
            {
                "id": entity_id,
                "schema": "Indicator",
                "properties": properties,
                "datasets": ["urlhaus"],
            }
        )
        # `entity.add()` (unlike the initial `properties` payload) cleans through the FtM date
        # type: a valid value normalizes to ISO-T, and any junk/null value (e.g. a nullable
        # `last_online`) is silently dropped rather than stored or raising. The trailing " UTC"
        # suffix is stripped first (defensive, spec §6 — see module docstring).
        entity.add("firstSeenAt", _strip_trailing_utc(entry.get("dateadded")))
        entity.add("lastSeenAt", _strip_trailing_utc(entry.get("last_online")))

        return [stamp(entity, provenance)]
