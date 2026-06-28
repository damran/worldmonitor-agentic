"""Integration test (RED): Feed collect -> landing -> ER queue via run_ingest (ADR 0066).

Drives the FeedConnector over ``httpx.MockTransport`` serving the local rss2.xml fixture (NO live
HTTP) into an in-memory landing fake + an ephemeral Postgres (testcontainers). Asserts the
unchanged run path (``runner/ingest.py``) lands every raw entry and enqueues an ``ErQueueItem`` per
FtM ``Article`` WITH provenance, and that a re-run is idempotent (the ``(source_record,
entity_id)`` dedup constraint means no duplicate rows).

The Feed analogue of ``test_opencorporates_ingest.py`` — in-memory landing (the ``_FakeLanding``
pattern from ``tests/integration/test_ingest_runner.py``) so it needs only Postgres, no MinIO.
Marked ``integration``.

RED today: ``worldmonitor.plugins.connectors.feeds`` does not exist -> ModuleNotFoundError at
collection (the correct RED). GREEN once the builder lands the connector + the ``feedparser`` dep.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem

# Top-level import of the not-yet-built connector — ModuleNotFoundError today (correct RED).
from worldmonitor.plugins.connectors.feeds import FeedConnector
from worldmonitor.runner.ingest import run_ingest

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "feeds"

# Loopback hosts the testcontainer Postgres DSN may use — these must resolve to loopback so the DB
# connection is NOT black-holed to the public ``ip``. Only the feed host the SSRF guard checks is
# pinned to the public ip.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        if host in _LOOPBACK_HOSTS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def _rss_transport() -> httpx.MockTransport:
    body = (_FIXTURES / "rss2.xml").read_bytes()

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/xml"})

    return httpx.MockTransport(_handler)


class _FakeLanding:
    """In-memory stand-in for LandingStore (duck-typed: ensure_bucket + put)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def ensure_bucket(self) -> None:
        pass

    def put(self, key: str, data: bytes, *, content_type: str = "application/json") -> str:
        self.objects[key] = data
        return f"s3://landing/{key}"


_CONFIG = {"feed_url": "https://news.example.com/rss", "max_items": 100}


def test_collect_lands_raw_and_enqueues_articles_idempotently(
    monkeypatch: pytest.MonkeyPatch, postgres_dsn: str
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    landing = _FakeLanding()

    with sessions() as session:
        stats = run_ingest(
            FeedConnector(transport=_rss_transport()),
            _CONFIG,
            landing=landing,  # type: ignore[arg-type]
            session=session,
        )

    # 3 RSS items: all landed + all enqueued as Articles, none dead-lettered.
    assert stats.collected == 3
    assert stats.landed == 3
    assert stats.queued == 3
    assert stats.dead_lettered == 0

    # Raw bytes landed under the connector prefix.
    assert len(landing.objects) == 3
    assert any(k.startswith("feeds/") for k in landing.objects)

    with sessions() as session:
        count = session.execute(select(func.count()).select_from(ErQueueItem)).scalar_one()
        assert count == 3
        rows = session.execute(select(ErQueueItem)).scalars().all()
        for row in rows:
            assert row.connector_id == "feeds"
            assert row.raw_entity["schema"] == "Article"
            assert row.entity_id.startswith("feed-")
            # Provenance travels with the enqueued entity (the non-negotiable invariant).
            assert row.raw_entity["wm_prov_source_id"]
            assert row.source_record.startswith("s3://landing/")

    # Re-run is idempotent: same landing URIs + entity ids -> no new ErQueueItem rows.
    with sessions() as session:
        again = run_ingest(
            FeedConnector(transport=_rss_transport()),
            _CONFIG,
            landing=landing,  # type: ignore[arg-type]
            session=session,
        )
    assert again.collected == 3
    assert again.queued == 0  # all three already enqueued

    with sessions() as session:
        count = session.execute(select(func.count()).select_from(ErQueueItem)).scalar_one()
        assert count == 3  # still exactly three — no duplicates

    engine.dispose()
