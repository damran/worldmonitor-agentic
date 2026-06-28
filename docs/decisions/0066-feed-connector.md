# 0066 — FeedConnector (RSS/Atom → FtM Article) (Phase-2 Stage-3 slice 2)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/3b-feed-connector` (off `master`). Second connector slice of Phase-2 Stage-3.
- **human_fork:** false (a connector is a status-tagged plugin; FtM has a native `Article` schema, so no
  L2 extension is introduced; every choice below is reversible).

## Context

Stage-3 slice 1 shipped the `RestApiConnector` base + OpenCorporates. The roadmap's next connector is a
**FeedConnector** (RSS/Atom → news). Two questions decided by orientation:
- **Ontology:** FollowTheMoney **4.9.2 ships a native `Article` schema** (`title`, `author`, `publishedAt`,
  `date`, `bodyText`, `summary`, `sourceUrl`, `publisher`, `language`, …) and a native `Event` schema. So
  per CLAUDE.md ("`wm:` extensions **only** where FtM can't reach"), the connector maps to FtM `Article`
  **with no `wm:` extension** — exactly as GeoNames used FtM `Address`. **No L2 contract change.**
- **Base:** feeds are XML, not paginated JSON, so `RestApiConnector` doesn't fit. The FeedConnector is a
  new plain `Connector` subclass (mirroring GeoNames), fetching feed XML via `guarded_stream`.

## Decision

**A single generic `FeedConnector` (`EXTERNAL_IMPORT` / `PASSIVE`) that fetches one RSS/Atom feed over the
SSRF guard, parses it with `feedparser`, and maps each entry to an FtM `Article` with provenance — v1 is
metadata-only.**

### 1. Parser — `feedparser` (reversible)
Add `feedparser` (the docs-named, de-facto Python feed parser — `docs/30_PLUGIN_FRAMEWORK.md:49`). It
normalizes RSS 2.0 / RSS 1.0 / Atom 1.0 into one structure, handles the date-format zoo, and — critically
for hostile input — **does not resolve external XML entities** (XXE-safe by default; it also sanitizes
entry HTML). Chosen over hand-rolling `lxml` (error-prone across feed variants; would need explicit XXE
hardening). Reversal cost: low (swap the parse call). Revisit trigger: a feed variant feedparser mishandles.

### 2. `FeedConnector` — one generic connector (not a base+instance)
Unlike `RestApiConnector` (which abstracts pagination across many sources), a feed connector is already
generic — any feed is just a `feed_url`. So **one** concrete `FeedConnector` class, not a base + instance.
- **Manifest:** `connector_id="feeds"`, `kind=CONNECTOR`, `mode=EXTERNAL_IMPORT`, `capability=PASSIVE`,
  `status=IMPLEMENTED`.
- **`config.schema.json`** (`additionalProperties:false`): `feed_url` (string, required), `max_items`
  (integer ≥1, default 100 — hard bound on entries per run), `timeout` (number, optional). No secret.
- **`collect(config)`**: `guarded_stream("GET", feed_url, transport=self._transport)` → `raise_for_status`
  → read the body **bounded to `_MAX_FEED_BYTES`** (fail-closed on an oversized/hostile feed) →
  `feedparser.parse(body)` → yield **one `RawRecord` per entry**, capped at `max_items`. `RawRecord.data`
  is the JSON of the normalized entry fields (`title`, `link`, `id`/`guid`, `author`, `published`/`updated`,
  `summary`, plus the feed-level `feed_title`/`language`); `key` = the entry's `id`/`guid` or `link` (stable).
- **`map(record, *, provenance)`**: parse the entry JSON → FtM **`Article`** via `validate_or_raise`
  (`title`, `author`, `publishedAt`=published, `date`, `sourceUrl`=link, `summary`, `publisher`=feed_title,
  `language`; set only present + FtM-valid properties) → `stamp(entity, provenance)`. Entity id is derived
  deterministically from the entry's guid/link (`feed-<stable>`). An entry with no link **and** no title →
  `[]` (fail-soft on one entry).
- Injectable `transport` ctor kwarg (like `RestApiConnector`) for hermetic `httpx.MockTransport` tests.

### 3. Deliberately deferred (decision-free v1)
- **Full-text (`bodyText`)** → a Phase-4 `INTERNAL_ENRICHMENT` enricher (fetch `sourceUrl` HTML via
  `guarded_stream` + extract with trafilatura/readability), **not** `collect()` (blocking, non-idempotent,
  scales poorly). v1 ships feed metadata only.
- **OPML bulk-import** (a *list* of feeds) is a UI/config concern (create N feed instances), not the
  connector — deferred.
- **`Event` mapping** — v1 emits `Article`; calendar/event feeds → `Event` is a later refinement.

### 4. Safety (treat feed XML as hostile)
SSRF: feed fetched only via `guarded_stream`. Bounded: `max_items` cap + `_MAX_FEED_BYTES` body cap.
**XXE/entity-expansion safe**: feedparser does not resolve external entities (no SSRF/file-read via a
crafted `<!DOCTYPE>`), and does not network-fetch on parse. Read-only: `collect → landing`, `map →
ER queue` candidates; **never writes the graph**. HTML in `title`/`summary` is stored as data (feedparser
sanitizes it); downstream rendering (the UI) must still escape — noted, not the connector's job.

## Consequences
- A second live connector; the first **news/article** source feeding the graph as FtM `Article`s with
  provenance. Establishes the feed pattern (any RSS/Atom URL). Unblocks the Phase-4 full-text enricher.
- **New dependency:** `feedparser` (`pyproject.toml` + `uv.lock`). Tests never hit the network
  (`httpx.MockTransport` + local feed fixtures).
- `EXTERNAL_IMPORT`/`PASSIVE`, read-only, SSRF + XXE + size bounded. **Not person-affecting** (emits
  candidates; L3 + merge guard own resolution). **No migration. No new datastore. Single-tenant.**

## Reversibility
Reversible — removable plugin + one dep. Reversal cost: low. Revisit triggers: feedparser mishandles a
variant → swap parser; full-text needed → the Phase-4 enricher; event feeds → add `Event` mapping; OPML
import → a UI bulk-add feature.

## Invariant gate note
A connector emits candidates (raw → landing, candidate → ER queue); it does not resolve/merge/write the
graph, so no ER/merge/canonical-id invariant is touched → no `@given` mandatory. The provenance invariant
IS exercised (every mapped `Article` carries `prov_*`, asserted via `get_provenance` round-trip).
Failing-test-first: manifest `EXTERNAL_IMPORT`/`PASSIVE`; config schema validates (feed_url required,
max_items bounded); `map()` emits an FtM `Article` (title/sourceUrl/publishedAt) + provenance over RSS 2.0
**and** Atom 1.0 fixtures; `collect()` yields one RawRecord per entry, is bounded by `max_items` + the byte
cap, fetches only via `guarded_stream`, and an XXE-payload feed neither reads a local file nor makes a
network call — all over `httpx.MockTransport` (no live HTTP).
