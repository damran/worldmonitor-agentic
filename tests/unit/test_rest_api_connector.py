"""Primary invariant tests (RED) for the RestApiConnector base — ADR 0065.

The base (``src/worldmonitor/plugins/rest_api.py``, sibling to ``plugins/ftm_bulk.py``) implements
``collect()`` ONCE for page-based JSON APIs over the SSRF guard, leaving four hooks to the subclass
(``_page_url`` / ``_extract_items`` / ``_total_pages`` / ``_record_key``); ``map()`` stays abstract.

A tiny in-test subclass implements the four hooks against ``httpx.MockTransport`` so the base's
pagination + safety contract is pinned independently of OpenCorporates:

* pagination walks pages and STOPS at ``total_pages``;
* ``max_pages`` is a HARD cap (a payload claiming far more pages still stops);
* a per-page response BYTE cap (``rest_api._MAX_RESPONSE_BYTES``) is fail-closed — a body over
  the cap RAISES and yields nothing (no unbounded read of a hostile body);
* a non-2xx (401) page fails LOUD via ``raise_for_status`` (a misconfigured token is not swallowed).

RED today: ``worldmonitor.plugins.rest_api`` does not exist, so the top-level import raises
``ModuleNotFoundError`` and every test errors at collection — the correct RED.

No live network: ``httpx.MockTransport`` injected via the ctor ``transport=`` kwarg (forwarded to
``guarded_stream``) + monkeypatched ``socket.getaddrinfo`` -> a public IP (the SSRF-guard hermetic
pattern from ``tests/unit/test_ssrf_guard.py``).
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import httpx
import pytest

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.plugins.base import (
    Capability,
    Kind,
    Manifest,
    Mode,
    RawRecord,
    Status,
)

# Top-level import of the not-yet-built base — ModuleNotFoundError today (correct RED). The
# ``_MAX_RESPONSE_BYTES`` constant is the named per-page byte cap from ADR 0065 (locked contract).
from worldmonitor.plugins.rest_api import _MAX_RESPONSE_BYTES, RestApiConnector
from worldmonitor.provenance.model import Provenance


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in resolving EVERY host to ``ip`` (one IPv4 5-tuple)."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


class _FakeRestConnector(RestApiConnector):
    """Minimal concrete subclass of the base: 4 hooks over a ``{items, total_pages}`` JSON shape.

    ``map()`` is irrelevant here (the base's pagination/safety is under test), so it returns ``[]``.
    """

    @property
    def manifest(self) -> Manifest:
        return Manifest(
            connector_id="fake-rest",
            name="Fake REST",
            version="0.0.1",
            kind=Kind.CONNECTOR,
            mode=Mode.EXTERNAL_IMPORT,
            capability=Capability.PASSIVE,
            status=Status.IMPLEMENTED,
        )

    @property
    def config_schema(self) -> dict[str, Any]:
        return {"type": "object"}

    def _page_url(self, config: Mapping[str, Any], page: int) -> str:
        return f"http://api.example.test/data?page={page}"

    def _extract_items(self, payload: Any) -> list[dict[str, Any]]:
        return list(payload["items"])

    def _total_pages(self, payload: Any) -> int:
        return int(payload["total_pages"])

    def _record_key(self, item: Mapping[str, Any]) -> str:
        return str(item["k"])

    def map(self, record: RawRecord, *, provenance: Provenance) -> Iterable[FtmEntity]:
        return []


def _page(items: list[dict[str, Any]], total_pages: int, filler: str = "") -> bytes:
    payload: dict[str, Any] = {"items": items, "total_pages": total_pages}
    if filler:
        payload["_filler"] = filler
    return json.dumps(payload).encode("utf-8")


def test_collect_paginates_and_stops_at_total_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two pages (total_pages=2, 2 items each) -> 4 records across exactly 2 fetches."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    pages: dict[str, bytes] = {
        "1": _page([{"k": "a"}, {"k": "b"}], total_pages=2),
        "2": _page([{"k": "c"}, {"k": "d"}], total_pages=2),
    }
    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        calls.append(page)
        return httpx.Response(200, content=pages[page])

    connector = _FakeRestConnector(transport=httpx.MockTransport(_handler))
    records = list(connector.collect({"max_pages": 10}))

    assert [r.key for r in records] == ["a", "b", "c", "d"]
    assert all(r.content_type == "application/json" for r in records)
    assert calls == ["1", "2"], f"did not stop at total_pages (fetched pages {calls})"


def test_collect_is_hard_bounded_by_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A payload advertising 999 pages still stops at max_pages (HARD cap)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))
    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        calls.append(page)
        return httpx.Response(200, content=_page([{"k": f"{page}-x"}], total_pages=999))

    connector = _FakeRestConnector(transport=httpx.MockTransport(_handler))
    records = list(connector.collect({"max_pages": 3}))

    assert len(calls) == 3, f"max_pages cap not enforced (saw {len(calls)} fetches)"
    assert len(records) == 3


def test_collect_fails_closed_on_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page body OVER the byte cap RAISES (fail-closed); the same structure UNDER the cap yields.

    Metamorphic pair: identical item/total_pages structure, only the body SIZE differs. Under the
    cap -> the items are yielded; over the cap -> ``collect()`` raises and yields nothing (the
    hostile-input bound is the discriminator, not a generic parse failure).
    """
    if _MAX_RESPONSE_BYTES > 64 * 1024 * 1024:  # pragma: no cover - sanity guard on an absurd cap
        pytest.skip(f"_MAX_RESPONSE_BYTES={_MAX_RESPONSE_BYTES} too large to exercise in-memory")

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    # UNDER the cap: a small single page yields its items.
    under = _page([{"k": "ok"}], total_pages=1)
    assert len(under) <= _MAX_RESPONSE_BYTES
    under_connector = _FakeRestConnector(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=under))
    )
    assert [r.key for r in under_connector.collect({"max_pages": 1})] == ["ok"]

    # OVER the cap: pad the SAME structure past the cap -> must raise, never yield.
    over = _page([{"k": "ok"}], total_pages=1, filler="x" * (_MAX_RESPONSE_BYTES + 1024))
    assert len(over) > _MAX_RESPONSE_BYTES
    over_connector = _FakeRestConnector(
        transport=httpx.MockTransport(lambda _req: httpx.Response(200, content=over))
    )
    with pytest.raises(Exception):  # noqa: B017,PT011 - fail-closed: ANY raise beats an unbounded read
        list(over_connector.collect({"max_pages": 1}))


def test_collect_raises_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 page fails LOUD via raise_for_status (a misconfigured token is not swallowed)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error":"unauthorized"}')

    connector = _FakeRestConnector(transport=httpx.MockTransport(_handler))
    with pytest.raises(httpx.HTTPStatusError):
        list(connector.collect({"max_pages": 5}))
