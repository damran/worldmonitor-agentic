"""Base connector for paginated JSON REST APIs (ADR 0065).

Where ``ftm_bulk.py`` serves sources that publish a single bulk dump, many sources expose a
**page-based JSON** search API instead (OpenCorporates, and future REST feeds). This base
implements ``collect()`` ONCE for that shape — fetch page 1..N over the SSRF guard, read each page
under a hard byte cap, extract its item list, and yield one ``RawRecord`` per item — and leaves the
source specifics to four small hooks (``_page_url`` / ``_extract_items`` / ``_total_pages`` /
``_record_key``). ``map()`` stays abstract (each source maps its own FtM/STIX entities).

Safety (the locked invariants):

* **Every fetch goes through** :func:`worldmonitor.net.ssrf.guarded_stream` — never a bare ``httpx``
  call to an attacker-influenced host (a redirect/DNS answer to an internal address is blocked).
* **Pagination is hard-bounded** by ``max_pages`` — a payload advertising 999 ``total_pages`` still
  stops at the configured cap.
* **Each page body is read under** :data:`_MAX_RESPONSE_BYTES` — a hostile, oversized body raises
  (fail-closed) instead of being read unbounded into memory.
* **The token is never logged** — the request URL carries the ``api_token`` secret, so this base
  logs only the page number / item count, never the URL.
"""

from __future__ import annotations

import json
import logging
from abc import abstractmethod
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from worldmonitor.net.ssrf import guarded_stream
from worldmonitor.plugins.base import Connector, RawRecord

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 60.0
# Default page cap when a config omits ``max_pages`` (the schema default is the real source of
# truth; jsonschema does not inject defaults, so the base falls back defensively).
_DEFAULT_MAX_PAGES = 5
# Per-page hostile-body bound: a single page response over this many bytes is refused (fail-closed)
# rather than read unbounded into memory. 8 MiB comfortably exceeds a max-size JSON page.
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class RestApiConnector(Connector):
    """Connector base for page-based JSON REST APIs (collect once; map per source)."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Store an optional injected ``transport`` (``httpx.MockTransport`` in tests).

        Production instantiation passes no transport (real HTTP via ``guarded_stream``); tests
        inject an ``httpx.MockTransport`` so no live network call is ever made.
        """
        self._transport = transport

    def collect(self, config: Mapping[str, Any]) -> Iterator[RawRecord]:
        """Yield one raw record per item across ``1..min(total_pages, max_pages)`` pages.

        Each page is fetched through :func:`guarded_stream` (SSRF-validated), checked with
        ``raise_for_status`` (a 401/5xx fails loud), and read under :data:`_MAX_RESPONSE_BYTES`
        (an oversized body raises). ``total_pages`` is captured from page 1; pagination is
        hard-capped by ``max_pages`` and stops on an empty page. The token URL is never logged.
        """
        self.validate_config(config)
        retrieved_at = datetime.now(UTC).isoformat()
        max_pages = int(config.get("max_pages", _DEFAULT_MAX_PAGES))

        total_pages = max_pages
        page = 1
        while True:
            url = self._page_url(config, page)
            with guarded_stream(
                "GET", url, timeout=_HTTP_TIMEOUT, transport=self._transport
            ) as response:
                response.raise_for_status()
                body = self._read_bounded(response)
            payload = json.loads(body)
            items = self._extract_items(payload)
            if page == 1:
                total_pages = self._total_pages(payload)
            # Log progress with the page number + item count ONLY — never the URL (it carries the
            # api_token secret).
            logger.debug("rest_api: page %d yielded %d item(s)", page, len(items))
            for item in items:
                yield RawRecord(
                    key=self._record_key(item),
                    data=json.dumps(item).encode("utf-8"),
                    retrieved_at=retrieved_at,
                    content_type="application/json",
                )
            if not items or page >= min(total_pages, max_pages):
                break
            page += 1

    @staticmethod
    def _read_bounded(response: httpx.Response) -> bytes:
        """Read the streaming body under :data:`_MAX_RESPONSE_BYTES`, raising if it exceeds the cap.

        Iterates ``iter_bytes`` accumulating chunks (never ``.read()``/``.text`` unbounded). A body
        over the cap raises :class:`ValueError` (fail-closed against a hostile, oversized response)
        before it is parsed.
        """
        chunks = bytearray()
        for chunk in response.iter_bytes():
            chunks.extend(chunk)
            if len(chunks) > _MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"response body exceeded the {_MAX_RESPONSE_BYTES}-byte cap (fail-closed)"
                )
        return bytes(chunks)

    @abstractmethod
    def _page_url(self, config: Mapping[str, Any], page: int) -> str:
        """Build the request URL for ``page`` (including query params + the api_token)."""

    @abstractmethod
    def _extract_items(self, payload: Any) -> list[dict[str, Any]]:
        """Return the list of per-item objects from a parsed page ``payload``."""

    @abstractmethod
    def _total_pages(self, payload: Any) -> int:
        """Return the total page count advertised by a parsed page ``payload``."""

    @abstractmethod
    def _record_key(self, item: Mapping[str, Any]) -> str:
        """Return the stable source key for one ``item`` (the landing-zone record key)."""
