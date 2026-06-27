"""Phase-B #4 — SSRF-guarded outbound HTTP (ADR 0057).

``geonames/connector.py`` and ``opensanctions/connector.py`` both stream with
``follow_redirects=True``, so a poisoned upstream/DNS answer that returns a ``3xx`` to an
internal address — ``http://169.254.169.254/`` (cloud metadata), RFC1918, loopback, link-local,
ULA — turns an outbound fetch into a Server-Side Request Forgery against the deploy network.

These tests pin the guard the builder must satisfy (ADR 0057): a new module
``src/worldmonitor/net/ssrf.py`` whose ``guarded_stream`` issues requests with
``follow_redirects=False`` and validates the resolved IP of the initial URL AND every redirect
``Location`` against the blocked ranges BEFORE connecting.

RED today: ``worldmonitor.net.ssrf`` does not exist, so the top-level import raises
``ModuleNotFoundError`` and every test errors at collection — the right RED. GREEN once the
builder lands the module.

No live network (``httpx.MockTransport`` + monkeypatched ``socket.getaddrinfo``); no Docker.
Runs in the default ``pytest -m "not integration"`` job.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import httpx
import pytest

# Top-level import of the not-yet-built module — ModuleNotFoundError today (correct RED).
from worldmonitor.net.ssrf import (
    BlockedAddressError,
    _is_blocked_ip,
    assert_public_host,
    guarded_stream,
)

# --------------------------------------------------------------------------------------------------
# Helpers: build a fake ``socket.getaddrinfo`` so host validation is deterministic with NO network.
#
# Real ``getaddrinfo`` returns 5-tuples ``(family, type, proto, canonname, sockaddr)`` where
# ``sockaddr`` is ``(ip, port)`` for IPv4 — the guard reads ``sockaddr[0]``. IP-literal hosts never
# hit DNS (they are checked directly via the REAL ``_is_blocked_ip``), so for the load-bearing
# private-redirect test the metadata IP is blocked through the real code path, not a stub.
# --------------------------------------------------------------------------------------------------


def _getaddrinfo_returning(ip: str) -> Callable[..., list[tuple[object, ...]]]:
    """A ``getaddrinfo`` stand-in that resolves EVERY host to ``ip`` (one IPv4 5-tuple)."""

    def _fake(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _fake


# --------------------------------------------------------------------------------------------------
# _is_blocked_ip — pure, parametrized. The cloud-metadata case is the headline.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "169.254.169.254",  # cloud metadata service — THE SSRF target this gate closes
        "10.0.0.1",  # RFC1918
        "172.16.5.5",  # RFC1918
        "192.168.1.1",  # RFC1918
        "127.0.0.1",  # IPv4 loopback
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
        "fc00::1",  # IPv6 unique-local (ULA)
        "fe80::1",  # IPv6 link-local
        "::ffff:10.0.0.1",  # IPv4-mapped IPv6 of a private addr
    ],
)
def test_is_blocked_ip_blocks_non_public(ip: str) -> None:
    """Every internal / reserved address class is blocked (IPv4 and IPv6)."""
    assert _is_blocked_ip(ip) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_is_blocked_ip_allows_public(ip: str) -> None:
    """Genuinely public addresses (v4 and v6) are not blocked."""
    assert _is_blocked_ip(ip) is False


# --------------------------------------------------------------------------------------------------
# assert_public_host — resolves via getaddrinfo and raises on any blocked resolved IP.
# --------------------------------------------------------------------------------------------------


def test_assert_public_host_raises_on_private_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that DNS-resolves to the metadata IP is rejected, message naming the host."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("169.254.169.254"))

    with pytest.raises(BlockedAddressError) as excinfo:
        assert_public_host("evil.example.com")

    assert "evil.example.com" in str(excinfo.value)


def test_assert_public_host_passes_on_public_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname that resolves to a public IP passes silently (no raise)."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    assert_public_host("api.example.com")  # must not raise


def test_assert_public_host_blocks_ip_literal_without_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked IP-literal host is rejected by direct check — DNS is never consulted."""

    def _boom(*_a: object, **_k: object) -> list[tuple[object, ...]]:
        raise AssertionError("getaddrinfo must NOT be called for an IP-literal host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)

    with pytest.raises(BlockedAddressError):
        assert_public_host("127.0.0.1")


# --------------------------------------------------------------------------------------------------
# guarded_stream — the streaming context manager, exercised with httpx.MockTransport (no network).
# --------------------------------------------------------------------------------------------------


def test_guarded_stream_direct_200_yields_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public URL that answers 200 directly yields the streaming response body."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"hello")

    transport = httpx.MockTransport(_handler)

    with guarded_stream("GET", "http://example.com/x", transport=transport) as resp:
        body = b"".join(resp.iter_bytes())

    assert body == b"hello"


def test_guarded_stream_refuses_redirect_to_metadata_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE load-bearing SSRF assertion.

    The first hop (``example.com``, resolved public by the patched DNS) answers ``302`` with
    ``Location: http://169.254.169.254/secret``. The guard MUST validate that redirect target —
    an IP literal checked through the REAL ``_is_blocked_ip`` path — and raise
    ``BlockedAddressError`` BEFORE issuing the second request. We prove the second request never
    leaves by counting transport calls: exactly ONE (the initial hop), never the metadata fetch.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "169.254.169.254":  # must never be reached
            return httpx.Response(200, content=b"AWS-CREDENTIALS-LEAKED")
        return httpx.Response(302, headers={"location": "http://169.254.169.254/secret"})

    transport = httpx.MockTransport(_handler)

    with (
        pytest.raises(BlockedAddressError),
        guarded_stream("GET", "http://example.com/x", transport=transport),
    ):
        pass

    assert calls == ["example.com"], (
        "the guard connected to the metadata service — the redirect to 169.254.169.254 was "
        f"NOT blocked before connecting (transport saw: {calls})"
    )


def test_guarded_stream_follows_public_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirect to another PUBLIC host (a CDN) is followed and its 200 body is yielded."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example.com":
            return httpx.Response(200, content=b"ok")
        return httpx.Response(302, headers={"location": "http://cdn.example.com/x"})

    transport = httpx.MockTransport(_handler)

    with guarded_stream("GET", "http://example.com/x", transport=transport) as resp:
        body = b"".join(resp.iter_bytes())

    assert body == b"ok"


def test_guarded_stream_bounds_redirect_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """An infinite redirect loop among PUBLIC hosts terminates (does not hang) and raises.

    The handler always 302s to the next public host, so a naive follower loops forever. The guard
    must stop after ``max_redirects`` hops — proven both by the raise AND by a bounded transport
    call count (never unbounded). The error must NOT be a ``BlockedAddressError`` (every hop is
    public) — it is the too-many-redirects path.
    """
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        nxt = f"http://hop{len(calls)}.example.com/x"
        return httpx.Response(302, headers={"location": nxt})

    transport = httpx.MockTransport(_handler)

    with (
        pytest.raises(Exception) as excinfo,  # noqa: PT011 - asserted to be non-Blocked below
        guarded_stream("GET", "http://example.com/x", transport=transport, max_redirects=5),
    ):
        pass

    assert not isinstance(excinfo.value, BlockedAddressError), (
        "a public redirect loop must raise the too-many-redirects error, not BlockedAddressError"
    )
    # Bounded: initial hop + at most max_redirects follow-ups. Anything more means it looped.
    assert len(calls) <= 6, (
        f"redirect chain was not bounded by max_redirects (saw {len(calls)} hops)"
    )


def test_guarded_stream_redirect_without_location_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 3xx with no ``Location`` header is a malformed redirect — the guard raises, not hangs."""
    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo_returning("8.8.8.8"))

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)  # no Location

    transport = httpx.MockTransport(_handler)

    with (
        pytest.raises(Exception) as excinfo,  # noqa: PT011 - asserted non-Blocked below
        guarded_stream("GET", "http://example.com/x", transport=transport),
    ):
        pass

    assert not isinstance(excinfo.value, BlockedAddressError)


# --------------------------------------------------------------------------------------------------
# Surface check — the named symbols / error type the ADR mandates exist with the right shape.
# --------------------------------------------------------------------------------------------------


def test_blocked_address_error_is_runtime_error() -> None:
    """``BlockedAddressError`` is a ``RuntimeError`` subclass (ADR 0057)."""
    assert issubclass(BlockedAddressError, RuntimeError)
