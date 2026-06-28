"""Integration test (RED): OpenCorporates collect -> landing -> ER queue via run_ingest (ADR 0065).

Drives the connector over ``httpx.MockTransport`` (NO live HTTP, NO live OpenCorporates token) into
an in-memory landing fake + an ephemeral Postgres (testcontainers). Asserts the unchanged run path
(``runner/ingest.py``) lands every raw company and enqueues an ``ErQueueItem`` per entity WITH
provenance, and that a re-run is idempotent (the ``(source_record, entity_id)`` dedup constraint
means no duplicate rows).

This is the OpenCorporates analogue of ``tests/integration/test_opensanctions_ingest.py``, but with
an in-memory landing (the ``_FakeLanding`` pattern from ``tests/integration/test_ingest_runner.py``)
so it needs only Postgres — no MinIO. Marked ``integration``.

RED today: ``worldmonitor.plugins.connectors.opencorporates`` does not exist -> ModuleNotFoundError
at collection (the correct RED). GREEN once the builder lands the connector.
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
from worldmonitor.plugins.connectors.opencorporates import OpenCorporatesConnector
from worldmonitor.runner.ingest import run_ingest

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "opencorporates"


# Loopback hosts the testcontainer Postgres DSN may use — these must resolve to loopback so the DB
# connection is NOT black-holed to the public ``ip`` (psycopg resolves the ``localhost`` DSN host
# via ``socket.getaddrinfo`` too). Only the public API host the SSRF guard checks is pinned to ip.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        if host in _LOOPBACK_HOSTS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


def _page_transport() -> httpx.MockTransport:
    page_body = {
        "1": (_FIXTURES / "companies_page1.json").read_bytes(),
        "2": (_FIXTURES / "companies_page2.json").read_bytes(),
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        body = page_body.get(page, b'{"results":{"companies":[],"total_pages":2}}')
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

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


_CONFIG = {"api_token": "T", "q": "acme", "per_page": 2, "max_pages": 5}


def test_collect_lands_raw_and_enqueues_candidates_idempotently(
    monkeypatch: pytest.MonkeyPatch, postgres_dsn: str
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    landing = _FakeLanding()

    with sessions() as session:
        stats = run_ingest(
            OpenCorporatesConnector(transport=_page_transport()),
            _CONFIG,
            landing=landing,  # type: ignore[arg-type]
            session=session,
        )

    # 4 companies across 2 pages: all landed + all enqueued, none dead-lettered.
    assert stats.collected == 4
    assert stats.landed == 4
    assert stats.queued == 4
    assert stats.dead_lettered == 0

    # Raw bytes landed under the connector prefix.
    assert len(landing.objects) == 4
    assert any(k.startswith("opencorporates/") for k in landing.objects)

    with sessions() as session:
        count = session.execute(select(func.count()).select_from(ErQueueItem)).scalar_one()
        assert count == 4
        row = session.execute(
            select(ErQueueItem).where(ErQueueItem.entity_id == "opencorporates-gb-01234567")
        ).scalar_one()
        assert row.connector_id == "opencorporates"
        assert row.raw_entity["schema"] == "Company"
        # Provenance travels with the enqueued entity (the non-negotiable invariant).
        assert row.raw_entity["wm_prov_source_id"]
        assert row.source_record.startswith("s3://landing/")

    # Re-run is idempotent: same landing URIs + entity ids -> no new ErQueueItem rows.
    with sessions() as session:
        again = run_ingest(
            OpenCorporatesConnector(transport=_page_transport()),
            _CONFIG,
            landing=landing,  # type: ignore[arg-type]
            session=session,
        )
    assert again.collected == 4
    assert again.queued == 0  # all four already enqueued

    with sessions() as session:
        count = session.execute(select(func.count()).select_from(ErQueueItem)).scalar_one()
        assert count == 4  # still exactly four — no duplicates

    engine.dispose()
