"""Full-text article-body pass (ADR 0116) — hermetic unit suite.

No real DNS, HTTP, MinIO, Neo4j, or Postgres: ``socket.getaddrinfo`` is monkeypatched (the SSRF
guard resolves every host to a public IP), ``httpx.MockTransport`` serves the page bytes, a stub
landing store records puts, a duck-typed Neo4j stub returns candidate rows, and the ledger runs on
in-memory SQLite. Pins: text derivation drops chrome/boilerplate and survives hostile bytes; the
cycle lands raw HTML + upserts the ledger; settled rows (text present / attempts exhausted) are
never refetched; failures burn an attempt with ``last_error`` and never abort the cycle; the
per-host cap holds; ``load_body`` is defensive.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ArticleText
from worldmonitor.runner.fulltext import (
    fulltext_cycle,
    html_to_text,
    load_body,
)

_RETRIEVED_AT = "2026-07-18T00:00:00Z"

_PAGE = b"""
<html><head><title>t</title><style>p{color:red}</style></head><body>
<nav><p>Home About Contact Subscribe Newsletter Careers and other navigation links</p></nav>
<script>var tracking = "should never appear in output";</script>
<article>
<p>The first substantial paragraph of the article body, describing what actually happened on the
ground in considerable detail.</p>
<p>ok</p>
<p>A second substantial paragraph with further reporting, quotes, and context for the event that
the extraction model should get to see.</p>
</article>
<footer><p>Copyright legal boilerplate footer text long enough to pass the gate.</p></footer>
</body></html>
"""


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _sqlite_sessions() -> tuple[Any, sessionmaker[Session]]:
    engine = make_engine("sqlite:///:memory:")
    create_all(engine)
    return engine, session_factory(engine)


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


class _StubNeo4j:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def execute_read(self, _query: str, **_params: Any) -> list[dict[str, Any]]:
        return self.rows


class _StubLanding:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes, str]] = []

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        self.puts.append((key, data, content_type))
        return f"s3://landing/{key}"


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------------------------------
# html_to_text — untrusted-bytes text derivation.
# --------------------------------------------------------------------------------------------------
def test_html_to_text_keeps_paragraphs_drops_chrome() -> None:
    text = html_to_text(_PAGE)
    assert "first substantial paragraph" in text
    assert "second substantial paragraph" in text
    assert "tracking" not in text  # script dropped
    assert "navigation links" not in text  # nav dropped
    assert "boilerplate footer" not in text  # footer dropped
    assert "\nok\n" not in text and not text.startswith("ok")  # short paragraph dropped


def test_html_to_text_falls_back_to_document_text_without_paragraphs() -> None:
    bare = b"<html><body><div>just bare text, long enough to clear the boilerplate floor easily"
    assert "just bare text" in html_to_text(bare + b"</div></body></html>")
    # ...but a sub-floor fallback (mojibake, chrome scraps) yields an empty body, never junk.
    assert html_to_text(b"<html><body><div>tiny scrap</div></body></html>") == ""


def test_html_to_text_survives_hostile_or_binary_bytes() -> None:
    assert html_to_text(b"") == ""
    assert html_to_text(b"\x00\xff\xfe\x01\x02") == ""
    assert isinstance(html_to_text(b"<<<>>>%%%<not html"), str)  # never raises


# --------------------------------------------------------------------------------------------------
# fulltext_cycle — fetch, land, upsert, bounds, isolation.
# --------------------------------------------------------------------------------------------------
def _cycle(
    rows: list[dict[str, Any]],
    sessions: sessionmaker[Session],
    landing: _StubLanding,
    handler: Callable[[httpx.Request], httpx.Response],
    **overrides: Any,
) -> Any:
    kwargs: dict[str, Any] = {
        "neo4j": _StubNeo4j(rows),
        "sessions": sessions,
        "landing": landing,
        "max_articles": 10,
        "max_per_host": 5,
        "max_attempts": 3,
        "max_fetch_bytes": 1_000_000,
        "retrieved_at": _RETRIEVED_AT,
        "transport": _transport(handler),
    }
    kwargs.update(overrides)
    return fulltext_cycle(**kwargs)


def test_cycle_lands_raw_and_upserts_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    _engine, sessions = _sqlite_sessions()
    landing = _StubLanding()
    stats = _cycle(
        [{"id": "feed-a1", "url": "https://news.example.com/story"}],
        sessions,
        landing,
        lambda _req: httpx.Response(200, content=_PAGE),
    )
    assert stats.fetched == 1 and stats.errors == 0
    assert landing.puts and landing.puts[0][0] == "fulltext/feeds/feed-a1.html"
    with sessions() as session:
        row = session.get(ArticleText, "feed-a1")
        assert row is not None
        assert "first substantial paragraph" in row.text
        assert row.raw_pointer == "s3://landing/fulltext/feeds/feed-a1.html"
        assert row.attempts == 1 and row.last_error == ""
        assert row.source_id == "fulltext:feeds"


def test_cycle_skips_rows_already_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row with text (done) or with attempts >= max (dead URL) is never refetched."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    _engine, sessions = _sqlite_sessions()
    with sessions() as session:
        session.add(ArticleText(entity_id="feed-done", url="u", text="already have it"))
        session.add(ArticleText(entity_id="feed-dead", url="u", text="", attempts=3))
        session.commit()
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, content=_PAGE)

    stats = _cycle(
        [
            {"id": "feed-done", "url": "https://a.example.com/x"},
            {"id": "feed-dead", "url": "https://a.example.com/y"},
        ],
        sessions,
        _StubLanding(),
        handler,
    )
    assert stats.scanned == 0 and not calls


def test_cycle_failure_burns_an_attempt_and_never_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    _engine, sessions = _sqlite_sessions()

    def handler(req: httpx.Request) -> httpx.Response:
        if "bad" in str(req.url):
            return httpx.Response(500)
        return httpx.Response(200, content=_PAGE)

    stats = _cycle(
        [
            {"id": "feed-bad", "url": "https://a.example.com/bad"},
            {"id": "feed-good", "url": "https://b.example.com/good"},
        ],
        sessions,
        _StubLanding(),
        handler,
    )
    assert stats.errors == 1 and stats.fetched == 1
    with sessions() as session:
        bad = session.get(ArticleText, "feed-bad")
        assert bad is not None and bad.text == "" and bad.attempts == 1
        assert bad.last_error != ""
        good = session.get(ArticleText, "feed-good")
        assert good is not None and good.text != ""


def test_cycle_per_host_cap_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    _engine, sessions = _sqlite_sessions()
    rows = [{"id": f"feed-{i}", "url": f"https://one.example.com/{i}"} for i in range(6)]
    stats = _cycle(
        rows,
        sessions,
        _StubLanding(),
        lambda _req: httpx.Response(200, content=_PAGE),
        max_per_host=5,
    )
    assert stats.scanned == 5 and stats.host_capped == 1


def test_cycle_truncates_oversized_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    _engine, sessions = _sqlite_sessions()
    landing = _StubLanding()
    huge = b"<html><body><p>" + b"x" * 500_000 + b"</p></body></html>"
    stats = _cycle(
        [{"id": "feed-big", "url": "https://big.example.com/x"}],
        sessions,
        landing,
        lambda _req: httpx.Response(200, content=huge),
        max_fetch_bytes=10_000,
    )
    assert stats.fetched == 1
    assert len(landing.puts[0][1]) == 10_000  # truncated, not failed


# --------------------------------------------------------------------------------------------------
# load_body — the extraction-side read.
# --------------------------------------------------------------------------------------------------
def test_load_body_returns_truncated_text_or_none() -> None:
    _engine, sessions = _sqlite_sessions()
    with sessions() as session:
        session.add(ArticleText(entity_id="feed-a", url="u", text="0123456789"))
        session.add(ArticleText(entity_id="feed-empty", url="u", text=""))
        session.commit()
    assert load_body(sessions, "feed-a", max_chars=4) == "0123"
    assert load_body(sessions, "feed-empty", max_chars=4) is None
    assert load_body(sessions, "feed-missing", max_chars=4) is None


def test_load_body_defensive_against_test_doubles() -> None:
    """A MagicMock sessions factory (the extraction unit suite's idiom) yields None, not a crash."""
    assert load_body(MagicMock(), "feed-a", max_chars=4) is None
