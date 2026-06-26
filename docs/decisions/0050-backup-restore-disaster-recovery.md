# 0050 — Backup / restore disaster recovery (Gate B-4b)

- **Status:** PROPOSED
- **Date:** 2026-06-26
- **Gate:** B-4b (`docs/reviews/GATE_B4B_BACKUP_RESTORE_SPEC.md`) — a focused DR gate off `master`.
  ONE B-4 slice (backup/restore only).
- **Touches:** new `src/worldmonitor/backup.py` (`backup` / `restore` + a `__main__` CLI); thin
  `deploy/backup/{backup,restore}.sh` ops wrappers; `tests/integration/test_backup_restore_roundtrip.py`;
  `docs/runbooks/backup-restore.md`. **No schema/migration.** `db/models.py` is read-only.
- **Addresses:** the cross-line production-readiness audit finding — **B has NO backup/restore / DR
  of any kind** (HIGH).
- **Preserves (does NOT relitigate):** [0042](0042-single-tenancy-teardown.md) (native `{id}` MERGE
  — the graph keys on the `id` property, which is what restore preserves), [0037](0037-transitive-negative-judgement.md)
  (H-1 transitive reject enforcement), [0044](0044-anchor-preferred-stable-ids.md) /
  [0048](0048-ftm-valid-injective-durable-id.md) (the durable canonical id + ledger),
  [0030](0030-alembic-migrations.md) (Alembic head; restore captures + re-stamps `alembic_version`),
  [0024](0024-merge-guard-alert-mode-build-phase.md)/[0047](0047-fail-closed-sensitivity-guard.md)
  (the merge/sensitivity guards — untouched).
- **Cross-line:** Workflow A built this independently (its ADR 0040; commits `f3c40c3` initial,
  `a5bb7d5` robustness + a CI round-trip). B **re-derives** — A is the cross-check, not the source.
  A's `a5bb7d5` robustness bugs are studied and defended against (§Consequences).
- **Not OPEN:** this is an engineering-method choice with a clear best answer (below), not a
  product/architecture fork. No human STOP.

## Context

B persists its system of record across **three** stores — Postgres (the human decisions: the H-1
`resolver_judgement` negatives/positives, `sign_off`, `merge_audit`, the no-un-merge
`canonical_id_ledger`, plus `er_queue_item`/`ingest_dead_letter`/`task_run`), Neo4j (the resolved
graph — the product, with `prov_*` G1 provenance on every node and edge), and MinIO (the raw bytes
behind every provenance pointer) — and can recover **none** of it. A single disk failure loses every
human reject/approve and the entire product graph.

This is product-specific, not generic ops: a restore that silently drops the `resolver_judgement`
negatives re-enables the exact **H-1 transitive re-merge** corruption ADR 0037 closes — a bridging
record re-fusing a human-rejected pair into one canonical node — now reached *via disaster recovery*.
So the correctness bar is **the human-reject guarantee and the canonical-id ledger survive DR
byte-for-byte**, not merely "the process exits 0".

Workflow A built backup/restore in bash against compose (`pg_dump`, offline `neo4j-admin database
dump` with neo4j stopped, `mc mirror`). Its `a5bb7d5` host-validation run found four defects —
a container-uid bind-mount `AccessDeniedException` on the Neo4j dump, a silent partial backup, an
empty-bucket mirror writing nothing, and a restore that touched stores before validating — and
closed the blind spot by exercising the scripts in a compose CI round-trip. That bug class is the
signal B must design against.

## Decision

**1. Implement backup/restore as a Python module (`src/worldmonitor/backup.py`), validated by a
pytest testcontainers round-trip, with thin `deploy/backup/*.sh` ops wrappers** — not bash against
compose. The H-1 and canonical-id assertions then run **in-process** (load the restored
`ResolverJudgement`s and run `cluster_and_merge`; snapshot the `canonical_id_ledger` + node-id set as
Python objects and assert byte-equality), the proof runs in the **existing** `integration` CI job via
`@pytest.mark.integration`, the design has **no container-uid bind-mount** (eliminating A's `a5bb7d5`
#1 by construction), and ops + test share **one** code path.

**2. Postgres — a SQLAlchemy logical dump of every `Base.metadata` table + the `alembic_version`
row** → JSON; restore truncates + bulk-inserts + re-stamps `alembic_version`. Lossless for B's
schema (all PKs are `String` — no sequences; one app DB; JSONB round-trips as JSON) and in-process
testable. (`pg_dump` is the considered general-purpose alternative — heavier, a subprocess, pulls in
the out-of-scope zitadel auth DB; addable later without changing the contract.)

**3. Neo4j — an online Cypher *logical* export, not the offline `neo4j-admin database dump`.**
Read nodes (`labels`, `properties` incl. `id`, `name`, every `prov_*`, `prov_witnesses`) and edges
(`type`, endpoint `id`s, `properties` incl. edge `prov_*`) over the existing `Neo4jClient` → JSON;
restore re-establishes constraints (`ensure_constraints`, idempotent) then MERGEs nodes (grouped by
label-set) and edges (grouped by type), with label/type tokens inlined from the **closed, validated
vocabulary** (FtM schema names + `topic.*` codes) — APOC-free (B's stack has no APOC).
Rationale: single-node **Community has no online physical/hot backup** (that is Enterprise), but a
logical Cypher export is a query-level read — **no neo4j stop / no outage**, runnable in-process by
the test fixture, and version/store-format portable. The graph keys on the `id` property (ADR 0042),
so the **application identity (the canonical id) round-trips byte-for-byte** even though Neo4j
**internal element ids are not preserved** (nothing references them).

**4. MinIO — an object mirror.** List all keys (paging past the 1000-key cap), download bytes →
`landing/` + an explicit `landing_index.json`; restore re-`put`s each. An empty bucket is an
**explicit empty list**, never a missing artifact.

**5. Halt loudly on partial failure.** Backup writes a `manifest.json` (per-store counts +
`complete:true`) **last**, only after all three stores are captured and verified non-empty/present,
and **raises** on any store-export failure. Restore **validates** the manifest + all three artifacts
**before touching any store** (abort, nothing touched, if incomplete), restores Postgres inside a
**single transaction** (commit only after Neo4j + MinIO also succeed), and **re-verifies post-restore
counts against the manifest** — a half-restore can never return success.

**6. The round-trip test is the deliverable.** `test_backup_restore_roundtrip.py` seeds an anchored
merge (a `wm-anchor-qid-*` canonical node + ledger), an edge with `prov_*`, a topic label, a human
**NEGATIVE** `ResolverJudgement` + `SignOff`, and a landing object; backs up; **WIPEs all three
stores**; restores; then asserts (a) the rejected pair stays rejected (row present **and**
`cluster_and_merge` over the restored judgements does not re-fuse it), (b) node/edge counts +
canonical ids (node-`id` set + ledger snapshot) are **byte-identical**, (c) `prov_*` survives on
every node and edge (G1), (d) Postgres rows count-match, (e) MinIO objects restored, plus halt-loud
and empty-store cases. Failing-first: with no `backup.py` the import is RED; GREEN once built.

## Alternatives considered

- **A's bash + compose + offline `neo4j-admin` dump + `pg_dump` + `mc mirror`.** Rejected as B's
  primary: the H-1/canonical-id checks degrade to `psql`/`cypher-shell` greps + a bash exit code
  (not in-process assertions); the scripts need a bespoke compose `selftest.sh` to be in CI (A's own
  blind spot); the offline Neo4j dump requires a **neo4j outage** and is susceptible to the
  container-uid bind-mount bug A had to fix. Kept only as the thin ops `.sh` wrappers (which delegate
  to the Python CLI) and as an optional bonus path (below).
- **Offline `neo4j-admin database dump` for Neo4j (physical).** Rejected as primary — needs an
  outage, cannot be driven in-process by the testcontainer fixture, and is store-format/version-bound.
  Retained as a documented **optional bonus** (faster for huge graphs; captures indexes + the
  `system` DB + internal ids) — a named follow-up, not this gate.
- **`pg_dump` for Postgres.** Rejected as primary — a subprocess that can't be asserted in-process
  and pulls in the out-of-scope zitadel auth DB; B's all-`String`-PK single-DB schema makes a logical
  row dump lossless. Addable later as an ops variant.
- **Host volume snapshots (filesystem-level).** Rejected as primary — fast and zero-downtime, but
  filesystem/host-coupled, not portable, and cannot assert the H-1/canonical-id semantics. A fine
  operational bonus on top of the dumps; out of scope here.
- **A new `backup_audit` / restore-status table.** Rejected — would force a migration; the
  `manifest.json` completion marker + the `task_run` trail already cover observability without
  touching the schema.
- **Including the zitadel auth DB now.** Rejected for scope — Phase-2 auth, single-tenant,
  reconstructable from config; a named follow-up (mirrors A's "also dump `system` if you add Neo4j
  users").

## Consequences

- ✅ B has DR for all three stores; a previously-REJECTED pair provably stays rejected after a full
  wipe + restore — the H-1 human-reject guarantee survives disaster recovery (the reason this gate
  exists).
- ✅ Canonical ids + the `canonical_id_ledger` round-trip **byte-for-byte** (ties to ADR 0048 — a
  mangled anchor id would drop edges); node/edge counts match; `prov_*` survives on every node **and**
  edge (G1).
- ✅ **A's `a5bb7d5` bug class is defended against by construction:** no container-uid bind-mount
  (#1 eliminated); verify-every-store + a `complete` manifest or raise (#2); explicit empty
  collections, never missing artifacts (#3); validate-before-wipe + all-or-nothing restore (#4).
- ✅ The proof runs in the existing `integration` CI job (testcontainers) — no new CI job for the
  core gate; backup is read-only on the live system (safe to schedule).
- ✅ Person-neutral ops: no merge/threshold/score/guard path is touched; canonical-canonical-only-via-
  the-guard is preserved vacuously. No per-run human sign-off (restore preserves human decisions
  rather than making them).
- ✅ No schema/migration: reads/writes existing tables; the Alembic drift guard stays green;
  `alembic_version` is captured + re-stamped so a post-restore `migrate_to_head` is a no-op.
- ➖ The Neo4j logical export is **not point-in-time consistent under concurrent writes** — run it in
  a low-activity window (single-node v0; the resolve cadence is the only writer). This trades A's hard
  outage for a quiet-window recommendation.
- ➖ Neo4j **internal element ids and indexes are not in the export** (re-established idempotently on
  restore); the offline physical dump + volume snapshot remain a named optional bonus for operators
  who want them.
- ⚠️ The thin `deploy/backup/*.sh` wrappers are only smoke-covered by the core slice; a full compose
  round-trip of the wrappers (closing A's exact ops blind spot) is the optional Slice 2.

## Relationship to other ADRs

- **New capability** (DR) — supersedes nothing. Reproduces, on restore, the durable state owned by
  ADR 0037 (H-1 judgements), 0044/0048 (the canonical-id ledger), and the graph projection of
  0042/0044; relies on those ADRs' invariants holding, and never alters them.
- **Independent confirmation:** Workflow A reached the same need + the same H-1-survives-restore +
  byte-count-match acceptance bar from the other line (A's ADR 0040 / `f3c40c3` + `a5bb7d5`); B
  converges on a more testable, outage-free method and treats A's robustness bugs as the cross-line
  hardening checklist.
