# Gate S-2 phase 2 — abuse.ch sibling IOC connectors + the Splink `matchable` fix

> Buildable gate spec. Companion ADR: `0119-abusech-sibling-connectors.md` (PROPOSED).
> Every endpoint / auth / licensing / format claim traces to the verified research summary
> (`scratchpad/research_summary.md`, 63 CONFIRMED / 6 corrected, as of 2026-07-21). The parent is
> ADR 0118 (`wm:Indicator` + Feodo); this executes ADR 0118's open defense-in-depth trigger and
> lands the three sibling connectors ADR 0118 anticipated.

## 0. What this gate is (and is NOT)

**IS:** (A) close the ER defense-in-depth gap ADR 0118 left open — `score_pairs` must consult
`schema.matchable` and never let a non-matchable entity enter the Splink fuzzy path; (B/C/D) add
three abuse.ch bulk-export connectors (`threatfox`, `urlhaus`, `sslbl`) that each emit
`wm:Indicator` nodes through the SHARED `ontology.ioc.indicator_id` scheme, seeded enabled.

**IS NOT:** any `indicates → Organization` edge (attribution). The deterministic chain ends at the
malware family; family→actor is fuzzy exactly where IOC volume lives (`win.cobalt_strike` → 21
actors). Attribution is the designated **S-2 phase 3** enricher (see ADR 0119 §Consequences). No
edges are created by any connector in this gate.

## 1. Locked invariants every slice must hold (CLAUDE.md)

- **G1 provenance on every node** — `map()` returns `stamp(entity, provenance)`; provenance
  round-trips via `get_provenance`. No edges are produced, so "on every edge" is vacuously held.
- **Ids ONLY via `ontology.ioc.indicator_id`** — never a connector-minted id. The same normalized
  IOC value from feodo/threatfox/urlhaus/sslbl converges on ONE node by identity, never by fuzzy
  matching. Pinned per connector (unit) and by the slice-A property suite (non-matchable entities
  can never fuzzy-pair, so id-only convergence is the sole cross-connector mechanism).
- **Append-only / never write the graph** — `EXTERNAL_IMPORT` / `PASSIVE`; `collect()` streams the
  feed, `map()` emits entities-with-provenance. Connectors never touch Neo4j/the ledger.
- **Canonical↔canonical only via the guard** — Indicators are `matchable: false` and `extends:
  [Thing]`; slice A adds the third, systemic gate (Splink pre-filter). No connector can influence a
  person/org merge.
- **`guarded_stream` for every fetch; bounded body reads** (16 MiB cap, feodo `_read_bounded`
  idiom); **malformed rows fail-soft `[]` in `map()`** (never raise per row); an **empty/tiny feed
  is not an error** (Feodo-empty-feed precedent, research corrected verdict).
- **Config via JSON-Schema** with `additionalProperties: false`; the `auth_key` field uses
  `"secret": true` (opencorporates `api_token` precedent → UI password field, vault-encrypted).

## 2. Registration, seeding, cadence (verified in-codebase — do NOT re-derive)

- **Registration is automatic.** `runner/driver.py::discover_connectors` and
  `api/main.py::_discover_registry` both `pkgutil.walk_packages(...)` recursively over
  `worldmonitor.plugins.connectors` and register every concrete `Connector` subclass found. A new
  connector is discovered the instant its `connector.py` defines a `Connector` subclass whose
  `manifest.connector_id` is unique. **`src/worldmonitor/plugins/registry.py` needs NO edit** (it is
  in scope only so the guard permits reading/inspecting it; a Write to it is a spec-level red flag).
- **Cadence is a single global setting.** `settings.ingest_cadence_seconds` (default **3600 s = 1 h**,
  `driver.py:772`) reschedules every batch instance; there is no per-connector cadence field. 1 h ≥
  the abuse.ch 5-minute etiquette floor for ALL four feeds (research: "poll no more often than every
  5 minutes"). So the three new connectors inherit the feodo cadence with **no code change** — the
  etiquette is satisfied structurally. Do NOT add a per-connector interval.
- **Seeding** (`db/seed.py::SEED_CONNECTORS`): add three `SeedSpec` rows, `enabled=True`,
  `category="cti"`, natural_key = the export name, **`url` spelled out explicitly** in the config
  (matching each connector's own pinned default — ADR 0117 residual-c / mitre_attack + feodo
  precedent) so an operator sees and can override the exact endpoint from the Integrations UI. **No
  `auth_key` in any seed config** (secret; operator adds it later if abuse.ch gates the endpoints).
  `category` is informational only (not stored). `test_seed.py` uses subset/`>=` assertions, so the
  additions do not break it; `test_every_seed_config_is_valid_for_its_connector` WILL validate each
  new seeded config against the new connector's schema (so the seeded `{"url": ...}` must be valid).

## 3. Shared `ioc_type → indicatorType` vocabulary (cross-connector consistency — LOAD-BEARING)

Node identity is the IOC value (via `indicator_id`); `indicatorType` is a descriptive property. But
consistency matters so the same value from two feeds reads the same. The canonical map:

| Source | source kind | `indicatorType` emitted | note |
|--------|-------------|-------------------------|------|
| feodo (existing) | `ip:port` | `ipv4` | precedent — the anchor the others must match |
| threatfox | `ip:port` | `ipv4` | **MUST equal feodo exactly** (same C2 IP converges cleanly) |
| threatfox | `domain` | `domain` | |
| threatfox | `url` | `url` | |
| threatfox | `md5_hash` | `md5` | |
| threatfox | `sha1_hash` | `sha1` | file hash |
| threatfox | `sha256_hash` | `sha256` | |
| threatfox | *unknown member* (`envelope_from`, `sha3_384_hash`, future) | `lower(raw ioc_type)` | **pass-through, never drop the IOC** — the value is real evidence; an unknown type is a labeling gap, not a discard reason (leads-not-verdicts) |
| urlhaus | always a URL | `url` | |
| sslbl | SHA1 cert fingerprint | `sha1_cert` | **DISTINCT** from threatfox `sha1` (a certificate fingerprint, not a file hash) |

`indicatorType` is a free-form string in `Indicator.yaml` (`type: string`), so pass-through is legal.
The observed threatfox `ioc_type` enum is OPEN (research line 23) — treat it as such; the six mapped
members cover >99% of volume, the pass-through rule handles the tail.

## 4. Slice A — `score_pairs` consults `schema.matchable` FIRST

**Scope (files):** `src/worldmonitor/resolution/splink_model.py` (only); tests under `tests/`.

**Decision (executes ADR 0118's open trigger):** before frame construction, filter `entities` to
those whose `schema.matchable` is True. A non-matchable schema (every `wm:Indicator`) NEVER enters
the DataFrame, the blocking, the linker, or `predict`. The existing `< 2 → []` short-circuit now
applies to the *matchable* subset (so a corpus of all-Indicators, or one Indicator + one Person,
short-circuits to `[]` exactly as an under-2 corpus does today). The existing post-`predict`
`_schema_compatible` guard STAYS (defense in depth for transitive/sibling clashes among matchable
schemas). Amend `docs/decisions/0118-wm-indicator-feodo.md` revisit-trigger (a): mark the
`score_pairs`-matchable path **EXECUTED (S-2 phase 2)** — precedent: the S-2b "EXECUTED EARLY" note.

**Acceptance criteria:**
1. `score_pairs([...])` returns no `ScoredPair` whose `left_id` or `right_id` resolves to an entity
   with `schema.matchable is False`, for ANY input mix.
2. Non-matchable entities are removed BEFORE `_flatten`/frame construction (not merely dropped after
   `predict`) — provable by non-interference (criterion 4) and by the short-circuit (criterion 3).
3. A corpus with `< 2` matchable entities returns `[]` (even if it has many Indicators).
4. **No live-path regression:** for a matchable-only corpus M and any disjoint Indicator set I,
   `score_pairs(M)` == `score_pairs(M ∪ I)` as a set of `(left_id, right_id, probability)`. (This is
   the implementation-agnostic form of "byte-identical to today": adding non-matchable entities
   cannot perturb the pairs among matchable ones — proving both the pre-filter and its neutrality.
   `prior`/m/u are fixed constants, not corpus-size-derived, so equality is exact.)

**Named tests:**
- `tests/property/test_prop_matchable_gate.py` (**MANDATORY `@given`** — ER-invariant touch):
  - `test_p_match_1_no_pair_references_a_non_matchable_entity` — generated mixed corpus of
    Indicators + Persons/Orgs, **including colliding names/values** (an Indicator whose
    `indicatorValue` equals an Org `name`; a Person and Org sharing a name to force a real candidate
    pair) → assert every returned pair's two ids are both matchable-schema entities; assert no
    Indicator id ever appears.
  - `test_p_match_2_adding_indicators_is_non_interfering` — generated matchable-only corpus M and a
    generated Indicator set I → `set(score_pairs(M)) == set(score_pairs(M ∪ I))`.
  - Use `deadline=None` + `suppress_health_check=[HealthCheck.too_slow]` (heavy Splink;
    `test_prop_indicator_lane.py` precedent). No DB, no network — pure in-process Splink/DuckDB.
- `tests/unit/test_splink_matchable_filter.py` (concrete oracle):
  - Indicator with `indicatorValue == "acme ltd"` + Org "Acme Ltd" + Person "Acme Ltd" → the
    Indicator never pairs; the Org/Person pair (if any) is unchanged vs a run without the Indicator.
  - All-Indicator corpus (≥2) → `[]`. One Indicator + one Org → `[]`.

## 5. Slice B — `threatfox` connector

**Scope (files):** `src/worldmonitor/plugins/connectors/threatfox/{__init__.py,connector.py,config.schema.json}`;
`src/worldmonitor/net/ssrf.py` (one-line hardening, below); tests under `tests/`.

**G-NET-1 hardening (this slice introduces the secret header, so this slice hardens it):** add
`"auth-key"` to `_SENSITIVE_HEADERS` (`net/ssrf.py:32`) so the `Auth-Key` header is stripped on a
cross-host redirect / https→http downgrade exactly like `Authorization` (ADR 0087 semantics —
abuse.ch does not redirect cross-host today, but the key must never survive one). Pin with one test
beside the existing G-NET-1 strip tests (find them via `_SENSITIVE_HEADERS`/`guarded_stream` strip
coverage in `tests/`): an `Auth-Key` header is forwarded same-host and stripped cross-host.

**Endpoint (default):** `https://threatfox.abuse.ch/export/json/recent/` — legacy unauthenticated
bulk export, 48 h window (~4.1k IOCs, ~2.6 MB), regenerated every 5 min (research CONFIRMED
2026-07-21). Config-overridable via `url` (feodo/mitre precedent). Optional `auth_key`
(`secret: true`) sent as HTTP header `Auth-Key: <value>` when present, via `guarded_stream(...,
headers={"Auth-Key": ...})`.

**Record shape (verified, research line 19):** the body is a JSON **object** keyed by numeric IOC id;
each value is a **one-element list** of record dicts. Fields: `ioc_value`, `ioc_type`, `threat_type`,
`malware` (Malpedia key e.g. `win.cobalt_strike` or `"unknown"`), `malware_alias`,
`malware_printable`, `first_seen_utc` (`"YYYY-MM-DD HH:MM:SS"`), `last_seen_utc` (nullable),
`confidence_level`, `is_compromised`, `reference`, `tags` (comma-separated **string** in the bulk
export, not an array), `anonymous`, `reporter`.

**`collect()`:** stream over `guarded_stream`; `raise_for_status`; `_read_bounded` (16 MiB). Parse
JSON; **traverse `{id: [record, ...]}`** — for each key, for each record in the list, yield one
`RawRecord` (data = `json.dumps(record)`). A top-level value that is not a list, or a list element
that is not a dict, is skipped (no yield). `limit` hard-caps yield count in traversal order.

**`map()` (one record → one Indicator or `[]`):**
- `value = ioc_value` (str, non-blank) else `[]` (fail-soft).
- `entity_id = indicator_id(value)`; `name` = `indicatorValue` = `value`.
- `indicatorType` per §3 (`ip:port`→`ipv4`, …, unknown→`lower(ioc_type)`); if `ioc_type` is absent
  emit no `indicatorType` rather than raising.
- `malwareFamily` = `malware_printable` **only when** `malware_printable` is a non-empty str AND the
  `malware` key is not `"unknown"` (and `malware_printable` is not `"Unknown"`); else omit.
- `firstSeenAt` = `first_seen_utc`, `lastSeenAt` = `last_seen_utc` (nullable → `entity.add(None)` is a
  no-op) via `entity.add()` (FtM date cleaning; feodo precedent).
- `datasets = ["threatfox"]`. No `topics`, no `country`, **no `indicates` edge**.
- **401/403 handling:** a `raise_for_status` 401/403 must surface an **actionable** message naming
  the auth requirement, e.g. *"threatfox: 401/403 — the legacy export appears gated; register a free
  Auth-Key at auth.abuse.ch and set the connector's `auth_key`"* (the researched deprecation risk).
  Raise (loud) — do not silently return `[]`. (An empty but 200 feed IS fine.)

**Named tests** (`tests/unit/test_threatfox_connector.py`, `httpx.MockTransport`, no live network):
- manifest (`connector_id="threatfox"`, EXTERNAL_IMPORT/PASSIVE/IMPLEMENTED);
- config schema: empty validates; `additionalProperties:false` rejects a smuggled key; `auth_key`
  carries `"secret": true`;
- `collect()` over a hermetic `{id:[record]}` fixture yields one record per inner element; unwraps
  the list; skips non-list/non-dict; honors `limit`;
- `Auth-Key` header IS sent when `auth_key` configured, ABSENT when not (assert on the recorded
  `httpx.Request.headers`);
- 401 response → raises with the actionable message (assert substring `auth`/`Auth-Key`);
- `map()`: `ip:port` → `ipv4` (id equals `indicator_id("<ip:port>")`, computed independently via
  stdlib `hashlib` as an oracle, feodo precedent); `domain`/`url`/`md5_hash`/`sha1_hash`/`sha256_hash`
  → mapped labels; unknown `ioc_type` (`envelope_from`) → `lower(raw)` and the IOC is still emitted;
  `malware="unknown"` → no `malwareFamily`; blank/missing `ioc_value` → `[]`; timestamps ISO-normalize;
  `datasets == {"threatfox"}`; no topics/country/edges; provenance round-trips; id deterministic.
- **Cross-connector convergence pin:** a threatfox `ip:port` record and a feodo-shaped record with
  the same `ip:port` value produce the **same entity id** and both `indicatorType == ["ipv4"]`.

## 6. Slice C — `urlhaus` connector

**Scope (files):** `src/worldmonitor/plugins/connectors/urlhaus/{__init__.py,connector.py,config.schema.json}`;
tests under `tests/`.

**Endpoint (default):** `https://urlhaus.abuse.ch/downloads/json_recent/` — anonymous, 30-day window
(~20.5k entries, **~10.8 MB — under the 16 MiB cap but assert the cap explicitly** in
`_read_bounded`), regenerated every 5 min (research CONFIRMED). Same optional `auth_key` header
pattern. `url`-overridable.

**Record shape (verified, research line 54):** JSON **object** keyed by urlhaus id; each value a
**list** of records with `dateadded` (carries a trailing **` UTC`** suffix in json_recent), `url`,
`url_status` (online/offline), `last_online`, `threat` (e.g. `malware_download`), `tags` (a proper
**JSON array** in json_recent — no comma-splitting), `urlhaus_link`, `reporter`.

**`collect()`:** identical `{id:[record,...]}` traversal as slice B.

**`map()`:**
- `value = url` (str, non-blank) else `[]`; `indicatorType = ["url"]`.
- `firstSeenAt` = `dateadded`, `lastSeenAt` = `last_online` — **strip a trailing ` UTC` (case-
  insensitive) + whitespace before `entity.add()`** (FtM's date type would otherwise silently drop
  `"... UTC"`; verify at build time — see §11). Both via `entity.add()`.
- **No `malwareFamily`** (locked): `tags`/`threat` are not a reliable family signal (a `Mozi`-style
  tag is a botnet label mixed with format tags like `elf`/`mips`; `threat` is a delivery category,
  not a family). Record this decision in the connector docstring. Do NOT emit `topics`/`country`/edges.
- `datasets = ["urlhaus"]`.

**Named tests** (`tests/unit/test_urlhaus_connector.py`, MockTransport): manifest; closed schema +
`auth_key` secret; `collect()` traversal + `limit` + 16 MiB cap enforced (a fabricated >16 MiB body
raises); `Auth-Key` header present/absent; `map()` url→`url`, id = `indicator_id(url)`,
`dateadded`/`last_online` with ` UTC` suffix normalize correctly (the ` UTC`-strip pin), no
`malwareFamily` even when `tags`/`threat` present, blank url → `[]`, `datasets == {"urlhaus"}`,
provenance round-trips.

## 7. Slice D — `sslbl` connector

**Scope (files):** `src/worldmonitor/plugins/connectors/sslbl/{__init__.py,connector.py,config.schema.json}`;
tests under `tests/`.

**Endpoint (default):** `https://sslbl.abuse.ch/blacklist/sslblacklist.csv` — anonymous, **CC0**,
columns `Listingdate,SHA1,Listingreason`, `#`-comment lines, **unquoted** values (~10k rows,
~771 KB) (research CONFIRMED). Same optional `auth_key` header pattern (harmless if the CC0 endpoint
ignores it). `url`-overridable.

**`collect()`:** stream + `_read_bounded` (16 MiB). Iterate lines; **skip `#`-prefixed comment lines
and the header row**. For each data line: split on `,` with the first token = `Listingdate`, second =
`SHA1`, and **the remainder rejoined = `Listingreason`** (defends against a comma inside a reason;
date and SHA1 never contain commas — research 222). Yield one `RawRecord` per data row (data =
`json.dumps({"Listingdate":..., "SHA1":..., "Listingreason":...})`). `limit` honored.

**`map()`:**
- `value = SHA1` (40-hex, non-blank) else `[]`; `indicatorType = ["sha1_cert"]` (§3 — DISTINCT).
- `firstSeenAt` = `Listingdate` via `entity.add()`; **no `lastSeenAt`** (the CSV has no last-seen).
- `malwareFamily` from `Listingreason` per the **verified nuances** (research corrected verdict 222–223):
  - ends with `" C&C"` (case-insensitive) → family = the stripped prefix;
  - else ends with `" malware distribution"` (case-insensitive) → family = the stripped prefix (no C2
    semantics, but a real family — e.g. `NetSupport RAT malware distribution` → `NetSupport RAT`;
    `ACRStealer malware distribution` → `ACRStealer`);
  - else → no family;
  - **exclude the generic `"Malware"`** (`Malware C&C` / `Malware distribution` → no family, case-
    insensitive compare on the extracted token);
  - **when in doubt, emit no family** (never guess). Emit the Indicator regardless (value is real).
- `datasets = ["sslbl"]`. No topics/country/edges.

**Named tests** (`tests/unit/test_sslbl_connector.py`, MockTransport over a fabricated CSV body):
manifest; closed schema + `auth_key` secret; `collect()` skips `#`/header, splits maxsplit-2 (a
reason with a comma stays intact), yields per data row, honors `limit`; `Auth-Key` header
present/absent; `map()`: `RatonRAT C&C`→`RatonRAT`, `Vidar C&C`→`Vidar`,
`Malware C&C`→no family, `NetSupport RAT malware distribution`→`NetSupport RAT`,
`Malware distribution`→no family, `sha1_cert` type, id = `indicator_id(sha1)`, no `lastSeenAt`,
blank SHA1 → `[]`, `datasets == {"sslbl"}`, provenance round-trips.

## 8. Failure-mode table (all fail-soft `[]` per row unless noted)

| Condition | Expected behavior | Where |
|-----------|-------------------|-------|
| 401 / 403 on fetch | **Raise loud**, actionable message naming Auth-Key/auth.abuse.ch (deprecation of a legacy anon endpoint) | `collect()` after `raise_for_status` (threatfox/urlhaus; sslbl too, though CC0/anon today) |
| other 4xx/5xx | `raise_for_status` raises; the driver isolates the instance (backoff, never aborts the tick) | `collect()` |
| body > 16 MiB | `ValueError` fail-closed (hostile oversized body) | `_read_bounded` |
| empty / tiny feed (200) | **NOT an error** — yields 0/few records; freshness alerts must tolerate it (Feodo-empty precedent) | `collect()` |
| malformed JSON body | JSON parse raises in `collect()` (whole run fails visibly, isolated by driver) — acceptable; NOT a per-row concern | `collect()` |
| top-level not the expected container | treat as empty (`{}`/`[]` → 0 records), do not raise | `collect()` |
| record not a dict / list element not a dict | skip (no yield) | `collect()` |
| blank/missing identity field (`ioc_value`/`url`/`SHA1`) | `map()` → `[]`, log at WARNING | `map()` |
| unknown `ioc_type` (threatfox) | emit IOC with `indicatorType = lower(raw)` (never drop) | `map()` |
| junk timestamp / ` UTC` suffix that FtM can't clean | `entity.add()` silently drops it; entity still emitted | `map()` |
| generic / ambiguous SSLBL reason | emit IOC with no `malwareFamily` | `map()` |

## 9. Explicit rejections (record in the ADR; do NOT build)

- **No `indicates → Organization` edges** anywhere in this gate (attribution = S-2 phase 3).
- **SSLBL JA3 feed** (`ja3_fingerprints.csv`) — frozen since 2021-08-03, 96 stale entries. Rejected.
- **SSLBL botnet-C2 IP blacklist** (`sslipblacklist*.csv`) — deprecated 2025-01-03, empty. Rejected.
- **Keyed v2 zip exports** (`.../v2/files/exports/<AUTH-KEY>/full.csv.zip`) — deferred. Adopting them
  costs URL-embedded-secret redaction + zip/decompression-bomb handling; only warranted if the
  legacy anon endpoints get gated (revisit trigger in the ADR).
- **ThreatFox / URLhaus full exports** and the ZIP full dumps — recent windows suffice for a live
  IOC feed; full/zip deferred with the v2 work.
- **No per-connector cadence, no egress domain-allowlist edit** (SSRF guard is IP-based; feodo hits
  abuse.ch today with no allowlist — confirmed in-codebase), **no `registry.py` edit** (auto-discovery).

## 10. Fixture data sketches (real shapes — hermetic, NEVER live in tests)

ThreatFox `export/json/recent/` (object → one-element lists):
```json
{"1428921":[{"ioc_value":"185.220.101.4:443","ioc_type":"ip:port","threat_type":"botnet_cc",
  "malware":"win.cobalt_strike","malware_printable":"Cobalt Strike","malware_alias":"BEACON,CobaltStrike",
  "first_seen_utc":"2026-07-20 14:03:11","last_seen_utc":"2026-07-21 06:12:44","confidence_level":75,
  "is_compromised":false,"reference":null,"tags":"c2,CobaltStrike","anonymous":0,"reporter":"abuse_ch"}],
 "1428922":[{"ioc_value":"bad.example.com","ioc_type":"domain","threat_type":"payload_delivery",
  "malware":"js.clearfake","malware_printable":"ClearFake","first_seen_utc":"2026-07-21 01:00:00",
  "last_seen_utc":null,"tags":null}],
 "1428923":[{"ioc_value":"44d88612fea8a8f36de82e1278abb02f","ioc_type":"md5_hash","malware":"unknown",
  "malware_printable":"Unknown","first_seen_utc":"2026-07-21 02:00:00"}]}
```
Hostile variants to include: unknown `ioc_type` (`"envelope_from"`); a value that is not a list; a
list element that is not a dict; blank `ioc_value`; missing `ioc_value` key.

URLhaus `downloads/json_recent/`:
```json
{"3889762":[{"dateadded":"2026-07-21 17:55:19 UTC","url":"http://113.231.238.227:60688/i",
  "url_status":"online","last_online":"2026-07-21 17:55:19 UTC","threat":"malware_download",
  "tags":["32-bit","elf","mips","Mozi"],"urlhaus_link":"https://urlhaus.abuse.ch/url/3889762/",
  "reporter":"geenensp"}]}
```
Hostile: blank `url`; missing `url`; `dateadded` without ` UTC`; empty `tags`.

SSLBL `blacklist/sslblacklist.csv` (unquoted, `#` comments; **live-probed 2026-07-22: the column
header is itself a `#`-comment line** — `# Listingdate,SHA1,Listingreason` — and data rows follow
the comment block directly; skip `#` lines AND defensively skip a bare `Listingdate,...` header row
should the format ever change):
```
################################################################
# abuse.ch SSLBL SSL Certificate Blacklist (SHA1 Fingerprints) #
# Last updated: 2026-07-22 07:30:49 UTC                        #
################################################################
#
# Listingdate,SHA1,Listingreason
2026-07-21 12:29:18,b8b339de5ea80d17fb5ce2eb144d7ba28b33337a,RatonRAT C&C
2026-07-21 12:29:16,9000e46cabc64219fb1447d59d5443afcb412e36,Vidar C&C
2026-07-21 06:09:26,632061b26a93455e9c4f0ac413deae710c920216,Malware C&C
2026-07-20 09:00:00,aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,NetSupport RAT malware distribution
2026-07-20 08:00:00,bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb,Malware distribution
```
Hostile: a blank SHA1; a `#`-only line; a bare (non-comment) header line (must be skipped); a reason
containing a comma.

## 11. Verification at build time (probe live ONCE, pull-only, bounded — NEVER in tests)

**Probe once (a single bounded `curl`/`httpx` GET is acceptable, pull-only, no data leaves):**
1. `GET https://threatfox.abuse.ch/export/json/recent/` — confirm the `{numeric_id:[record]}` shape,
   the exact field names (`ioc_value`/`ioc_type`/`malware`/`malware_printable`/`first_seen_utc`/
   `last_seen_utc`/`tags`-as-**string**), the current `ioc_type` members, and that it still returns
   **200 anonymous**.
2. `GET https://urlhaus.abuse.ch/downloads/json_recent/` — confirm `dateadded` carries the ` UTC`
   suffix, `tags` is a JSON **array**, `last_online` format, 200 anonymous, body < 16 MiB.
3. `GET https://sslbl.abuse.ch/blacklist/sslblacklist.csv` — confirm the header, `#` comments, the
   `Listingreason` forms (`<Family> C&C`, generic `Malware C&C`, `<Family> malware distribution`),
   200 anonymous.
- **STOP-and-flag:** if any of the three returns **401/403**, the legacy-anon default is gated — do
  NOT silently switch to v2 keyed exports (that is the out-of-scope reversal in the ADR); report to
  the human (URL-secret redaction + zip handling would be required).

**NEVER live in tests:** all HTTP goes through `httpx.MockTransport` injected via the `transport=`
ctor kwarg + monkeypatched `socket.getaddrinfo` (feodo/mitre precedent). No test may reach
abuse.ch. Any Auth-Key literal in a test is a short dummy (e.g. `"k"*8`) to avoid the secret-scan
hook; never a real key.

## 12. CI / merge mechanics (per repo convention)

- ADR 0119 lands **PROPOSED**; the **slice-D merge PR flips it to ACCEPTED** (0117/0118 precedent).
- After adding/flipping ADR 0119, run `python scripts/gen_adr_index.py` and commit the regenerated
  `docs/decisions/README.md` — the `adr-index` CI check fails on drift. (README.md is IN scope for
  this reason; this gate spec file is NOT indexed — its name lacks the `NNNN-` prefix.)
- One focused PR per slice (A→B→C→D), each independently mergeable, green `quality` + `security`.
- Before every push: full `pytest -m "not integration"` + local integration + `ruff format --check .`
  **repo-wide**. Builders never push; the human/coordinator merges on green.
- Operator runbook: add ONE line under `docs/runbooks/OPERATOR_SESSION.md` §3 (Real-seed corpus)
  noting that registering one free Auth-Key at `auth.abuse.ch` and setting it on the abuse.ch
  connectors future-proofs them against the legacy anon endpoints being gated. No new doc.

## 13. Slice independence

A (Splink) touches only `splink_model.py` + its tests. B/C/D each add a self-contained connector
package + one seed row + unit tests. None imports another. A can merge before or after B/C/D (it is a
pure defense-in-depth tightening; B/C/D emit non-matchable entities that A would filter, but they are
already filtered today by `_schema_compatible` post-`predict`, so ordering is not a correctness
dependency — recommended order A→B→C→D for narrative clarity).
