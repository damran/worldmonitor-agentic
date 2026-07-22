"""SSLBL connector — the abuse.ch SSL Certificate Blacklist, mapped to `wm:Indicator` (ADR 0119).

abuse.ch's SSLBL publishes a free, anonymous, **CC0** (public-domain) CSV export of SHA1
certificate fingerprints seen in malware/botnet C2 traffic: an unquoted
``Listingdate,SHA1,Listingreason`` table with ``#``-comment lines. Unlike its two siblings in
this gate (``threatfox``, ``urlhaus`` — both governed by the abuse.ch AG Terms of Use fair-use
grant), SSLBL is explicitly **CC0**: no license restriction beyond attribution courtesy (spec §7,
ADR 0119 §Consequences "Licensing posture"). Each row becomes exactly one FtM ``Indicator``
(``ontology/schema/wm/Indicator.yaml``): a non-matchable, deterministic-id-only node that
converges on re-ingest — from THIS connector or any sibling (``feodo``, ``threatfox``,
``urlhaus``) — via the shared ``ioc-<sha1(value)>`` scheme
(``worldmonitor.ontology.ioc.indicator_id``, S-2b), never by fuzzy resolution. CTI infrastructure
indicators must never enter the person/org merge path (CLAUDE.md catastrophic-merge guard).

``collect()`` streams the CSV over the SSRF guard (bounded to ``_MAX_FEED_BYTES``, feodo/
threatfox/urlhaus ``_read_bounded`` idiom) and iterates it line by line: a ``#``-prefixed line is
a comment (the live 2026-07-22 export's own column header is itself such a line —
``# Listingdate,SHA1,Listingreason`` — inside a leading comment block, so skipping ``#`` lines
already disposes of it) and is skipped without yielding; a bare, non-comment
``Listingdate,SHA1,Listingreason`` header row is a DEFENSIVE guard against a future format change
and is also skipped (spec §7/§10's own hostile-variant list) — detected by the first parsed field
literally reading ``"listingdate"`` case-insensitively, which no genuine data row's timestamp
value could ever equal. Every other non-blank line is split on ``,`` with ``maxsplit=2``: the
first token is ``Listingdate``, the second is ``SHA1``, and the THIRD is the remainder of the
line rejoined verbatim — so a ``Listingreason`` containing an internal comma (e.g. a hypothetical
``"Cobalt, Strike C&C"``) survives intact rather than being truncated at the first embedded comma
(date and SHA1 never contain commas, so this is unambiguous). One ``RawRecord`` is yielded per
data row, in order; ``limit`` hard-caps the yield count. An optional ``auth_key`` config value
rides as the ``Auth-Key`` HTTP header (harmless if the CC0 endpoint ignores it, and future-proofing
against a hypothetical gating); a 401/403 response is NOT swallowed as an empty feed, it raises
loud with an actionable message naming the header/registration path (spec §8).

``map()`` turns each row into an ``Indicator`` with provenance. ``value`` is the ``SHA1`` field —
required to be exactly 40 hex characters (spec §7: "``value = SHA1`` (40-hex, non-blank) else
``[]``"); a blank OR malformed (wrong length, or containing a non-hex character) value fails soft
to ``[]`` rather than minting an id from a corrupted/truncated value, which would silently break
cross-connector convergence for that value's TRUE fingerprint (the load-bearing invariant of this
whole gate, spec §1). ``indicatorType`` is unconditionally ``["sha1_cert"]`` — deliberately
DISTINCT from ThreatFox's file-hash ``["sha1"]`` (spec §3): a certificate fingerprint is not a
file hash, and conflating the two would silently merge unrelated evidence classes under one
label. The shared ``ontology.ioc.indicator_id`` scheme casefold-normalizes before hashing, so an
uppercase SHA1 converges on the same node as its lowercase form.

``malwareFamily`` is parsed from ``Listingreason`` per the verified live-probed nuances (spec §7):

- a reason ending in ``" C&C"`` (case-insensitive) → family = the text before that suffix
  (e.g. ``"RatonRAT C&C"`` → ``"RatonRAT"``);
- else a reason ending in ``" malware distribution"`` (case-insensitive) → family = the text
  before that suffix (e.g. ``"NetSupport RAT malware distribution"`` → ``"NetSupport RAT"``;
  ``"ACRStealer malware distribution"`` → ``"ACRStealer"`` — no C2 semantics, but still a real
  family);
- else → no family;
- the generic bare token ``"Malware"`` is EXCLUDED (case-insensitive) even when it is what the
  suffix-stripping would otherwise extract (``"Malware C&C"`` / ``"Malware distribution"`` →
  no family — abuse.ch's own catch-all label, not a real family name);
- a BARE reason with no recognized suffix is left with NO family, never guessed. The live feed's
  bare reasons are heterogeneous — ``"FIN7"`` is a threat-ACTOR name (not a malware family),
  ``"Dridex"`` is a genuine family name but carries no C2/distribution suffix to key off of,
  ``"KINS MITM"`` mixes a family token with an attack-technique word, and ``"QuasarRAT"`` is a
  family with no suffix — guessing which of these are "really" families would be exactly the kind
  of invented certainty CLAUDE.md's "leads, not verdicts" principle forbids. The Indicator is
  still emitted in every case (the SHA1 value itself is real evidence regardless of whether a
  family could be safely inferred).

``firstSeenAt`` comes from ``Listingdate`` via ``entity.add()`` (FtM date cleaning, ISO-
normalized, feodo/threatfox precedent). ``lastSeenAt`` is **never** set at all (not merely usually
absent) — the CSV carries no last-seen column, so there is nothing to even attempt to add.
``datasets = ["sslbl"]``; no ``topics``, no ``country``, no ``indicates`` edge — attribution
(family → threat actor) is the designated S-2 phase 3 enricher, out of scope here. This is a
passive ``EXTERNAL_IMPORT`` connector; it never writes to the graph.
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

# Default feed (anonymous, CC0, abuse.ch — spec §7). Config-overridable via `url` (feodo/
# threatfox/urlhaus precedent).
_DEFAULT_URL = "https://sslbl.abuse.ch/blacklist/sslblacklist.csv"
_HTTP_TIMEOUT = 30.0
# Hostile-body bound: the real CSV is ~771 KB in practice, comfortably under this cap — but a
# hostile/corrupted response must still be refused (fail-closed) rather than read unbounded into
# memory (feodo/threatfox/urlhaus `_read_bounded` idiom, 16 MiB).
_MAX_FEED_BYTES = 16 * 1024 * 1024

# A valid SHA1 hex digest: exactly 40 hex characters (spec §7's own format qualifier — see the
# module docstring for why a malformed value must fail-soft rather than mint a wrong id).
_SHA1_HEX_RE = re.compile(r"^[0-9a-fA-F]{40}$")

# `Listingreason` suffix rules (spec §7 — leading space is the separator between an arbitrary
# family prefix and the suffix words themselves; matched case-insensitively, prefix case
# preserved). Checked in this order: C&C first, then the (no-C2-semantics) distribution suffix.
_FAMILY_SUFFIX_CC = " C&C"
_FAMILY_SUFFIX_DISTRIBUTION = " malware distribution"
# abuse.ch's own generic/catch-all token — never a real family name (case-insensitive exclusion).
_GENERIC_FAMILY_TOKENS = frozenset({"malware"})


def _parse_malware_family(reason: object) -> str | None:
    """Extract a `malwareFamily` from a raw `Listingreason` value, or `None` if none applies.

    See the module docstring for the full rule table. Conservative by construction: any shape
    that isn't unambiguously a `"<family> C&C"` / `"<family> malware distribution"` pair (with
    the generic `"Malware"` token excluded either way) yields `None` rather than a guess.
    """
    if not isinstance(reason, str):
        return None
    stripped = reason.strip()
    if not stripped:
        return None
    lowered = stripped.casefold()
    if lowered.endswith(_FAMILY_SUFFIX_CC.casefold()):
        candidate = stripped[: -len(_FAMILY_SUFFIX_CC)]
    elif lowered.endswith(_FAMILY_SUFFIX_DISTRIBUTION.casefold()):
        candidate = stripped[: -len(_FAMILY_SUFFIX_DISTRIBUTION)]
    else:
        return None
    candidate = candidate.strip()
    if not candidate or candidate.casefold() in _GENERIC_FAMILY_TOKENS:
        return None
    return candidate


class SslblAuthError(RuntimeError):
    """Raised when SSLBL responds 401/403 — the anonymous CC0 export appears gated.

    Deliberately NOT swallowed as an empty feed (an empty-but-200 body is fine and expected on a
    quiet window; a 401/403 is a distinct, actionable operator condition).
    """


class SslblConnector(Connector):
    """Imports the abuse.ch SSLBL SSL Certificate Blacklist CSV as `wm:Indicator` entities."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store an optional injected `transport` (`httpx.MockTransport` in tests).

        Production instantiation passes no transport (real HTTP via `guarded_stream`); tests
        inject an `httpx.MockTransport` so no live network call is ever made.
        """
        self._transport = transport

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="sslbl",
            name="SSLBL",
            version="0.1.0",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            description=(
                "abuse.ch SSLBL SSL Certificate Blacklist (CC0); one wm:Indicator per SHA1 "
                "certificate fingerprint."
            ),
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        schema_text = resources.files(__package__).joinpath("config.schema.json").read_text("utf-8")
        result: dict[str, Any] = json.loads(schema_text)
        return result

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Stream the CSV over the SSRF guard and yield one `RawRecord` per data row.

        Fetched through :func:`guarded_stream` (SSRF-validated, `Auth-Key` header attached only
        when `auth_key` is configured), checked for a 401/403 BEFORE the generic
        `raise_for_status` (so the raised message is actionable rather than a bare status line),
        then read under :data:`_MAX_FEED_BYTES` (an oversized body raises, fail-closed). The body
        is decoded and iterated line by line: a `#`-prefixed line (comment, including the live
        export's own commented column header) and a bare `Listingdate,...` header row are both
        skipped without yielding; every other non-blank line is split on `,` with `maxsplit=2` so
        an internal comma inside `Listingreason` survives intact. A malformed line (fewer than
        three comma-separated fields) is skipped — hostile-body tolerance is entirely `collect()`'s
        job, per-row identity/format rejection is `map()`'s job. `limit` (when set) hard-caps the
        number of records yielded, counted in traversal order.
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
                raise SslblAuthError(
                    f"sslbl: {response.status_code} — the anonymous CC0 export appears gated; "
                    "register a free Auth-Key at auth.abuse.ch and set the connector's `auth_key`"
                )
            response.raise_for_status()
            body = self._read_bounded(response)

        text = body.decode("utf-8", errors="replace")

        count = 0
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(",", 2)
            if len(parts) != 3:
                continue
            listing_date, sha1, listing_reason = parts
            if listing_date.strip().casefold() == "listingdate":
                # Defensive skip: a bare, non-comment header row (spec §7/§10's hostile-variant
                # list) — no genuine data row's Listingdate value could ever equal this literal.
                continue
            row = {"Listingdate": listing_date, "SHA1": sha1, "Listingreason": listing_reason}
            key = sha1 if sha1.strip() else str(count)
            yield RawRecord(
                key=key,
                data=json.dumps(row).encode("utf-8"),
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
                    f"sslbl feed body exceeded the {_MAX_FEED_BYTES}-byte cap (fail-closed)"
                )
        return bytes(chunks)

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        """Map one raw sslbl row to an FtM `Indicator` with its deterministic id + provenance.

        A blank or malformed (non-40-hex) `SHA1` — the identity field an Indicator cannot be
        built without — is dropped (`[]`, fail-soft) rather than raising and failing the batch.
        The entity id comes from the SHARED connector-independent scheme
        (`worldmonitor.ontology.ioc.indicator_id`), so a re-ingest of the same fingerprint — from
        THIS or any sibling connector — converges on the same node by identity; the scheme's own
        casefold normalization means an uppercase SHA1 converges on the same id as its lowercase
        form. `indicatorType` is unconditionally `["sha1_cert"]` (spec §3 — DISTINCT from
        ThreatFox's file-hash `["sha1"]`). `malwareFamily` is parsed from `Listingreason` via
        :func:`_parse_malware_family` (see the module docstring for the full rule table).
        `firstSeenAt` is added via `entity.add()` (not the initial `properties` payload) so FtM's
        own date-type cleaning normalizes the feed's timestamp to ISO-T without ever raising.
        `lastSeenAt` is NEVER set — the CSV carries no last-seen column. No `topics`, no
        `country`, no `indicates` edge — attribution is S-2 phase 3, out of scope for this gate.
        """
        entry: Any = json.loads(record.data)
        if not isinstance(entry, dict):
            return []
        entry = cast("dict[str, Any]", entry)

        raw_value = entry.get("SHA1")
        if not isinstance(raw_value, str) or not _SHA1_HEX_RE.match(raw_value.strip()):
            logger.warning("sslbl: dropping entry with a blank/malformed (non-40-hex) SHA1")
            return []
        value = raw_value.strip()

        entity_id = indicator_id(value)

        properties: dict[str, list[str]] = {
            "name": [value],
            "indicatorValue": [value],
            "indicatorType": ["sha1_cert"],
        }

        family = _parse_malware_family(entry.get("Listingreason"))
        if family is not None:
            properties["malwareFamily"] = [family]

        entity = validate_or_raise(
            {
                "id": entity_id,
                "schema": "Indicator",
                "properties": properties,
                "datasets": ["sslbl"],
            }
        )
        # `entity.add()` (unlike the initial `properties` payload) cleans through the FtM date
        # type: a valid "YYYY-MM-DD HH:MM:SS" value normalizes to ISO-T. `lastSeenAt` is
        # intentionally never touched — the sslbl CSV has no last-seen column at all.
        entity.add("firstSeenAt", entry.get("Listingdate"))

        return [stamp(entity, provenance)]
