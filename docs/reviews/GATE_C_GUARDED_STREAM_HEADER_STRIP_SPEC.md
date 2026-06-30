# Gate C — `guarded_stream` cross-host header strip

- **Status:** buildable
- **ADR:** `docs/decisions/0087-guarded-stream-cross-host-header-strip.md` (PROPOSED)
- **Source:** adversarial review 2026-06-29 of PRs #138–145 — cheap-high-value LOW
  (`session-review-2026-06-29-backlog`). 0 critical / 0 high.
- **Origin:** ADR 0081 / PR #141 added the optional `headers` param to `guarded_stream`.
- **Class:** security-hardening behaviour change (credential-exfiltration defence-in-depth).

## Problem

`guarded_stream` (`src/worldmonitor/net/ssrf.py`) follows redirects manually with
`follow_redirects=False`, re-validating every hop's host against the blocked-address ranges via
`assert_public_host` (line ~161). The SSRF guard is correct. The defect is orthogonal: the
caller-supplied `headers` mapping is forwarded **unconditionally** into every redirect hop —
`headers=headers` at line ~163 (injected-transport path) and ~170 (production `httpx.stream`
path). The loop variable `current` is rebound to the joined `Location` (line ~181) but `headers`
is never re-scoped.

Consequence: if a server answers a `30x` with a `Location` on a **different host**, any sensitive
header the caller scoped to the original host (`Authorization`, `Cookie`, `Proxy-Authorization`)
is replayed to the redirect target. A poisoned/compromised upstream, or an open-redirect on a
trusted host, becomes a credential-exfiltration channel. The SSRF host re-check does **not** stop
this — the redirect target can be a perfectly public attacker-controlled host.

Current in-tree callers pass only non-sensitive headers (`wikidata` sends `User-Agent` via
`_SPARQL_HEADERS`; `feeds`, `telegram`, `opencorporates`, `geonames` pass none or non-sensitive).
So this is **forward-looking defence-in-depth on a shared `net/` primitive**: the public API
already accepts arbitrary headers, and the first connector that passes a bearer token to an
authenticated source (a near-certain future, e.g. any API-key-in-header OSINT source from the
master inventory) inherits a silent leak. We fix the primitive once.

## Invariant (the gate's reason to exist)

> **G-NET-1 — credential scoping.** A sensitive header supplied to `guarded_stream` is sent ONLY
> to the host it was scoped to (the original request host). It is NEVER transmitted on a hop whose
> host differs from the original request host.

This sits alongside the existing SSRF invariant (every hop host is public-validated before
connect) — neither weakens the other.

## Fix specification

### Denylist (which headers are sensitive)

Strip the **known-sensitive set**, case-insensitively by header name, not all caller headers:

```
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
```

Rationale for a denylist (strip sensitive set) over a blanket strip (drop ALL caller headers on
host change): functional headers (`User-Agent`, `Accept`, `Accept-Language`) are intentionally
host-agnostic and SHOULD survive a cross-CDN redirect — that is exactly the
`wikidata → CDN` shape we already support and test (`test_guarded_stream_follows_public_redirect`).
Stripping them would be a needless functional regression with no security benefit. The denylist is
the same set `requests.Session.rebuild_auth` strips, plus `Cookie`/`Proxy-Authorization`
(browser/`requests` strip `Authorization` on host change; we extend to the other two credential
carriers because the primitive is generic).

This list is **defined in `ssrf.py`** (single source of truth), matched case-insensitively
against header keys.

### Cross-host comparison rule

Compare the **redirect target host against the ORIGINAL request host** (the host the credential
was scoped to), not against the immediately-previous hop. Capture the original host once before
the loop:

- `origin_host = httpx.URL(url).host`, compared **case-insensitively** (DNS hostnames are
  case-insensitive; `httpx.URL.host` already lowercases, but the comparison is written
  case-insensitively for robustness).
- On each follow, compute the next host from the joined `Location`. If `next_host != origin_host`,
  the headers used for the next hop have the sensitive set removed.
- **Scheme downgrade also strips.** Treat `https → http` as a credential-bearing-context change
  even when the host string is identical: a downgrade to cleartext on the same host still exposes
  the credential on the wire. So strip when `next_host != origin_host` **OR**
  `origin_scheme == "https" and next_scheme == "http"`. (`http → https` upgrade on the same host
  keeps headers.)
- **Port:** a port change on the same hostname does NOT strip (matches `requests`; same-host
  different-port is the same security origin for credential-scoping purposes). Documented as a
  deliberate choice, not an oversight.

### Chained-redirect behaviour (A → B → A)

Once the sensitive headers are stripped for a cross-host hop, they are **NOT restored** if a later
hop redirects back to `origin_host`. This matches `requests`/browser behaviour
(`rebuild_auth` does not re-add stripped `Authorization` on return to origin) and is the simplest
safe rule: a sticky strip. Implementation: maintain a single `stripped` boolean (or carry the
post-strip headers forward) — once stripped it stays stripped for the remainder of the chain. A
later same-host hop never re-widens the credential exposure.

This is conservative (a benign A→B→A chain loses auth on the final A hop) but secure and
predictable, and matches the dominant HTTP client. The first hop (the original request to
`origin_host`) always carries the full headers.

### Non-goals / unchanged

- SSRF host validation (`assert_public_host` on every hop) — unchanged, still runs first.
- The `headers=None` default path — unchanged (nothing to strip).
- `max_redirects` bound, no-`Location` raise, redirect-loop bound — unchanged.
- Non-sensitive headers — preserved across all hops.

## Acceptance criteria

1. `_SENSITIVE_HEADERS` is defined in `ssrf.py` and contains at least
   `authorization`, `cookie`, `proxy-authorization` (lowercased).
2. A redirect to a **different host** does NOT carry any sensitive header on the next-hop request
   (proven by inspecting `request.headers` at the MockTransport).
3. A redirect to the **same host** (any path/port, non-downgrade) DOES carry the sensitive header.
4. A redirect that is an `https → http` downgrade to the **same host** does NOT carry the
   sensitive header.
5. Non-sensitive headers (`User-Agent`, `Accept`) survive a cross-host redirect.
6. A→B→A chain: the final hop back to A does NOT carry the sensitive header (sticky strip).
7. `headers=None` and existing-caller behaviour are byte-for-byte unchanged (all current
   `test_ssrf_guard.py` and `test_ssrf_guarded_stream_headers.py` stay green).
8. The SSRF guard still fires on a cross-host redirect to a blocked address regardless of headers
   (existing `test_guarded_stream_blocks_redirect_to_metadata_even_with_headers` stays green).
9. Header-name matching is case-insensitive (`AUTHORIZATION` is stripped).

## Named tests (the proof)

In `tests/unit/test_ssrf_guarded_stream_headers.py` (extend the ADR 0081 suite):

- `test_sensitive_header_stripped_on_cross_host_redirect` — AC2. Authorization set; first hop on
  `example.com` 302s to `http://other.example.net/x`; assert the transport's second-hop request
  has NO `Authorization`.
- `test_sensitive_header_kept_on_same_host_redirect` — AC3. Same host, different path; assert
  `Authorization` present on hop 2.
- `test_sensitive_header_stripped_on_https_to_http_same_host_downgrade` — AC4.
- `test_nonsensitive_header_survives_cross_host_redirect` — AC5. `User-Agent` survives.
- `test_cross_host_strip_is_sticky_through_chain` — AC6. A→B→A; final A hop lacks the header.
- `test_sensitive_header_match_is_case_insensitive` — AC9. Pass `"AUTHORIZATION"`.
- `test_cookie_and_proxy_auth_also_stripped` — AC1/AC2 for the other two denylist members.

**Recommended (stronger) — property test** in
`tests/property/test_prop_guarded_stream_header_scoping.py`:

- `test_prop_sensitive_never_leaves_origin` — a Hypothesis strategy generates a redirect chain
  (list of hosts, schemes drawn from `{http, https}`, all public via patched DNS) and a header set
  mixing sensitive + functional names. Drive `guarded_stream`, record every transport request.
  **Metamorphic assertion:** for every recorded hop request, if the hop's host != origin host (or
  it is a same-host https→http downgrade after the credential was scoped), NO sensitive header is
  present. Functional headers may appear anywhere. `max_examples >= 150`, `deadline=None`.

This gate touches a **security invariant** but NOT the ER/merge/canonical-id/provenance/sensitivity
invariant class, so per CLAUDE.md build discipline the mandatory minimum is an **example test**
(AC2/AC4/AC6 above). The property test is **recommended** because the bug is exactly a
"some path through a redirect chain leaks" defect that example tests under-sample — and it is cheap
here (pure, hermetic, MockTransport). Builder SHOULD include it; reviewer should push back if it is
dropped without cause.

## Scope (exact)

- **Edit:** `src/worldmonitor/net/ssrf.py` — `guarded_stream` only (add `_SENSITIVE_HEADERS`
  constant + a small pure helper `_scope_headers(headers, *, origin_host, origin_scheme, next_url)
  -> Mapping | None`; rebind the per-hop headers before the follow). Update the `guarded_stream`
  docstring to state the scoping rule.
- **Edit (tests):** `tests/unit/test_ssrf_guarded_stream_headers.py`;
  **new** `tests/property/test_prop_guarded_stream_header_scoping.py`.
- **Do NOT touch** any caller (`wikidata.py`, `feeds`, `telegram`, `opencorporates`, `geonames`,
  `rest_api.py`) — the fix is entirely inside the shared primitive; callers are unaffected.
- **Do NOT touch** `.claude/gate.scope` — Gate B owns it this session.

Proposed `gate.scope` globs (for whoever runs the builder, to set when Gate B releases the file):

```
src/worldmonitor/net/ssrf.py
tests/unit/test_ssrf_guarded_stream_headers.py
tests/property/test_prop_guarded_stream_header_scoping.py
docs/decisions/0087-guarded-stream-cross-host-header-strip.md
docs/reviews/GATE_C_GUARDED_STREAM_HEADER_STRIP_SPEC.md
```

## Slice breakdown

Single small primitive; **one slice is sufficient and preferred** (the helper + the loop rebind +
its tests are not independently mergeable without leaving a half-applied security fix). Two
optional slices if the property suite is split out:

- **Slice C1 (core fix + example tests) — REQUIRED, individually mergeable.**
  Add `_SENSITIVE_HEADERS` + `_scope_headers` pure helper in `ssrf.py`; rebind per-hop headers in
  the redirect loop (capture `origin_host`/`origin_scheme` before the loop; both transport paths,
  lines ~163 and ~170, use the scoped headers). Add the seven example tests (AC1–AC9). Closes the
  invariant. Green on its own.
- **Slice C2 (property suite) — RECOMMENDED, individually mergeable, depends on C1.**
  Add `tests/property/test_prop_guarded_stream_header_scoping.py` (`test_prop_sensitive_never_
  leaves_origin`). Pure test addition; no source change. Can land in the same PR as C1 or
  immediately after.

No slice is person-affecting; no schema/API change; no default flips. The `headers` API signature
is unchanged (callers pass the same mapping; the primitive now scopes it internally).
