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

Gate C (ADR 0087) tests are appended at the end of this file and are RED on the current
tree.  They pin invariant G-NET-1: a sensitive header (Authorization / Cookie /
Proxy-Authorization) must NOT be forwarded on a redirect hop whose host differs from the
ORIGINAL request host, or on a same-host https→http downgrade.

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


# ---------------------------------------------------------------------------
# Gate C — cross-host header strip (ADR 0087)
#
# These tests pin invariant G-NET-1:
#   "A sensitive header supplied to guarded_stream is sent ONLY to the host it was
#    scoped to (the original request host). It is NEVER transmitted on a hop whose
#    host differs from the original request host."
#
# Sensitive denylist: {authorization, cookie, proxy-authorization} (case-insensitive).
# Scheme downgrade (https→http, same host) also strips.
# Port-only change on same host does NOT strip.
# Sticky strip: once stripped, NOT restored even if the chain returns to origin_host.
#
# Tests marked RED below FAIL on the current tree (unconditional header forwarding).
# Tests marked GREEN pass today and must stay green after the fix (regression guards).
# ---------------------------------------------------------------------------

_AUTHZ = "Bearer gate-c-test-token"
_COOKIE_VAL = "session=gate-c-test; id=42"
_PROXY_AUTH = "Basic Z2F0ZWM6dGVzdA=="
_UA = "GateCTestAgent/1.0"


def test_sensitive_header_stripped_on_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2 — cross-host 302 MUST NOT carry Authorization, Cookie, or Proxy-Authorization.

    Hop 1: GET http://example.com/x  (origin)
    Redirect: 302 → http://other.example.net/dest  (DIFFERENT host)
    Hop 2: GET http://other.example.net/dest  → 200

    Assert: hop-2 request headers contain NONE of the three sensitive members.

    RED today: guarded_stream passes ``headers=headers`` unconditionally, so all three
    appear on hop 2.  The assertion fires because the leak is the current behaviour.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://other.example.net/dest"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={
            "Authorization": _AUTHZ,
            "Cookie": _COOKIE_VAL,
            "Proxy-Authorization": _PROXY_AUTH,
            "User-Agent": _UA,
        },
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 2, (
        f"Expected exactly 2 hops (origin + cross-host), got {len(calls)}: {[c[0] for c in calls]}"
    )
    hop2 = calls[1][1]

    assert "authorization" not in hop2, (
        f"G-NET-1 VIOLATED (AC2): Authorization leaked to cross-host target "
        f"'other.example.net'. hop-2 headers: {hop2}"
    )
    assert "cookie" not in hop2, (
        f"G-NET-1 VIOLATED (AC2): Cookie leaked to cross-host target "
        f"'other.example.net'. hop-2 headers: {hop2}"
    )
    assert "proxy-authorization" not in hop2, (
        f"G-NET-1 VIOLATED (AC2): Proxy-Authorization leaked to cross-host target "
        f"'other.example.net'. hop-2 headers: {hop2}"
    )


def test_sensitive_header_kept_on_same_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 — same-host, same-scheme redirect (different path) KEEPS sensitive headers.

    Hop 1: GET http://example.com/x  → 302 → http://example.com/final
    Hop 2: GET http://example.com/final  → 200

    Assert: Authorization IS present on hop 2 (same host, no strip triggered).

    GREEN today (unconditional forwarding); must stay GREEN after the denylist fix.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.path == "/x":
            return httpx.Response(302, headers={"location": "http://example.com/final"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={"Authorization": _AUTHZ, "User-Agent": _UA},
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 2, f"Expected 2 hops, got {len(calls)}"
    hop2 = calls[1][1]
    assert "authorization" in hop2, (
        f"AC3 regression: Authorization was stripped on a same-host redirect — "
        f"it must be kept. hop-2 headers: {hop2}"
    )


def test_sensitive_header_stripped_on_https_to_http_same_host_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4 — same-host https→http scheme downgrade STRIPS sensitive headers.

    Origin: https://example.com/x
    Redirect: 302 → http://example.com/final  (same hostname, cleartext downgrade)
    Hop 2: http://example.com/final  → 200

    Assert: Authorization NOT present on hop 2 (credential exposed on wire otherwise).

    RED today: guarded_stream forwards Authorization unconditionally, exposing it in
    cleartext on the same-host hop.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.scheme == "https":
            # Downgrade: same host, http
            return httpx.Response(302, headers={"location": "http://example.com/final"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "https://example.com/x",
        headers={"Authorization": _AUTHZ, "User-Agent": _UA},
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 2, f"Expected 2 hops, got {len(calls)}"
    hop2 = calls[1][1]
    assert "authorization" not in hop2, (
        f"G-NET-1 VIOLATED (AC4): Authorization forwarded on https→http downgrade to "
        f"the SAME host 'example.com'. Credential now travels in cleartext. "
        f"hop-2 headers: {hop2}"
    )


def test_nonsensitive_header_survives_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC5 — User-Agent and Accept survive a cross-host redirect (no blanket strip).

    The fix MUST be a denylist (strip only Authorization/Cookie/Proxy-Authorization),
    NOT a blanket strip of all caller headers on host change.  Functional headers that
    are intentionally host-agnostic (User-Agent, Accept) MUST be present on every hop.

    GREEN today; must stay GREEN after the denylist fix.  If this fails after the fix,
    the builder used a blanket strip instead of a denylist.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://cdn.other.net/asset"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={
            "Authorization": _AUTHZ,
            "User-Agent": _UA,
            "Accept": "application/octet-stream",
        },
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 2, f"Expected 2 hops, got {len(calls)}"
    hop2 = calls[1][1]
    assert "user-agent" in hop2, (
        f"AC5 regression: User-Agent was stripped on cross-host redirect — blanket strip "
        f"detected. Only the sensitive denylist must be stripped. hop-2 headers: {hop2}"
    )
    assert "accept" in hop2, (
        f"AC5 regression: Accept was stripped on cross-host redirect — blanket strip "
        f"detected. Only the sensitive denylist must be stripped. hop-2 headers: {hop2}"
    )


def test_cross_host_strip_is_sticky_through_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6 — sticky strip: A→B→A chain does NOT restore sensitive headers on the return to A.

    Hop 1: GET http://example.com/x        → 302 → http://other.net/b   (cross-host, strip)
    Hop 2: GET http://other.net/b          → 302 → http://example.com/final  (back to origin)
    Hop 3: GET http://example.com/final    → 200

    Assert: Authorization NOT present on hop 3 (the return to origin is sticky-stripped).

    RED today: Authorization is present on ALL hops because headers are unconditional.
    The assertion at hop 3 fires because the header leaks back on the A→B→A return.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.path == "/x":
            # Hop 1: example.com → other.net
            return httpx.Response(302, headers={"location": "http://other.net/b"})
        if request.url.host == "other.net":
            # Hop 2: other.net → example.com (back to origin)
            return httpx.Response(302, headers={"location": "http://example.com/final"})
        # Hop 3: example.com/final → 200
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={"Authorization": _AUTHZ, "User-Agent": _UA},
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 3, f"Expected 3 hops (A→B→A), got {len(calls)}: {[c[0] for c in calls]}"
    # Hop 2 (other.net) must not have Authorization
    hop2 = calls[1][1]
    assert "authorization" not in hop2, (
        f"G-NET-1 VIOLATED (AC6 hop-2): Authorization leaked to cross-host target "
        f"'other.net'. hop-2 headers: {hop2}"
    )
    # Hop 3 (return to example.com/final) must also not have Authorization — sticky strip
    hop3 = calls[2][1]
    assert "authorization" not in hop3, (
        f"G-NET-1 VIOLATED (AC6 sticky strip): Authorization was RESTORED on return "
        f"to origin host 'example.com' after A→B→A chain. "
        f"Sticky strip must not restore headers. hop-3 headers: {hop3}"
    )
    # Non-sensitive header must survive all hops including the return
    assert "user-agent" in hop3, (
        f"AC6: User-Agent (non-sensitive) was unexpectedly absent on hop 3. hop-3 headers: {hop3}"
    )


def test_sensitive_header_match_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC9 — strip is case-insensitive: 'AUTHORIZATION' (uppercase key) is still sensitive.

    The caller passes the header with an uppercase key.  httpx normalises it to lowercase
    internally ('authorization'), and the denylist match must be case-insensitive so the
    header is stripped on a cross-host redirect regardless of the caller's key casing.

    RED today: Authorization is forwarded unconditionally (no case-aware strip at all).
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://evil.net/steal"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        # Uppercase key — must still be treated as sensitive
        headers={"AUTHORIZATION": _AUTHZ, "User-Agent": _UA},
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 2, f"Expected 2 hops, got {len(calls)}"
    hop2 = calls[1][1]
    # httpx lowercases header names; 'AUTHORIZATION' arrives as 'authorization'
    assert "authorization" not in hop2, (
        f"G-NET-1 VIOLATED (AC9): AUTHORIZATION (uppercase key) leaked to cross-host "
        f"target 'evil.net'. Case-insensitive denylist matching failed. "
        f"hop-2 headers: {hop2}"
    )


def test_cookie_and_proxy_auth_also_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1/AC2 for Cookie and Proxy-Authorization — each denylist member is independently stripped.

    Runs two sub-scenarios to confirm Cookie and Proxy-Authorization are each stripped on a
    cross-host redirect, independently of Authorization.

    RED today: both headers are forwarded unconditionally.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)

    # --- Sub-scenario A: Cookie only ---
    calls_cookie: list[tuple[str, dict[str, str]]] = []

    def _cookie_handler(request: httpx.Request) -> httpx.Response:
        calls_cookie.append((request.url.host, dict(request.headers)))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://evil.net/steal"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={"Cookie": _COOKIE_VAL, "User-Agent": _UA},
        transport=httpx.MockTransport(_cookie_handler),
    ) as resp:
        resp.read()

    assert len(calls_cookie) == 2, f"Sub-A: expected 2 hops, got {len(calls_cookie)}"
    assert "cookie" not in calls_cookie[1][1], (
        f"G-NET-1 VIOLATED: Cookie leaked to cross-host target 'evil.net'. "
        f"hop-2 headers: {calls_cookie[1][1]}"
    )

    # --- Sub-scenario B: Proxy-Authorization only ---
    calls_proxy: list[tuple[str, dict[str, str]]] = []

    def _proxy_handler(request: httpx.Request) -> httpx.Response:
        calls_proxy.append((request.url.host, dict(request.headers)))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://evil.net/steal"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={"Proxy-Authorization": _PROXY_AUTH, "User-Agent": _UA},
        transport=httpx.MockTransport(_proxy_handler),
    ) as resp:
        resp.read()

    assert len(calls_proxy) == 2, f"Sub-B: expected 2 hops, got {len(calls_proxy)}"
    assert "proxy-authorization" not in calls_proxy[1][1], (
        f"G-NET-1 VIOLATED: Proxy-Authorization leaked to cross-host target 'evil.net'. "
        f"hop-2 headers: {calls_proxy[1][1]}"
    )


def test_no_redirect_sends_all_headers_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC7 — no redirect: all headers (including sensitive) reach the origin unchanged.

    Happy path: a 200 on the first hop means no stripping occurs at all.  Guards against
    accidental stripping on a direct (non-redirect) request.

    GREEN today; must stay GREEN after the fix.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    seen: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/resource",
        headers={
            "Authorization": _AUTHZ,
            "Cookie": _COOKIE_VAL,
            "User-Agent": _UA,
        },
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert seen.get("authorization") == _AUTHZ, (
        f"Authorization must be present on a direct (no-redirect) request. seen={seen}"
    )
    assert seen.get("cookie") == _COOKIE_VAL, (
        f"Cookie must be present on a direct (no-redirect) request. seen={seen}"
    )
    assert seen.get("user-agent") == _UA, (
        f"User-Agent must be present on a direct (no-redirect) request. seen={seen}"
    )


def test_same_host_port_only_change_keeps_sensitive_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port-only change on the same hostname does NOT strip sensitive headers (per spec).

    Origin: http://example.com:8080/x
    Redirect: 302 → http://example.com:9090/y  (same hostname, different port only)

    httpx.URL.host returns the bare hostname for both URLs ('example.com'), so the
    origin-host comparison is equal and no strip is triggered.  This matches
    requests.Session.rebuild_auth behaviour and is a deliberate spec choice.

    GREEN today (unconditional forwarding); must stay GREEN after the fix.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.path == "/x":
            return httpx.Response(302, headers={"location": "http://example.com:9090/y"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com:8080/x",
        headers={"Authorization": _AUTHZ, "User-Agent": _UA},
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 2, f"Expected 2 hops, got {len(calls)}"
    hop2 = calls[1][1]
    assert "authorization" in hop2, (
        f"Port-only-change regression: Authorization was stripped on a same-hostname "
        f"port-change redirect. Spec says port-only change must NOT strip. "
        f"hop-2 headers: {hop2}"
    )


# ---------------------------------------------------------------------------
# Gate S-2 phase 2, slice B (ADR 0119) — "auth-key" joins the G-NET-1 denylist
#
# The threatfox connector introduces the first `Auth-Key` secret header (abuse.ch's
# per-endpoint auth scheme). Spec: `docs/decisions/GATE_S2P2_ABUSECH_SIBLINGS_SPEC.md` §5 —
# "add `auth-key` to `_SENSITIVE_HEADERS` ... so the `Auth-Key` header is stripped on a
# cross-host redirect / https->http downgrade exactly like `Authorization`". These two tests
# mirror `test_sensitive_header_stripped_on_cross_host_redirect` /
# `test_sensitive_header_kept_on_same_host_redirect` mechanics exactly, substituting the
# `Auth-Key` header for `Authorization`.
#
# RED today: `_SENSITIVE_HEADERS` is `frozenset({"authorization", "cookie",
# "proxy-authorization"})` (net/ssrf.py:32) — "auth-key" is NOT a member, so
# `_scope_headers` never strips it and it survives onto the cross-host hop.
# ---------------------------------------------------------------------------

_AUTH_KEY_VAL = "k" * 8  # short dummy — never a real key (secret-scan hook)


def test_auth_key_header_stripped_on_cross_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G-NET-1 (ADR 0119 slice B): a cross-host 302 MUST NOT carry Auth-Key.

    Hop 1: GET http://example.com/x  (origin)
    Redirect: 302 -> http://other.example.net/dest  (DIFFERENT host)
    Hop 2: GET http://other.example.net/dest  -> 200

    Assert: hop-2 request headers do NOT contain Auth-Key.

    RED today: "auth-key" is absent from `_SENSITIVE_HEADERS`, so guarded_stream forwards it
    unconditionally and this assertion fires on the leak.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://other.example.net/dest"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={"Auth-Key": _AUTH_KEY_VAL, "User-Agent": _UA},
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 2, (
        f"Expected exactly 2 hops (origin + cross-host), got {len(calls)}: {[c[0] for c in calls]}"
    )
    hop2 = calls[1][1]
    assert "auth-key" not in hop2, (
        f"G-NET-1 VIOLATED (ADR 0119 slice B): Auth-Key leaked to cross-host target "
        f"'other.example.net'. hop-2 headers: {hop2}"
    )


def test_auth_key_header_kept_on_same_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth-Key IS forwarded same-host and on a same-host redirect (no strip triggered).

    Hop 1: GET http://example.com/x  -> 302 -> http://example.com/final
    Hop 2: GET http://example.com/final  -> 200

    Assert: Auth-Key IS present on hop 2 (same host, no strip).

    This mirrors `test_sensitive_header_kept_on_same_host_redirect` and must be GREEN both
    before and after the builder's denylist addition — it pins that the fix is scoped to
    cross-host/downgrade, never a blanket strip.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    calls: list[tuple[str, dict[str, str]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, dict(request.headers)))
        if request.url.path == "/x":
            return httpx.Response(302, headers={"location": "http://example.com/final"})
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/x",
        headers={"Auth-Key": _AUTH_KEY_VAL, "User-Agent": _UA},
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert len(calls) == 2, f"Expected 2 hops, got {len(calls)}"
    hop2 = calls[1][1]
    assert "auth-key" in hop2, (
        f"Auth-Key must be kept on a same-host redirect. hop-2 headers: {hop2}"
    )
    assert hop2["auth-key"] == _AUTH_KEY_VAL


def test_auth_key_header_forwarded_on_original_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth-Key IS forwarded on the ORIGINAL (non-redirected) request — the base happy path
    a threatfox fetch relies on."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_public)
    seen: dict[str, str] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.headers))
        return httpx.Response(200, content=b"ok")

    with guarded_stream(
        "GET",
        "http://example.com/export/json/recent/",
        headers={"Auth-Key": _AUTH_KEY_VAL, "User-Agent": _UA},
        transport=httpx.MockTransport(_handler),
    ) as resp:
        resp.read()

    assert seen.get("auth-key") == _AUTH_KEY_VAL, (
        f"Auth-Key must reach the origin request unchanged. seen={seen}"
    )
