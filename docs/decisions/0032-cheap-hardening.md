# 0032 — Cheap hardening + audit follow-ups

- **Status:** accepted
- **Date:** 2026-06-21
- **Follows up:** [0029](0029-ingest-driver-gate-a.md) (driver), [0030](0030-alembic-migrations.md) (migrations), `docs/ARCHITECTURE_REVIEW.md` (latent-issue hunt)

## Context

The architecture review (`docs/ARCHITECTURE_REVIEW.md`) surfaced one BLOCKER and several
HIGH findings, and ADRs 0029/0030 left small follow-ups. This ADR collects the **cheap,
clearly-correct** fixes; the gate-sized / architectural ones are explicitly **deferred**
(below), not built.

## Decision — built

1. **Driver-loop resilience (review B1).** `run_forever` now wraps each tick, and
   `run_due_ingests` / `run_resolution` wrap each per-instance / per-tenant call, in a
   `try/except` that logs and continues. A transient failure (a finalize commit, one bad
   instance, one tenant's resolution) can no longer propagate out and kill the whole
   long-running driver. Proven by `test_run_due_ingests_survives_a_crashing_instance`.

2. **`task_run` retention/pruning (ADR 0029 follow-up).** `TASK_RUN_RETENTION_DAYS`
   (default 30, 0 disables); `IngestDriver.prune_task_runs` deletes **finished** rows past
   the window on startup (`running` rows are never pruned). Keeps the history table
   bounded. Proven by `test_prune_task_runs_removes_old_finished_only`.

3. **Config → URL hardening (review H5, SSRF / path injection).** The OpenSanctions
   `dataset` and GeoNames `country` config fields now carry strict `pattern`s
   (`^[a-z0-9_]+$`, `^[A-Za-z]{2}$`) in their `config.schema.json`, so a value cannot
   inject a path segment or host into the outbound URL. Proven by `test_ingest_safety.py`.

4. **Landing-key sanitization (review H6, tenant-prefix escape).** `_safe_segment`
   sanitizes the connector-controlled `record.key` (and `dataset`) before the S3 object
   key is built, collapsing path separators and stripping leading dots — so a hostile key
   cannot escape the `tenant_id/connector_id/...` prefix (G4 at the storage layer).

5. **Notes (the two requested doc follow-ups).** `db/migrations/env.py` documents the
   `compare_type` callback as the fix if a future model type isn't synonym-covered by the
   drift guard. The single-node `recover_stale` → HA-lease (X2) assumption stays
   documented in the driver docstring and ADR 0029.

## Decision — deferred (NOT built here)

These need an architectural change or a gate decision; they are tracked, not fixed:

- **HA driver lease (X2)** — a gate's worth of work (lease/heartbeat/owner column to
  replace startup stale-reset under multi-replica). **Surface, do not build.**
- **Cross-store write-before-commit (review H1/H2)** — Neo4j write precedes the Postgres
  commit; a crash between them + non-deterministic `NK-` ids can orphan a node. The real
  fix (outbox/saga, or content-addressed canonical ids) overlaps **Gate B** (cross-batch
  dedup / stable ids). Deferred.
- **Entity-reference link drop (review H3)** — ~~deferred~~ **FIXED 2026-06-22** (a richer
  smoke run hit it, dropping ~1867 real `addressEntity` relationships). The fix was NOT
  Gate-C work: `graph/writer.py._align_entity_link_ids` strips the `entity:` prefix from
  the entity-link `MATCH` endpoint ids so they realign with the raw node ids — **scoped to
  the entity-link path only** (edge-schema + topic batches already use raw ids; abstract-
  `Thing`-range / **G3** links are skipped inside ftmg before a batch exists, so they are
  untouched and **remain deferred**). The H3/G3 boundary is pinned by
  `tests/integration/test_entity_link_materialization.py` (concrete range materializes,
  abstract range stays dropped). G3 (abstract-`Thing`-range, `Sanction.entity`) and H4
  below stay deferred.
- **Provenance collapse on merged nodes (review H4)** — a canonical fused from N sources
  projects only the first source's `prov_*`. Multi-valued provenance projection is a
  graph-write-model change; full lineage still survives in `raw_entity` + `merge_audit`.
  Deferred.
- **Single-writer-per-tenant (review H7 / X3)** — concurrent `resolve_pending` for one
  tenant can double-process; mitigated by the single-node lock until X3 is built.
- **Robust multi-script name scoring (abjad gap of [0035])** — [0035] fixed the
  cross-script ER miss with a `fingerprints` key, but `fingerprints` renders abjad
  (Arabic/Persian) as lossy consonant skeletons, so it is not a reliable *sole* key there.
  Follow-up: adopt nomenklatura `LogicV2` as a **post-blocking re-scoring** step (a row-wise
  Python matcher that does not vectorise in DuckDB → its own ADR, not a `_flatten` tweak).

## Consequences

- ✅ The driver survives transient failures and bounds its own history — safe for the
  sustained smoke run (`docs/runbooks/smoke-run.md`).
- ✅ Two real input-trust holes (SSRF, landing-key escape) closed at the boundary.
- The deferred items are recorded in `ARCHITECTURE_REVIEW.md` §7 + here, each gated on an
  explicit decision (X2/X3) or a locked later gate (B/C).
