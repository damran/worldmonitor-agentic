# 0057 — SSRF-guarded outbound HTTP for connectors

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** Phase-B #4 (`gate/connector-ssrf-redirects`) — a focused fix off `master`.
- **Addresses:** audit **M-9** (the SSRF half) — confirmed at file:line in Round-2 (flagged single-verifier;
  verification found it affects **both** stream connectors, not one).

## Context — the bug

`geonames/connector.py:97` and `opensanctions/connector.py:59` both do
`httpx.stream("GET", url, ..., follow_redirects=True)`. A poisoned upstream or DNS answer can return a
`3xx` redirect to an **internal** address — `http://169.254.169.254/…` (cloud metadata),
`http://10./172.16./192.168.` (RFC1918), `http://127.0.0.1`, link-local, ULA — and httpx will follow it,
turning an outbound fetch into a **Server-Side Request Forgery** against the deploy network
(Neo4j/MinIO/Zitadel/the metadata service). Inputs are already regex-constrained (country code, dataset),
so the URL itself is safe; the residual amplifier is the **redirect**.

## Decision

Add a small, reusable **SSRF guard** and route both connectors' streaming fetches through it instead of
`follow_redirects=True`:

- New module `src/worldmonitor/net/ssrf.py`:
  - `BlockedAddressError(RuntimeError)` — raised when a target/redirect resolves to a non-public address.
  - `_is_blocked_ip(ip) -> bool` — pure: blocks loopback / private (RFC1918) / link-local (incl.
    `169.254.169.254`) / unique-local / reserved / multicast / unspecified, for both IPv4 and IPv6
    (incl. IPv4-mapped IPv6), **plus carrier-grade NAT `100.64.0.0/10` (RFC 6598)** — a shared
    internal range that stdlib `ipaddress` does NOT flag `is_private`, so it is blocked explicitly.
    Uses the stdlib `ipaddress` module — no allowlist to maintain.
  - `assert_public_host(host)` — resolve the host via `socket.getaddrinfo` and raise
    `BlockedAddressError` (naming the host + offending IP) if **any** resolved address is blocked. An IP
    literal is checked directly.
  - `guarded_stream(method, url, *, timeout, max_redirects=5, transport=None)` — a context manager that
    issues the request with `follow_redirects=False`, validates the host of **every** hop (initial URL
    and each `3xx` `Location`, resolved relative to the current URL) before connecting, follows up to
    `max_redirects` public hops, then yields the streaming `2xx` response (after `raise_for_status`).
    Exceeding `max_redirects`, or a redirect with no `Location`, raises. `transport` is injectable so the
    guard is unit-testable with `httpx.MockTransport` (no network).
- `geonames._download_lines` and `opensanctions.collect` call `guarded_stream(...)` in place of
  `httpx.stream(..., follow_redirects=True)`. Behaviour for a legitimate public redirect (e.g. to a CDN)
  is preserved; a redirect to an internal address now raises `BlockedAddressError` instead of being
  followed.

## Alternatives considered

- **`follow_redirects=False` only.** Simplest, and the audit notes these endpoints "serve 200 directly".
  But it is brittle: the day either host adds a CDN/mirror redirect, the connector silently breaks (a
  `3xx` body is not a `4xx/5xx`, so `raise_for_status` would not even flag it). Rejected — a guard that
  *validates* redirects is both safer and non-fragile, and is reusable by future connectors/enrichers.
- **An allowlist of permitted hosts.** Over-fits today's two hosts; a new connector needs a code change.
  Deny-by-IP-range is the right default. (An allowlist could be layered on later.)
- **A custom httpx transport / `AsyncHTTPTransport` wrapper that blocks at connect time.** Cleaner
  TOCTOU story (validate the *connected* socket peer) but more code and httpx-internal coupling than this
  gate warrants. Named as the upgrade if DNS-rebinding TOCTOU becomes a real threat (see Consequences).

## Consequences

- The SSRF redirect vector is closed for both stream connectors; internal-address redirects fail loud.
- **Known limitation — DNS-rebinding TOCTOU:** the guard resolves + validates, then httpx resolves again
  to connect; a hostile resolver could answer differently between the two. Mitigating fully needs
  connect-time peer validation (the custom-transport upgrade above). Documented, not closed here — the
  realistic threat (a redirect to a *literal* internal IP/hostname) is fully blocked.
- `wikidata` enricher uses `httpx.get` with httpx's default `follow_redirects=False`, so it is not part of
  this gate; routing it through the guard too is a named follow-up.
- No migration; no merge/score/guard(sensitivity)/resolver change. **Not person-affecting** (network
  egress safety). `human_fork: false`.

## Reversibility

Reversible (network policy). Reversal cost: low — revert the two call sites + drop `net/ssrf.py`. Revisit
trigger: if DNS-rebinding TOCTOU is demonstrated in the threat model, upgrade to connect-time peer
validation via a custom transport.
