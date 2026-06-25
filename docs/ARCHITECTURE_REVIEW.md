# WorldMonitor — Architecture & Review

> Standalone architecture and review of the WorldMonitor ingest → resolution → graph pipeline.
> File:line citations are to the source tree at the time of writing (`master` after the WS1–WS2 merges).

---

## 1. Overview

WorldMonitor is a self-hosted, **graph-native, ontology-first, plugin-extensible** OSINT /
geopolitical-intelligence platform. **The resolved entity graph is the product**: many independent
sources are mapped into one canonical property graph (Neo4j), every node and edge carries
provenance, and analysis and the API/MCP surface sit on top of that graph. CTI is just one plugin
domain.

The system is layered around a single architectural rule: **L2 (the FollowTheMoney / STIX ontology)
is the contract.**

- **Below L2 — producers.** Connectors `collect()` raw bytes and `map()` them into FtM/STIX
  entities *with provenance*. They write raw records to the landing zone (S3/MinIO) and mapped
  candidates to the ER queue (Postgres). **Connectors never write to the graph and never dedupe.**
- **L2 — the ontology contract.** Every emitted object is validated against the FtM schema
  (`validation.validate_or_raise`); provenance and canonical anchors travel as flat scalar context
  keys (`wm_prov_*`, `wm_anchor_*`) so they survive FtM `merge_context`.
- **Above L2 — consumers.** Entity resolution (Splink score → nomenklatura cluster → catastrophic-
  merge guard → referent rewrite → graph write) is the only path that mints canonical IDs and writes
  Neo4j. The driver schedules both passes on a cadence; human sign-off is a side path that feeds
  durable judgements back into resolution.

A new source or method is a **new plugin against that contract** — no layer above it changes.

The pipeline is built in **gated milestones** (the "runway"). What is implemented today:
single-node, batch-on-a-timer ingest and within-batch entity resolution, with a catastrophic-merge
guard and durable human sign-off. (Per **D1 / ADR 0042** the system is **single-tenant**; the former
tenant-isolation invariant G4 is retired — `tenant_id` has been removed from code and schema, so the
G4 enforcement points described below are historical.) Cross-batch ER (Gate B), persisted cross-run
graph mutation (Gate C), and canonical-canonical routing (S4) are explicitly **deferred** with their
seams left visible in the code (Section 6). The streaming cursor (X1) is likewise deferred; the former
HA-lease / single-writer-per-tenant forks (X2/X3) are **moot under single-tenancy** (ADR 0042).

---

## 2. End-to-end control & data flow

### 2.1 The cadence loop (driver)

`IngestDriver.run_forever` (`runner/driver.py:263`) is the only async surface and the clock for
everything:

1. On startup, call `recover_stale()` once (`driver.py:86`): every globally-`running` `TaskRun` →
   `error` and every `running` `ConnectorInstance` → `enabled`, so a crash mid-run is re-runnable.
2. Loop, once per `driver_tick_seconds` (`driver.py:267-`):
   a. `await asyncio.to_thread(self.run_due_ingests, now=now)` — ingest pass.
   b. If `resolve_cadence` has elapsed (wall-clock delta), `await asyncio.to_thread(self.run_resolution, now=now)` — resolution pass.
   c. `asyncio.sleep(driver_tick_seconds)`.

The two passes are **synchronous** and take an injected `now` for determinism.

### 2.2 Ingest → land → map → enqueue

`run_due_ingests` (`driver.py:116`) selects enabled instances whose `next_run` is NULL or due, then
calls `_ingest_instance` sequentially per id. `_ingest_instance` (`driver.py:135`) uses **three
separate transactions**:

1. **Claim** (`driver.py:138-158`): re-fetch the instance, guard `status == "enabled"` (silent
   return otherwise — TOCTOU seam, formerly X3; moot under single-tenancy per ADR 0042), flip to
   `running`, INSERT a `running` `TaskRun`, snapshot `(connector_id, config_encrypted)`, commit.
2. **Work** (`driver.py:160-178`): `ConfigCipher.decrypt(config_token)` → `json.loads` →
   `registry.get(connector_id)`; if `manifest.capability is Capability.ACTIVE`, raise
   `ActiveConnectorRefused` (`driver.py:165`); else `run_ingest(...)` in its own `work` session.
3. **Finalize** (`driver.py:184` → `_finalize` at `driver.py:234`): stamp the `TaskRun` terminal
   status/error/stats, set the instance `enabled`/`error` + `last_run`/`next_run`, commit. Note this
   is **outside** the try/except (see §7, BLOCKER candidate).

`run_ingest` (`runner/ingest.py:94`) is the call-once primitive. For each `RawRecord` yielded by
`connector.collect(config)` (`ingest.py:135`):

3. Build the landing key: `connector_id/dataset/{record.key}.json` (`ingest.py:138`; the former
   `tenant_id/` prefix is removed under single-tenancy, ADR 0042).
4. `landing.put(key, record.data, ...)` → returns an `s3://` URI (`ingest.py:141`,
   `storage/landing.py:put`). On failure: dead-letter `stage="land"` (no provenance ever built) and
   continue (`ingest.py:143-154`).
5. Build `Provenance(source_id, retrieved_at, reliability, source_record=uri)` — **only after a
   successful land** (`ingest.py:156`), so no provenance-less entity is ever queued.
6. `connector.map(record, provenance=...)` (`ingest.py:163`). `map()` ends in
   `stamp(entity, provenance)` (`plugins/ftm_bulk.py:27` / `plugins/connectors/geonames/connector.py:116`).
   On failure: dead-letter `stage="map"` (URI retained, replayable) and continue (`ingest.py:166-175`).
7. For each mapped entity, **idempotent enqueue**: `pg_insert(ErQueueItem)
   .on_conflict_do_nothing(constraint="uq_er_queue_dedup").returning(id)` (`ingest.py:184-197`);
   `queued` counts only rows actually inserted.
8. Commit when `since_commit >= window`; break on `max_records` cap or wall-clock timeout
   (`ingest.py:201-208`); final `_commit_window()` (`ingest.py:210`). `stopped_reason` ∈
   {`exhausted`, `max_records`, `timeout`}.

### 2.3 Resolve: score → cluster → guard → referent-rewrite → graph write

`run_resolution` (`driver.py:189`) acquires a non-blocking `threading.Lock` (skip-tick if held), then
opens a `resolve` `TaskRun` and calls `resolve_pending`. (Under single-tenancy, ADR 0042, the former
per-tenant routing — a DISTINCT-tenant select feeding `_resolve_tenant` per tenant — is removed; there
is a single resolution pass.)

`resolve_pending` (`resolution/pipeline.py:67`) loads the durable `ResolverJudgement`s once
(`_load_judgements`, `pipeline.py:143`), then drains the queue in **bounded batches**:

9. SELECT `ErQueueItem WHERE status == 'pending' ORDER BY created_at, id LIMIT size`
   (`pipeline.py:102-112`); break when empty. (The former `tenant_id == t` predicate is removed under
   single-tenancy, ADR 0042.)
10. `_resolve_batch` (`pipeline.py:180`):
    - `make_entity` per row; `score_pairs(entities)` (Splink/DuckDB, `splink_model.py:125`).
    - `cluster_and_merge(...)` (`merge.py:74`): a **fresh ephemeral in-memory nomenklatura resolver**
      per call (the G4 fix, ADR 0028); seed sign-off judgements **first** (they take precedence over
      Splink), then decide Splink pairs `>= merge_threshold` (0.92), then FtM-merge each cluster into a
      `ResolvedCluster(canonical_id, member_ids, entity, score)`. A real merge mints a fresh
      nomenklatura `NK-…` canonical id; a singleton keys to itself.
    - `_approved_groups(judgements)` (`pipeline.py:151`): union-find over positive judgement pairs.
    - Per cluster: `needs_review(cluster, by_id)` (`review.py:34`) — **unconditional** guard
      evaluation. If flagged AND members ⊆ a *single* approved group, unflag (exemption,
      `pipeline.py:228`). Then:
      - flagged + `mode == "block"` → `record_merge(decision="pending_review")`, set rows
        `pending_review`, `continue` (never written) (`pipeline.py:232-238`).
      - flagged + `mode == "alert"` → `record_merge_alert` + WARNING log, then fall through and merge
        (`pipeline.py:242-251`).
      - merge → `record_merge(decision="merged")`, set rows `resolved`, `enrich(cluster.entity)`,
        collect for promotion (`pipeline.py:253-258`).
    - **Safety sweep** (`pipeline.py:264-276`): any FtM id not in any cluster (dropped / id-less) →
      rows `invalid` + WARNING, so the bounded-drain loop always terminates.
    - If anything promoted: `build_referent_map(promoted_clusters)` (PROMOTED only, `pipeline.py:284`)
      → `rewrite_referents` per entity (`pipeline.py:285-286`) → `write_entities(neo4j, …)`
      (`pipeline.py:287`; the former `tenant_id` argument is removed under single-tenancy, ADR 0042).
11. Back in `resolve_pending`: `session.commit()` **per batch** (`pipeline.py:125`).

`write_entities` (`graph/writer.py:117`) runs ftmg's two passes, each in its own session: Pass 1
nodes (`writer.py:141-148`), Pass 2 edges + entity-links + topic-labels (`writer.py:157-171`). Under
single-tenancy (ADR 0042) every node MERGE key and relationship MATCH key is ftmg's native `{id}`; the
former tenant-scoping machinery (`_KEY_REWRITES` / `_tenantize_query`, `writer.py:44-59`) is removed.
Provenance (`prov_*`) and anchors are projected onto every node (`writer.py:133-138,147`) and every
edge (`writer.py:160,170`).

### 2.4 Sign-off side path

`MERGE_GUARD_MODE="block"` parks flagged clusters as `pending_review`. An operator drives
`resolution/signoff.py` (CLI: `python -m worldmonitor.review`):

- **approve** (`signoff.py:151-170`): re-merge member rows into one canonical, rewrite outbound edges
  to the canonical id, `write_entities([canonical, *edges])` (Neo4j) → `_record_judgements(...,
  "positive")`, flip rows `resolved`, audit → `merged`, write a `SignOff` row, `session.commit()`.
- **reject** (`signoff.py:173-205`): write each member as its own entity (+ outbound edges),
  `_record_judgements(..., "negative")` (so future batches never re-merge), flip rows `resolved`,
  audit → `rejected`, write `SignOff`, commit.

The persisted `ResolverJudgement`s are loaded into **every future batch's** ephemeral resolver
(`_load_judgements`), so a reviewed cluster never re-parks. Both approve and reject **write Neo4j
before the Postgres commit** (same cross-store ordering gap as the pipeline — see §7).

---

## 3. Module map

### Connectors + plugin framework (`plugins/`)
- `base.py` — plugin contract: `Kind`/`Mode`/`Capability`/`Status` enums, frozen `Manifest` +
  `RawRecord`, abstract `Connector` (`manifest`/`config_schema`/`collect`/`map`) with jsonschema
  `validate_config`.
- `registry.py` — in-memory connector catalog keyed by `connector_id`; `register` (dup-reject),
  `discover_module`/`discover_package` via `inspect` + `pkgutil`.
- `ftm_bulk.py` — `FtmBulkConnector` base: `map()` = `json.loads` → `validate_or_raise` → `stamp`.
- `plugins/connectors/opensanctions/` — passive `EXTERNAL_IMPORT`; streams `entities.ftm.json` line-by-line.
- `plugins/connectors/geonames/` — passive `EXTERNAL_IMPORT`; downloads `<CC>.zip`, splits TSV → FtM
  `Address`, `set_anchor("geonames_id", …)`.

### Ingest + landing + provenance (`runner/`, `storage/`, `provenance/`)
- `runner/ingest.py` — `run_ingest`: bounded/windowed collect → land → map → idempotent enqueue,
  with dead-lettering.
- `runner/driver.py` — `IngestDriver`: cadence loop, `TaskRun` lifecycle, ACTIVE-refusal, stale
  recovery, the only caller of `run_ingest` / `resolve_pending`.
- `runner/subprocess.py` — `run_command`: argv-list subprocess primitive with hard timeout +
  process-group kill (scaffolded, no in-tree caller).
- `runner/smoke_metrics.py` — read-only one-line metrics snapshot for watching a sustained run.
- `storage/landing.py` — `LandingStore`: the single boto3/S3 boundary; `put()` returns the
  `s3://` provenance pointer.
- `provenance/model.py` — `Provenance` dataclass + `stamp`/`get_provenance`/
  `provenance_node_properties` (flat `wm_prov_*` context keys ↔ `prov_*` node props).

### Resolution: scoring, clustering, guard, sign-off (`resolution/`)
- `splink_model.py` — Splink/DuckDB Fellegi-Sunter pairwise scorer; expert-set m/u weights; emits
  `ScoredPair`.
- `merge.py` — `cluster_and_merge` (seed judgements → decide Splink pairs → FtM merge),
  `_ephemeral_resolver` (per-batch in-memory nomenklatura ledger = the G4 fix), `_cluster_score`.
- `review.py` — catastrophic-merge guard evaluator: `needs_review` (size > 10 or sensitive member),
  `is_sensitive` (OpenSanctions topics).
- `pipeline.py` — `resolve_pending` bounded-batch drain + `_resolve_batch` orchestration +
  `_approved_groups`.
- `referents.py` — `build_referent_map` (promoted clusters only) + `rewrite_referents` (redirect
  entity-typed property values to canonical ids before write; G2).
- `audit.py` — `record_merge` (`MergeAudit`) + `record_merge_alert` (`MergeAlert`).
- `signoff.py` — `list_parked`/`approve`/`reject`: durable `ResolverJudgement` + `SignOff`, graph
  write of canonical/members + outbound edges.

### Graph write + read (`graph/`)
- `writer.py` — ftmg adapter: two-pass `write_entities` (ftmg-native `{id}` node key) + provenance/
  anchor projection. (The former tenant-scoping machinery is removed under single-tenancy, ADR 0042.)
- `neo4j_client.py` — frozen dataclass wrapping the neo4j Driver; `execute_write`/`execute_read` +
  raw `session()`.
- `constraints.py` — `ensure_constraints`: single-column uniqueness on each of the 4 canonical-id
  anchor fields. (Formerly composite `(tenant_id, anchor)` + a `tenant_id` index; reduced under
  single-tenancy, ADR 0042.)
- `queries.py` — reads `get_entity`/`get_neighbors`/`get_provenance`. (The former `tenant_id`
  predicate is removed under single-tenancy, ADR 0042.)

### Schema, settings, crypto (`db/`, `settings.py`)
- `db/models.py` — the 8 ORM tables (formerly all `tenant_id`-scoped; the column is removed under
  single-tenancy, ADR 0042 / migration `0004_drop_tenant_id`).
- `db/engine.py` — engine/session factory + `migrate_to_head` adoption logic.
- `db/migrate.py` / `db/migrations/` — Alembic CLI + baseline/runway/sign-off revisions.
- `db/crypto.py` — `ConfigCipher` (Fernet) for `config_encrypted` at rest.
- `settings.py` — pydantic-settings (12-factor env), DSN rewrite, guard/batch/cadence knobs.

### Ontology (`ontology/`)
- `ftm.py` — `FtmEntity = ValueEntity`, `make_entity`, FtM `merge()` primitive.
- `anchors.py` — `set_anchor`/`get_anchors`, `CANONICAL_ID_FIELDS`.
- `validation.py` — `validate_or_raise` (non-empty id + resolvable schema).

---

## 4. Schema

### 4.1 Postgres (8 tables, `db/models.py`)

All PKs are app-generated `String(64)` (uuid4 hex, no DB sequence). Under single-tenancy (D1 / ADR
0042), the former `tenant_id String(128)` index on every table is **removed** (migration
`0004_drop_tenant_id`) and the two composite uniques are redefined without their leading `tenant_id`
column; the "Tenant scoping" column below is retained for historical context and now reads as
*formerly* `tenant_id`. No RLS, no tenant FK, no composite PK including tenant_id.

| Table | Purpose | Tenant scoping (historical — column dropped, ADR 0042) | Key indexes / uniques |
|---|---|---|---|
| `connector_instance` (`:23`) | configured connector plugin; config Fernet-encrypted at rest | *(formerly `tenant_id` idx)* | `connector_id` idx; `config_encrypted` TEXT |
| `er_queue_item` (`:39`) | mapped FtM candidate awaiting resolution; `raw_entity` JSONB carries provenance | *(formerly `tenant_id` idx)* | **`uq_er_queue_dedup (source_record, entity_id)`** (`:48`; formerly led by `tenant_id`, ADR 0042); `entity_id` idx (nullable); `status` idx |
| `merge_audit` (`:64`) | every resolution decision (`merged`/`pending_review`/`rejected`); rollback record | *(formerly `tenant_id` idx)* | `canonical_id` idx; `decision` idx; `source_ids` JSONB; no unique (append-only) |
| `ingest_dead_letter` (`:83`) | land/map failures (the dead-letter trail) | *(formerly `tenant_id` idx)* | `stage` idx; `source_record` nullable (null for `land`) |
| `merge_alerts` (`:108`) | flagged-but-merged clusters under `alert` mode | *(formerly `tenant_id` idx)* | `canonical_id` idx; `source_ids` JSONB |
| `task_run` (`:130`) | run-history/observability per ingest/resolve pass | *(formerly `tenant_id` idx)* | `connector_instance_id` idx (NULL for resolve); `kind`/`status` idx; `stats` JSONB |
| `resolver_judgement` (`:155`) | durable sign-off judgements (`positive`/`negative`) | *(formerly `tenant_id` idx)* | **`uq_resolver_judgement_pair (left_id, right_id)`** (`:171`; formerly led by `tenant_id`, ADR 0042); `left_id <= right_id` **caller-enforced, no DB CHECK** |
| `sign_off` (`:183`) | human approve/reject record (audit trail) | *(formerly `tenant_id` idx)* | `canonical_id` idx; `decision` idx |

Migrations: `0001_baseline` (pre-runway: connector_instance, er_queue_item without entity_id,
merge_audit, merge_alerts) → `0002_runway` (entity_id + uq_er_queue_dedup, ingest_dead_letter,
task_run) → `0003_signoff_judgements` (resolver_judgement, sign_off) → `0004_drop_tenant_id` (drops
the `tenant_id` column + its 8 indexes and redefines the two composite uniques without it, under
single-tenancy / ADR 0042; 0001–0003 are left unedited, the delta layers in 0004). `migrate_to_head`
(`engine.py:53-76`) branches on column-existence heuristics; `create_all == alembic head` is asserted
by `tests/integration/test_migrations.py` + an `alembic check` drift guard.

### 4.2 Neo4j node/edge model + provenance

`write_entities` drives ftmg's FtM → property-graph transform (`graph/writer.py`):

- **Nodes.** One node per non-edge FtM entity, keyed `{id}` (ftmg's native MERGE key, `writer.py:45`).
  `SET n = props` (full replacement) where props = `id / caption / datasets / schema props / anchors
  (wm_anchor_*) / prov_*`. ftmg stamps the base label `:Entity` plus FtM-schema labels. (Formerly
  keyed `{id, tenant_id}` via tenant-scoped MERGE; reduced under single-tenancy, ADR 0042.)
- **Edges.** FtM edge schemata (Ownership, Directorship, Sanction…) become typed relationships via
  `generate_edge_entity`, endpoints MATCHed on the raw `{id}` key; `SET r = item.props` including the
  **asserting entity's** `prov_*`. Entity-reference properties on non-edge schemata route through
  `generate_entity_links` (see HIGH issue in §7 — these MATCH on the `entity:`-prefixed node_id and
  silently drop). (Formerly the endpoint key and `r` props also carried `tenant_id`; removed under
  single-tenancy, ADR 0042.)
- **Constraints** (`graph/constraints.py`): single-column uniqueness on each `CANONICAL_ID_FIELDS`
  property, `IF NOT EXISTS`. (Formerly composite `(tenant_id, <anchor>)` uniqueness + a `tenant_id`
  index; reduced under single-tenancy, ADR 0042.)
- **Provenance** is the GDPR/audit log: `prov_source_id / prov_retrieved_at / prov_reliability /
  prov_source_record` on every node and every edge, projected from the entity's flat `wm_prov_*`
  context by `provenance_node_properties` (`provenance/model.py:70`).

---

## 5. Invariants and exactly where each is enforced

| Invariant | Enforcement point | How |
|---|---|---|
| **G1 — provenance on every node** | `provenance/model.py:49-54` (stamp); `graph/writer.py:133-138,147` (project) | Connector `map()` ends in `stamp`; `run_ingest` builds `Provenance` only after a successful land (`ingest.py:156`); writer projects `prov_*` onto node props. *Caveat: writer projects only if stamped — `provenance_node_properties` returns `{}` when unstamped (`model.py:73-75`); the write boundary does not reject a provenance-less entity.* |
| **G1 — provenance on every edge** | `graph/writer.py:160,170` | `edge_prov = provenance_node_properties(asserting entity)` merged into `params.props` (`writer.py:85-86`); proven by `tests/integration/test_graph_writer.py`. (Formerly this merge step also stamped `tenant_id`; that stamp is removed under single-tenancy, ADR 0042.) |
| **G4 — tenant isolation (resolution)** — *RETIRED under D1 / ADR 0042 (single-tenant)* | *(historical)* `pipeline.py` pending SELECT / `_load_judgements`; `merge.py:55-71` | **Retired:** with one tenant there is nothing to isolate, so the `tenant_id` predicates on the queue/judgement reads are removed. The **ephemeral per-batch nomenklatura resolver** (ADR 0028) is **kept** — its G4 motivation is now historical but it is still required for B-1 crash recovery / batch purity (proven `tests/unit/test_resolution.py:73`, which already passes zero `tenant_id`). |
| **G4 — tenant isolation (graph)** — *RETIRED under D1 / ADR 0042 (single-tenant)* | *(historical)* `graph/writer.py`; `graph/constraints.py` | **Retired:** the node/edge key returns to ftmg's native `{id}`; the `_KEY_REWRITES` / `_tenantize_query` machinery and the `write_entities` `tenant_id` guard are removed, and the graph constraint reverts to single-column anchor uniqueness. (A future managed-cloud multi-tenant tier would reintroduce isolation as its own gate — RLS / Neo4j Enterprise multi-db — per ADR 0042.) |
| **Append-only / no un-merge** | `signoff.py:167,193-197,202`; `graph/writer.py` (MERGE only) | Audit `decision` is flipped, never deleted; reject writes members as **new separate** nodes rather than splitting a written canonical; the writer only MERGEs/CREATEs — no delete/split path exists. |
| **Canonical-canonical via guard** | `pipeline.py:209-229` + `_approved_groups` (`:151`) | Guard exemption fires **only** when all cluster members ⊆ a *single* approved connected component; a new member accreting or two approved groups fusing re-parks (proven `test_signoff.py:348`). Routes canonical-canonical fusion *through* the guard, not around it. |
| **Resolve to canonical IDs (edges, G2)** | `resolution/referents.py:47-67`; `pipeline.py:284-287`; `signoff.py:158-161` | `rewrite_referents` redirects entity-typed property values to the canonical id before write, so no edge MATCHes a merged-away, never-materialised id; map built from **promoted clusters only** (`referents.py:33`) so parked merges never rewrite (proven `test_referent_rewriting.py:237`). |
| **De-dupe before counting** | `merge.py:74` (cluster_and_merge); `pipeline.py:289-296` (ResolveStats) | Duplicates collapse into canonical clusters before any write; `ResolveStats.clusters`/`promoted` count post-dedup. *Caveat: dedup is within-batch only (Gate B).* |
| **Catastrophic-merge guard** | `resolution/review.py:34-50` (eval); `pipeline.py:226-251` (action) | `needs_review` is **unconditional** (flags size > `MAX_AUTO_MERGE_SIZE=10` or any sensitive member); only the *action* depends on `MERGE_GUARD_MODE` (ADR 0024). *Caveat: `SENSITIVE_TOPICS` is a denylist — fails open for unmodelled topics.* |
| **Idempotent enqueue** | `db/models.py:47-49` (uq_er_queue_dedup); `ingest.py:184-199` | `pg_insert(...).on_conflict_do_nothing(constraint="uq_er_queue_dedup").returning(id)`; `queued` counts only returned rows. *Caveat: NULL `entity_id` is distinct in Postgres → id-less entities are not deduped.* |
| **Decrypt at use (secrets at rest)** | `db/crypto.py:18-39`; `driver.py:153-163` | Connector config is Fernet-encrypted in `config_encrypted`; decrypted into a local only at point of use, never cached plaintext; `ConfigCipher` rejects an empty `CONFIG_ENCRYPTION_KEY`. |
| **ACTIVE-connector refusal** | `runner/driver.py:165-169` | An `ACTIVE`-capability connector raises `ActiveConnectorRefused` **inside the try**, so a visible `task_run` error row is recorded — never silently skipped. (The authorized-scope-token system that would gate it is unbuilt.) |

---

## 6. Deferred surfaces

Each deferred milestone has its seam left intentionally visible in the code, owned by an ADR.

| Surface | What is deferred | Seam in code | ADR |
|---|---|---|---|
| **Gate B** — incremental / cross-batch ER | Dedup is **within a batch only**; cross-batch duplicates land as separate canonical nodes | `pipeline.py:11-12,82-84`; the per-batch ephemeral resolver `merge.py:55-71`; the ER queue itself (no "resolved-against-existing" marker). Referent map built per-batch from this batch's promoted clusters (`pipeline.py:284`) — an edge referencing a prior-batch merged-away id silently drops at the ftmg MATCH | ADR 0019 |
| **Gate C** — persisted cross-run referent rewriting / graph mutation | **Inbound** cross-references (edges pointing *at* an approved/merged entity from a prior run) are not restored; no graph-side sweep re-points existing edges or removes orphan nodes | `signoff.py:13-15` (named deferred); `referents.py` only rewrites within the current write set; `SET n = props` full-replace (`ftmg.transform`) is where a cross-run re-canonicalization would clobber state | ADR 0025 |
| **S4** — canonical-canonical routing | No first-class canonical-canonical merge path; every run re-mints a fresh `NK-` id; fusion is routed back *through* the guard rather than around it | `pipeline.py:209-229` (`_approved_groups` exemption); `merge_audit/merge_alerts/sign_off` key on a single `canonical_id` (no canonical→canonical route table); `test_signoff.py:329-344` asserts a re-ingest mints a NEW id | ADR 0031 |
| **X1** — stream cursor | `Mode.STREAM` is modelled but no STREAM connector exists; `collect()` is a one-shot cursorless iterator; `run_ingest` re-drains from the start, relying on `uq_er_queue_dedup` | `plugins/base.py:44` (`Mode.STREAM`); `ingest.py:16-18,135`; `connector_instance` has `last_run`/`next_run` but no per-source offset column | (runway — X1) |
| **X2** — driver lease / HA — *fork moot under single-tenancy (ADR 0042)* | Single-node best-effort: `recover_stale` resets all `running` rows at startup; no lease/heartbeat/owner column. The HA-lease fork is moot under single-tenancy, though single-node concurrency hardening (a real lease) remains valid future work | `driver.py:86-113` (recover_stale); `db/models.py:130-139` (TaskRun docstring names the single-node assumption); the in-process `_resolve_lock` (`driver.py:195`) | (runway — X2; ADR 0042) |
| **X3** — single-writer — *fork moot under single-tenancy (ADR 0042)* | The former "single-writer-**per-tenant**" fork is moot under single-tenancy. Single-node concurrency is still unguarded: nothing serializes two `run_ingest` or a concurrent `resolve` + `sign-off`; relies on the single-node lock + dedup constraint | `driver.py:139-141` (TOCTOU claim, silent loser); `pipeline.py:101-112` (no `FOR UPDATE`/`SKIP LOCKED`); `landing.py` (last-writer-wins S3 key race) | (runway — X3; ADR 0042) |
| **Active-connector scope tokens** | The authorized-scope-token gate for active plugins is unbuilt; refusal is the placeholder | `driver.py:54-60,165-169` (`ActiveConnectorRefused`) | (plugin framework) |
| **`wm:Place` ontology extension** | GeoNames maps places to FtM `Address` as a load-bearing stand-in | `plugins/connectors/geonames/connector.py:6` (docstring), `:91` | (ontology) |

---

## 7. Latent issues & risks (deduped, ranked)

### ⚠ BLOCKERS

**B1 — `_finalize` outside the try/except crashes the whole driver loop.**
`_ingest_instance` calls `_finalize` **outside** the surrounding try/except (`driver.py:184` vs the
except ending at `:181`), and `run_forever` has **no outer try/except** around the per-instance
ingest / resolution passes (`driver.py:263-269`). If the finalize commit raises (a transient DB error) — or
any pass throws — the exception propagates through `asyncio.to_thread` → `run_forever` and **kills the
entire driver loop**. The instance + task are stranded `running` (recover_stale only runs at startup),
the just-completed ingest work is lost from the trail, and **the whole pipeline stops** with only a
stack trace and no supervisor/restart. *Direction:* wrap each pass body in `run_forever` in a try/except
that logs and continues; move `_finalize` inside (or give it its own guard) so a finalize failure can
never escape a single instance.

### HIGH

**H1 — Cross-store write-before-commit with non-deterministic canonical ids.**
In `_resolve_batch`, `write_entities` (Neo4j, committed by ftmg's `QueryBatcher.flush`) runs at
`pipeline.py:287` **before** the Postgres `session.commit()` at `pipeline.py:125`. A crash between the
two leaves canonical nodes in Neo4j while the `er_queue` rows stay `pending` and `MergeAudit` rolls
back. The next run re-resolves the same rows, and because nomenklatura mints a **fresh non-
deterministic `NK-` canonical id**, it writes a **duplicate canonical node** the MERGE idempotency
cannot catch — a silent de-dup violation with no audit of the orphaned first write. There is no
saga/outbox/two-phase coordination. *Direction:* introduce an outbox or make `canonical_id`
deterministic (content-addressed over sorted member ids) so a re-run MERGEs onto the same node.

**H2 — Sign-off approve/reject share the same cross-store ordering gap.**
`signoff.approve` writes Neo4j at `signoff.py:162` then commits Postgres at `:169`; `reject` writes at
`:197` then commits at `:204`. A commit failure after the graph write leaves the audit `pending_review`;
a re-approve mints a **second canonical** (duplicate node), or a re-reject silently loses the negative
judgement. No idempotency guard on re-running a sign-off. *Direction:* same as H1, plus an idempotency
key on the sign-off decision.

**H3 — Entity-reference links are silently dropped at the ftmg MATCH.**
`generate_entity_links` MATCHes endpoints on the **`entity:`-prefixed** `node_id` while nodes are
written with the raw id (`writer.py:163-166`; `registry.entity.node_id('abc') == 'entity:abc'`). For
all non-edge entity-typed properties (Membership/affiliation-style references modelled as links, not
edge schemata), `MATCH (s {id:'entity:abc'})` finds nothing and the relationship is **silently not
created** — no error, no dead-letter. The integration suite only exercises edge schemata
(Ownership/Directorship, which use raw ids via `generate_edge_entity`), so it never catches this.
*Direction:* normalize the link MATCH to the raw id form in the adapter, and add a test fixture with
an entity-reference link.

**H4 — Provenance collapses to one source on a merged canonical node (partial G1).**
The FtM-merged canonical entity's context holds **all** members' provenance as a list, but
`_context_scalar` returns only the **first** element (`provenance/model.py:41-46`), so
`provenance_node_properties` writes only **one** source's `prov_*` onto the node (`writer.py:137`). A
canonical node fused from N sources silently loses N-1 sources' lineage from the queryable graph (full
lineage survives only in `raw_entity` JSON and `MergeAudit.source_ids`). Fails closed (no error) but
degrades the audit/GDPR posture. *Direction:* project multi-valued provenance (e.g. `prov_source_id`
as a list property, or per-source provenance sub-nodes).

**H5 — SSRF / path injection at the connector config → URL boundary.**
Both connectors interpolate **unsanitized** config into outbound URLs with no allowlist or pattern.
OpenSanctions: `f"{_BASE_URL}/{dataset}/entities.ftm.json"` with `dataset` only `minLength:1`
(`plugins/connectors/opensanctions/connector.py:55`); GeoNames: `f"{_BASE_URL}/{country}.zip"` with `country`
len-2 but no pattern, and `..` is a valid 2-char value (`plugins/connectors/geonames/connector.py:121`). Both
use `follow_redirects=True`. This violates "treat all external data as hostile" at the config→URL
boundary. *Direction:* add a strict ISO-3166 / dataset-slug regex to each `config.schema.json` and
disable cross-host redirects.

**H6 — Unsanitized connector-controlled landing key (prefix escape).**
`record.key` (derived from hostile parsed source data; for OpenSanctions, from the untrusted entity
id at `connector.py:71-77`) is interpolated directly into the S3 object key at `ingest.py:138` with no
normalization, then passed straight to `put_object` (`landing.py`). A `record.key` containing `../`
or a leading `/` escapes the `connector_id/dataset` prefix and enables landing-zone collision/overwrite
across connectors/datasets. (Under single-tenancy, ADR 0042, the former cross-*tenant* G4 framing no
longer applies — the key no longer carries a `tenant_id/` prefix — but the path-traversal/overwrite
risk itself is unchanged.) *Direction:* slugify/quote the key and reject path separators before
building the S3 key.

**H7 — Concurrent `resolve_pending` double-processes.**
The pending SELECT has no row lock / `SKIP LOCKED` / `processing` transition before the batch commits
(`pipeline.py:101-112`). Two workers (or a retried driver task) load the same rows, both
score+cluster+`write_entities`, producing duplicate canonical nodes with different `NK-` ids and
duplicate audit rows. Mitigated today only by the deferred X3 single-writer assumption (the
*per-tenant* qualifier is moot under single-tenancy, ADR 0042, but single-node single-writer is still
unbuilt). *Direction:* `SELECT … FOR UPDATE SKIP LOCKED` (or a `processing` status) when X3 is built.

### MEDIUM

**M1 — Catastrophic-merge guard is a denylist (fails open).**
`SENSITIVE_TOPICS` (`review.py:20-22`) plus the `role.pep`/`sanction` prefix check (`:31`) is an
enumerated set; any sensitive classification not in it (a future OpenSanctions topic, a differently-
spelled value, or sensitivity expressed only via a linked `Sanction` edge entity rather than a
`topics` value on the member) yields `is_sensitive=False` and the merge **auto-promotes without
review** — violating "never auto-merge a sensitive entity" for unmodelled cases. *Direction:* move to
default-deny / allowlist semantics, and consider edge-derived sensitivity.

**M2 — `guard_mode` is not validated; an unexpected value fails open.**
`batch_size > 0` is validated (`pipeline.py:94`) but `mode` is not constrained to `{"alert","block"}`.
A typo'd `MERGE_GUARD_MODE` override makes `mode == "block"` false (`:232`), so every flagged cluster
falls through to **alert behavior** (writes the sensitive merge) rather than failing closed. The
settings `Literal` only guards the env var, not the `guard_mode` param. *Direction:* validate the
override against the allowed set and default to `block`.

**M3 — `reliability` is a hardcoded constant in production.**
`driver.py:171` calls `run_ingest` without `reliability=`, so every entity from every connector is
stamped `reliability="B"` (`ingest.py:101`). There is no `ConnectorInstance.reliability` column to
source it from, so a G1 provenance field — and an input to "calibrate before concluding" — carries no
real signal. *Direction:* add a per-instance reliability column and thread it through.

**M4 — `migrate_to_head` adoption can silently miss a partial migration.**
The adoption branch keys on the mere presence of `er_queue_item` + the `entity_id` column
(`engine.py:70-76`). A DB that crashed mid-`0002` (entity_id added but `uq_er_queue_dedup` /
`task_run` / `ingest_dead_letter` not yet created) is stamped at head and the missing objects are
**never created** — a silent schema gap with no error. *Direction:* validate that all expected objects
exist (or always `upgrade` from the detected baseline rather than `stamp`).

**M5 — `invalid` quarantine sweep drops rows with only an aggregate WARNING.**
The sweep (`pipeline.py:264-276`) quarantines id-less/unclusterable rows but logs only a count — no
per-row `source_record`, no dead-letter. These records vanish from resolution untraceably, weakening
the audit-on-failure posture that `IngestDeadLetter` otherwise upholds. *Direction:* write a
dead-letter (or per-row WARNING with `source_record`).

**M6 — A failing batch makes zero progress and produces zero durable audit.**
`_resolve_batch` mutates `ErQueueItem.status` in memory and adds `MergeAudit` rows throughout the
loop; if `write_entities`/`enrich` raises near the end, the whole batch rolls back (commit skipped),
**including audit for clusters already decided**. A deterministically-failing batch is re-loaded
forever with no durable trail (until the exception aborts the run via B1). *Direction:* commit audit
incrementally or in a separate transaction from the graph write.

**M7 — GeoNames provenance loses the source country + reads the whole zip into memory.**
`run_ingest` derives `source_id` from `config["dataset"]` (`ingest.py:119`), which GeoNames lacks
(its key is `country`), so `source_id` collapses to `"geonames"` and the landing key omits the country
(`ingest.py:138`) — VA and MC dumps share a non-discriminating `source_id` (degraded G1). Separately,
`_download` reads the entire country zip + decompressed `.txt` into memory and `splitlines()`
materializes every row (`plugins/connectors/geonames/connector.py:120-125`) — a large country can OOM.
*Direction:* let connectors contribute their own provenance discriminator; stream the GeoNames
archive line-by-line.

**M8 — GeoNames `map()` silently drops short/blank rows.**
Returns `[]` with no dead-letter and no log for rows with too few columns or a blank id/name
(`connector.py:94,98`); `run_ingest`'s map try/except catches exceptions, not empty lists, so
`collected > landed-mapped` with zero trace — a "de-dupe before counting" completeness gap.
*Direction:* debug-log or dead-letter dropped rows.

**M9 — ConfigCipher has no key rotation.**
Single Fernet, no `MultiFernet`/rotation path (`crypto.py:21`). Rotating `CONFIG_ENCRYPTION_KEY`
orphans every stored config — all decrypts fail at `driver.py:163`, surfacing as a fleet-wide ingest
outage with no migration path. *Direction:* `MultiFernet` with a key list for rotation.

**M10 — Orphaned landing objects on mid-window crash; `ensure_bucket` misclassifies auth errors.**
`landing.put` (S3) is not transactional with the windowed Postgres commit (`ingest.py:141` vs
`:201-210`): a crash after land but before commit leaves a raw object with no queue row, and every
crash accretes orphans with no GC. Separately, `ensure_bucket` treats any `ClientError` from
`head_bucket` as "bucket absent" and attempts `create_bucket` (`landing.py:71-74`), so an auth/network
error surfaces as a confusing create failure. *Direction:* landing-zone GC keyed on queue rows; only
treat a 404 as "missing".

### LOW

**L1 — er_queue dedup NULL-distinct hole.** Id-less entities (`entity_id=NULL`) bypass
`uq_er_queue_dedup` because Postgres treats NULLs as distinct (`models.py:46-48`,
`ingest.py:190`), so a crash/restart double-enqueues them — the idempotent-enqueue invariant silently
does not hold for that case.

**L2 — G4 (tenant isolation) is RETIRED under single-tenancy (D1 / ADR 0042).** This risk is now
moot: with one tenant there is nothing to isolate, and `tenant_id` has been removed from code and
schema. *(Historical: G4 had no database-level enforcement — no RLS, no tenant in any PK/FK; isolation
rested entirely on every caller's `.where(tenant_id == ...)`, so a single missed filter could silently
cross tenants. If a future managed-cloud tier reintroduces multi-tenancy as its own gate, it should
prefer DB-level enforcement — RLS / Neo4j Enterprise multi-db — over the app-layer scoping ADR 0017
settled for.)*

**L3 — `resolver_judgement` has no `left_id <= right_id` CHECK.** The canonical ordering is
caller-only (`models.py:170-177`); a mis-ordered insert `(B,A)` when `(A,B)` exists creates a second
contradictory row the pair-unique constraint does not catch, potentially bypassing a negative
judgement.

**L4 — `SET n = props` / `SET r = item.props` are full replacements.** A thinner re-emission of a
canonical entity silently clobbers a previously-stored anchor/`prov_*` (harmless within one run, but
the Gate-C re-canonicalization path will erase provenance with no diff/audit). (ftmg `transform.py`.)

**L5 — No write-boundary enforcement of G1.** `write_entities` projects provenance only if present;
an unstamped entity yields a node/edge with zero `prov_*` and the writer raises nothing
(`provenance/model.py:73-75`). The non-negotiable is upheld only by upstream discipline.

**L6 — OpenSanctions unstable landing key on malformed JSON.** `_record_key` falls back to
`record-<ordinal>` (`connector.py:77`); ordinals shift across re-ingests, so the dedup key won't match
for previously-bad lines, risking duplicate ER-queue rows.

**L7 — `smoke_metrics` counts `MATCH (n:Entity)` while nodes also carry FtM-schema labels.** The
`:Entity` base label is in fact stamped by ftmg, so this should match — but the count is brittle to
any ftmg labeling change and is the one observability signal during a sustained smoke run; verify it
returns non-zero against a real graph (`smoke_metrics.py:64`).

**L8 — Resource lifecycle leaks.** `score_pairs` builds a fresh `DuckDBAPI()` + `Linker` per batch
with no close (`splink_model.py:151`); `build_driver`/`main` never dispose the Engine or close the
Neo4j Driver (`driver.py:292-325`). Harmless for a short-lived process, but accumulates over a long
bounded-drain.

**L9 — `registry.discover_module` has no per-connector error isolation.** It instantiates every
concrete `Connector` with a no-arg `obj()` (`registry.py:69`); one bad `__init__` or a duplicate
`connector_id` aborts the whole registry build (`driver.py:288`), disabling the entire fleet.

**L10 — Mixed time sources.** `created_at`/`started_at` use the DB clock (`server_default now()`)
while `last_run`/`next_run`/`finished_at` use the app clock (`driver.py:251`); durations across these
can be slightly skewed/negative if clocks differ.

**L11 — `settings.sqlalchemy_dsn` rewrites only `postgresql://`.** Any other scheme passes through
unvalidated and fails late or uses the wrong driver (`settings.py:89-91`). `get_settings()` is
`@lru_cache`-memoized process-wide, so post-first-read env changes are ignored.

**L12 — `recover_stale` does not back off a crash-looping connector.** It resets a `running` instance
to `enabled` without advancing `next_run` (`driver.py:108-109`), so a crashing connector is
immediately re-due every tick. Also, under any future multi-writer it would stomp another live
writer's `running` rows (no lease).
