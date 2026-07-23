# Gate S-4 — Ransomware.live victim/group connector

> Buildable gate spec. Companion ADR: `docs/decisions/0120-ransomware-live-connector.md` (PROPOSED).
> Backlog row: `docs/fable-review/91_OG_HARVEST_BACKLOG.md` S-4. Precedent: ADR 0118/0119 +
> `docs/decisions/GATE_S2P2_ABUSECH_SIBLINGS_SPEC.md` (the abuse.ch sibling pattern). Every
> endpoint / T&C / data-shape claim traces to the research dossier (`scratchpad/S4_DOSSIER.md`)
> and the two live probe captures (`scratchpad/recentvictims.json`, `scratchpad/groups.json`),
> spot-verified against the installed code + FtM model 2026-07-23 — corrections are called out
> inline as **[CODE-WINS]**.

## 0. What this gate is (and is NOT)

**IS:** ONE passive `EXTERNAL_IMPORT` connector, `ransomware_live`, that turns the two free
Ransomware.live v2 datasets into provenance-stamped FtM entities:

- **groups** (`GET /v2/groups`) → one **Organization** per ransomware group, `topics=crime.cyber`
  (deliberately sensitive — group merges park for human review).
- **recentvictims** (`GET /v2/recentvictims`) → per claimed victim: one **Company** (the victim),
  a thin **Organization** (the claiming group), and an **UnknownLink** *edge* between them
  (`subject`=group, `object`=victim, `role="ransomware victim (claimed by group)"`). **This is the
  codebase's first edge-emitting `map()`** — provenance is stamped on the edge too (G1).

Plus the small **reliability threading** so this source is stamped Admiralty **`"E"`** (unreliable —
criminal self-declaration, operator-disclaimed as unverified), never the global `"B"` default.

**IS NOT** (explicit NON-goals — record in the ADR, do NOT build):
- **No PRO endpoints** (`api-pro.ransomware.live`, `X-API-KEY`). The config is PRO-ready
  (`api_key`, `url` override) but no PRO code, no PRO T&C dependency.
- **No backfill** via `/v2/victims/<year>/<month>`, `/v2/allcyberattacks`, the `data.ransomware.live`
  bulk dump, or the RSS feed. (Ransomware.live RSS is already a separate `feeds` seed row,
  `db/seed.py` line ~102 — a distinct connector emitting Articles; no conflict, do not touch it.)
- **No screenshots** re-hosted or fetched (`screenshot` stays raw-only in the landing zone).
- **No `ttps` → MITRE ATT&CK attack-pattern linking** (deferred STIX lane; `ttps` raw-only).
- **No geo enrichment** (the ISO-3166 `country` code is passed through; no geocoding / globe pin).
- **No `indicates`/actor-attribution edges**, no `/v2/group/<name>` single-group endpoint.
- **No dedupe in the connector** — L3 (Splink/nomenklatura) owns canonicalization (hard rule).

## 1. Locked invariants every slice must hold (CLAUDE.md)

- **G1 provenance on every node AND edge.** `map()` returns `stamp(entity, provenance)` for the
  victim Company, the group Organization(s), **and the UnknownLink edge**. Provenance round-trips
  via `get_provenance`. (Verified: `provenance/model.py:74-79` `stamp()` writes flat `wm_prov_*`
  context keys to any FtM entity, edge included.)
- **Append-only / never write the graph.** `EXTERNAL_IMPORT` / `PASSIVE`: `collect()` streams the
  feed to the landing zone, `map()` emits entities-with-provenance to the ER queue via `run_ingest`.
  The connector never touches Neo4j or the ledger.
- **Canonical↔canonical only via the guard.** Group Organizations carry `topics=crime.cyber` ⇒
  `guard.sensitivity.is_sensitive` is True (verified — clause (b) dot-ancestor: `crime` ∈
  `registry.topic.RISKS`), so any cluster containing a group Org **parks for human review**
  (`needs_review`, `guard/sensitivity.py:204`). Victim Companies **never** carry a risk topic. The
  UnknownLink edge is `matchable: false` (verified) ⇒ ADR 0119's `score_pairs` matchable pre-filter
  (`splink_model.py:523`) excludes it from Splink entirely — the edge can never fuzzy-fuse.
- **Deterministic, connector-minted ids from source natural keys** (§3). Re-ingest converges by
  identity, never by fuzzy matching. L3 resolves the victim Company against the graph
  (OpenCorporates / OpenSanctions / LEI) — the connector never dedupes.
- **`guarded_stream` for every fetch; bounded body reads** (16 MiB cap, feodo/threatfox
  `_read_bounded` idiom). **Malformed rows fail-soft `[]` in `map()`** (never raise per row); an
  empty/tiny feed is not an error.
- **Config via JSON-Schema** with `additionalProperties: false`; `api_key` uses `"secret": true`
  (threatfox `config.schema.json` precedent → UI password field, vault-encrypted).
- **Leads, not verdicts.** Victim claims are criminal self-declarations, operator-disclaimed as
  unverified. Nothing in this lane presents as a verdict: the allegation lives on the **edge**
  (`role="ransomware victim (claimed by group)"`), stamped reliability `"E"`; the victim **Company
  node carries no allegation topic/score**.

## 2. Registration, dataset parametrization, cadence (verified in-codebase — do NOT re-derive)

- **Registration is automatic.** `runner/driver.py::discover_connectors` and
  `api/main.py::_discover_registry` both `walk_packages(...)` over `worldmonitor.plugins.connectors`.
  A new connector is discovered the instant `connector.py` defines a `Connector` subclass with a
  unique `manifest.connector_id`. **No `plugins/registry.py` edit** (a Write to it is a red flag).
- **ONE connector, two datasets, parametrized by a `dataset` config value** — the **opensanctions
  precedent** (verified: `opensanctions/config.schema.json` declares `required: ["dataset"]`; two
  `SeedSpec` rows differ only by `dataset`). `dataset ∈ {"recentvictims","groups"}`. This gives the
  correct provenance `source_id` for free: `run_ingest` computes
  `source_id = f"{connector_id}:{dataset}".rstrip(":")` (`ingest.py:135`), i.e.
  `ransomware_live:recentvictims` / `ransomware_live:groups`, and lands raw under
  `ransomware_live/<dataset>/<key>.json`. **[CODE-WINS]** the dossier said "follow the abuse.ch
  sibling pattern" (separate connectors) — but the S-4 scope is a single package and the two
  datasets share almost all code, so opensanctions-style `dataset` parametrization is the fit;
  `config.schema.json` (secret `api_key`, `url` override) still follows the threatfox shape.
- **Cadence is a single global setting** (`settings.ingest_cadence_seconds`, default 3600 s). Each
  instance fetches exactly ONE endpoint ONCE per `collect()` (no pagination), so the free-tier
  **1 request / minute / endpoint** ceiling is satisfied 60× over by the 1 h cadence — the same
  structural argument the abuse.ch siblings use (S2P2 spec §2). **[CODE-WINS]** the dossier's "hard
  self-throttle ≥ 60 s/endpoint" is NOT implemented as an in-connector `sleep` (that would block the
  driver's ingest loop); it is satisfied by single-request-per-`collect()` + the global cadence.
  Do NOT add a per-connector interval or a sleep. (recentvictims and groups are *distinct*
  endpoints, so running both instances in one tick never breaches the per-endpoint minute.)

## 3. Field mapping (probed shapes → FtM; corrections marked [CODE-WINS])

### 3.1 Entity-id scheme (deterministic, namespaced, from source natural keys)

Aligned with the readable-prefix precedents (`mitre_attack/connector.py:219` → `f"mitre-{gid}"`;
`opencorporates/connector.py:126` → `f"opencorporates-{jurisdiction}-{company_number}"`) and the
hash-the-opaque-key precedent (`ontology/ioc.py::indicator_id` → `ioc-<sha1(value)>`). Define a
module-level helper:

```
_slug(x)  = re.sub(r"[^a-z0-9]+", "", x.lower())        # strip ALL non-alphanumerics
_h(s)     = hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]
```

| Entity | id | natural key | note |
|--------|----|-------------|------|
| group **Organization** | `f"ransomware-live-group-{_slug(slug)}"` | groups side: `_slug` of the last path segment of `groups[].url` (the site's authoritative slug, `.../group/<slug>`); victim side: `_slug(recentvictims[].group)` | **[CODE-WINS]** dossier proposed a *hash* of the slug — but the slug is a safe short token, so use the readable `mitre_attack`/`opencorporates` style. `_slug` (strip-all-non-alnum + lower) is the **only** normalization that converges all three observed forms — verified on the probe: name `"Booba Project"`, url-slug `boobaproject`, victim `group` `"Booba Project"`/`"BrainCipher"` all fold to one id. Do NOT derive the group id from `groups[].name` (has spaces/caps). |
| victim **Company** | `f"ransomware-live-victim-{_h(url)}"` | `recentvictims[].url` (the permalink; path = base64 of `<Victim>@<slug>` — the site's opaque victim key) | hash the opaque permalink (indicator_id rationale). de-dup `RawRecord.key` = the same `url`. |
| **UnknownLink** edge | `f"ransomware-live-claim-{_h(group_id + chr(10) + victim_id + chr(10) + url)}"` | subject-id + object-id + permalink | one victim record = one claim = one edge; converges on re-ingest. |

Re-ingesting the same record yields identical ids (pin in unit tests). The group id derived from the
victim side and from the groups side is **byte-identical** for the same group (pin this).

### 3.2 recentvictims → victim Company + group Organization + UnknownLink

| Source field (probed) | Target | FtM property | Rule |
|---|---|---|---|
| `victim` | Company | `name` | non-blank str, else `map()` → `[]` (fail-soft — the identity field). |
| `domain` | Company | `website` | omit when `""` (empty 14/100). |
| `country` | Company | `country` | ISO-3166 alpha-2, FtM-native; omit when `""` (empty 3/100). |
| `activity` | Company | `sector` | omit when `""` or the `"Not Found"` sentinel (16/100). |
| — | Company | *(no `topics`, no risk)* | **victims never get a risk topic** (invariant). |
| `group` | Organization (thin) | `name` + `weakAlias` = raw `group`; `topics=["crime.cyber"]` | id per §3.1; the thin org is the edge `subject`; folds by id with the rich groups-side org. |
| group id | UnknownLink | `subject` | |
| victim id | UnknownLink | `object` | |
| — | UnknownLink | `role` = `"ransomware victim (claimed by group)"` | fixed string (allegation-grade framing). |
| `attackdate` | UnknownLink | `date` via `entity.add(...)` | ISO w/ microseconds + offset; add via `entity.add` for FtM date-cleaning (threatfox `:264-265` idiom). |
| `claim_url` | UnknownLink | `sourceUrl` | the `.onion` leak-post URL. |
| `description` | UnknownLink | `summary` | omit the placeholders: `description.strip()` casefolded ∈ `{"", "n/a"}`, or a leading `"[ai generated]"` marker whose remainder is `n/a`/empty → omit. |
| `url` | — | *(natural key only)* | de-dup `RawRecord.key`; victim + edge id input. |
| `discovered`, `screenshot`, `data_size`, `press`, `ransom`, `infostealer` | — | **raw-only (landing zone)** | `infostealer` is **type-unstable** — `""` (52/100) *or* a nested dict (48/100); `collect()`/`map()` must tolerate BOTH and never map it. `data_size`/`press`/`ransom` null in the sample — do not assume null forever. |

### 3.3 groups → group Organization (rich)

| Source field (probed) | FtM property | Rule |
|---|---|---|
| `groups[].url` → last path segment | id (`_slug`) + `weakAlias` = the raw url-slug | the authoritative slug; also the join key. |
| `name` | `name` | never null. |
| `altname` | `alias` | omit when null (337/361). |
| `description` | `description` | omit when null (17/361); may contain operator editorial — map verbatim under the `"E"` stamp, no filtering. |
| `url` | `sourceUrl` | the group page. |
| `locations[].slug` (onion DLS mirrors) | `website` | one value per non-empty `slug`; omit when none. Reversible (could move to raw-only) — recorded as an ADR revisit trigger. |
| — | `topics` = `["crime.cyber"]` | **every group Org is sensitive** (both datasets). |
| `added_date`, `tools`, `ttps` | **raw-only (landing zone)** | `added_date` = profile-creation, not org founding (do not map to `incorporationDate`); `ttps` = future STIX lane (NON-goal). |

**Canonical-id leads** (resolution-time, NOT connector writes): `country` → ISO-3166 (direct);
victim Company → OpenCorporates/LEI via L3; group Org → Wikidata Q for notable groups (leads only).

## 4. `collect()` / `map()` behavior

**`collect(config)`** (mirror `threatfox/connector.py:124-201`):
1. `self.validate_config(config)`; read `dataset` (required, `∈ {"recentvictims","groups"}`),
   `url` (override; else the pinned default per dataset), `limit`, `api_key`.
2. `api_key`, when a non-blank str, rides as an HTTP header (PRO-ready). **[VERIFY at build]** the
   exact PRO header name is third-party-sourced (`X-API-KEY`, unconfirmed) — the free v2 endpoints
   ignore it, so send it but treat the header name as a build-time TODO; do not block on it.
3. `guarded_stream("GET", url, transport=self._transport, headers=...)`; `raise_for_status`;
   `_read_bounded` at **16 MiB** (fail-closed `ValueError` above the cap; groups ≈ 574 KB,
   recentvictims ≈ 90 KB — both fit, but assert the cap with a fabricated oversized body).
4. Parse the body as a **flat JSON array** (`[record, ...]` — NOT the abuse.ch `{id:[record]}`
   object shape). `if not isinstance(parsed, list): return`. For each element that is a `dict`,
   yield one `RawRecord` (`data = json.dumps(item)`, `content_type="application/json"`,
   `retrieved_at`), `key` = the permalink `url` (recentvictims) / the url-slug (groups). A non-dict
   element is skipped (no yield). `limit` hard-caps the yield count in order.
5. A `429` / `{"message":"1 per 1 minute"}` throttle body is not expected under the 1 h cadence; if
   it occurs, `raise_for_status` (429) surfaces it and the driver backs the instance off — do not
   special-case it.

**`map(record, *, provenance)`** — **shape-dispatch** (map() does not receive `config`):
- **[CODE-WINS]** because one connector serves both datasets and `map()` gets no config, dispatch on
  the record's own shape: a record with a non-blank `victim` string → the **victim path** (emit
  Company + thin group Org + UnknownLink edge); else a record with a non-blank `name` string → the
  **group path** (emit the rich group Org); else fail-soft `[]`. The two probed shapes are cleanly
  disjoint (`victim` keys vs `name`+`locations`/`ttps`) — pin both routes + the neither-shape case.
- Build entities via `validate_or_raise({...})` (FtM gate, `ontology/validation.py`); add dates via
  `entity.add(...)`; return `[stamp(e, provenance) for e in entities]` — **including the edge**.
- Reversible fallback if shape-sniffing ever proves fragile (record for the builder, do NOT build
  now): have `collect()` prefix `RawRecord.key`/embed a `_dataset` marker; noted only.

## 5. Reliability threading (Admiralty `"E"`, small + reversible) — **[CODE-WINS], own slice**

The dossier said `ConnectorInstance.reliability` (`db/models.py:362`) "can override". **Spot-check
correction:** that column has **no migration** — migration `0009_statement_spine.py`'s `reliability`
column is on the **`statement`** table, not `connector_instance`; the ORM field is model/DB **drift**
(tests pass via `create_all`, production alembic does not have the column). AND the live caller,
`driver.py:533`, calls `run_ingest(...)` **without** `reliability`, so today every source stamps the
`"B"` default (`ingest.py:116`). Threading `"E"` therefore requires three additive, reversible changes:

1. **New migration** `0015_connector_reliability.py` **[CODE-WINS correction: renamed from
   `0015_connector_instance_reliability.py` — that 35-char revision id overflows
   `alembic_version.version_num VARCHAR(32)`; every prior revision id equals its filename and
   `0013_erasure_scrub_dataset_index` sits exactly at the 32-char boundary, so this repo's chain
   depends on staying at/under it]** — `op.add_column("connector_instance",
   sa.Column("reliability", sa.String(length=16), nullable=True))` (+ `down_revision` = the current
   head `0014_article_text`; `downgrade()` drops it). Closes the drift; no data change.
2. **`SeedSpec`** (`db/seed.py:44-58`) gains `reliability: str | None = None`; `seed()` writes
   `ConnectorInstance(..., reliability=spec.reliability)`. Existing rows pass `None` (unchanged).
3. **`driver.py`** — in the claim txn (`driver.py:505-511`) also read `instance.reliability`, and pass
   `reliability=<that value> if not None else "B"` to `run_ingest` (`driver.py:533`). `None` ⇒ the
   existing `"B"` behavior for every other connector is byte-identical.

`ingest.py`'s `reliability` param already exists — no change (in scope for reference only).
**[CODE-WINS correction]** the sentence above ("`models.py` already declares the column — no
change") is **wrong**: `db/models.py:362` is `StatementRecord.reliability` (the `statement` table),
and `ConnectorInstance` had **no** `reliability` ORM field at all — not drift-from-a-migration, an
outright missing field. Slice 1 therefore adds **four** things, not three: the `ConnectorInstance`
ORM field itself (`db/models.py`), the migration, the `SeedSpec` field, and the driver thread.
**Known residual** (documented, not fixed here to keep the gate small): `operator_run.py` (manual
REST-triggered runs) still passes no `reliability` ⇒ a *manual* run of this connector stamps `"B"`.
The autonomous/live path (the driver, "shipping") stamps `"E"`. Recorded as an ADR revisit trigger.

## 6. Failure-mode table (fail-soft `[]` per row unless noted)

| Condition | Behavior | Where |
|-----------|----------|-------|
| blank/missing `victim` (victim record) | `map()` → `[]`, log WARNING | `map()` |
| record is neither victim- nor group-shaped | `map()` → `[]` | `map()` |
| `infostealer` is `""` or a dict | tolerated — raw-only, never mapped | `collect()`/`map()` |
| `activity="Not Found"` / `domain=""` / `country=""` | omit that property (still emit the Company) | `map()` |
| `description` `"N/A"`/`"[AI generated] N/A"` | omit the edge `summary` (still emit the edge) | `map()` |
| 4xx/5xx (incl. 429) | `raise_for_status` raises; driver isolates the instance (backoff, never aborts the tick) | `collect()` |
| body > 16 MiB | `ValueError` fail-closed | `_read_bounded` |
| empty / tiny 200 feed | NOT an error — 0/few records | `collect()` |
| top-level not a list | treat as empty (0 records), do not raise | `collect()` |
| non-dict array element | skip (no yield) | `collect()` |
| junk `attackdate` | `entity.add` silently drops it; edge still emitted | `map()` |

## 7. Seed rows (slice 3)

Append two `SeedSpec` rows to `SEED_CONNECTORS` (`db/seed.py`), each `enabled=True`,
`category="cti"`, **`reliability="E"`**, `url` spelled out explicitly (mitre/feodo/threatfox
precedent — operator sees + can override from the Integrations UI), **no `api_key`** in seed config:

```
SeedSpec("ransomware_live", "recentvictims",
    {"dataset": "recentvictims", "url": "https://api.ransomware.live/v2/recentvictims"},
    enabled=True, category="cti", reliability="E"),
SeedSpec("ransomware_live", "groups",
    {"dataset": "groups", "url": "https://api.ransomware.live/v2/groups"},
    enabled=True, category="cti", reliability="E"),
```

`test_seed.py` uses subset/`>=` assertions (unaffected);
`test_every_seed_config_is_valid_for_its_connector` will validate each seeded config against the new
`config.schema.json` (so `{dataset, url}` must be schema-valid).

## 8. `config.schema.json` (threatfox/opensanctions shape)

Draft 2020-12, `additionalProperties: false`, `required: ["dataset"]`:
- `dataset` — `string`, `enum: ["recentvictims", "groups"]`.
- `url` — `string`, `minLength: 1` (override; connector defaults per dataset when omitted).
- `limit` — `integer`, `minimum: 1`.
- `api_key` — `string`, `"secret": true`, `minLength: 1` (PRO-ready; UI password field, vault-encrypted).

## 9. Acceptance criteria + named tests

### Slice 1 — reliability threading (infra; connector-independent)
Scope: `db/migrations/versions/0015_connector_reliability.py` (new; see the §5 [CODE-WINS
correction] — renamed from `0015_connector_instance_reliability.py`, VARCHAR(32) revision-id
column), `db/seed.py`,
`runner/driver.py`, `docs/decisions/0120-*.md` (PROPOSED) + regenerated `docs/decisions/README.md`.
- **AC1** `SeedSpec(reliability="E")` persists to `ConnectorInstance.reliability`; a spec with
  `reliability=None` writes NULL. — `tests/unit/test_seed.py` (extend): a new case asserting the
  column round-trips; existing subset assertions still pass.
- **AC2** the driver passes an instance's `reliability` into `run_ingest`; `None` ⇒ `"B"` default,
  byte-identical to today for every existing connector. — `tests/unit/test_driver_reliability.py`
  (new) OR extend the driver ingest test: stub a connector, assert the `Provenance.reliability` on
  the enqueued entity equals the instance's value (`"E"`), and `"B"` when unset.
- **AC3** the migration upgrades (adds the column) and downgrades (drops it) cleanly against a real
  Postgres. — `tests/integration/test_migrations.py` (extend if present) OR an alembic
  upgrade/downgrade round-trip; at minimum a `create_all`-independent check that the column exists
  after `alembic upgrade head`.
- **AC4** ADR 0120 is PROPOSED and `docs/decisions/README.md` is regenerated by
  `python scripts/gen_adr_index.py` (CI `adr-index` gate green).

### Slice 2 — the `ransomware_live` connector (both datasets)
Scope: `plugins/connectors/ransomware_live/{__init__.py,connector.py,config.schema.json}` + tests.
Auto-discovered but NOT yet seeded (inert until slice 3) — CI green on unit tests alone.
Named tests — `tests/unit/test_ransomware_live_connector.py` (mirror
`tests/unit/test_threatfox_connector.py`; `httpx.MockTransport` via the `transport=` ctor kwarg —
**no live network, ever**; hermetic fixtures shaped like §3, NOT the raw probe files):
- **manifest** — `connector_id="ransomware_live"`, `EXTERNAL_IMPORT`/`PASSIVE`/`IMPLEMENTED`.
- **config schema** — a valid `{dataset:"groups"}` validates; missing `dataset` rejected; a smuggled
  key rejected (`additionalProperties:false`); `dataset` enum enforced; `api_key` carries
  `"secret": true`.
- **`collect()`** — over a hermetic recentvictims array: one `RawRecord` per element, `key` = the
  `url` permalink, honors `limit`, skips non-dict elements, tolerates the `infostealer` `""`/dict
  shapes; over a hermetic groups array: `key` = the url-slug; a fabricated > 16 MiB body raises
  (`_read_bounded`); the `api_key` header is present when configured and absent when not (assert on
  the recorded `httpx.Request.headers`); the default url per `dataset` is used when `url` omitted.
- **`map()` victim path** — a victim record → exactly THREE entities: victim `Company`
  (name/website/country/sector with the empty/"Not Found" omissions), thin group `Organization`
  (`crime.cyber`, `weakAlias`), and the `UnknownLink` edge (`subject`=group id, `object`=victim id,
  `role` fixed string, `date` from `attackdate`, `sourceUrl`=`claim_url`, `summary` from
  `description` with the `"N/A"`/`"[AI generated] N/A"` omission). Ids equal the independently
  computed `_slug`/`_h` oracles. **Provenance round-trips on ALL THREE** (`get_provenance` →
  `reliability` from the passed `Provenance`, flat `wm_prov_*`).
- **`map()` group path** — a groups record → one rich group `Organization`
  (name/alias/description/website/sourceUrl, `topics=["crime.cyber"]`), provenance round-trips;
  `added_date`/`tools`/`ttps` are NOT mapped.
- **convergence pin** — a victim record with `group="BrainCipher"` and a groups record with
  `url=".../group/braincipher"` produce the **same group Organization id**.
- **fail-soft** — blank `victim` → `[]`; a record that is neither shape → `[]`; id determinism
  (re-map ⇒ identical ids).

### Slice 3 — seed + property tests + ADR ACCEPTED
Scope: `db/seed.py` (two rows), `tests/property/*`, flip `0120-*.md` → ACCEPTED + regen README.
Depends on slices 1 (SeedSpec.reliability) + 2 (the connector).
- **MANDATORY `@given` property tests** (this gate touches the merge-guard + provenance invariants):

  `tests/property/test_prop_ransomware_live_sensitivity.py` (mirror
  `tests/property/test_prop_merge_guard.py` oracle style; `import strategies as wm`; pin
  `Settings(enforcement_profile="strict")` in the guard-driving test; wrap any container-backed body
  in `try/finally` engine disposal per the memory trap):
  - `test_group_org_with_cyber_topic_is_sensitive` — a group `Organization` built like `map()`'s
    output (`topics=["crime.cyber"]`, `@given` over generated names/weakAlias) → `is_sensitive(...)`
    is `True` (independent oracle: `crime.cyber` has RISKS ancestor `crime`). Fail = FAIL-OPEN.
  - `test_victim_company_is_never_topic_sensitive` — a victim `Company` built like `map()`'s output
    (name/country/sector, NO topics, `@given` over generated fields) → `is_sensitive(...)` is
    `False`. Pins "victims never get a risk topic".
  - `test_p_s4_victim_never_auto_merges_into_a_sensitive_group` (**the gate-mandatory negative
    property**) — for any generated victim `Company` + group `Organization(crime.cyber)` driven
    through the REAL merge helper at/above threshold: `cluster_and_merge([victim, group],
    [ScoredPair(victim_id, group_id, score≥0.92)])` then `needs_review(cluster, by_id)` (the guard
    called from `resolution/pipeline.py:404`) → for any cluster whose members include the group,
    `flagged is True` (parked — never auto-promoted). (Company/Organization share the LegalEntity
    family, so the pair is schema-compatible and WILL cluster; the sensitivity park is what stops
    the silent fuse.) Mirror `test_prop_indicator_lane.py::test_p_ind_2c...` mechanics for driving
    `cluster_and_merge`.

  `tests/property/test_prop_ransomware_live_matchable_gate.py` (mirror
  `tests/property/test_prop_matchable_gate.py`; `deadline=None` +
  `suppress_health_check=[HealthCheck.too_slow]`; pure in-process Splink/DuckDB, no DB/network):
  - `test_p_s4_no_pair_references_an_unknownlink_edge` — a generated corpus of victim Companies +
    group Orgs + `UnknownLink` edges → every `score_pairs(corpus)` pair's `left_id`/`right_id` is a
    matchable-schema entity; **no edge id ever appears** (edges are `matchable:false`, filtered
    before frame construction — this pins the FIRST edge-emitting connector against the S-2-phase-2
    gate). Assert `>= 1` edge is present in the generated corpus (generator contract).
  - `test_p_s4_adding_edges_is_non_interfering` — `set(score_pairs(M)) == set(score_pairs(M ∪ E))`
    for a matchable corpus `M` and a disjoint `UnknownLink` edge set `E`.

- **Recommended integration smoke** (Docker is available — memory): drive ONE hermetic victim record
  through `run_ingest` → `resolve_pending` → the writer and assert the `UnknownLink` edge is
  projected to Neo4j with `prov_*` on the relationship. **STOP-and-escalate** if the writer/ftmg
  projection cannot yet emit edges (first edge-emitting connector; do NOT force it — flag the human).
  `tests/integration/test_ransomware_live_edge_projection.py`.
- **ADR** flips to ACCEPTED; `python scripts/gen_adr_index.py` re-run; CI `adr-index` green.

## 10. Build-time verification (probe live ONCE, pull-only, bounded — NEVER in tests)

- `GET https://api.ransomware.live/v2/recentvictims` — confirm the flat array, the field names in
  §3.2, that `victim` is never null, the `infostealer` `""`|dict split, 200 anonymous.
- `GET https://api.ransomware.live/v2/groups` — confirm the flat array, `groups[].url` =
  `.../group/<slug>`, body < 16 MiB, 200 anonymous.
- Confirm the exact PRO header name only if/when a PRO key is adopted (out of scope; NON-goal).
- **STOP-and-flag** if either free endpoint returns 401/403 (the personal-tier posture changed) —
  do NOT silently switch to PRO; that is an operator/ADR decision.
- **Verify the writer projects an `UnknownLink` edge** (first edge-emitting connector) before
  claiming the gate done; escalate if not.

**Fixture note:** the scratchpad `recentvictims.json` (100 records) is complete; **`groups.json` is
truncated** (~430 KB, 291/361 records, unterminated) — use it only to read field shapes, never parse
it whole. All test fixtures are hand-crafted hermetic samples of the §3 shapes.

## 11. Slice independence + CI

Order 1 → 2 → 3. Slices 1 and 2 are mutually independent (threading infra vs the connector
package); slice 3 depends on both (seed row needs `SeedSpec.reliability` + the connector). Each
slice leaves `pytest -m "not integration"` + local integration + `ruff format --check .` (repo-wide)
green; each is one focused PR. ADR 0120 lands PROPOSED in slice 1 (with the first README regen) and
flips to ACCEPTED in slice 3 (0118/0119 precedent) — **every commit that adds or edits an ADR file
MUST re-run `scripts/gen_adr_index.py` in the same commit** (the `adr-index` CI check fails on drift;
`README.md` is in scope for exactly this reason). Builders never push; the coordinator merges on green.

## 12. Open items for the test-author / builder

1. **PRO header name** is unconfirmed (`X-API-KEY`, third-party-sourced). Free v2 ignores it — send
   it, mark the name a build-time TODO; do NOT block.
2. **Edge through the projector/fold** is unproven in this codebase (first edge). Slice-3 integration
   smoke is the tripwire — STOP-and-escalate if the writer can't emit edges.
3. **`operator_run.py` reliability residual** (§5) — manual runs stamp `"B"`; documented, not fixed.
4. **`groups[].locations[].slug` → `website`** is reversible (onion DLS ≠ a website); could be
   raw-only. Default = `website`; ADR revisit trigger recorded.
5. **`_slug` collisions** — two distinct groups whose names differ only by punctuation/spacing would
   fold to one id; both are `crime.cyber` ⇒ any resulting merge parks for human review (safe, not
   silent). Recorded as a revisit trigger.
