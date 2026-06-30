"""SSRF-guarded outbound HTTP for connectors (ADR 0057).

Both ``geonames`` and ``opensanctions`` stream their dumps over HTTP. With
``follow_redirects=True`` a poisoned upstream or DNS answer can return a ``3xx`` to an internal
address — ``http://169.254.169.254/`` (cloud metadata), RFC1918, loopback, link-local, ULA — and
httpx would follow it, turning an outbound fetch into a Server-Side Request Forgery against the
deploy network. This module follows redirects manually with ``follow_redirects=False`` and
validates the resolved host of EVERY hop (initial URL + each ``Location``) against the blocked
address ranges BEFORE connecting.

Known limitation — DNS-rebinding TOCTOU: we resolve + validate, then httpx resolves again to
connect; a hostile resolver could answer differently between the two. Closing that fully needs
connect-time peer validation (a custom transport) — documented in ADR 0057 as the upgrade path.
A redirect to a *literal* internal IP/hostname is fully blocked here.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
from collections.abc import Generator, Mapping

import httpx

_DEFAULT_TIMEOUT = 120.0

# G-NET-1 (ADR 0087): headers whose value is scoped to the origin host and must NOT be
# forwarded to a redirect target on a different host (or on a same-host https→http downgrade).
# Matched case-insensitively against header key names.
_SENSITIVE_HEADERS: frozenset[str] = frozenset({"authorization", "cookie", "proxy-authorization"})


def _quiet_http_request_logging() -> None:
    """Stop ``httpx``/``httpcore`` from logging full request URLs (which can carry secrets).

    A request URL can legitimately carry a secret in a query param — e.g. OpenCorporates requires
    ``?api_token=<secret>`` (no header auth). ``httpx`` logs the FULL request URL at INFO
    (``logging.getLogger("httpx")``, ``"HTTP Request: GET <url> ..."``), so with the driver's root
    logger at INFO that secret would leak in plaintext to the driver log on every page fetch,
    defeating the connector's ``"secret": true`` flag + encryption-at-rest. Raising these loggers to
    WARNING suppresses the URL-bearing request line platform-wide, for every connector present or
    future, while still letting real WARNING+ errors surface. ``setLevel`` is idempotent, so calling
    this per-fetch (before the first request httpx issues) is harmless.
    """
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# Carrier-grade NAT (RFC 6598). Python's ``ipaddress`` does NOT flag it ``is_private``, but it is a
# shared internal range used for cloud/k8s/CGNAT internal services — a redirect to a literal
# ``100.64.x.x`` must not be a fetch target. Blocked explicitly (the predicates below miss it).
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


def _scope_headers(
    headers: Mapping[str, str] | None,
    *,
    origin_host: str,
    origin_scheme: str,
    next_url: str,
) -> dict[str, str] | None:
    """Return headers to use for the next redirect hop, stripping credentials if needed.

    G-NET-1 (ADR 0087): a sensitive header (member of ``_SENSITIVE_HEADERS``, matched
    case-insensitively) is stripped from the next hop whenever the redirect target triggers
    credential exposure:

    - **Cross-host**: ``next_host != origin_host`` (case-insensitive).  ``httpx.URL.host``
      already lowercases and excludes the port, so a port-only change on the same hostname
      compares equal and does NOT strip (deliberate, matches ``requests``).
    - **Scheme downgrade**: ``origin_scheme == "https"`` and ``next_scheme == "http"`` on the
      SAME host — the credential would travel in cleartext even though the hostname is identical.

    Non-sensitive headers (``User-Agent``, ``Accept``, …) are never stripped.

    The sticky-strip invariant is upheld automatically: once sensitive keys are absent from
    the returned dict, subsequent calls on that dict cannot restore them.  Callers therefore
    simply replace their working ``hop_headers`` with the return value and never need a
    separate ``stripped`` boolean.
    """
    if headers is None:
        return None

    parsed = httpx.URL(next_url)
    next_host = parsed.host.lower()
    next_scheme = parsed.scheme.lower()

    should_strip = next_host != origin_host.lower() or (
        origin_scheme.lower() == "https" and next_scheme == "http"
    )

    if not should_strip:
        return dict(headers)

    return {k: v for k, v in headers.items() if k.lower() not in _SENSITIVE_HEADERS}


class BlockedAddressError(RuntimeError):
    """Raised when a target or redirect host resolves to a non-public address.

    Signals an attempted egress to loopback / private (RFC1918) / link-local (incl. the cloud
    metadata service ``169.254.169.254``) / unique-local / reserved / multicast / unspecified
    space — the SSRF vector this guard closes.
    """


def _is_blocked_ip(ip: str) -> bool:
    """Return ``True`` if ``ip`` is a non-public address that must never be a fetch target.

    Blocks loopback / private (RFC1918, ULA ``fc00::/7``) / link-local (``169.254.0.0/16``,
    ``fe80::/10``) / reserved / multicast / unspecified, for IPv4 and IPv6. IPv4-mapped IPv6
    (``::ffff:a.b.c.d``) is unwrapped and the embedded IPv4 is checked too. An unparseable value
    fails closed (treated as blocked).
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # fail closed — an address we cannot reason about is not public

    if (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    ):
        return True

    if isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_V4:
        return True  # RFC 6598 carrier-grade NAT — not flagged is_private by ipaddress

    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return _is_blocked_ip(str(mapped))

    return False


def assert_public_host(host: str) -> None:
    """Raise :class:`BlockedAddressError` if ``host`` is (or resolves to) a non-public address.

    An IP-literal host is checked directly — DNS is never consulted. A hostname is resolved via
    :func:`socket.getaddrinfo` and EVERY returned address is checked; if any is blocked the host
    is rejected, the error naming the host and the offending IP.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _is_blocked_ip(host):
            raise BlockedAddressError(f"refusing to connect to blocked address: {host}")
        return

    infos = socket.getaddrinfo(host, None)
    for info in infos:
        sockaddr = info[4]
        ip = str(sockaddr[0])
        if _is_blocked_ip(ip):
            raise BlockedAddressError(
                f"host {host!r} resolves to blocked address {ip} — refusing to connect"
            )


@contextlib.contextmanager
def guarded_stream(
    method: str,
    url: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_redirects: int = 5,
    transport: httpx.BaseTransport | None = None,
    headers: Mapping[str, str] | None = None,
) -> Generator[httpx.Response]:
    """Stream ``method url`` with SSRF-validated, manually-followed redirects.

    Issues the request with ``follow_redirects=False`` and validates the host of every hop (the
    initial URL and each ``3xx`` ``Location``, resolved relative to the current URL) via
    :func:`assert_public_host` BEFORE connecting. Follows up to ``max_redirects`` public hops then
    yields the streaming response (the caller may then call ``raise_for_status`` / ``iter_bytes`` /
    ``iter_lines`` / ``read``). A redirect with no ``Location``, or exceeding ``max_redirects``,
    raises. ``transport`` is injectable for ``httpx.MockTransport`` unit tests.

    ``headers`` is an optional mapping of request headers.  Non-sensitive headers (``User-Agent``,
    ``Accept``, …) are forwarded on every hop.  Sensitive headers (``Authorization``, ``Cookie``,
    ``Proxy-Authorization`` — see ``_SENSITIVE_HEADERS``) are forwarded ONLY to the original
    request host; they are stripped before any hop whose host differs from the original request
    host, or whose scheme is an ``https → http`` downgrade on the same host (G-NET-1, ADR 0087).
    The strip is sticky: once triggered it is not reversed even if a later redirect returns to the
    origin host.  Headers default to ``None`` (no additional headers), keeping existing callers
    unaffected.

    Headers do NOT influence SSRF host validation — :func:`assert_public_host` is called on every
    hop's host exactly as before, independently of any ``headers`` value.
    """
    # Quiet httpx/httpcore request-URL logging BEFORE the first request issues — a request URL can
    # carry a secret query param (e.g. OpenCorporates ``?api_token=``) and httpx logs the full URL
    # at INFO. Idempotent; see ``_quiet_http_request_logging``.
    _quiet_http_request_logging()
    # When a transport is injected (unit tests with ``httpx.MockTransport``) we drive an explicit
    # ``httpx.Client`` so the transport is honoured. In production (``transport is None``) we use
    # the module-level ``httpx.stream`` per hop — the seam connectors' tests monkeypatch — which
    # itself opens and closes a short-lived client. Either way redirects are followed manually with
    # the host of every hop validated BEFORE the request leaves.
    client = (
        httpx.Client(timeout=timeout, follow_redirects=False, transport=transport)
        if transport is not None
        else None
    )
    # Capture the ORIGINAL origin host and scheme BEFORE the redirect loop so that
    # credential-scoping comparisons are always anchored to the host the caller named,
    # not to the previous hop (ADR 0087 D1).
    origin_parsed = httpx.URL(url)
    origin_host: str = origin_parsed.host.lower()
    origin_scheme: str = origin_parsed.scheme.lower()
    # Mutable working copy of caller headers; sensitive entries are dropped in-place
    # (by reassignment) whenever a cross-host or downgrade redirect is detected.
    hop_headers: dict[str, str] | None = dict(headers) if headers is not None else None
    current = url
    try:
        for _hop in range(max_redirects + 1):
            assert_public_host(httpx.URL(current).host)
            hop = (
                client.stream(method, current, headers=hop_headers)
                if client is not None
                else httpx.stream(
                    method,
                    current,
                    timeout=timeout,
                    follow_redirects=False,
                    headers=hop_headers,
                )
            )
            with hop as response:
                if getattr(response, "is_redirect", False):
                    location = response.headers.get("location")
                    if not location:
                        raise httpx.RemoteProtocolError(
                            f"redirect from {current} had no Location header",
                            request=response.request,
                        )
                    next_url = str(httpx.URL(current).join(location))
                    # G-NET-1: strip sensitive headers before following a cross-host or
                    # https→http-downgrade redirect.  The returned dict never re-adds
                    # stripped keys, so the strip is automatically sticky through the chain.
                    hop_headers = _scope_headers(
                        hop_headers,
                        origin_host=origin_host,
                        origin_scheme=origin_scheme,
                        next_url=next_url,
                    )
                    current = next_url
                    continue
                yield response
                return
        raise httpx.TooManyRedirects(
            f"exceeded {max_redirects} redirects starting from {url}",
            request=httpx.Request(method, current),
        )
    finally:
        if client is not None:
            client.close()
