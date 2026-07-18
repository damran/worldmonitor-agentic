"""Full-text article bodies for extraction (ADR 0116) — a pull-only, SSRF-guarded driver pass.

For recent, **not-yet-extracted** curated-feed Article nodes this pass fetches the page behind
``sourceUrl`` (through :func:`worldmonitor.net.ssrf.guarded_stream` — never a bare ``httpx``;
every redirect hop re-validated), lands the raw HTML in the landing zone (immutable, replayable:
``fulltext/feeds/<article-id>.html``), and derives a plain-text body into the rebuildable
``article_text`` Postgres cache that the extraction pass joins by entity id. The body deliberately
never touches the graph — ftmg drops long text properties and Neo4j is not a blob store.

Bounds (all settings-driven): articles per cycle, fetches per host per cycle (politeness — we only
fetch pages the source's own feed advertises), bytes per response (truncated, not failed), and
attempts per URL (a dead link stops being refetched once ``attempts >= max_attempts``). Per-article
failures are isolated; one bad page never aborts a cycle.

Selection is coupled to extraction need: only articles still awaiting extraction
(``n.extraction_done IS NULL``) are fetched, so steady-state egress is bounded by the feeds'
arrival rate, exactly like extraction's LLM cost. Best-effort by design — an extraction cycle that
outruns this pass simply processes that article title-only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import lxml.html
from sqlalchemy.orm import Session

from worldmonitor.db.models import ArticleText
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.net.ssrf import guarded_stream

# The single source of truth for the landing-key path sanitizer (same reuse rule as erasure).
from worldmonitor.runner.ingest import _safe_segment  # pyright: ignore[reportPrivateUsage]
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)

# Provenance source id for the derived rows (the article's ORIGINAL source stays on its node).
_FULLTEXT_SOURCE_ID = "fulltext:feeds"
# Stored-body cap — the extraction prompt truncates further (extraction_body_max_chars).
_STORED_TEXT_MAX = 20_000
_ERROR_MAX = 500
# A paragraph shorter than this is boilerplate more often than content (cookie bars, bylines).
_MIN_PARAGRAPH_CHARS = 40
_FETCH_TIMEOUT_SECONDS = 20.0


@dataclass(slots=True)
class FulltextStats:
    """What one full-text cycle did (returned to the driver for the task_run record)."""

    scanned: int = 0
    fetched: int = 0
    empty: int = 0
    errors: int = 0
    host_capped: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "fetched": self.fetched,
            "empty": self.empty,
            "errors": self.errors,
            "host_capped": self.host_capped,
        }


def html_to_text(payload: bytes) -> str:
    """Derive a plain-text body from untrusted page HTML (dependency-free lxml, ADR 0116 §5).

    Drops script/style/nav/chrome subtrees, joins the substantial ``<p>`` paragraphs, and falls
    back to whole-document text when a page has no paragraph markup. The output is DATA for the
    extraction prompt's trust boundary — it is never rendered or executed; hostile markup ends up
    as inert text at worst. Returns ``""`` when nothing parseable/substantial survives.
    """
    if not payload.strip():
        return ""
    try:
        doc = lxml.html.document_fromstring(payload)
    except Exception:  # noqa: BLE001 — hostile/binary bytes: no body, never an exception out
        return ""
    for element in doc.xpath(
        "//script|//style|//noscript|//template|//svg|//nav|//header|//footer|//aside|//form"
    ):
        element.drop_tree()
    paragraphs = [" ".join(p.text_content().split()) for p in doc.xpath("//p")]
    paragraphs = [p for p in paragraphs if len(p) >= _MIN_PARAGRAPH_CHARS]
    if paragraphs:
        return "\n\n".join(paragraphs)[:_STORED_TEXT_MAX]
    fallback = " ".join(doc.text_content().split())
    # Below the paragraph floor the "document text" is mojibake/chrome, not content — lxml will
    # happily wrap binary junk in a document; an empty body beats junk in an extraction prompt.
    if len(fallback) < _MIN_PARAGRAPH_CHARS:
        return ""
    return fallback[:_STORED_TEXT_MAX]


def select_candidates(
    neo4j: Neo4jClient, sessions: Callable[[], Session], *, limit: int, max_attempts: int
) -> list[dict[str, str]]:
    """Recent unextracted curated-feed Articles (id, url) still needing a body fetch.

    Over-selects from the graph (newest ``limit * 4``) then drops ids the ``article_text`` ledger
    already settled: a row with text (done) or with ``attempts >= max_attempts`` (dead URL). The
    per-host cap is applied later, at fetch time, so one prolific host cannot consume the window.
    """
    query = (
        "MATCH (n:Entity) "
        "WHERE 'Article' IN labels(n) AND 'feeds' IN n.datasets AND n.extraction_done IS NULL "
        "AND n.sourceUrl IS NOT NULL "
        "RETURN n.id AS id, head(n.sourceUrl) AS url "
        "ORDER BY coalesce(head(n.publishedAt), head(n.date), n.prov_retrieved_at) DESC "
        f"LIMIT {max(1, int(limit)) * 4}"
    )
    rows = [row for row in neo4j.execute_read(query) if row.get("id") and row.get("url")]
    if not rows:
        return []
    with sessions() as session:
        candidates: list[dict[str, str]] = []
        for row in rows:
            existing = session.get(ArticleText, str(row["id"]))
            if existing is not None and (existing.text or existing.attempts >= max_attempts):
                continue
            candidates.append({"id": str(row["id"]), "url": str(row["url"])})
            if len(candidates) >= limit:
                break
    return candidates


def fetch_page(url: str, *, max_bytes: int, transport: httpx.BaseTransport | None = None) -> bytes:
    """GET ``url`` through the SSRF guard and return at most ``max_bytes`` of the body.

    Over-cap responses are TRUNCATED (not failed): unlike a feed document, a partial page still
    yields useful paragraphs, and a hard fail would burn an attempt on every long page.
    """
    chunks = bytearray()
    with guarded_stream(
        "GET", url, timeout=_FETCH_TIMEOUT_SECONDS, transport=transport
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            room = max_bytes - len(chunks)
            if room <= 0:
                break
            chunks.extend(chunk[:room])
    return bytes(chunks)


def _upsert(
    session: Session,
    *,
    entity_id: str,
    url: str,
    text: str,
    raw_pointer: str,
    retrieved_at: str,
    error: str,
) -> None:
    """Insert-or-update one ledger row (dialect-free; the driver pass is the single writer)."""
    row = session.get(ArticleText, entity_id)
    if row is None:
        row = ArticleText(entity_id=entity_id, url=url)
        session.add(row)
    row.url = url
    row.attempts = (row.attempts or 0) + 1
    row.last_error = error[:_ERROR_MAX]
    row.source_id = _FULLTEXT_SOURCE_ID
    row.retrieved_at = retrieved_at
    if not error:
        row.text = text
        row.raw_pointer = raw_pointer
    session.commit()


def fulltext_cycle(
    *,
    neo4j: Neo4jClient,
    sessions: Callable[[], Session],
    landing: LandingStore,
    max_articles: int,
    max_per_host: int,
    max_attempts: int,
    max_fetch_bytes: int,
    retrieved_at: str,
    transport: httpx.BaseTransport | None = None,
) -> FulltextStats:
    """Run one bounded full-text cycle: select → fetch → land raw → derive text → upsert ledger."""
    stats = FulltextStats()
    per_host: dict[str, int] = {}
    candidates = select_candidates(neo4j, sessions, limit=max_articles, max_attempts=max_attempts)
    for candidate in candidates:
        article_id, url = candidate["id"], candidate["url"]
        host = urlsplit(url).netloc.lower()
        if per_host.get(host, 0) >= max_per_host:
            stats.host_capped += 1
            continue
        per_host[host] = per_host.get(host, 0) + 1
        stats.scanned += 1
        try:
            payload = fetch_page(url, max_bytes=max_fetch_bytes, transport=transport)
            raw_pointer = landing.put(
                f"fulltext/feeds/{_safe_segment(article_id)}.html",
                payload,
                content_type="text/html",
            )
            text = html_to_text(payload)
            with sessions() as session:
                _upsert(
                    session,
                    entity_id=article_id,
                    url=url,
                    text=text,
                    raw_pointer=raw_pointer,
                    retrieved_at=retrieved_at,
                    error="",
                )
            stats.fetched += 1
            if not text:
                stats.empty += 1
        except Exception as exc:  # noqa: BLE001 — one page's failure must not abort the cycle
            logger.warning("fulltext fetch failed for %s (%s): %s", article_id, url, exc)
            stats.errors += 1
            try:
                with sessions() as session:
                    _upsert(
                        session,
                        entity_id=article_id,
                        url=url,
                        text="",
                        raw_pointer="",
                        retrieved_at=retrieved_at,
                        error=f"{type(exc).__name__}: {exc}",
                    )
            except Exception:  # noqa: BLE001 — the ledger write itself failing is only loggable
                logger.exception("fulltext ledger write failed for %s", article_id)
    return stats


def load_body(sessions: Callable[[], Session], entity_id: str, *, max_chars: int) -> str | None:
    """The cached body for one article, prompt-truncated — ``None`` when absent/empty.

    Defensive on shape (test doubles may hand back non-string attributes): only a real, non-empty
    ``str`` body is returned.
    """
    try:
        with sessions() as session:
            row: Any = session.get(ArticleText, entity_id)
    except Exception:  # noqa: BLE001 — a cache miss must never break extraction
        return None
    text = getattr(row, "text", None)
    if isinstance(text, str) and text.strip():
        return text[: max(1, int(max_chars))]
    return None
