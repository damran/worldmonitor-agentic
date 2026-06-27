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
import socket
from collections.abc import Generator

import httpx

_DEFAULT_TIMEOUT = 120.0

# Carrier-grade NAT (RFC 6598). Python's ``ipaddress`` does NOT flag it ``is_private``, but it is a
# shared internal range used for cloud/k8s/CGNAT internal services — a redirect to a literal
# ``100.64.x.x`` must not be a fetch target. Blocked explicitly (the predicates below miss it).
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")


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
) -> Generator[httpx.Response]:
    """Stream ``method url`` with SSRF-validated, manually-followed redirects.

    Issues the request with ``follow_redirects=False`` and validates the host of every hop (the
    initial URL and each ``3xx`` ``Location``, resolved relative to the current URL) via
    :func:`assert_public_host` BEFORE connecting. Follows up to ``max_redirects`` public hops then
    yields the streaming response (the caller may then call ``raise_for_status`` / ``iter_bytes`` /
    ``iter_lines`` / ``read``). A redirect with no ``Location``, or exceeding ``max_redirects``,
    raises. ``transport`` is injectable for ``httpx.MockTransport`` unit tests.
    """
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
    current = url
    try:
        for _hop in range(max_redirects + 1):
            assert_public_host(httpx.URL(current).host)
            hop = (
                client.stream(method, current)
                if client is not None
                else httpx.stream(method, current, timeout=timeout, follow_redirects=False)
            )
            with hop as response:
                if getattr(response, "is_redirect", False):
                    location = response.headers.get("location")
                    if not location:
                        raise httpx.RemoteProtocolError(
                            f"redirect from {current} had no Location header",
                            request=response.request,
                        )
                    current = str(httpx.URL(current).join(location))
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
