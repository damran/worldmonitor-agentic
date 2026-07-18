"""MITRE ATT&CK connector — the intrusion-set catalog, anchored on `mitre_gid` (ADR 0117).

MITRE's ``attack-stix-data`` repository publishes versioned STIX 2.1 bundles for the ATT&CK
Enterprise matrix (free, pull-only, attribution-licensed). Each ``intrusion-set`` object is a
named threat-actor group (a G-id, e.g. ``G0032``) with a name + aliases — exactly the substrate
the dashboard's news-extracted actor candidates and future IOC feeds (S-2) need to resolve onto.
``collect()`` streams the PINNED versioned bundle (never the floating ``enterprise-attack.json``,
which changes underneath a fixed config) over the SSRF guard and yields one raw record per
non-revoked, non-deprecated intrusion set, keyed by its G-id. ``map()`` turns each into an FtM
``Organization`` carrying the ``mitre_gid`` canonical anchor (ADR 0117 D1) + provenance — no FtM
``topics`` are stamped, because catalog membership is not a risk verdict (leads, not verdicts,
CLAUDE.md). This is a passive ``EXTERNAL_IMPORT`` connector; it never writes to the graph.
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
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.ontology.validation import validate_or_raise
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, RawRecord, Status
from worldmonitor.plugins.ftm_bulk import FtmBulkConnector
from worldmonitor.provenance.model import Provenance, stamp

logger = logging.getLogger(__name__)

# PINNED versioned release (never the floating `enterprise-attack.json`, which is silently
# rewritten in place on every ATT&CK release). Resolved 2026-07-18 via the GitHub contents API
# against mitre-attack/attack-stix-data (`enterprise-attack/`) — the highest `x_mitre_version`
# published at the time (19.1); verified with a real HEAD + first-bytes fetch. Bump manually
# (config-overridable via `url`) when a newer release matters (ADR 0117 residual (c)).
_DEFAULT_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/"
    "enterprise-attack/enterprise-attack-19.1.json"
)
_HTTP_TIMEOUT = 120.0
# Hostile-body bound (feeds connector's `_MAX_FEED_BYTES` idiom): the bundle is ~40-60 MB, so a
# response over this cap is refused (fail-closed) rather than read unbounded into memory.
_MAX_BUNDLE_BYTES = 256 * 1024 * 1024

# The G-id validity shape (ADR 0117 D1 / spec `_is_gid`): a literal "G" + exactly 4 digits.
_GID_RE = re.compile(r"^G\d{4}$")

_MITRE_SOURCE_NAME = "mitre-attack"


def _extract_gid(obj: Mapping[str, Any]) -> str | None:
    """The G-id from the FIRST `external_references` entry sourced `mitre-attack`, else `None`.

    Returns the RAW `external_id` verbatim (unvalidated) — `map()` applies the validity gate
    before ever deriving an anchor from it; a record without a usable mitre-attack reference (or
    a non-string/empty `external_id`) yields `None`.
    """
    refs = obj.get("external_references")
    if not isinstance(refs, list):
        return None
    for raw_ref in cast("list[Any]", refs):
        if not isinstance(raw_ref, dict):
            continue
        ref = cast("dict[str, Any]", raw_ref)
        if ref.get("source_name") == _MITRE_SOURCE_NAME:
            external_id = ref.get("external_id")
            return external_id if isinstance(external_id, str) and external_id else None
    return None


def _is_eligible_intrusion_set(obj: Mapping[str, Any]) -> bool:
    """An `intrusion-set` object that is neither `revoked` nor `x_mitre_deprecated`."""
    return (
        obj.get("type") == "intrusion-set"
        and not obj.get("revoked", False)
        and not obj.get("x_mitre_deprecated", False)
    )


class MitreAttackConnector(FtmBulkConnector):
    """Imports the ATT&CK Enterprise intrusion-set catalog.

    One FtM ``Organization`` per non-revoked, non-deprecated intrusion set, ``mitre_gid``-anchored.
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
            connector_id="mitre_attack",
            name="MITRE ATT&CK",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description=(
                "MITRE ATT&CK Enterprise intrusion-set catalog; named threat actors "
                "anchored on their G-id."
            ),
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Stream the pinned bundle and yield one `RawRecord` per eligible intrusion set.

        Fetched through :func:`guarded_stream` (SSRF-validated), checked with `raise_for_status`
        (a 4xx/5xx fails loud), and read under :data:`_MAX_BUNDLE_BYTES` (an oversized body
        raises, fail-closed). Revoked / `x_mitre_deprecated` / non-`intrusion-set` objects are
        never yielded. `limit` (when set) hard-caps the number of records yielded, counted over
        ELIGIBLE objects only, in bundle order.
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

        bundle: Any = json.loads(body)
        raw_objects: Any = (
            cast("dict[str, Any]", bundle).get("objects") if isinstance(bundle, dict) else None
        )
        objects = cast("list[Any]", raw_objects) if isinstance(raw_objects, list) else []

        count = 0
        for raw_obj in objects:
            if not isinstance(raw_obj, dict):
                continue
            obj = cast("dict[str, Any]", raw_obj)
            if not _is_eligible_intrusion_set(obj):
                continue
            gid = _extract_gid(obj) or str(obj.get("id", ""))
            yield RawRecord(
                key=gid,
                data=json.dumps(obj).encode("utf-8"),
                retrieved_at=retrieved_at,
                content_type="application/json",
            )
            count += 1
            if limit is not None and count >= int(limit):
                break

    @staticmethod
    def _read_bounded(response: httpx.Response) -> bytes:
        """Read the streaming body under :data:`_MAX_BUNDLE_BYTES`, raising if it exceeds the cap.

        Iterates `iter_bytes` accumulating chunks (never `.read()`/`.text` unbounded). A body
        over the cap raises :class:`ValueError` (fail-closed against a hostile, oversized
        response) before it is parsed.
        """
        chunks = bytearray()
        for chunk in response.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > _MAX_BUNDLE_BYTES:
                raise ValueError(
                    f"mitre_attack bundle body exceeded the {_MAX_BUNDLE_BYTES}-byte cap "
                    "(fail-closed)"
                )
        return bytes(chunks)

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Map one raw `intrusion-set` STIX object to an FtM Organization with its `mitre_gid`
        anchor + provenance.

        A record with no `mitre-attack`-sourced `external_references` entry, or whose
        `external_id` fails the `^G\\d{4}$` G-id shape, is identity-less for this namespace and
        dropped (`[]`, fail-soft) rather than raising and failing the batch. `alias` carries the
        object's `aliases`, minus the primary `name` (never duplicated into the alias list). No
        FtM `topics` are stamped — catalog membership is not a risk verdict.
        """
        obj = json.loads(record.data)
        gid = _extract_gid(obj)
        if not gid or not _GID_RE.fullmatch(gid):
            logger.warning(
                "mitre_attack: dropping intrusion-set %r — no valid mitre-attack G-id "
                "(external_id=%r)",
                obj.get("id"),
                gid,
            )
            return []

        name = str(obj.get("name") or "").strip()
        if not name:
            logger.warning("mitre_attack: dropping intrusion-set %r — missing name", gid)
            return []

        raw_aliases = obj.get("aliases")
        alias_candidates = cast("list[Any]", raw_aliases) if isinstance(raw_aliases, list) else []
        aliases: list[str] = []
        for alias in alias_candidates:
            if isinstance(alias, str) and alias and alias != name and alias not in aliases:
                aliases.append(alias)

        entity = validate_or_raise(
            {
                "id": f"mitre-{gid}",
                "schema": "Organization",
                "properties": {"name": [name], "alias": aliases},
                "datasets": ["mitre_attack"],
            }
        )
        set_anchor(entity, "mitre_gid", gid)
        return [stamp(entity, provenance)]
