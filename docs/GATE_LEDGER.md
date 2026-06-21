# WorldMonitor — Gate & Audit-Gap Completion Ledger

One consolidated record of every gate and Phase-1 audit gap: **what it was → the ADR
that owns the decision → status → the tests that prove it.** Source material:
`docs/reviews/PHASE_1_AUDIT.md` (the gaps), `docs/decisions/` (ADRs 0001–0031), and the
test suite. Companion to `docs/ARCHITECTURE_REVIEW.md` (latent-issue hunt).

Status legend: **CLOSED** (built + proven) · **OPEN** (tracked debt, scheduled) ·
**DEFERRED** (a named later gate, locked decision) · **BY DESIGN** (intentional v0).

---

## 1. Phase-1 audit gaps (`PHASE_1_AUDIT.md`, Q2)

| Gap | What | ADR | Status | Proof / tests |
|---|---|---|---|---|
| **G1** | Provenance not written on **edges** (GDPR/audit invariant broken for relationships) | 0018 | **CLOSED** | `graph/writer.py` stamps `prov_*` on every relationship; `tests/integration/test_edge_provenance.py`, `test_graph_writer.py` |
| **G2** | Edge **referent-rewriting** for merged-away ids not done (orphaned edges after a merge) | 0025 | **CLOSED** (batch) | `resolution/referents.py` rewrites entity-typed values to canonical before write; `tests/integration/test_referent_rewriting.py`, `tests/unit/test_referents.py` |
| **G3** | Abstract `Thing`-range entity-links not materialised (`Sanction.entity` etc. dropped) | 0023 | **OPEN** (pre-phase-4) | Re-confirmed live in `ARCHITECTURE_REVIEW.md` **H3** (ftmg link MATCH uses `entity:`-prefixed id) |
| **G4** | No two-tenant same-canonical-ID test; resolver leaked across tenants (the **D1** regression) | 0017, **0028** | **CLOSED** | Ephemeral per-batch resolver; `tests/integration/test_tenant_isolation.py`, `test_signoff.py` (tenant-scoped judgement), `tests/unit/test_resolution.py` |
| **G5** | Size-threshold guard (`>10`) untested at the boundary | 0020 | **OPEN** (nice-to-have) | guard eval `resolution/review.py`; no 11-member boundary test yet |
| **G6** | Sensitive-topic guard is a hardcoded **denylist** (fails open for unmodelled topics) | 0020 | **OPEN** (pre-phase-4) | Re-confirmed in `ARCHITECTURE_REVIEW.md` §5 caveat + MEDIUM list |
| **G7** | Expert-set Splink weights / fixed thresholds (uncalibrated) | 0016 | **OPEN** (pre-phase-3) | calibration deferred; `resolution/splink_model.py` |
| **G8** | Batch-bound ingest (`collect()` to exhaustion, one commit, no dead-letter) | 0027 | **CLOSED** | windowed commits + wall-clock/record bounds + `ingest_dead_letter`; `tests/integration/test_ingest_runner.py`, `tests/unit/test_settings.py` |
| **G9** | Whole-queue batch ER (loads all pending, all-pairs) | 0026 | **CLOSED** (batch-first) | bounded windows per `RESOLVE_BATCH_SIZE`; `tests/integration/test_resolution_batching.py` |
| **G10** | Enricher output not re-validated before write | — | **OPEN** (pre-phase-3) | `resolution/pipeline.py` enrich path; external enrichers not in scope yet |
| **G11** | Landing `ensure_bucket` swallows `ClientError` (hides misconfig) | — | **OPEN** (nice-to-have) | `storage/landing.py`; flagged in `ARCHITECTURE_REVIEW.md` |
| **G12** | Settings ship empty placeholders (boots `/health` without a stack, fails loud on use) | — | **BY DESIGN** | `settings.py`; `tests/unit/test_settings.py`, `tests/unit/test_api_health.py` |

---

## 2. The runway (build gates)

The vertical slice built one gate at a time, each green on quality + security +
integration with independent adversarial review.

| Gate | What | ADR | Status | Proof / tests |
|---|---|---|---|---|
| **G1 provenance** | `prov_*` on every node **and** edge | 0018 | **CLOSED** | `test_edge_provenance.py`, `test_graph_writer.py` |
| **G4 isolation** | App-layer composite `(id, tenant_id)` keys; two-tenant proof | 0017 | **CLOSED** | `test_tenant_isolation.py` |
| **G2 referent rewriting** | Redirect merged-away ids to canonical before the write | 0025 | **CLOSED** | `test_referent_rewriting.py` |
| **resolve_pending (G9)** | Batch-first drain in bounded windows | 0026 | **CLOSED** | `test_resolution_batching.py` |
| **D1 / G4 resolver leak** | Ephemeral per-batch nomenklatura resolver (no shared ledger) | 0028 | **CLOSED** | `test_resolution.py`, `test_signoff.py` |
| **run_ingest (G8)** | Windowed commits + bounded collection + dead-letter | 0027 | **CLOSED** | `test_ingest_runner.py` |
| **ER-streaming Gate A** | Long-running asyncio driver; cadence; ACTIVE-refusal; idempotent enqueue | 0029 | **CLOSED** | `test_ingest_driver.py`, `test_connector_instance.py` |
| **Alembic migrations** | In-package baseline + delta; adopt pre-Alembic DBs; drift guard | 0030 | **CLOSED** | `test_migrations.py` (fresh ≡ create_all ≡ adopted; `alembic check`) |
| **Return-to-block + sign-off** | `block` default; durable tenant-scoped judgements; approve/reject CLI | 0031 | **CLOSED** | `test_signoff.py` (consumption + isolation + approve/reject + accretion re-park), `test_settings.py` |
| **Smoke-run harness** | Driver launcher + read-only metrics + runbook (operator-run) | 0029 | **CLOSED** (build) | `test_driver_wiring.py`; `docs/runbooks/smoke-run.md` |

---

## 3. Deferred surfaces (locked, not built)

These are intentional later gates with their seams left visible in code (see
`ARCHITECTURE_REVIEW.md` §6). **Not to be built without an explicit go** (Gate B/C/S4
are gated on a named real-time consumer / explicit incremental-ER decision).

| Surface | What is deferred | ADR | Why now |
|---|---|---|---|
| **Gate B** | Incremental / cross-batch ER (cross-batch dedup, stable canonical ids) | 0019 | F0: no real-time consumer; batch cadence covers downstream |
| **Gate C** | Persisted cross-run referent rewriting / graph-mutation surface; inbound-edge restore on sign-off | 0023, 0025 | append-only locked; reconstructable from retained landing + queue |
| **S4** | First-class canonical-canonical merge routing | 0031 | routed *through* the guard for now (never auto-fuse two canonicals) |
| **X1** | STREAM cursor / checkpoint | (runway) | no STREAM connector in scope |
| **X2** | Driver lease / HA (replace single-node startup stale-reset) | 0029 | single-node now; gate-sized — surface before building |
| **X3** | Single-writer-per-tenant (advisory lock / `SKIP LOCKED`) | 0029 | single-node lock holds; needed under concurrency |

---

## 4. Summary

- **Closed:** G1, G2, G4, G8, G9 (audit blockers + phase-2 pay-downs) and the full
  runway (referent rewriting → batch resolution → bounded ingest → driver → migrations →
  return-to-block sign-off), each ADR-backed and test-proven.
- **Open debt (scheduled):** G3, G6 (phase-4), G7, G10 (phase-3), G5, G11 (nice-to-have).
  Several are re-confirmed with fresh file:line evidence in `ARCHITECTURE_REVIEW.md` §7.
- **Deferred (locked):** Gate B / Gate C / S4 / X1 / X2 / X3 — none built; each gated on
  an explicit decision.
