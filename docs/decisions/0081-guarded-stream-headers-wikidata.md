# 0081 — Optional `headers` extension to `guarded_stream` + route Wikidata through the SSRF guard

- **Status:** accepted
- **Date:** 2026-06-29
- **Gate:** Stage-4 MEDIUM/LOW sweep — ADR 0057 consistency gap (wikidata enricher bypassed the guard).
- **Touches:** `src/worldmonitor/net/ssrf.py` (one new optional kwarg to `guarded_stream`);
  `src/worldmonitor/plugins/enrichers/wikidata.py` (use `guarded_stream` instead of bare `httpx.get`;
  add `transport` ctor param for test injection); `tests/unit/test_ssrf_guarded_stream_headers.py`
  (11 new tests). **No schema change, no migration, no new runtime dependency, not person-affecting**
  (`human_fork: false`). Does NOT touch `resolve_pending` / `run_ingest` / ER-streaming.

## Context

ADR 0057 establishes a hard discipline: **ALL outbound HTTP from connectors/enrichers goes through
`guarded_stream`** (SSRF-validated, manually-followed redirects, blocked private/internal addresses).
`guarded_stream` lives in `src/worldmonitor/net/ssrf.py` and is used by every connector:
`RestApiConnector` (ADR 0065), `FeedConnector` (ADR 0066), the GeoNames and OpenSanctions connectors.

One gap remained: `src/worldmonitor/plugins/enrichers/wikidata.py::WikidataEnricher._lookup_qid` called
`httpx.get(...)` DIRECTLY — the only enricher/connector that bypassed the SSRF guard. A crafted DNS
answer or a redirect from the SPARQL endpoint could route the enricher to an internal address without
the guard blocking it.

A second, smaller issue: the Wikidata SPARQL endpoint (and Wikimedia policy generally) requires a
descriptive `User-Agent` header on every request. The existing `httpx.get` call sent one. `guarded_stream`
had no `headers` parameter, so routing through the guard would have silently dropped the `User-Agent`
without this fix.

## Decision

### D1 — Extend `guarded_stream` with an optional `headers` kwarg (BACKWARD-COMPATIBLE)

Add `headers: Mapping[str, str] | None = None` to `guarded_stream`. The default is `None` (no new
headers), so all existing callers are unaffected. When non-`None`, the mapping is threaded into BOTH
request paths:

- The **injected-transport path** (`httpx.Client.stream(method, current, headers=headers)`).
- The **production path** (`httpx.stream(method, current, ..., headers=headers)`).

**Host validation is unchanged.** `assert_public_host` is still called on the host of EVERY hop
(initial URL + each redirect `Location`) before connecting — independently of any `headers` value.
Headers do not influence SSRF validation: the guard is host-based, not header-based. This is the
security-primitive change reviewed against the existing test suite (`test_ssrf_guard.py` — all 12
pre-existing tests pass unchanged).

### D2 — Route `WikidataEnricher._lookup_qid` through `guarded_stream`

Replace the bare `httpx.get(...)` with a `guarded_stream("GET", url, headers=_SPARQL_HEADERS,
timeout=self._timeout, transport=self._transport)` call. Query-string params (`query`, `format=json`)
are baked into the URL via `urllib.parse.urlencode` (guarded_stream has no `params` arg).

The `except` clause is widened to also catch `BlockedAddressError` (what `assert_public_host` raises
on a blocked host) alongside the existing `(httpx.HTTPError, KeyError, ValueError)` — so a blocked host
is treated identically to a network error (best-effort: return `None`, no anchor set). This preserves
the original "lead not verdict" behavior.

### D3 — Add `transport: httpx.BaseTransport | None = None` to `WikidataEnricher.__init__`

Stored as `self._transport` and passed to `guarded_stream(transport=...)`. Default `None` ⇒ production
behavior is unchanged (real HTTP). Mirrors the pattern used by `RestApiConnector` and `FeedConnector`
so tests can inject `httpx.MockTransport` without making live network calls.

## Alternatives considered

- **Leave the `User-Agent` header to a default `httpx.Client` header:** rejected — Wikimedia's user
  agent policy explicitly requires identifying the application; a generic `python-httpx/x.y.z` string
  risks rate-limiting or blocks on their end.
- **Add a separate `guarded_get` wrapper instead of a `headers` kwarg on `guarded_stream`:** rejected —
  the existing callers all use `guarded_stream`; adding a second entry point would split the surface and
  make it easier to miss future callers. A keyword argument is the least-surprise, backward-compatible
  extension.
- **Add a `params` kwarg to `guarded_stream` as well:** out of scope for this gate. The wikidata use
  case only needs headers + URL-with-baked-params. `params` can be a follow-up if a future enricher
  needs it.

## Consequences

- **ADR 0057 gap closed:** every enricher and connector now routes ALL outbound HTTP through
  `guarded_stream`. The Wikidata SPARQL enricher is no longer the exception.
- **Backward-compatible:** existing `guarded_stream` callers (GeoNames, OpenSanctions, RestApiConnector,
  FeedConnector) pass no `headers` kwarg and behave exactly as before.
- **SSRF safety unchanged:** `assert_public_host` validation is called on every hop regardless of
  `headers`. The new test `test_guarded_stream_blocks_private_host_even_with_headers` and
  `test_guarded_stream_blocks_redirect_to_metadata_even_with_headers` prove this.
- **No `@given` property test required:** this gate touches the SSRF primitive but does NOT touch an
  ER/merge/canonical-id/provenance/sensitivity invariant — per CLAUDE.md, the `@given` mandate applies
  to gates that touch those invariants. The SSRF guard is fully covered by the existing
  `test_ssrf_guard.py` property-style parametrized tests; the new tests add 3 more SSRF-safety proofs.

## Reversibility

**Reversible.** Reversal cost: low — `guarded_stream` drops the `headers=None` kwarg (one-line revert);
`wikidata.py` reverts to `httpx.get(...)` (five lines); remove `test_ssrf_guarded_stream_headers.py`.
No data-shape lock-in, no migration, nothing public-facing.

**Revisit trigger:** if a future enricher needs to pass `params` to `guarded_stream` (no such case today),
extend the function in the same backward-compatible manner.
