# htmx Vendor Provenance

**Upstream project:** htmx (bigskysoftware/htmx)
**Exact vendored version:** 2.0.10
**License:** 0BSD (Zero-Clause BSD)
**Upstream URL:** https://raw.githubusercontent.com/bigskysoftware/htmx/v2.0.10/dist/htmx.min.js
**Retrieval date:** 2026-07-05
**sha256:** 71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de

## Purpose

`htmx.min.js` is the vendored, self-hosted front-end library backing the review-queue web
UI (Gate 1a, ADR 0103 Decision E). It is downloaded **at build time** and committed here —
never loaded from a CDN — so the UI is zero-egress (`docs/70_UI_AND_EXPERIENCE.md` §1.7/§9).
This mirrors the FtM schema vendor-as-data pattern (ADR 0098, `ontology/vendor/ftm/PROVENANCE.md`):
build-time vendoring is not runtime egress.

## Verifying the shipped bytes

```sh
sha256sum src/worldmonitor/api/static/vendor/htmx.min.js
```

The result MUST equal the `sha256` recorded above (`tests/unit/test_vendored_assets.py` enforces
this so the provenance record can never silently drift from the shipped file).

## Re-vendoring on upgrade

1. Pick the latest htmx **2.x** release tag from https://github.com/bigskysoftware/htmx/tags.
2. `curl -sL "https://raw.githubusercontent.com/bigskysoftware/htmx/<tag>/dist/htmx.min.js" -o src/worldmonitor/api/static/vendor/htmx.min.js`
3. Recompute the sha256 and update the `Exact vendored version` / `Upstream URL` / `Retrieval date` /
   `sha256` fields above.
4. Re-run `uv run pytest tests/unit/test_vendored_assets.py -q`.
