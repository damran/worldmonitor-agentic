# 0116 — Full-text article bodies for extraction (Phase-4 pull-forward)

- **Status:** ACCEPTED (2026-07-18)
- **Date:** 2026-07-18
- **human_fork:** false — a reversible build-order decision inside the ADR 0115 product lane,
  operator-directed in-session (2026-07-18: "build the full-text enricher now"). Reversal =
  `FULLTEXT_ENABLED=false` (the code default) + drop the derived `article_text` table; no SoR data
  shape is locked in.
- **person_affecting:** false — no ER/merge/erasure logic changes; the pass reads Article nodes and
  writes a derived text cache + landing objects. Extraction's derived candidates still flow through
  the full resolver (merge guard, provenance-on-write) unchanged.

## Context

The news→event extraction pass (ADR 0115 Slice B) only ever sees the **headline** (and feed
summary): ftmg drops long text properties, so article bodies never reach the graph, and PR #189
recorded "title-only input" as its accepted limitation. This caps extraction quality (event typing,
actor recall) and brief quality. The roadmap filed full-text as a Phase-4 enricher; the operator
pulled it forward for the product lane (plan `stateless-tumbling-koala`, WP-2b).

## Decision

1. **A default-OFF driver pass** (`runner/fulltext.py`, `FULLTEXT_ENABLED`, own cadence) fetches
   the pages behind recent, not-yet-extracted curated-feed Articles (`sourceUrl`) — **pull-only**,
   through the SSRF-guarded `guarded_stream`, redirect-validated, byte-capped, bounded per cycle
   and per host. Fetch-for-analysis only; nothing is republished.
2. **Raw HTML lands in the landing zone** (`fulltext/feeds/<article-id>.html`) — the immutable,
   replayable raw store, per the connector invariant.
3. **The derived plain text goes to Postgres** (`article_text`, keyed by the Article's entity id),
   NOT the graph — deliberately outside Neo4j because ftmg drops long props and the graph is not a
   blob store. The table is a **rebuildable read-model cache** (re-derivable from the landed HTML),
   carrying `source_id`/`retrieved_at`/`raw_pointer` provenance columns and a bounded
   `attempts`/`last_error` retry ledger so dead URLs stop being refetched.
4. **Extraction reads the body when present** (truncated to `EXTRACTION_BODY_MAX_CHARS`), headline
   fallback otherwise — same defensive trust boundary; body text is hostile input like everything
   else.
5. Text derivation is a **dependency-free lxml paragraph extractor** (lxml is already a transitive
   dependency); no GPL extraction library (trafilatura) enters the tree.

This shape follows Slice B exactly (a bounded driver pass, not yet a framework plugin): the product
lane's light process applies (ADR 0115 §3), and the cheap always-on invariants hold — SSRF guard on
the new outbound fetch, provenance columns on the derived rows, leads-not-verdicts downstream.

## Consequences

- **Reversal cost:** low. Flip the flag off (default), drop `article_text` (migration downgrade);
  landed HTML ages out via the existing landing GC. No graph or SoR unwind.
- **Revisit triggers:** (a) Phase 4 proper — promote to an `INTERNAL_ENRICHMENT` plugin with
  manifest/config-schema when the enricher framework lane opens; (b) extraction quality still poor
  on paragraph-extracted text — consider a dedicated extractor (license-checked) then; (c) volume
  or per-host complaints — add robots.txt handling / per-host backoff beyond the current per-cycle
  host cap.
- **Trade-off accepted:** ~one page fetch per fresh article (bounded by
  `FULLTEXT_MAX_ARTICLES_PER_CYCLE` × cadence) when enabled; storage for landed HTML (byte-capped,
  GC-covered).
