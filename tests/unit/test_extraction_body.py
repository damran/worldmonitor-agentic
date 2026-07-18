"""Extraction ↔ full-text integration seam (ADR 0116): the prompt carries the cached body.

Kept out of ``test_extraction.py`` deliberately (parallel-PR merge hygiene). The body is hostile
input like the headline; these tests pin only the WIRING — body present → in the user message;
absent → headline-only, byte-identical to the pre-0116 prompt.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import worldmonitor.runner.extraction as extraction
from worldmonitor.runner.extraction import build_messages, extract_cycle


def test_build_messages_appends_body_when_present() -> None:
    messages = build_messages("Title", "Sum", "The cached article body.")
    assert "Article text: The cached article body." in messages[1]["content"]
    assert "Article text:" not in build_messages("Title", "Sum")[1]["content"]
    assert "Article text:" not in build_messages("Title", None, None)[1]["content"]


class _FakeNeo4j:
    def __init__(self, articles: list[dict[str, Any]]) -> None:
        self.articles = articles
        self.writes: list[str] = []

    def execute_read(self, _query: str, **_params: Any) -> list[dict[str, Any]]:
        return self.articles

    def execute_write(self, _query: str, **params: Any) -> None:
        self.writes.append(str(params.get("article_id")))


def _gateway(reply: str) -> MagicMock:
    gw = MagicMock()
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = reply
    gw.chat.return_value = response
    return gw


def test_cycle_feeds_the_cached_body_into_the_prompt(monkeypatch: Any) -> None:
    monkeypatch.setattr(extraction, "load_body", lambda *_a, **_k: "CACHED BODY TEXT")
    neo4j = _FakeNeo4j([{"id": "feed-1", "title": "Headline 1", "source_record": "s3://x/1"}])
    gw = _gateway('{"is_event": false}')
    extract_cycle(neo4j=neo4j, sessions=MagicMock(), gateway=gw, max_articles=5, retrieved_at="t")
    sent = gw.chat.call_args[0][0]
    assert "Article text: CACHED BODY TEXT" in sent[1]["content"]


def test_cycle_without_a_cached_body_stays_headline_only(monkeypatch: Any) -> None:
    monkeypatch.setattr(extraction, "load_body", lambda *_a, **_k: None)
    neo4j = _FakeNeo4j([{"id": "feed-1", "title": "Headline 1", "source_record": "s3://x/1"}])
    gw = _gateway('{"is_event": false}')
    extract_cycle(neo4j=neo4j, sessions=MagicMock(), gateway=gw, max_articles=5, retrieved_at="t")
    sent = gw.chat.call_args[0][0]
    assert "Article text:" not in sent[1]["content"]
