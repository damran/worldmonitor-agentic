# 0065 — RestApiConnector base + OpenCorporates connector (Phase-2 Stage-3 slice 1)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/3a-opencorporates` (off `master`). First slice of Phase-2 Stage-3 (connectors + UI).
- **Milestone:** Phase 2 (`docs/40_ROADMAP.md:44`) — the read surface (Stage 2) is live; this begins the
  **live connectors** half. Needs **NO ADR 0019** (that gates only the StreamConnector).
- **human_fork:** false (a connector is a status-tagged plugin; every choice below is reversible).

## Context

The plugin framework is built and proven by two `EXTERNAL_IMPORT`/`PASSIVE` connectors — OpenSanctions
(`FtmBulkConnector`) and GeoNames (a plain `Connector`). The pipeline `collect() → landing.put() →
Provenance → map()/stamp() → ErQueueItem` runs through `runner/ingest.py::run_ingest`; the driver refuses
`ACTIVE` connectors (`ActiveConnectorRefused`). What's missing is the **generic paginated-JSON REST**
pattern the roadmap names (`docs/30`): every prior connector reads a bulk dump, not a paginated API.

This slice adds a reusable **`RestApiConnector`** base (paginated JSON over the SSRF guard) and its first
instance, **OpenCorporates** (company data — a strong ontology fit: canonical company identifiers).

OpenCorporates API v0.4 (verified): `GET https://api.opencorporates.com/v0.4/companies/search` with query
params `q`, `jurisdiction_code`, `page`, `per_page` (≤100), and `api_token` (a **secret**). Response:
`{"results": {"companies": [{"company": {name, company_number, jurisdiction_code, incorporation_date,
dissolution_date, company_type, current_status, registered_address_in_full, opencorporates_url,
registry_url, inactive}}], "page", "per_page", "total_count", "total_pages"}}`. Errors: 401 (auth),
403 (rate-limit), 404, 503. Default quota 200/month, 50/day.

## Decision

### 1. `RestApiConnector` base — `src/worldmonitor/plugins/rest_api.py` (mirrors `plugins/ftm_bulk.py`)
An abstract `Connector` subclass that implements `collect()` once for **page-based JSON** APIs and leaves
the source specifics to small hooks. `map()` stays abstract (each source maps its own FtM entities).

- **Concrete `collect(config)`**: validate config; for `page` in `1..min(total_pages, max_pages)`: fetch
  the page via `guarded_stream("GET", url)` (SSRF-validated, like GeoNames), `raise_for_status()`, read
  **at most `_MAX_RESPONSE_BYTES`** (fail-closed if exceeded — hostile-input bound), `json.loads`, extract
  the item list + `total_pages`, and **yield one `RawRecord` per item** (`key` = the source's stable item
  key; `data` = the item's JSON bytes; `content_type="application/json"`). Stop at `total_pages`,
  `max_pages` (hard cap), or an empty page.
- **Subclass hooks (abstract):** `_page_url(config, page) -> str` (build the request URL incl. params +
  token), `_extract_items(payload) -> list[dict]`, `_total_pages(payload) -> int`, `_record_key(item) -> str`.
- **Safety:** all fetches through `guarded_stream` (no direct `httpx` to an attacker-controlled host);
  pagination **bounded** by `max_pages`; per-page **byte cap**; the connector **never logs the token or the
  token-bearing URL** (redact). Transient failures (403 rate-limit, 5xx) propagate and are retried by the
  driver's backoff (ADR 0054); a 401 fails loud (misconfigured token). In-`collect` rate-limit-aware backoff
  is a noted future enhancement — v1 leans on the driver.
- **Secret-in-URL log hygiene (added after adversarial review):** OpenCorporates' `api_token` is a
  **required query parameter** (it has no header auth), so the request URL inherently carries the secret.
  `httpx` logs the full request URL at **INFO**, and the driver sets root logging to INFO — so the token
  would leak in plaintext to the driver log on every page fetch, defeating the `"secret": true` flag +
  encryption-at-rest. **Fix at the egress chokepoint** (`net/ssrf.py`, through which all platform HTTP
  flows): suppress the `httpx` + `httpcore` request-URL logging below `WARNING`. This protects **every**
  connector that ever puts a secret in a URL, not just OpenCorporates; `guarded_stream`'s behaviour is
  unchanged. The token-not-logged test captures **all** loggers (not just `worldmonitor`) so the leak is
  genuinely locked.

### 2. OpenCorporates connector — `src/worldmonitor/plugins/connectors/opencorporates/`
`connector.py` (subclasses `RestApiConnector`) + `config.schema.json` + `__init__.py`, registered the same
way GeoNames is.
- **Manifest:** `connector_id="opencorporates"`, `kind=CONNECTOR`, `mode=EXTERNAL_IMPORT`,
  `capability=PASSIVE` (the driver would refuse `ACTIVE`), `status=IMPLEMENTED`.
- **`config.schema.json`** (drives the UI form, JSON-Schema 2020-12, `additionalProperties:false`):
  `api_token` (string, **`"secret": true`** — UI password field, vault-encrypted at rest), `q` (search
  term), `jurisdiction_code` (optional filter), `per_page` (int 1–100, default 30), `max_pages` (int ≥1,
  default a small bound). `required: ["api_token", "q"]`.
- **Hooks:** `_page_url` builds `…/v0.4/companies/search?q=&jurisdiction_code=&per_page=&page=&api_token=`;
  `_extract_items` returns `payload["results"]["companies"]` (each unwrapped from its `{"company": …}`
  envelope); `_total_pages` returns `payload["results"]["total_pages"]`; `_record_key` =
  `f"{jurisdiction_code}/{company_number}"`.
- **`map()`** (mirrors GeoNames): build an FtM **`Company`** via `validate_or_raise` — `name`,
  `registrationNumber`=company_number, `jurisdiction`=jurisdiction_code, `incorporationDate`,
  `dissolutionDate`, `legalForm`=company_type, `status`=current_status, `address`=registered_address_in_full,
  `sourceUrl`=opencorporates_url (only set properties FtM validates) — `set_anchor(entity,
  "opencorporates_id", f"{jurisdiction_code}/{company_number}")`, then `stamp(entity, provenance)`. Entity id
  `f"opencorporates-{jurisdiction_code}-{company_number}"`. Returns `[]` for a malformed/identity-less record
  (fail-soft on a single row, not the batch).

### 3. Provenance + write path (unchanged framework)
`collect()` writes raw → landing (via `run_ingest`); `map()` stamps every entity with `Provenance`
(`source_id="opencorporates:{q/jurisdiction}"`, `retrieved_at`, `reliability` default `"B"`, `source_record`
= the landing S3 URI); candidates go to the **ER queue** (`ErQueueItem`, idempotent on
`(source_record, entity_id)`). **Never writes to the graph** (L3 resolves). Dedup/merge is L3's job.

## Alternatives considered
- **A bespoke OpenCorporates `collect()` with no base.** Works, but the roadmap explicitly wants a reusable
  paginated-REST base; extracting it now (proven by a real instance) avoids re-deriving pagination per source.
- **Yield one `RawRecord` per page** (not per item). Rejected: per-item matches GeoNames/OpenSanctions
  (one raw record → one entity → one `source_record` pointer), giving clean per-entity provenance + idempotency.
- **In-`collect` rate-limit/backoff handling.** Deferred — the driver already retries with backoff (ADR 0054);
  v1 keeps the base simple and fails loud only on auth.
- **`ACTIVE` capability.** No — a read-only importer is `PASSIVE`; `ACTIVE` is refused by the driver and is a
  later gated slice.

## Consequences
- The first **live paginated-API connector**; the `RestApiConnector` base unblocks future REST sources
  (the FeedConnector and others) without re-deriving pagination. OpenCorporates needs an API token configured
  (via the Integrations UI, a later slice) to run live; tests never make live calls (`httpx.MockTransport`).
- `EXTERNAL_IMPORT`/`PASSIVE`, read-only into landing + ER queue, SSRF-guarded, bounded, secret token
  encrypted at rest. **Not person-affecting** (import + candidate-enqueue; L3 + the merge guard own resolution).
  **No migration. No new datastore. Single-tenant.**

## Reversibility
Reversible — a connector is a removable, status-tagged plugin; the base is additive. Reversal cost: low.
Revisit triggers: a source needs cursor/Link-header pagination → extend the base (it's page-based in v1);
rate-limit backoff needed in-`collect` → add it; a STREAM source → that's the StreamConnector (ADR 0019).

## Invariant gate note
A connector emits **candidates** (raw → landing, candidate → ER queue) — it does **not** resolve, merge, or
write the graph, so it does not touch an ER/merge/canonical-id invariant directly (those live in L3 and are
already gated). Therefore **no `@given` is mandatory**. The provenance invariant IS exercised: every mapped
entity must carry `prov_*` (asserted via `get_provenance` round-trip on the `map()` output). Failing-test-first:
manifest is `EXTERNAL_IMPORT`/`PASSIVE`; config schema validates (api_token secret + required q); `map()`
emits an FtM `Company` with the `opencorporates_id` anchor + provenance over a **real-API-shaped fixture**;
`collect()` paginates across pages, is bounded by `max_pages`, fetches only via `guarded_stream`, never logs
the token, and makes **no live HTTP** (driven by `httpx.MockTransport`).
