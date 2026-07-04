# ADR 0087 — `guarded_stream` cross-host header strip (review remediation)

- **Status:** ACCEPTED
- **Gate:** Gate C — `guarded_stream` cross-host header strip
  (`docs/reviews/GATE_C_GUARDED_STREAM_HEADER_STRIP_SPEC.md`).
- **Addresses:** adversarial review 2026-06-29 of PRs #138–145 — a cheap-high-value LOW against the
  optional `headers` param added to `guarded_stream` in ADR 0081 / PR #141. 0 critical / 0 high.
- **Touches:** outbound-HTTP credential handling (a security invariant), NOT the
  ER/merge/canonical-id/provenance/sensitivity invariant class. Extends ADR 0057 (SSRF guard) and
  ADR 0081 (the `headers` param); both stay Accepted.

## Context

ADR 0081 gave `guarded_stream` an optional `headers` mapping, forwarded into every hop so a
connector can attach a `User-Agent` (wikidata) or, in future, an auth token. ADR 0057's redirect
loop re-validates every hop's host against the blocked-address ranges (the SSRF guard) — that part
is correct and unchanged.

The review found that `headers` is forwarded **unconditionally** into every redirect hop
(`headers=headers` at `ssrf.py` ~163 and ~170), including hops whose `Location` points at a
**different host**. The SSRF guard only proves the redirect target is *public* — it does nothing to
stop a sensitive header (`Authorization`, `Cookie`, `Proxy-Authorization`) being replayed to that
different, possibly attacker-controlled, public host. A poisoned upstream or an open-redirect on a
trusted source becomes a credential-exfiltration channel.

No current in-tree caller passes a sensitive header (wikidata sends only `User-Agent`; the rest
pass none/non-sensitive), so there is **no live leak today**. This is defence-in-depth on a shared
`net/` primitive whose public API already accepts arbitrary headers: the first authenticated-source
connector (header bearer token) would silently inherit the leak. Fix the primitive once, before
that caller exists.

## Decision

### D1 — Strip a known-sensitive header set on cross-host (and same-host https→http) redirects

When following a redirect, scope the headers before issuing the next hop:

- **Denylist (defined in `ssrf.py`):**
  `_SENSITIVE_HEADERS = {"authorization", "cookie", "proxy-authorization"}`, matched
  case-insensitively against header names.
- **Strip condition:** remove the sensitive headers from the next hop when the redirect target host
  differs from the **ORIGINAL request host** (case-insensitive), **OR** when the hop is a
  same-host `https → http` downgrade. A same-host `http → https` upgrade, or a same-host
  different-port redirect, keeps the headers.
- **Comparison is against the original origin host**, not the previous hop — the credential was
  scoped to the host the caller named, so that is the only host allowed to receive it.
- **Sticky strip on chained redirects (A→B→A):** once stripped, the sensitive headers are not
  restored if the chain returns to the origin host. This matches `requests.Session.rebuild_auth` /
  browser behaviour (stripped `Authorization` is not re-added on return to origin) and is the
  simplest provably-safe rule.

**Denylist, not blanket strip.** We strip only the known-sensitive set so functional, host-agnostic
headers (`User-Agent`, `Accept`, `Accept-Language`) survive a cross-CDN redirect — the
`wikidata → CDN` shape already supported and tested. Dropping those would regress function for no
security gain. The set mirrors `requests` (`Authorization`) extended with the other two credential
carriers because this primitive is generic, not Wikidata-specific.

The fix is entirely inside `guarded_stream`; no caller changes; the `headers` API signature is
unchanged (callers pass the same mapping, the primitive now scopes it per-hop).

- **Classify (reversibility): reversible.** Behaviour-only change to a single shared function; no
  data shape, no schema, no public API surface, no metric. **Reversal cost: low** — delete the
  `_scope_headers` call (revert to `headers=headers`) in one function; the denylist/helper are
  self-contained. **Revisit trigger:** (a) a legitimate caller needs a credential to deliberately
  survive a cross-host redirect (e.g. a federated-auth flow) — then add an explicit opt-in
  `allow_cross_host_headers=True` kwarg rather than removing the default strip; (b) we adopt a real
  HTTP client's redirect handling (a custom `httpx` transport per the ADR 0057 upgrade path) that
  already implements `rebuild_auth` — then this becomes redundant and is removed.

## Consequences

- No live behaviour change for any current deployment (no caller passes a sensitive header today).
  The only observable effect is forward: a future hop that cross-host-redirects with a sensitive
  header set now drops that header instead of leaking it.
- Functional headers (`User-Agent` etc.) are unaffected on every hop, including cross-host — the
  existing `wikidata`/CDN redirect tests stay green.
- The SSRF guard (ADR 0057) is untouched and still runs first on every hop.
- Slightly more conservative than a benign A→B→A would strictly require (final-A hop loses auth),
  accepted for predictability and parity with `requests`.

## Alternatives considered

- **Blanket-strip ALL caller headers on host change.** Rejected: regresses functional headers
  (`User-Agent`, `Accept`) that are legitimately host-agnostic, for zero security benefit; would
  break the supported cross-CDN redirect shape.
- **Compare against the previous hop instead of the origin.** Rejected: a chain A→A'(same
  host)→B could be argued either way, but the credential is scoped to the *origin*; origin-anchored
  comparison is the correct security boundary and matches `requests`.
- **Restore headers when a chain returns to origin (A→B→A).** Rejected: more state, and `requests`
  /browsers do not do it; sticky strip is simpler and strictly safer.
- **Strip on same-host different-port too.** Rejected: same hostname is the same credential origin
  for this purpose (matches `requests`); over-stripping with no real exposure. Recorded so the
  choice is deliberate, not an oversight.
- **Leave it (no live leak today).** Rejected: it is a latent credential-exfiltration vector on a
  shared primitive whose API invites the dangerous input; the fix is small, hermetic, and
  caller-transparent.

## Tests

Mandatory minimum is an **example test** (this is a security invariant but outside the
ER/merge/canonical-id/provenance/sensitivity property-test class). A **property/metamorphic test
over redirect chains is recommended** and cheap here (pure, MockTransport) — it directly targets the
"some path through the chain leaks" failure mode that examples under-sample.

- **Unit (`tests/unit/test_ssrf_guarded_stream_headers.py`):** strip-on-cross-host,
  keep-on-same-host, strip-on-https→http-downgrade, non-sensitive-survives-cross-host, sticky-strip
  through A→B→A, case-insensitive match, cookie/proxy-auth also stripped (AC1–AC9 of the gate spec).
- **Property (`tests/property/test_prop_guarded_stream_header_scoping.py`, recommended):**
  `test_prop_sensitive_never_leaves_origin` — generated redirect chains + mixed header sets;
  metamorphic assertion that no sensitive header ever appears on a hop whose host != origin (or a
  same-host downgrade); `max_examples >= 150`, `deadline=None`.
- **Regression:** existing `test_ssrf_guard.py` and the ADR 0081
  `test_ssrf_guarded_stream_headers.py` suite (forwarding, SSRF-blocks-with-headers) stay green.
