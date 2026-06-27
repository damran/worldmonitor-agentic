# 0053 — Dead-letter (`ingest_dead_letter`) retention / pruning

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** B-4d (the dead-letter half of audit finding M-6; last B-4-family slice)
- **Branch:** `gate/b4d-deadletter-pruning` (off `master` @ `f8c322b`)
- **Supersedes / relates to:** ADR 0029 (ingest driver), ADR 0038 / 0041 (dead-letter quarantine
  stages), `task_run` retention (`prune_task_runs`, the proven sibling pattern).
- **Spec:** `docs/reviews/GATE_B4D_DEADLETTER_PRUNING_SPEC.md`

## Context

Audit finding **M-6** (`docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md:183`): "`ingest_dead_letter`
has no retention (`task_run` is pruned, but only at startup — H-8). Disk eventually fills."

The `ingest_dead_letter` table (`src/worldmonitor/db/models.py:78-107`) accumulates every record that
failed during ingest (`land`/`map`) or resolution (`resolve-row`/`resolve-batch`/`resolve-write`/
`resolve-noid`/`resolve-incompat`/`signoff-poison`). It is **never pruned** — it grows unbounded for the
life of the driver, and on a flaky source it grows fast. Per the `IngestDeadLetter` docstring, **all
quarantines are replayable**: the table is an error-audit trail, not canonical or human-decision state.

The codebase already solves the identical problem for `task_run` in `IngestDriver.prune_task_runs`
(`driver.py:125-152`): read a `*_retention_days` setting, compute `cutoff = now - timedelta(days=N)`,
delete rows older than the cutoff, `retention <= 0` disables, return the count, log at info, called once
per maintenance cycle (`driver.py:309-310`). B-4d adds the symmetric retention for dead-letters.

M-6 also covers landing-zone orphan GC, disk-usage alerting, and deterministic `record.key`. Those are
**explicitly out of scope** here — separate follow-ups. This ADR decides **dead-letter retention only**.

## Decision

1. Add a setting `dead_letter_retention_days: int = Field(default=30, ge=0)` to `settings.py`
   (env var `DEAD_LETTER_RETENTION_DAYS`), mirroring `task_run_retention_days` byte-for-byte in type and
   bound.
2. Add `IngestDriver.prune_dead_letters(*, now: datetime | None = None) -> int`, mirroring
   `prune_task_runs` but deleting **all** `IngestDeadLetter` rows with `created_at < cutoff` — **no
   status / finished_at filter**, because dead-letter rows are *terminal* (written once, never mutated;
   there is no in-flight state to protect). `retention <= 0` disables; returns the count; logs at info.
3. Call `self.prune_dead_letters()` once in `run_forever`'s startup maintenance cycle, immediately after
   `prune_task_runs()` (`driver.py:309-311`). Strictly additive — no change to the tick loop, ingest/
   resolve cadence, serialization, `recover_stale`, `_finalize`, or `prune_task_runs`.
4. Document `DEAD_LETTER_RETENTION_DAYS` in `.env.example`.

### Default = 30 days (justified)

The default is **30**, which **matches the house value** — `task_run_retention_days` defaults to `30`
(`settings.py:82`). Dead-letters are a *replayable* audit trail: the window must be long enough to
investigate a failure spree after the fact, but bounded so disk does not fill. 30 days covers a typical
incident-review cycle and keeps one mental model and one default across both bounded ops tables
(run history and dead-letter trail). Operators who want a different window set
`DEAD_LETTER_RETENTION_DAYS`; `0` disables pruning entirely.

### No migration

The prune filters on `created_at`, which already exists on `ingest_dead_letter` (`models.py:106`).
No column/index/constraint is added; `db/models.py` is untouched; `tests/integration/test_migrations.py`
(alembic head == `create_all`, ADR 0030) is not triggered and stays green. We deliberately do **not**
add a `created_at` index — `prune_task_runs` already queries `finished_at` without a dedicated index, and
this gate keeps the table small by design. If volume ever makes the scan hot, a `created_at` index is a
clean single-migration follow-up.

## Alternatives considered

- **A — Add a `created_at` index + alembic migration.** Rejected for this gate: triggers the migration
  drift guard, grows the gate, and buys nothing on a table this retention is specifically keeping small.
  Mirrors the accepted `prune_task_runs` (no index). Deferred as an optional follow-up if volume demands.
- **B — Filter on a status/terminal flag like `prune_task_runs` does.** Rejected: dead-letter rows have
  no lifecycle — every row is terminal. A status filter would be dead code and could mask rows. Prune all
  rows older than the cutoff.
- **C — A different / shorter default (e.g. 7 or 14 days).** Rejected: diverges from the house value for
  no reason; 30 days aligns with `task_run_retention_days` and an incident-review cycle. Operators tune
  via env if they disagree.
- **D — Periodic in-loop pruning instead of startup-only.** Rejected for this gate: `prune_task_runs`
  runs startup-only (H-8 owns the cadence question). B-4d mirrors that placement to stay minimal; if a
  future gate moves both to a periodic cycle, they move together.
- **E — A separate table-agnostic retention/GC framework.** Rejected: over-engineering for one table.
  Clone the proven method; generalize only if a third table needs it.
- **F — Do nothing (let ops truncate manually / rely on H-8).** Rejected: H-8 is about `task_run`'s
  startup-only cadence, not dead-letters; the audit explicitly calls out unbounded `ingest_dead_letter`.

## Consequences

- The dead-letter trail is bounded: rows older than `DEAD_LETTER_RETENTION_DAYS` are pruned each driver
  maintenance cycle, removing the "disk eventually fills" risk for this table.
- No data loss of consequence: dead-letters are replayable; an expired dead-letter can be regenerated by
  re-ingesting the source. No canonical entity, merge decision, or human determination is lost.
- Startup-only cadence is inherited from `prune_task_runs` — a very long-running driver prunes on each
  restart; a future gate can promote both to a periodic cycle (out of scope here).
- M-6 is partially closed: **dead-letter retention is done**; **landing-zone orphan GC** and **disk-usage
  alerting** (and deterministic `record.key`) remain separate follow-ups.

## Person-affecting / sign-off assessment

**Not person-affecting; no human sign-off.** `ingest_dead_letter` is a *replayable error-audit* table —
not an individual-affecting score, not an ER threshold, not a human-decision record. Pruning an expired
dead-letter loses no canonical or human-decision state (contrast the H-1 reject-durability concern, which
lives in `resolver_judgement` — a durable record of a human reject decision — and is explicitly **not**
touched by this retention). `dead_letter_retention_days` is an ops/disk knob, so the "changes affecting a
real person always need human sign-off" rule does not apply. `human_fork: false` — no OPEN architectural
question; this is a proven-pattern clone. Status PROPOSED, no human STOP.
