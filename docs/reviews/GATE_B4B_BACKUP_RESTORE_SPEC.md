# Gate B-4b — Backup / restore disaster recovery

- **Gate:** B-4b (ONE B-4 slice — backup/restore DR only).
- **Branch:** `gate/b4b-backup-restore` (off `master` @ `748e35b`).
- **ADR:** `docs/decisions/0050-backup-restore-disaster-recovery.md` (PROPOSED).
- **Severity:** HIGH (cross-line audit). Workflow B has **NO** backup/restore / DR of any kind.
- **Independently built on the other line:** Workflow A (`f3c40c3` initial, `a5bb7d5`
  robustness + CI round-trip). B re-derives — does **not** copy. A's line is the cross-check.
- **NOT in this gate (other B-4 slices):** app `Dockerfile`, supervised compose, `/ready`,
  driver heartbeat, dead-letter pruning. Those are B-4c/d/e — hard stops (§11).

---

## 1. The gap (verified against B's code)

B persists its entire system of record across **three stores** and can recover **none** of it:

| Store | What lives there | Loss on a disk failure today |
|---|---|---|
| Postgres (app DB) | `resolver_judgement` (the H-1 negative/positive human decisions), `sign_off`, `merge_audit`, `canonical_id_ledger` (the no-un-merge durable-id ledger), `er_queue_item`, `ingest_dead_letter`, `task_run`, `merge_alerts`, `er_gold_pair`, `connector_instance`, `alembic_version` | every human reject/approve + the canonical-id ledger — **unrecoverable** |
| Neo4j (the graph) | the resolved entity graph — the product (nodes + edges, each with `prov_*` G1 provenance, topic labels) | the product graph — **unrecoverable** |
| MinIO (landing) | every raw record behind every `source_record` provenance pointer | provenance bytes — **unrecoverable** |

Verified absent in B: no `src/worldmonitor/backup.py`, no `deploy/backup/`, no DR runbook,
no backup test. `find src -name backup*` and `find deploy -type d` confirm.

**Why this matters for THIS product specifically.** A restore that silently drops the
`resolver_judgement` negatives re-enables the exact **H-1 transitive re-merge** corruption ADR 0037
closes: a bridging record would re-fuse a human-rejected pair into one canonical node, silently
overriding "these are distinct" — but now *via disaster recovery*. Backup/restore is therefore not
generic ops plumbing; its correctness bar is **the human-reject guarantee and the canonical-id
ledger survive DR byte-for-byte.**

---

## 2. Workflow A studied (re-derived from `f3c40c3` + `a5bb7d5`) — and the robustness bug

A's approach (bash, compose-driven):
- **Postgres:** `pg_dump --clean --if-exists` (app **and** zitadel DBs).
- **Neo4j (Community):** `neo4j-admin database dump` — Community has no online/hot backup, so A
  **stops neo4j** (a brief outage), runs a transient container mounting the `neo4j-data` volume +
  a host staging dir, dumps offline, restarts. Restore is the symmetric `neo4j-admin database load`.
- **MinIO:** `mc mirror` the landing bucket out, `mc mirror` back on restore.
- **Validation:** `deploy/backup/selftest.sh` wired into `compose-boot.yml`: migrate → seed
  (graph nodes + a **NEGATIVE** `resolver_judgement` + a `sign_off` + a landing object) → baseline
  counts → backup → `down -v` (WIPE) → restore → ASSERT graph counts match, judgement + sign_off
  rows present, and **the previously-REJECTED pair is STILL rejected** (the H-1 guarantee survives).

### 2.1 The `a5bb7d5` robustness bug(s) — the cross-line signal, and B's susceptibility

A's first real run + the new CI round-trip surfaced **four** defects. Each is a probe for whether
B's *different* design is susceptible:

1. **Neo4j dump `AccessDeniedException: /dumps`.** The transient `neo4j-admin` container runs as the
   in-image **non-root `neo4j` uid** and could not write the host-mounted staging dir (fixed by
   `chmod 777`). → **B is NOT susceptible.** B's Neo4j export goes through the Python process over
   Bolt; the process owns the output dir. There is **no container-uid bind-mount** anywhere in B's
   design. This is a concrete argument *for* B's method (§3).
2. **Silent partial backup.** A originally could `down -v` believing a backup succeeded when the
   Neo4j dump had silently produced nothing. Fixed by: guard the dump (always restart neo4j), **fail
   non-zero if the dump errors OR yields an empty file**, and **verify EVERY artifact present +
   non-empty before declaring success.** → **B IS susceptible in principle** (any backup tool can
   skip a store) and MUST guard by construction: a manifest with per-store counts + a completion
   marker, and `backup()` **raises** if any of the three stores fails or is empty (§4.4). This is a
   HARD invariant for B (halt-loud).
3. **Empty-collection writes nothing.** `mc mirror` of an **empty** bucket created no directory, so
   the later `cp` failed ("Could not find the file") on a fresh stack. → **B's closest analogue.**
   B's *logical* export must represent "zero objects / zero nodes / zero edges" as an **explicit
   empty collection in the manifest, never as a missing artifact.** A missing-vs-empty confusion is
   exactly the failure that would make restore think the backup is corrupt (or silently restore
   nothing). The round-trip test includes an empty-store case (§6).
4. **Restore touched stores before validating / before they were ready.** Fixed by: validate the
   backup dir + all artifacts **up front (abort, nothing touched, if any missing)**, `up -d --wait`
   the stores first, `psql ON_ERROR_STOP=1`. → **B IS susceptible** to the validate-before-wipe half
   (wiping then discovering a bad backup = total loss). B replicates: **all-or-nothing restore** —
   validate the manifest + all three artifacts before touching any store; Postgres restore inside a
   single transaction; raise on any partial failure; re-verify post-restore counts against the
   manifest (§5). The wait-for-ready half is compose-timing-specific; B's clients are already
   connected, the ops `.sh` wrapper still `up -d --wait`s.

**Net:** B's method sidesteps A's #1 entirely and must defensively replicate #2/#3/#4. These three
become B's halt-loud HARD invariant and three of its round-trip assertions.

---

## 3. B's design — implementation-language decision (justified)

**Decision: a Python module `src/worldmonitor/backup.py` (`backup(...)` / `restore(...)` + a thin
`python -m worldmonitor.backup` CLI), validated by a pytest testcontainers round-trip; with thin
`deploy/backup/{backup,restore}.sh` ops wrappers that build the clients from settings and call the
CLI.** Not A's bash-against-compose.

Weighed against A's shell approach:

| | A: bash + compose + `neo4j-admin`/`pg_dump`/`mc` | **B: Python module + testcontainers round-trip (chosen)** |
|---|---|---|
| H-1 / canonical-id assertions | bash exit code + `cypher-shell`/`psql` greps in a `.sh` | **in-process** — load the restored `ResolverJudgement`s and run `cluster_and_merge`; snapshot the `canonical_id_ledger` + node-id set as Python objects and assert byte-equality |
| CI exercise | needs a bespoke compose `selftest.sh` (A's blind spot — `a5bb7d5`) | runs in the **existing** `integration` job via `@pytest.mark.integration` + the existing `postgres_dsn`/`neo4j_client`/`minio` testcontainer fixtures — no new CI job for the core proof |
| container-uid bind-mount bug (`a5bb7d5` #1) | susceptible | **eliminated** (export over Bolt / SQLAlchemy / boto3, process owns the dir) |
| Neo4j outage | required (offline `neo4j-admin`) | none for the logical export (§4.2) |
| version/store-format portability | `neo4j-admin` dump is store-format/version-bound | logical export is version-portable |
| consistency | three separate CLI tools, two languages | **one** code path for ops **and** test |

This is the "B's way — more testable + consistent" the gate calls for, and it aligns with CLAUDE.md
(testability, 12-factor, the graph-as-logical-projection model). The `.sh` wrappers exist only so an
operator has a one-command ops entrypoint; they carry no logic (they `python -m worldmonitor.backup
{backup,restore} ...`).

**This is not a product/architecture fork** — it is an engineering-method choice with a clear best
answer given single-node Community + the in-process-assertion requirement + A's independent
precedent. ADR 0050 is **PROPOSED**, not OPEN. No human STOP.

---

## 4. Store-by-store backup plan

`backup(*, neo4j, session, landing, dest: Path) -> BackupManifest`. Writes, under `dest/`:
`postgres.json`, `neo4j.json`, `landing/` (+ a `landing_index.json`), and `manifest.json` (the
completion marker + per-store counts). Backup is **read-only on the live system** — it never mutates
any store, so it is safe to run autonomously / on a schedule.

### 4.1 Postgres — SQLAlchemy logical dump of every table (+ `alembic_version`)

Read every table in `Base.metadata.sorted_tables` (connector_instance, er_queue_item, merge_audit,
ingest_dead_letter, merge_alerts, task_run, **resolver_judgement**, **sign_off**, **canonical_id_ledger**,
er_gold_pair) plus the single `alembic_version` row, serialize each row to JSON → `postgres.json`
(`{table: [rows...], "alembic_version": "<rev>"}`).

- **Why logical, not `pg_dump`:** B's PKs are all `String(64)` (no autoincrement sequences to
  preserve), one app DB, JSONB columns round-trip as JSON — so a row-level dump is **lossless for
  B's schema** and runs **in-process** against the testcontainer (the H-1 + count assertions read
  the same objects). `pg_dump` is the right general tool but is a subprocess that can't be asserted
  in-process and pulls in the zitadel auth DB (out of scope, §11). Considered + rejected as primary;
  the door is open to add a `pg_dump` ops variant later without changing the contract.
- **`alembic_version` is captured and restored** so the rebuilt DB reports the **same migration
  head** — a post-restore `migrate_to_head` is then a no-op, not a surprise re-migration.

### 4.2 Neo4j — online Cypher logical export (THE Community dump decision)

**Decision: an online Cypher logical export, not the offline `neo4j-admin database dump` A used.**

- **Nodes:** `MATCH (n) RETURN labels(n) AS labels, properties(n) AS props` — captures the node `id`
  (the canonical id), `name` arrays, **every `prov_*` key + `prov_witnesses`** (G1), and topic labels.
- **Edges:** `MATCH (a)-[r]->(b) RETURN type(r) AS type, a.id AS start, b.id AS end,
  properties(r) AS props` — captures the relationship type, endpoint **`id`s**, and **edge `prov_*`** (G1).
- Serialized → `neo4j.json` (`{"nodes": [...], "edges": [...]}`).

**Why online logical, given single-node Community:**
- Single-node **Community has no online/hot *physical* backup** (that is Enterprise) — but a logical
  Cypher export is a **query-level read**, fully supported on Community, with **no neo4j stop / no
  outage**. (A's offline `neo4j-admin` dump needed an outage precisely because it is physical.)
- It runs **in-process via the existing `Neo4jClient`** → the testcontainer round-trip exercises it
  and asserts H-1 + byte-identical canonical ids in-process (the offline dump cannot be driven by the
  test fixture — the data lives inside the container; you'd have to exec in and stop neo4j).
- It is **version/store-format portable** (a `neo4j-admin` dump is bound to the store format).

**Trade-offs (stated):**
- An offline `neo4j-admin` dump is *physically* complete (internal element ids, indexes, the
  `system` DB) and faster for very large graphs. The logical export does **not** preserve Neo4j
  **internal element ids** — but **nothing in B references them**: the graph keys on the `id`
  property (ADR 0042 native `{id}` MERGE), so the **application identity** (the canonical id) is what
  round-trips byte-for-byte, which is exactly the byte-identity invariant. Indexes/constraints are
  **not** carried in the export; restore re-establishes them via the existing **idempotent**
  `graph/constraints.py:ensure_constraints` (§5).
- Logical export is **not point-in-time consistent under concurrent writes** — run it in a
  low-activity window (single-node v0; the driver's resolve cadence is the only writer and is
  pausable). This is the *same* operational constraint as A's offline dump (which stops neo4j); B
  trades a hard outage for a "quiet window" recommendation.
- An offline physical `neo4j-admin` dump + a host **volume snapshot** remain a fine *optional bonus*
  (faster, captures everything) — explicitly a named follow-up, not this gate.

**Restore label/type fidelity, APOC-free.** B's test/compose Neo4j has **no APOC**, so dynamic labels
/ relationship types are reconstructed by **grouping** the export by label-set (resp. type) and
emitting one parameterized `MERGE` per group, with the label/type tokens inlined as a `LiteralString`
built from the **closed, validated vocabulary** (FtM schema names + `topic.*` codes — the same
closed-vocabulary-inlining idiom the sensitivity guard already uses for its k-hop bound). The
round-trip test (§6) is what proves the export/import is lossless (counts + byte-identity + a
preserved topic label).

### 4.3 MinIO landing — object mirror

`landing.list_keys("")` (already pages past the 1000-key cap), download each object's bytes →
`dest/landing/<key>`, and write `landing_index.json` (the explicit key list — so an **empty bucket
is an explicit empty list**, never a missing dir; A's `a5bb7d5` #3). Restore re-`put`s each object.

### 4.4 Manifest + halt-loud (verify EVERY store — A's `a5bb7d5` #2)

`manifest.json` records `{created_at, alembic_version, postgres_row_counts:{table:n},
neo4j:{nodes:n, edges:n}, landing:{objects:n}, complete:true}`. `backup()`:
- **Raises** (does not silently skip) if any store's read fails.
- Writes the manifest **last**, with `complete:true`, only after all three artifacts exist.
- Re-verifies each artifact is present and its count matches before returning the manifest.

So it is impossible to wipe believing a backup succeeded.

---

## 5. Restore plan — all-or-nothing, halt-loud (A's `a5bb7d5` #4)

`restore(*, neo4j, session, landing, src: Path) -> RestoreResult`. **Destructive** (overwrites all
stores) — a deliberate operator action (runbook), **never agent-auto-run**.

1. **Validate before touching anything:** `manifest.json` exists with `complete:true`; `postgres.json`,
   `neo4j.json`, `landing_index.json` all present and parseable. Any miss → **raise, nothing
   touched.**
2. **Postgres — single transaction:** `TRUNCATE` all `Base.metadata` tables, bulk-insert every row,
   stamp `alembic_version`. Staged on the caller's `session`; **commit only after Neo4j + MinIO also
   succeed** (or `restore()` raises and the caller's context manager rolls back) → Postgres is
   all-or-nothing.
3. **Neo4j:** `MATCH (n) DETACH DELETE n`; `ensure_constraints`; MERGE nodes (grouped by label-set),
   then MERGE edges by endpoint `id` (grouped by type). Raise on any error.
4. **MinIO:** `ensure_bucket`; delete existing objects; re-`put` every backed-up object. Raise on any
   error.
5. **Post-restore re-verification:** re-count all three stores; **raise** if any count ≠ the
   manifest. A half-restore can never return success.

---

## 6. Round-trip test plan (`tests/integration/test_backup_restore_roundtrip.py`)

`@pytest.mark.integration`, fixtures `postgres_dsn` + `clean_graph` + `minio` (testcontainers; Docker
is available locally — run it). Temp `dest` in the scratchpad / `tmp_path`.

### 6.1 The main round-trip — `test_backup_wipe_restore_preserves_h1_and_canonical_ids`

**SEED pre-backup truth** (re-using the `test_t6` erasure-test idiom):
- Two anchored Company rows sharing `wikidataId=Q888` + a **positive** `ResolverJudgement` forcing
  their merge → resolve → a durable **`wm-anchor-qid-Q888`**-class canonical node, a
  `canonical_id_ledger` (canonical + alias rows), and a `merge_audit("merged")`.
- An **OWNS edge** from that canonical node to a second canonical node, both carrying `prov_*` (the
  G1-on-edges oracle); the canonical node also carries a topic label (the guard-vocabulary oracle).
- A human **NEGATIVE** `ResolverJudgement` on a forbidden pair `('a','c')` (the H-1 oracle) + a
  `SignOff`.
- A landing object (a probe) with known bytes.
- **Baselines captured as Python objects:** sorted node-`id` set; edge count; the full
  `canonical_id_ledger` snapshot `(canonical_id, canonical_alias, anchor_kind, anchor_value)`; the
  `resolver_judgement` rows (incl `('a','c','negative')`); `sign_off`/`merge_audit`/per-table counts;
  the landing key→bytes map.

**BACKUP** → `dest`. **WIPE** all three (`TRUNCATE …`; `MATCH (n) DETACH DELETE n`;
`delete_prefix("")`) and **assert all three are empty** (the real test). **RESTORE** from `dest`.

**ASSERT (the load-bearing invariants):**
1. **H-1 preserved — THE reason this gate exists.** (a) `('a','c','negative')` is present in the
   restored `resolver_judgement`. (b) *Functional* proof: load the restored judgements
   (`pipeline._load_judgements`) and call `cluster_and_merge([a,b,c], [a~b 0.99, b~c 0.95],
   judgements=restored)` — **assert `a` and `c` are NOT co-clustered** (the bridging `b` joins its
   stronger side). The human reject survived DR; a post-DR resolve cannot re-fuse it. (Mirrors
   `tests/unit/test_resolution_negative_judgement.py`.)
2. **Canonical-id byte-identity (ties to ADR 0048).** Restored node-`id` set **==** baseline,
   byte-for-byte (esp. `wm-anchor-qid-Q888` — if the export mangled the FtM-clean anchor id, edge
   endpoints would break: the CID-class bug). Edge count == baseline. `canonical_id_ledger` snapshot
   **==** baseline, byte-for-byte.
3. **G1 on every node AND edge.** Every restored node has `prov_source_id`; the restored OWNS edge
   has its `prov_*` (the seeded edge-provenance survives); the topic label survives (the guard still
   sees it).
4. **Postgres count-match.** `resolver_judgement` / `sign_off` / `merge_audit` / `canonical_id_ledger`
   / `er_queue_item` / `ingest_dead_letter` / `task_run` counts == baselines; restored
   `alembic_version` == head.
5. **MinIO restored.** the landing probe's bytes are byte-identical after restore.
6. **Exact-state (idempotent-ish).** the whole restored snapshot equals the pre-backup baseline.

### 6.2 Halt-loud tests
- `test_restore_aborts_on_incomplete_backup_without_touching_stores`: a missing/`complete:false`
  manifest (or a missing artifact) makes `restore()` **raise before any store is touched** (seed a
  store, attempt the bad restore, assert the store is unchanged).
- `test_backup_raises_if_a_store_export_fails`: a store read raising propagates (no silent partial;
  no `complete:true` manifest written).

### 6.3 Empty-store test (A's `a5bb7d5` #3)
- `test_backup_restore_of_empty_stores_roundtrips`: backup with zero landing objects / empty graph
  yields **explicit empty collections** (not missing artifacts); restore succeeds and leaves empty
  stores. No missing-vs-empty confusion.

### 6.4 Failing-first
The test file does `from worldmonitor.backup import backup, restore` (lazily, so it collects). With
**no `src/worldmonitor/backup.py`** the import fails → the suite is **RED** (ImportError /
missing scripts). It goes **GREEN** once the module is built. Stated here so the RED→GREEN signal is
explicit, exactly as B-4a was failing-first.

---

## 7. Migration conclusion

**NONE.** Backup/restore only **reads/writes existing tables** (every `Base.metadata` table +
`alembic_version`) and the existing graph/landing surfaces. No new table/column/constraint. The
Alembic drift guard `tests/integration/test_migrations.py` (alembic head == create_all, ADR 0030) is
**not triggered and MUST stay green** (FROZEN). `db/models.py` is **read-only** (imported, not
edited) and is **not** in scope.

---

## 8. Person-affecting / sign-off assessment

**Ops / DR — not person-affecting in the merge/score sense → NO per-run human sign-off.** Backup is
read-only (safe to schedule autonomously). Restore is destructive ops (overwrites all stores) — a
deliberate, runbook-documented operator action, **never agent-auto-run**, but it does not require
per-record sign-off (it neither merges nor scores nor mutates a threshold).

**BUT** the correctness bar **is** the preservation of human decisions: restore MUST reproduce the
`resolver_judgement` negatives + `sign_off` + `canonical_id_ledger` byte-for-byte. That is the H-1
invariant (§6.1, §10) — a restore that loses a human rejection is a **DENY**. `human_fork: false`
(the design is determinable from CLAUDE.md + the existing ADRs + A's precedent).

---

## 9. Locked invariants the gate must hold + APPROVE/DENY

- **G1 — provenance on every node AND edge.** Backup captures and restore reproduces `prov_*` on
  every node **and** every edge byte-identically. **DENY** if a restored node/edge loses provenance,
  or any value/edge-provenance test regresses.
- **Append-only / no un-merge.** Backup never mutates the live system. Restore is a point-in-time
  **rebuild** that reproduces the append-only `canonical_id_ledger` **exactly** (the §6.1 byte-identity
  snapshot) — it does not un-merge, re-cluster, split, or resurrect. **DENY** if the restored ledger
  differs from the backed-up ledger, or restore mutates ledger semantics.
- **Canonical-canonical only via the guard.** PRESERVED **VACUOUSLY** — backup/restore runs no
  resolver, performs no merge, creates no canonical-canonical edge. `DEFAULT_MERGE_THRESHOLD` /
  Splink / `cluster_and_merge` / the sensitivity guard are **untouched**; restoring the H-1 negatives
  is exactly what keeps the guard's prior decisions intact post-DR. **DENY** if any
  merge/threshold/score/guard path is altered.
- **Halt-loud (the gate's own HARD invariant).** Backup raises + writes no `complete` manifest on any
  partial failure; restore validates before touching any store and is all-or-nothing. **DENY** if a
  partial failure is silent.

**APPROVE** iff the round-trip proves: a previously-REJECTED pair stays rejected after restore; node/
edge counts + canonical ids are byte-identical; Postgres rows count-match; MinIO objects restored;
and any partial failure halts loudly. **DENY** if a restore loses a human rejection, or canonical
ids change, or a partial failure is silent.

---

## 10. Out of scope (hard stops — the other B-4 slices)

- **NO** app `Dockerfile`, **NO** supervised compose service definitions, **NO** `/ready`, **NO**
  driver heartbeat, **NO** dead-letter pruning (B-4c/d/e).
- **NO** zitadel auth-DB backup (Phase-2 auth; single-tenant, reconstructable from config) — named
  follow-up.
- **NO** offline `neo4j-admin`/physical volume-snapshot path, **NO** `system`-DB / index export —
  named optional bonus.
- **NO** edit to `db/models.py`, `merge.py`, `writer.py`, `pipeline.py`, `provenance/model.py`, the
  resolver, or any migration.
- **NO** new table / migration / status field; **NO** live API/MCP restore surface (Phase 2).
- **NO** incremental / point-in-time-recovery / scheduling daemon (the runbook gives a sample cron).

---

## 11. Slice plan

**ONE slice (core).** Independently mergeable, failing-test-first.

- **Slice 1 (core):** `src/worldmonitor/backup.py` (`backup` / `restore` + `__main__` CLI) +
  `deploy/backup/{backup,restore}.sh` thin wrappers + `tests/integration/test_backup_restore_roundtrip.py`
  (§6.1–6.4) + `docs/runbooks/backup-restore.md`. Proves H-1 + byte-identical canonical ids +
  halt-loud in the **existing** `integration` CI job (no CI-config edit — the test is
  `@pytest.mark.integration`).
- **Slice 2 (OPTIONAL hardening, may be its own follow-up):** a thin `compose-boot.yml` step that
  runs the `deploy/backup/*.sh` wrappers against the real compose stack (backup → `down -v` →
  restore → re-verify), so the ops entrypoint is not the untested blind spot A hit (`a5bb7d5`). Only
  this slice touches `.github/workflows/compose-boot.yml`. Skip if keeping the gate minimal.

`human_fork: false`.

---

## 12. Verdict

**Build slice 1.** The design re-derives A's intent on B's terms (Python + testcontainers,
in-process H-1/CID assertions, no container-uid bug, no outage), defensively replicates A's
`a5bb7d5` halt-loud / empty-collection / validate-before-wipe fixes, and locks the H-1 +
byte-identical-canonical-id invariants as the APPROVE/DENY bar. Not a product fork; ADR 0050 is
PROPOSED, no human STOP required.
