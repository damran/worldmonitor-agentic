"""Ransomware.live connector — victim claims + group catalog (ADR 0120, Gate S-4).

Ransomware.live publishes two free, unauthenticated v2 JSON datasets:

- ``groups`` (``GET /v2/groups``) — one FtM ``Organization`` per ransomware group,
  ``topics=["crime.cyber"]`` (deliberately sensitive — group merges park for human review,
  CLAUDE.md catastrophic-merge guard).
- ``recentvictims`` (``GET /v2/recentvictims``) — per claimed victim: one FtM ``Company`` (the
  victim), a thin FtM ``Organization`` (the claiming group), and an ``UnknownLink`` **edge**
  between them (``subject``=group, ``object``=victim,
  ``role="ransomware victim (claimed by group)"``). This is the codebase's first edge-emitting
  ``map()`` — provenance is stamped on the edge too (G1).

Both datasets are served by ONE connector, parametrized by a ``dataset`` config value (the
opensanctions precedent, ``docs/reviews/GATE_S4_RANSOMWARE_LIVE_SPEC.md`` §2). ``collect()``
streams a flat JSON array (NOT the abuse.ch ``{id:[record]}`` object shape) over the SSRF guard,
bounded to :data:`_MAX_FEED_BYTES` (``_read_bounded`` idiom, feodo/threatfox precedent). ``map()``
does not receive config, so it shape-dispatches on the record itself: a non-blank ``victim``
string is the victim path; else a non-blank ``name`` string is the group path; else fail-soft
``[]``. Victim claims are criminal self-declarations — the allegation lives ONLY on the
disclaimed edge (Admiralty ``"E"`` reliability, threaded by the driver per slice 1); the victim
``Company`` node never carries a risk topic (leads, not verdicts, CLAUDE.md). This is a passive
``EXTERNAL_IMPORT`` connector; it never writes to the graph.
"""

from __future__ import annotations

import hashlib
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

# Pinned free v2 defaults per dataset (spec §7's SeedSpec literals) — config-overridable via `url`.
_DEFAULT_URLS: dict[str, str] = {
    "recentvictims": "https://api.ransomware.live/v2/recentvictims",
    "groups": "https://api.ransomware.live/v2/groups",
}
_HTTP_TIMEOUT = 30.0
# Hostile-body bound (feodo/threatfox/urlhaus `_read_bounded` idiom, spec §4/§6): a response over
# this cap is refused (fail-closed) rather than read unbounded into memory.
_MAX_FEED_BYTES = 16 * 1024 * 1024

# TODO(ransomware_live): the PRO API-key HTTP header NAME is third-party-sourced and UNCONFIRMED
# (spec §4 step 2 / §12 open item 1) — the free v2 endpoints this connector uses ignore it
# entirely, so the header is sent defensively (PRO-ready) but this name is not verified against
# a real PRO subscription. Revisit if/when a PRO key is actually adopted (out of scope now, NON-
# goal, spec §0).
_API_KEY_HEADER = "X-API-KEY"

# recentvictims `activity` sentinel meaning "no sector data" (spec §3.2) — matched case-folded.
_ACTIVITY_NOT_FOUND = "not found"
# recentvictims `description` placeholder values meaning "no real description" (spec §3.2),
# matched case-folded after stripping any leading "[AI generated]" marker.
_DESCRIPTION_PLACEHOLDERS = frozenset({"", "n/a"})
_AI_GENERATED_PREFIX = "[ai generated]"

# The edge role is a fixed allegation-grade string (spec §3.2) — never derived from source data.
_CLAIM_ROLE = "ransomware victim (claimed by group)"

_CRIME_CYBER_TOPIC = "crime.cyber"
_DATASET_ID = "ransomware_live"


def _slug(value: str) -> str:
    """``_slug(x) = re.sub(r"[^a-z0-9]+", "", x.lower())`` — spec §3.1, verbatim.

    Strips ALL non-alphanumerics after lower-casing — the only normalization that converges the
    three observed forms of a group's identity (a display `name`, a `groups[].url` slug, and the
    raw `recentvictims[].group` string) onto one id.
    """
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _h(value: str) -> str:
    """``_h(s) = sha1(s.encode("utf-8")).hexdigest()[:16]`` — spec §3.1, verbatim."""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _url_slug(url: str) -> str:
    """The last path segment of a ``groups[].url`` value (the site's authoritative slug)."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def _group_id(slug_or_name: str) -> str:
    """``ransomware-live-group-{_slug(slug_or_name)}`` — converges the victim- and groups-side
    natural keys (a raw `group` string, or a `groups[].url` slug) onto one id (spec §3.1)."""
    return f"ransomware-live-group-{_slug(slug_or_name)}"


def _victim_id(permalink_url: str) -> str:
    """``ransomware-live-victim-{_h(permalink_url)}`` — hashes the opaque permalink (the
    `indicator_id` rationale: the URL's path is itself an opaque base64 token, not a readable
    slug)."""
    return f"ransomware-live-victim-{_h(permalink_url)}"


def _claim_id(group_id: str, victim_id: str, permalink_url: str) -> str:
    """``ransomware-live-claim-{_h(group_id + "\\n" + victim_id + "\\n" + permalink_url)}`` — one
    victim record is one claim; converges on re-ingest (spec §3.1)."""
    joined = f"{group_id}\n{victim_id}\n{permalink_url}"
    return f"ransomware-live-claim-{_h(joined)}"


def _is_placeholder_description(value: str) -> bool:
    """True for the recentvictims `description` placeholders: ``""``, ``"N/A"``, and
    ``"[AI generated] N/A"`` (any casing/whitespace) — spec §3.2's omission rule. A leading
    ``"[AI generated]"`` marker is stripped before checking whether the remainder is itself a
    placeholder, so any OTHER `"[AI generated] ..."` text (a real, if AI-authored, description)
    is kept.
    """
    stripped = value.strip()
    if stripped.casefold() in _DESCRIPTION_PLACEHOLDERS:
        return True
    if stripped.casefold().startswith(_AI_GENERATED_PREFIX):
        remainder = stripped[len(_AI_GENERATED_PREFIX) :].strip()
        return remainder.casefold() in _DESCRIPTION_PLACEHOLDERS
    return False


def _record_key(dataset: str, item: Mapping[str, Any]) -> str:
    """The `RawRecord.key` for one raw dict element — the permalink `url` (recentvictims) / the
    url-slug (groups), spec §3.1/§4. Falls back to a deterministic hash of the whole item when
    the expected key field is itself missing/blank (hostile-body tolerance — `collect()` must
    never raise per row; a landed record with a synthesized key is still recoverable, unlike a
    dropped one)."""
    raw_url = item.get("url")
    if isinstance(raw_url, str) and raw_url.strip():
        return _url_slug(raw_url) if dataset == "groups" else raw_url
    return _h(json.dumps(item, sort_keys=True))


class RansomwareLiveConnector(Connector):
    """Imports Ransomware.live's `recentvictims` and `groups` v2 datasets.

    ONE connector serving both, parametrized by `config["dataset"]` (opensanctions precedent).
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store an optional injected `transport` (`httpx.MockTransport` in tests).

        Production instantiation passes no transport (real HTTP via `guarded_stream`); tests
        inject an `httpx.MockTransport` so no live network call is ever made.
        """
        self._transport = transport

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="ransomware_live",
            name="Ransomware.live",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description=(
                "Ransomware.live v2 free datasets: claimed-victim disclosures "
                "(Company + claiming-group Organization + UnknownLink edge) and the "
                "ransomware-group catalog (Organization)."
            ),
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Stream the configured dataset's flat JSON array and yield one `RawRecord` per element.

        Fetched through :func:`guarded_stream` (SSRF-validated, an `api_key` header attached only
        when configured — PRO-ready, free v2 ignores it), `raise_for_status`-checked, then read
        under :data:`_MAX_FEED_BYTES` (an oversized body raises, fail-closed) BEFORE any parsing.
        The body is a flat JSON array (`[record, ...]` — NOT the abuse.ch `{id:[record]}` object
        shape used by the sibling connectors); a non-list top-level value is treated as zero
        records (never raised — an empty/tiny feed, or a throttle-message object body, is not an
        error), and a non-dict array element is skipped (no yield). `limit` (when set) hard-caps
        the number of records yielded, counted in array order. No throttling/sleep logic — a
        single bounded request per `collect()` call, the 1 h global cadence satisfies the
        free-tier rate ceiling structurally (spec §2).
        """
        self.validate_config(config)
        dataset = str(config["dataset"])
        url = str(config.get("url") or _DEFAULT_URLS[dataset])
        limit = config.get("limit")
        api_key = config.get("api_key")
        headers: dict[str, str] | None = (
            {_API_KEY_HEADER: str(api_key)}
            if isinstance(api_key, str) and api_key.strip()
            else None
        )
        retrieved_at = datetime.now(UTC).isoformat()

        with guarded_stream(
            "GET", url, timeout=_HTTP_TIMEOUT, transport=self._transport, headers=headers
        ) as response:
            response.raise_for_status()
            body = self._read_bounded(response)

        parsed: Any = json.loads(body)
        if not isinstance(parsed, list):
            return
        items = cast("list[Any]", parsed)

        count = 0
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            item = cast("dict[str, Any]", raw_item)
            yield RawRecord(
                key=_record_key(dataset, item),
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
                    f"ransomware_live feed body exceeded the {_MAX_FEED_BYTES}-byte cap "
                    "(fail-closed)"
                )
        return bytes(chunks)

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Shape-dispatch one raw record to the victim or group mapping path.

        `map()` receives no config, so dispatch is on the record's own shape (spec §4): a
        non-blank `victim` string is the VICTIM path (Company + thin group Organization +
        UnknownLink edge); else a non-blank `name` string is the GROUP path (one rich
        Organization); else fail-soft `[]` (neither shape).
        """
        entry: Any = json.loads(record.data)
        if not isinstance(entry, dict):
            return []
        entry = cast("dict[str, Any]", entry)

        victim = entry.get("victim")
        if isinstance(victim, str) and victim.strip():
            return self._map_victim(entry, victim.strip(), provenance)

        name = entry.get("name")
        if isinstance(name, str) and name.strip():
            return self._map_group(entry, name.strip(), provenance)

        return []

    def _map_victim(
        self, entry: dict[str, Any], victim_name: str, provenance: Provenance
    ) -> list[FtmEntity]:
        """recentvictims -> victim Company + thin group Organization + UnknownLink edge.

        A record missing either identity field the ids are built from (the permalink `url`, the
        claiming `group`) is fail-soft dropped (`[]`, logged) rather than raising and failing the
        batch — the victim `victim` name alone is not enough to mint deterministic ids.
        """
        permalink = entry.get("url")
        if not isinstance(permalink, str) or not permalink.strip():
            logger.warning(
                "ransomware_live: dropping victim %r — blank/missing permalink url", victim_name
            )
            return []
        group_raw = entry.get("group")
        if not isinstance(group_raw, str) or not group_raw.strip():
            logger.warning(
                "ransomware_live: dropping victim %r — blank/missing claiming group", victim_name
            )
            return []

        victim_id = _victim_id(permalink)
        group_id = _group_id(group_raw)
        claim_id = _claim_id(group_id, victim_id, permalink)

        # -- victim Company: never a topics/risk value (leads, not verdicts) --------------------
        company_properties: dict[str, list[str]] = {"name": [victim_name]}
        domain = entry.get("domain")
        if isinstance(domain, str) and domain.strip():
            company_properties["website"] = [domain]
        country = entry.get("country")
        if isinstance(country, str) and country.strip():
            company_properties["country"] = [country]
        activity = entry.get("activity")
        if (
            isinstance(activity, str)
            and activity.strip()
            and activity.strip().casefold() != _ACTIVITY_NOT_FOUND
        ):
            company_properties["sector"] = [activity]

        company = validate_or_raise(
            {
                "id": victim_id,
                "schema": "Company",
                "properties": company_properties,
                "datasets": [_DATASET_ID],
            }
        )

        # -- thin claiming-group Organization: always crime.cyber (catastrophic-merge park) -----
        group_org = validate_or_raise(
            {
                "id": group_id,
                "schema": "Organization",
                "properties": {
                    "name": [group_raw],
                    "weakAlias": [group_raw],
                    "topics": [_CRIME_CYBER_TOPIC],
                },
                "datasets": [_DATASET_ID],
            }
        )

        # -- UnknownLink edge: the disclaimed allegation itself (G1 — provenance on the edge) ----
        edge_properties: dict[str, list[str]] = {
            "subject": [group_id],
            "object": [victim_id],
            "role": [_CLAIM_ROLE],
        }
        claim_url = entry.get("claim_url")
        if isinstance(claim_url, str) and claim_url.strip():
            edge_properties["sourceUrl"] = [claim_url]
        description = entry.get("description")
        if isinstance(description, str) and not _is_placeholder_description(description):
            edge_properties["summary"] = [description]

        edge = validate_or_raise(
            {
                "id": claim_id,
                "schema": "UnknownLink",
                "properties": edge_properties,
                "datasets": [_DATASET_ID],
            }
        )
        # `entity.add()` (unlike the initial `properties` payload) cleans through the FtM date
        # type: a valid ISO timestamp normalizes; a junk/missing `attackdate` is silently dropped
        # rather than stored or raising (the edge is still emitted, per spec §6).
        edge.add("date", entry.get("attackdate"))

        return [
            stamp(company, provenance),
            stamp(group_org, provenance),
            stamp(edge, provenance),
        ]

    def _map_group(
        self, entry: dict[str, Any], name: str, provenance: Provenance
    ) -> list[FtmEntity]:
        """groups -> one rich group Organization. `added_date`/`tools`/`ttps` are raw-only —
        never mapped onto any FtM property (spec §3.3)."""
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            logger.warning("ransomware_live: dropping group %r — blank/missing url", name)
            return []
        slug = _url_slug(url)
        group_id = _group_id(slug)

        properties: dict[str, list[str]] = {
            "name": [name],
            "weakAlias": [slug],
            "sourceUrl": [url],
            "topics": [_CRIME_CYBER_TOPIC],
        }
        altname = entry.get("altname")
        if isinstance(altname, str) and altname.strip():
            properties["alias"] = [altname]
        description = entry.get("description")
        if isinstance(description, str) and description.strip():
            properties["description"] = [description]

        websites: list[str] = []
        raw_locations = entry.get("locations")
        if isinstance(raw_locations, list):
            for raw_location in cast("list[Any]", raw_locations):
                if not isinstance(raw_location, dict):
                    continue
                location = cast("dict[str, Any]", raw_location)
                location_slug = location.get("slug")
                if isinstance(location_slug, str) and location_slug.strip():
                    websites.append(location_slug)
        if websites:
            properties["website"] = websites

        org = validate_or_raise(
            {
                "id": group_id,
                "schema": "Organization",
                "properties": properties,
                "datasets": [_DATASET_ID],
            }
        )
        return [stamp(org, provenance)]
