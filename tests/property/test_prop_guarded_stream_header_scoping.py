"""Gate C property/metamorphic tests — guarded_stream cross-host header scoping (ADR 0087).

Pins the G-NET-1 invariant over randomly generated redirect chains:

  "A sensitive header supplied to guarded_stream is sent ONLY to the host it was
   scoped to (the original request host). It is NEVER transmitted on a hop whose
   host differs from the original request host."

Metamorphic invariant tested:
  For every outgoing hop request, no denylisted header is present UNLESS:
  - that hop's host == original origin host, AND
  - the scheme is not an https→http downgrade (relative to the origin scheme), AND
  - no prior hop in the chain has already triggered stripping (sticky strip).

Functional (non-sensitive) headers survive ALL hops — a blanket strip is also a failure.

This is a SECURITY invariant (credential scoping / G-NET-1), not the ER/merge/canonical-id/
provenance class.  Per CLAUDE.md build discipline the property test is RECOMMENDED for this
gate because the defect is exactly a "some path through a redirect chain leaks" failure mode
that example tests under-sample.  A chain where every hop happens to be same-host is safe
by accident; a cross-host hop at any position in the chain triggers the leak.

All tests in this file are RED on the current tree because guarded_stream forwards
``headers=headers`` unconditionally.  Hypothesis finds failing examples quickly: any
generated chain with a cross-host element will contain a hop where the sensitive header
is present but must not be.

No live network — all tests use ``httpx.MockTransport`` + ``unittest.mock.patch``
(Hypothesis @given is not compatible with pytest monkeypatch fixtures).
"""

from __future__ import annotations

import socket
from unittest import mock

import httpx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from worldmonitor.net.ssrf import guarded_stream

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mirror of the denylist the builder will add to ssrf.py (single source of truth there;
# duplicated here so the test is self-contained and independent of the builder's naming).
_DENYLIST: frozenset[str] = frozenset({"authorization", "cookie", "proxy-authorization"})

# Fixed origin host.  A small set of hop-host variants gives enough cross-host / same-host
# combinations without an unbounded search space.
_ORIGIN_HOST = "origin.test"
_HOP_HOSTS = ["origin.test", "cdn.test", "api.test", "other.test"]

# Fake getaddrinfo stub: every hostname resolves to 8.8.8.8 (a public IP).
# Matches the pattern in tests/unit/test_ssrf_guard.py.
_FAKE_ADDR_RESULT: list[tuple[object, ...]] = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))
]


def _fake_getaddrinfo(host: str, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
    return _FAKE_ADDR_RESULT


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_SETTINGS = settings(
    max_examples=150,
    deadline=None,  # MockTransport + redirect-chain setup exceeds the 200 ms default
    suppress_health_check=[HealthCheck.too_slow],
)

# ---------------------------------------------------------------------------
# Helper: determine whether a redirect target triggers credential stripping
# ---------------------------------------------------------------------------


def _triggers_strip(
    next_host: str,
    next_scheme: str,
    *,
    origin_host: str,
    origin_scheme: str,
) -> bool:
    """Return True if the redirect to (next_host, next_scheme) triggers header stripping.

    Mirrors the spec rule (ADR 0087 D1):
    - Cross-host: next_host != origin_host (case-insensitive, both already lowercase
      because they come from httpx.URL.host / our test constants).
    - Scheme downgrade: origin was https, next is http (same host, cleartext exposure).
    Port-only changes are NOT a trigger (same hostname, same security origin for this purpose).
    """
    cross_host = next_host.lower() != origin_host.lower()
    downgrade = origin_scheme == "https" and next_scheme == "http"
    return cross_host or downgrade


# ---------------------------------------------------------------------------
# Property: G-NET-1 over generated redirect chains
# ---------------------------------------------------------------------------


@given(
    origin_scheme=st.sampled_from(["http", "https"]),
    chain=st.lists(
        st.tuples(
            st.sampled_from(_HOP_HOSTS),
            st.sampled_from(["http", "https"]),
        ),
        min_size=1,
        max_size=5,
    ),
)
@_SETTINGS
def test_prop_sensitive_never_leaves_origin(
    origin_scheme: str,
    chain: list[tuple[str, str]],
) -> None:
    """G-NET-1 metamorphic property over randomly generated redirect chains.

    Strategy:
    - ``origin_scheme`` in {http, https}; fixed origin host 'origin.test'.
    - ``chain`` is a list of (host, scheme) tuples representing the sequence of redirect
      targets (up to 5 hops).  Hosts are sampled from a small vocabulary that mixes
      'origin.test' (same-host) with 'cdn.test', 'api.test', 'other.test' (cross-host).
    - A recording MockTransport issues a 302 for each chain element and a 200 for the
      final request; all hops are intercepted without network.
    - ``socket.getaddrinfo`` is patched so every hostname resolves to 8.8.8.8 (public).

    Assertions (per hop k, 0-indexed):
    1. Hop 0 (initial request to origin): ALL sensitive headers present — no accidental
       stripping on the non-redirect request.
    2. Hop k (k > 0): if stripping has been triggered at any point up to and including
       hop k (cross-host or downgrade target in chain[0..k-1]), then NO sensitive header
       is present.  Stripping is sticky: once triggered, it is NOT reversed even if the
       chain returns to origin_host.
    3. Every hop (including cross-host): User-Agent IS present — the fix must be a
       denylist (strip only the sensitive set), not a blanket strip.

    RED today: guarded_stream forwards ``headers=headers`` unconditionally.  Any chain
    with at least one cross-host or downgrade element has a hop where a sensitive header
    is present but must not be.  Hypothesis finds this in the first few examples.

    max_examples=150, deadline=None: 150 examples sweep a wide variety of chain shapes
    (all-same-host, all-cross-host, mixed, upgrades, downgrades, A→B→A returns) while
    remaining hermetic and bounded.
    """
    # --- Build a recording MockTransport ---
    # hops[k] = (host_of_request_k, headers_dict_of_request_k)
    hops: list[tuple[str, dict[str, str]]] = []
    hop_idx: list[int] = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        hops.append((request.url.host, dict(request.headers)))
        idx = hop_idx[0]
        hop_idx[0] += 1
        if idx < len(chain):
            next_host, next_scheme = chain[idx]
            loc = f"{next_scheme}://{next_host}/step-{idx + 1}"
            return httpx.Response(302, headers={"location": loc})
        return httpx.Response(200, content=b"done")

    transport = httpx.MockTransport(_handler)
    request_headers = {
        "Authorization": "Bearer prop-test-token-g-net-1",
        "Cookie": "prop=test; id=123",
        "Proxy-Authorization": "Basic cHJvcDp0ZXN0",
        "User-Agent": "PropTestAgent/1.0 (G-NET-1)",
    }

    # max_redirects must cover the full chain; default=5, but be explicit.
    max_redir = len(chain) + 1

    with (
        mock.patch("socket.getaddrinfo", _fake_getaddrinfo),
        guarded_stream(
            "GET",
            f"{origin_scheme}://{_ORIGIN_HOST}/start",
            headers=request_headers,
            transport=transport,
            max_redirects=max_redir,
        ) as resp,
    ):
        resp.read()

    # --- Sanity: exactly len(chain)+1 requests were issued ---
    expected_total = len(chain) + 1
    assert len(hops) == expected_total, (
        f"Expected {expected_total} total hops (1 initial + {len(chain)} redirected), "
        f"got {len(hops)}.  origin_scheme={origin_scheme!r}, chain={chain!r}"
    )

    # --- Assertion 1: initial request (hop 0) carries ALL sensitive headers ---
    hop0_headers = hops[0][1]
    for sensitive in _DENYLIST:
        assert sensitive in hop0_headers, (
            f"G-NET-1 initial-request regression: {sensitive!r} absent from hop 0. "
            f"The original request to '{_ORIGIN_HOST}' must carry all headers — "
            f"no stripping before any redirect. hop-0 headers={hop0_headers!r}, "
            f"origin_scheme={origin_scheme!r}, chain={chain!r}"
        )
    assert "user-agent" in hop0_headers, (
        f"User-Agent absent from hop 0. request_headers={request_headers!r}"
    )

    # --- Assertions 2 + 3: walk through hops tracking sticky-strip state ---
    stripped = False
    for k, (hop_host, hop_headers) in enumerate(hops):
        # Assertion 2: if stripping is active, no sensitive header must appear
        if stripped:
            for sensitive in _DENYLIST:
                assert sensitive not in hop_headers, (
                    f"G-NET-1 VIOLATED at hop {k}: {sensitive!r} present despite "
                    f"stripping having been triggered earlier in the chain. "
                    f"hop-{k} host={hop_host!r}, stripped=True, "
                    f"origin={origin_scheme}://{_ORIGIN_HOST}, chain={chain!r}, "
                    f"hop-{k} headers={hop_headers!r}"
                )

        # Assertion 3: non-sensitive User-Agent survives every hop (no blanket strip)
        assert "user-agent" in hop_headers, (
            f"G-NET-1 blanket-strip detected at hop {k}: User-Agent absent. "
            f"Only the sensitive denylist {{authorization, cookie, proxy-authorization}} "
            f"must be stripped — functional headers must survive cross-host hops. "
            f"hop-{k} host={hop_host!r}, chain={chain!r}"
        )

        # Update sticky-strip state for next hop:
        # chain[k] is the redirect target that hop k redirects to (if k < len(chain)).
        if k < len(chain):
            next_host, next_scheme = chain[k]
            if _triggers_strip(
                next_host, next_scheme, origin_host=_ORIGIN_HOST, origin_scheme=origin_scheme
            ):
                stripped = True
