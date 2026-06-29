"""Wikidata reference-anchor enricher.

Anchors entities to a Wikidata Q-number (the ``wikidata_id`` canonical anchor):
first from any FtM ``wikidataId`` the source already carries (OpenSanctions often
does), otherwise a best-effort SPARQL lookup by exact English label — a *lead*,
not a verdict. A passive INTERNAL_ENRICHMENT plugin.

All outbound HTTP goes through :func:`worldmonitor.net.ssrf.guarded_stream` (ADR 0057
discipline: ALL outbound HTTP is SSRF-guarded). The Wikimedia UA policy requires a
descriptive ``User-Agent`` header, forwarded via the optional ``headers`` param
added in ADR 0081.
"""

from __future__ import annotations

import json
import re
import urllib.parse

import httpx

from worldmonitor.net.ssrf import BlockedAddressError, guarded_stream
from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, Status

_SPARQL_URL = "https://query.wikidata.org/sparql"
_USER_AGENT = "WorldMonitor/0.1 (+https://github.com/damran/worldmonitor)"
_QID_RE = re.compile(r"Q\d+")

# Headers forwarded with every SPARQL request: Wikimedia UA policy + explicit JSON accept type.
_SPARQL_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "application/sparql-results+json",
}


class WikidataEnricher:
    """Sets the ``wikidata_id`` anchor on an entity (from FtM data or via SPARQL)."""

    def __init__(
        self,
        *,
        timeout: float = 30.0,
        lookup: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a ``WikidataEnricher``.

        Args:
            timeout: HTTP timeout in seconds for SPARQL requests.
            lookup: When ``False``, SPARQL lookup is disabled (only FtM-carried ``wikidataId``
                properties are used). Useful for tests that do not want any outbound requests.
            transport: Optional ``httpx.BaseTransport`` injected for unit tests
                (``httpx.MockTransport``). ``None`` ⇒ real HTTP via ``guarded_stream``.
                Mirrors the pattern used by ``RestApiConnector`` and ``FeedConnector``.
        """
        self._timeout = timeout
        self._lookup_enabled = lookup
        self._transport = transport

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="wikidata",
            name="Wikidata anchor",
            version="0.1.0",
            kind=Kind.ENRICHER,
            mode=Mode.INTERNAL_ENRICHMENT,
            capability=Capability.PASSIVE,
            description="Anchors entities to Wikidata Q-numbers.",
            status=Status.IMPLEMENTED,
        )

    def enrich(self, entity: FtmEntity) -> FtmEntity:
        """Attach the Wikidata Q anchor to ``entity`` if one can be determined."""
        existing = entity.first("wikidataId", quiet=True)
        qid = _normalize_qid(existing) if existing else self._lookup_qid(entity)
        if qid:
            set_anchor(entity, "wikidata_id", qid)
        return entity

    def _lookup_qid(self, entity: FtmEntity) -> str | None:
        if not self._lookup_enabled:
            return None
        name = entity.first("name", quiet=True) or entity.caption
        if not name:
            return None
        query = (
            f'SELECT ?item WHERE {{ ?item rdfs:label "{_escape_literal(name)}"@en }} '
            "ORDER BY ?item LIMIT 1"
        )
        # Build the full URL with query-string params baked in (guarded_stream has no params arg).
        url = _SPARQL_URL + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
        try:
            with guarded_stream(
                "GET",
                url,
                headers=_SPARQL_HEADERS,
                timeout=self._timeout,
                transport=self._transport,
            ) as response:
                response.raise_for_status()
                body = response.read()
            bindings = json.loads(body)["results"]["bindings"]
        except (httpx.HTTPError, BlockedAddressError, KeyError, ValueError):
            return None
        if not bindings:
            return None
        return _normalize_qid(bindings[0]["item"]["value"])


def _normalize_qid(value: str) -> str | None:
    """Extract a bare ``Q\\d+`` id from a value or URL, or ``None``."""
    match = _QID_RE.search(value)
    return match.group(0) if match else None


def _escape_literal(value: str) -> str:
    """Escape a string for safe embedding in a SPARQL string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return escaped.replace("\n", " ").replace("\r", " ").replace("\t", " ")
