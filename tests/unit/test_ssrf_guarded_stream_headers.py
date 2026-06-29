"""Tests for the optional ``headers`` extension to ``guarded_stream`` (ADR 0081).

Covers three things:
1. ``guarded_stream`` forwards the ``headers`` kwarg into the outbound request via both
   the injected-transport path (``httpx.Client.stream``) and records them on the request
   object the ``MockTransport`` receives.
2. SSRF safety is **not** weakened by the presence of headers — passing a ``User-Agent``
   header does NOT bypass ``assert_public_host`` for a blocked host.
3. ``WikidataEnricher._lookup_qid`` uses ``guarded_stream`` via the injected transport
   (so a ``MockTransport`` can intercept the SPARQL call and return a canned result) and
   parses the Q-number correctly.  When ``lookup=True`` and a transport is injected, the
   enricher NEVER calls the bare ``httpx.get`` — proved by patching ``httpx.get`` to
   explode and confirming the enricher still works.

No live network — all tests use ``httpx.MockTransport`` + ``monkeypatch``.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from worldmonitor.net.ssrf import BlockedAddressError, guarded_stream
from worldmonitor.ontology.anchors import get_anchors
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.plugins.enrichers.wikidata import WikidataEnricher

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _getaddrinfo_public(host: str, *_a: object, **_k: object) -> list[tuple[object, ...]]:
    """Resolve every hostname to 8.8.8.8 (a public IP)."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))]


def _org(props: dict[str, list[str]]):
    return make_entity(
        {"id": "x", "schema": "Organization", "properties": props, "datasets": ["t"]}
    )


# ---------------------------------------------------------------------------
# 1. guarded_stream THREADS headers through to the outbound request
# ---------------------------------------------------------------------------


def test_guarded_stream_headers_forwarded_to_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Headers passed to ``guarded_stream`` arrive on the ``httpx.Request`` the transport sees."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    seen_headers: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        return httpx.Response(200, content=b"ok")

    transport = httpx.MockTransport(_handler)
    custom_ua = "TestAgent/1.0 (+https://example.com)"

    with guarded_stream(
        "GET",
        "http://example.com/sparql",
        headers={"User-Agent": custom_ua, "Accept": "application/json"},
        transport=transport,
    ) as resp:
        resp.read()

    assert seen_headers.get("user-agent") == custom_ua, (
        f"Expected User-Agent '{custom_ua}' in request headers but got: {seen_headers}"
    )
    assert seen_headers.get("accept") == "application/json", (
        f"Expected Accept header in request but got: {seen_headers}"
    )


def test_guarded_stream_no_headers_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling ``guarded_stream`` with NO headers (the default) is backward-compatible."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello")

    transport = httpx.MockTransport(_handler)

    with guarded_stream("GET", "http://example.com/x", transport=transport) as resp:
        body = resp.read()

    assert body == b"hello"


def test_guarded_stream_headers_forwarded_through_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headers are threaded through on the hop that is NOT a redirect too (post-redirect yield)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    calls: list[dict[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if "redirect" not in str(request.url):
            # First hop: redirect to cdn
            return httpx.Response(302, headers={"location": "http://cdn.example.com/redirect"})
        # Second hop: final response
        return httpx.Response(200, content=b"final")

    transport = httpx.MockTransport(_handler)

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={"X-Custom": "sentinel"},
        transport=transport,
    ) as resp:
        body = resp.read()

    assert body == b"final"
    # The final (non-redirect) hop should also carry the custom header
    assert any("x-custom" in h for h in calls), "Custom header was not seen on any hop"


# ---------------------------------------------------------------------------
# 2. SSRF guard NOT weakened by headers
# ---------------------------------------------------------------------------


def test_guarded_stream_blocks_private_host_even_with_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing headers does NOT bypass the SSRF host check for a blocked address."""
    # Do NOT monkeypatch getaddrinfo — 127.0.0.1 is an IP literal, checked directly.
    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"leaked")

    transport = httpx.MockTransport(_handler)

    with (
        pytest.raises(BlockedAddressError),
        guarded_stream(
            "GET",
            "http://127.0.0.1/secret",
            headers={"User-Agent": "ShouldNeverReachServer/1.0"},
            transport=transport,
        ) as resp,
    ):
        resp.read()

    assert calls == [], (
        "Transport was called despite the host being blocked — SSRF guard was bypassed!"
    )


def test_guarded_stream_blocks_redirect_to_metadata_even_with_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect to the metadata IP (169.254.169.254) is still blocked when headers are set."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/secret"})

    transport = httpx.MockTransport(_handler)

    with (
        pytest.raises(BlockedAddressError),
        guarded_stream(
            "GET",
            "http://example.com/x",
            headers={"User-Agent": "PoisonedRedirectTest/1.0"},
            transport=transport,
        ),
    ):
        pass

    assert "169.254.169.254" not in calls, (
        "Transport connected to the metadata IP despite the SSRF guard"
    )
    assert calls == ["example.com"], f"Unexpected transport calls: {calls}"


# ---------------------------------------------------------------------------
# 3. WikidataEnricher uses guarded_stream, not httpx.get
# ---------------------------------------------------------------------------

# A minimal SPARQL-JSON response for a single binding
_SPARQL_Q42 = json.dumps(
    {
        "results": {
            "bindings": [{"item": {"type": "uri", "value": "http://www.wikidata.org/entity/Q42"}}]
        }
    }
).encode()

_SPARQL_EMPTY = json.dumps({"results": {"bindings": []}}).encode()


def test_wikidata_enricher_uses_injected_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_lookup_qid`` routes through ``guarded_stream`` and honours an injected transport."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SPARQL_Q42)

    transport = httpx.MockTransport(_handler)
    entity = _org({"name": ["Douglas Adams"]})

    enricher = WikidataEnricher(transport=transport)
    enricher.enrich(entity)

    anchors = get_anchors(entity)
    assert anchors.get("wikidata_id") == "Q42", (
        f"Expected wikidata_id='Q42' but got anchors={anchors}"
    )


def test_wikidata_enricher_does_not_call_httpx_get_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_lookup_qid`` MUST NOT call ``httpx.get`` directly — it goes through guarded_stream."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SPARQL_Q42)

    transport = httpx.MockTransport(_handler)

    # Patch httpx.get to explode — if the enricher still calls it the test fails.
    def _boom(*_a: object, **_k: object) -> httpx.Response:
        raise AssertionError("WikidataEnricher called httpx.get directly — SSRF guard bypassed!")

    monkeypatch.setattr(httpx, "get", _boom)

    entity = _org({"name": ["Douglas Adams"]})
    enricher = WikidataEnricher(transport=transport)
    enricher.enrich(entity)  # must not raise

    assert get_anchors(entity).get("wikidata_id") == "Q42"


def test_wikidata_enricher_empty_sparql_result_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty SPARQL binding list leaves no wikidata_id anchor on the entity."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SPARQL_EMPTY)

    transport = httpx.MockTransport(_handler)
    entity = _org({"name": ["Unknown Entity XYZ"]})

    WikidataEnricher(transport=transport).enrich(entity)

    assert "wikidata_id" not in get_anchors(entity)


def test_wikidata_enricher_sends_user_agent_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Wikidata UA header is forwarded to the SPARQL endpoint via guarded_stream."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    seen: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, content=_SPARQL_Q42)

    transport = httpx.MockTransport(_handler)
    entity = _org({"name": ["Test Entity"]})

    WikidataEnricher(transport=transport).enrich(entity)

    assert "user-agent" in seen, f"No User-Agent in request headers: {seen}"
    assert "WorldMonitor" in seen["user-agent"], (
        f"Unexpected User-Agent value: {seen['user-agent']!r}"
    )


def test_wikidata_enricher_transport_error_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport/network error in ``_lookup_qid`` is swallowed (best-effort behavior)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    transport = httpx.MockTransport(_handler)
    entity = _org({"name": ["Unreachable Entity"]})

    # Must not raise — best-effort
    WikidataEnricher(transport=transport).enrich(entity)

    assert "wikidata_id" not in get_anchors(entity)


def test_wikidata_enricher_http_error_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 4xx/5xx response from the SPARQL endpoint is swallowed (best-effort behavior)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"Service Unavailable")

    transport = httpx.MockTransport(_handler)
    entity = _org({"name": ["Rate Limited Entity"]})

    WikidataEnricher(transport=transport).enrich(entity)

    assert "wikidata_id" not in get_anchors(entity)
