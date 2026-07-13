"""News→event LLM extraction — parsing trust boundary + derivation + cycle (ADR 0115, Slice B).

The model's output is HOSTILE input: these tests pin that malformed/garbage/injection-shaped
replies are dropped (never trusted, never executed), that a valid extraction derives a correctly
shaped + provenance-stamped FtM Event (+ actors), and that the cycle is bounded, idempotent, and
error-isolated. No Ollama, no DB — the gateway + graph are stubbed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from worldmonitor.provenance.model import get_provenance
from worldmonitor.runner.extraction import (
    ExtractStats,
    build_messages,
    derive_entities,
    extract_cycle,
    parse_extraction,
    response_text,
)


# ------------------------------------------------------------------------------------------------
# parse_extraction — the trust boundary against hostile/malformed model output.
# ------------------------------------------------------------------------------------------------
def test_parse_valid_event() -> None:
    got = parse_extraction('{"is_event": true, "summary": "A strike", "country": "ua"}')
    assert got is not None and got["summary"] == "A strike"


def test_parse_tolerates_markdown_fences_and_chatter() -> None:
    reply = 'Sure!\n```json\n{"is_event": true, "summary": "Quake"}\n```\nHope that helps.'
    got = parse_extraction(reply)
    assert got is not None and got["summary"] == "Quake"


def test_parse_drops_non_event() -> None:
    assert parse_extraction('{"is_event": false, "summary": "an opinion piece"}') is None


def test_parse_drops_missing_is_event() -> None:
    assert parse_extraction('{"summary": "no flag"}') is None


def test_parse_drops_garbage() -> None:
    for bad in ("", "not json at all", "{", "}{", '{"is_event": true', "[1, 2, 3]", '"a string"'):
        assert parse_extraction(bad) is None, f"garbage must be dropped: {bad!r}"


def test_parse_hostile_content_is_inert_data() -> None:
    """An injection-shaped summary is returned as inert DATA — never executed (no eval/exec)."""
    got = parse_extraction('{"is_event": true, "summary": "<script>alert(1)</script>"}')
    assert got is not None
    assert got["summary"] == "<script>alert(1)</script>"  # a plain string, nothing ran


def test_response_text_defensive() -> None:
    assert response_text(None) == ""
    assert response_text(MagicMock(choices=[])) == ""
    good = MagicMock()
    good.choices = [MagicMock()]
    good.choices[0].message.content = "hi"
    assert response_text(good) == "hi"


def test_build_messages_includes_summary_when_present() -> None:
    with_summary = build_messages("Title", "Body")
    assert with_summary[0]["role"] == "system"
    assert "Summary: Body" in with_summary[1]["content"]
    assert "Summary:" not in build_messages("Title", None)[1]["content"]


# ------------------------------------------------------------------------------------------------
# derive_entities — the FtM Event + actor shapes, provenance, geo.
# ------------------------------------------------------------------------------------------------
_ARTICLE = {"id": "feed-abc", "source_record": "s3://landing/feeds/abc.json"}


def _derive(extraction: dict[str, Any]):
    return derive_entities(_ARTICLE, extraction, retrieved_at="2026-07-12T00:00:00Z")


def test_derive_builds_stamped_event_with_links_and_country() -> None:
    entities = _derive(
        {
            "is_event": True,
            "summary": "Workers strike in Kharkiv",
            "event_type": "protest",
            "country": "ua",
            "place": "Kharkiv",
            "actors": [{"name": "Company X", "kind": "organization"}],
        }
    )
    event = next(e for e in entities if e.schema.name == "Event")
    actor = next(e for e in entities if e.schema.name == "Organization")

    assert list(event.get("summary")) == ["Workers strike in Kharkiv"]
    assert list(event.get("country")) == ["ua"]  # -> globe plots at UA centroid
    assert list(event.get("location")) == ["Kharkiv"]
    assert list(event.get("proof")) == ["feed-abc"]  # receipt link to the source Article
    assert list(event.get("involved")) == [actor.id]  # actor link
    # Provenance stamped (reliability C — a derived lead) so the fail-closed writer accepts it.
    assert get_provenance(event).source_id == "derivation:geoevent"
    assert get_provenance(event).reliability == "C"
    assert get_provenance(actor).source_id == "derivation:geoevent"
    assert list(actor.get("name")) == ["Company X"]


def test_derive_drops_invalid_country_but_keeps_event() -> None:
    entities = _derive({"is_event": True, "summary": "A thing happened", "country": "zz"})
    event = next(e for e in entities if e.schema.name == "Event")
    assert not event.get("country")  # unknown code dropped, never plotted wrongly
    assert list(event.get("summary")) == ["A thing happened"]


def test_derive_returns_empty_without_summary_or_type() -> None:
    assert _derive({"is_event": True, "summary": "", "event_type": ""}) == []


def test_derive_precise_geo_emits_address_for_known_city() -> None:
    """A known city (Slice F) yields a precise FtM Address linked to the Event; the Event then
    carries NO country (so /points plots the precise pin, not a duplicate country-centroid dot)."""
    entities = _derive(
        {"is_event": True, "summary": "Blast in Kyiv", "country": "ua", "place": "Kyiv"}
    )
    event = next(e for e in entities if e.schema.name == "Event")
    address = next((e for e in entities if e.schema.name == "Address"), None)
    assert address is not None, "a known city must yield a precise Address"
    assert float(list(address.get("latitude"))[0]) == 50.45
    assert float(list(address.get("longitude"))[0]) == 30.52
    assert list(address.get("full")) == ["Kyiv"]
    assert list(event.get("addressEntity")) == [
        address.id
    ]  # -> (:Event)-[:ADDRESS_ENTITY]->Address
    assert not event.get("country"), "no country on the Event when it has a precise Address"


def test_derive_unknown_place_falls_back_to_country_centroid() -> None:
    entities = _derive(
        {"is_event": True, "summary": "Clash reported", "country": "ua", "place": "Nowhereville"}
    )
    assert not any(e.schema.name == "Address" for e in entities), "unknown city -> no Address"
    event = next(e for e in entities if e.schema.name == "Event")
    assert list(event.get("country")) == ["ua"]  # coarse country fallback
    assert not event.get("addressEntity")


def _org_id(article_id: str, extraction: dict[str, Any]) -> str:
    entities = derive_entities(
        {"id": article_id, "source_record": "s"}, extraction, retrieved_at="t"
    )
    return next(e for e in entities if e.schema.name == "Organization").id


def test_actor_id_is_stable_within_an_article_but_distinct_across_articles() -> None:
    """Same actor + same article -> same id (idempotent re-extraction); same actor + DIFFERENT
    article -> DIFFERENT id, so two same-named entities are NOT merged by id-fiat — the ER pipeline
    + the catastrophic-merge guard decide any cross-article merge, never this code."""
    a = {"is_event": True, "summary": "x", "actors": [{"name": "ACME", "kind": "organization"}]}
    a_case = {
        "is_event": True,
        "summary": "y",
        "actors": [{"name": "acme", "kind": "organization"}],
    }
    assert _org_id("feed-1", a) == _org_id("feed-1", a_case), "same (article, name) must be stable"
    assert _org_id("feed-1", a) != _org_id("feed-2", a), "different articles must NOT collide"


def test_derive_person_vs_organization_and_actor_cap() -> None:
    actors = [{"name": f"Person {i}", "kind": "person"} for i in range(8)]
    entities = _derive({"is_event": True, "summary": "many", "actors": actors})
    persons = [e for e in entities if e.schema.name == "Person"]
    assert len(persons) == 5, "actors are capped at 5"


def test_derive_ignores_junk_actor_entries() -> None:
    entities = _derive(
        {
            "is_event": True,
            "summary": "s",
            "actors": ["not-a-dict", {"kind": "person"}, {"name": "  "}, {"name": "Real Co"}],
        }
    )
    names = {n for e in entities if e.schema.name != "Event" for n in e.get("name")}
    assert names == {"Real Co"}


def test_derive_keeps_model_output_out_of_risk_topics_and_actor_country() -> None:
    """Two hardening invariants: the model's event_type must NOT become a typed FtM risk topic
    (which the sensitivity guard reads), and an actor must NOT inherit the event's country."""
    entities = _derive(
        {
            "is_event": True,
            "summary": "x",
            "event_type": "sanction",  # a REAL FtM risk-topic token
            "country": "us",
            "actors": [{"name": "Bank Co", "kind": "organization"}],
        }
    )
    event = next(e for e in entities if e.schema.name == "Event")
    actor = next(e for e in entities if e.schema.name == "Organization")
    assert not event.get("topics"), "model event_type must NOT become a typed FtM risk topic"
    assert not actor.get("country"), "an actor must not inherit the event's country"


# ------------------------------------------------------------------------------------------------
# extract_cycle — bounded, idempotent, error-isolated orchestration (stubbed gateway + graph).
# ------------------------------------------------------------------------------------------------
class _FakeNeo4j:
    """Honors the watermark: the selection read returns only UNmarked articles; execute_write
    (``_mark_extracted``) records the mark — this lets the cycle tests prove non-starvation."""

    def __init__(self, articles: list[dict[str, Any]], marked: set[str] | None = None) -> None:
        self._articles = articles
        self.marked: set[str] = set(marked or set())

    def execute_read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        return [a for a in self._articles if a["id"] not in self.marked]

    def execute_write(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.marked.add(params["article_id"])
        return []


def _gateway(reply: Any) -> MagicMock:
    """Stub gateway: chat() returns a canned reply (str -> content, or an Exception to raise)."""
    gw = MagicMock()

    def _chat(messages: Any, *, caller_tag: str = "gateway", **kw: Any) -> Any:
        if isinstance(reply, Exception):
            raise reply
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = reply
        return resp

    gw.chat.side_effect = _chat
    return gw


def test_select_query_gates_to_feeds_and_excludes_processed() -> None:
    """The selection Cypher gates to curated feeds (never Bluesky) and skips watermarked rows."""
    captured: dict[str, str] = {}

    class _CaptureNeo4j:
        def execute_read(self, query: str, **params: Any) -> list[dict[str, Any]]:
            captured["q"] = query
            return []

    from worldmonitor.runner.extraction import select_feed_articles

    select_feed_articles(_CaptureNeo4j(), limit=5)
    assert "'feeds' IN n.datasets" in captured["q"]
    assert "n.extraction_done IS NULL" in captured["q"]
    assert "bluesky" not in captured["q"]


def _sessions() -> MagicMock:
    return MagicMock()  # supports the `with sessions() as s` protocol; execute/commit are no-ops


def _articles(n: int) -> list[dict[str, Any]]:
    return [
        {"id": f"feed-{i}", "title": f"Headline {i}", "source_record": f"s3://x/{i}"}
        for i in range(n)
    ]


def test_cycle_extracts_events_and_watermarks_them() -> None:
    neo4j = _FakeNeo4j(_articles(2))
    gw = _gateway('{"is_event": true, "summary": "Event happened", "country": "us"}')
    stats = extract_cycle(
        neo4j=neo4j, sessions=_sessions, gateway=gw, max_articles=10, retrieved_at="t"
    )
    assert isinstance(stats, ExtractStats)
    assert stats.extracted == 2 and stats.events == 2
    assert gw.chat.call_count == 2
    assert neo4j.marked == {"feed-0", "feed-1"}, "processed articles must be watermarked"


def test_cycle_marks_non_events_so_they_never_starve_fresh_events() -> None:
    """A non-event is watermarked too, so a SECOND cycle does not re-scan it (no starvation)."""
    neo4j = _FakeNeo4j(_articles(3))
    gw = _gateway('{"is_event": false}')
    first = extract_cycle(
        neo4j=neo4j, sessions=_sessions, gateway=gw, max_articles=10, retrieved_at="t"
    )
    assert first.skipped == 3 and neo4j.marked == {"feed-0", "feed-1", "feed-2"}

    second = extract_cycle(
        neo4j=neo4j, sessions=_sessions, gateway=gw, max_articles=10, retrieved_at="t"
    )
    assert second.scanned == 0, "watermarked non-events must not be re-scanned"
    assert gw.chat.call_count == 3, "the second cycle made no new LLM calls"


def test_cycle_respects_per_cycle_cap() -> None:
    neo4j = _FakeNeo4j(_articles(20))
    gw = _gateway('{"is_event": true, "summary": "x"}')
    extract_cycle(neo4j=neo4j, sessions=_sessions, gateway=gw, max_articles=3, retrieved_at="t")
    assert gw.chat.call_count == 3, "the per-cycle cap must bound LLM calls"
    assert len(neo4j.marked) == 3, "only the capped articles are consumed; the rest remain"


def test_cycle_skips_watermarked_articles() -> None:
    neo4j = _FakeNeo4j(_articles(2), marked={"feed-0"})
    gw = _gateway('{"is_event": true, "summary": "x"}')
    stats = extract_cycle(
        neo4j=neo4j, sessions=_sessions, gateway=gw, max_articles=10, retrieved_at="t"
    )
    assert gw.chat.call_count == 1, "an already-watermarked article must not re-call the LLM"
    assert stats.scanned == 1


def test_cycle_isolates_errors_and_leaves_them_unmarked_for_retry() -> None:
    neo4j = _FakeNeo4j(_articles(2))
    gw = _gateway(RuntimeError("ollama down"))
    stats = extract_cycle(
        neo4j=neo4j, sessions=_sessions, gateway=gw, max_articles=10, retrieved_at="t"
    )
    assert stats.errors == 2 and stats.extracted == 0  # one bad article never aborts the cycle
    assert neo4j.marked == set(), "an errored article stays UNmarked so it retries next cycle"
