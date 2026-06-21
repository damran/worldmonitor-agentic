"""Wikidata reference-anchor enricher.

Anchors entities to a Wikidata Q-number (the ``wikidata_id`` canonical anchor):
first from any FtM ``wikidataId`` the source already carries (OpenSanctions often
does), otherwise a best-effort SPARQL lookup by exact English label — a *lead*,
not a verdict. A passive INTERNAL_ENRICHMENT plugin.
"""

from __future__ import annotations

import re

import httpx

from worldmonitor.ontology.anchors import set_anchor
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import Capability, Kind, Manifest, Mode, Status

_SPARQL_URL = "https://query.wikidata.org/sparql"
_USER_AGENT = "WorldMonitor/0.1 (+https://github.com/damran/worldmonitor)"
_QID_RE = re.compile(r"Q\d+")


class WikidataEnricher:
    """Sets the ``wikidata_id`` anchor on an entity (from FtM data or via SPARQL)."""

    def __init__(self, *, timeout: float = 30.0, lookup: bool = True) -> None:
        self._timeout = timeout
        self._lookup_enabled = lookup

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
        try:
            response = httpx.get(
                _SPARQL_URL,
                params={"query": query, "format": "json"},
                headers={"User-Agent": _USER_AGENT, "Accept": "application/sparql-results+json"},
                timeout=self._timeout,
            )
            response.raise_for_status()
            bindings = response.json()["results"]["bindings"]
        except (httpx.HTTPError, KeyError, ValueError):
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
