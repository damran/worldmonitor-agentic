# 0054 — Driver connector retry with exponential backoff

- **Status:** PROPOSED
- **Date:** 2026-06-27
- **Gate:** Phase-B #1 (`gate/driver-retry-backoff`) — a focused fix off `master`.
- **Addresses:** audit H-8 (the retry half) — confirmed at file:line in the cross-workflow Round-2
  cross-examination. The sibling line carries the **identical** bug and never noticed it.

## Context — the bug

`IngestDriver._finalize` sets a failed instance `status="error"` (`runner/driver.py:326`), but the
due-query `run_due_ingests` selects only `status == "enabled"` (`runner/driver.py:189`). So a
connector that hits **one** transient failure (a Neo4j restart, a 503, one bad decrypt) is parked in
`error` and **never runs again** until a human manually re-enables it and restarts the driver —
`next_run` is set on failure but is moot because the status no longer matches the due-query. There is
no backoff, so even the recover-stale path (which resets `running`→`enabled` only at startup) does not
help a steady-state failure.

## Decision

On ingest **failure**, keep the instance **retryable** and schedule it with an **exponential backoff**:

- `instance.status = "enabled"` (so the next tick's due-query re-selects it) — not `"error"`.
- `instance.next_run = now + backoff`, where
  `backoff = min(ingest_retry_base_seconds * 2**(consecutive_failures - 1), ingest_retry_max_seconds)`.
- `consecutive_failures` is derived from the **existing `task_run` history** (the count of the most
  recent consecutive `kind="ingest"` rows with `status="error"` for this instance, including the run
  just finalized) — **no schema change**, no new column.
- On **success**, behaviour is unchanged: `status="enabled"`, `next_run = now + ingest_cadence_seconds`
  (which naturally resets the backoff, since the consecutive-error streak is broken).

The **failure stays visible**: the `task_run` row is still written with `status="error"` and the
bounded error summary (`driver.py:319`), so run history and any future alerting (H-8 alerting half,
deferred) still see every failure. Only the *instance* is no longer a dead end.

New settings (mirror `ingest_cadence_seconds`):
- `ingest_retry_base_seconds: int = Field(default=60, gt=0)` — first-failure backoff.
- `ingest_retry_max_seconds: int = Field(default=3600, gt=0)` — backoff cap.

## Alternatives considered

- **Sweep `error`→`enabled` after a cooldown in a periodic maintenance pass.** Workable but needs the
  periodic-maintenance loop (a separate H-8 slice) and a way to read the cooldown; deferring the whole
  fix on that is worse than fixing the failure path directly. Rejected for this gate; the periodic
  recover/maintenance cadence remains a named follow-up.
- **Add a `failure_count` column to `connector_instance`.** A second source of truth requiring a
  migration + `db/models.py` edit + the drift guard, when `task_run` already losslessly encodes the
  streak. Rejected (heavier blast radius for no gain).
- **Keep `status="error"` but make the due-query also pick `error` past `next_run`.** Conflates a
  permanent operator-disable with a transient retry; "error" should stay reservable for a future
  hard-disable. Rejected — `enabled` + backoff is the cleaner state model.

## Consequences

- A transient connector failure self-heals after a bounded backoff instead of darkening permanently.
- A *permanently* broken connector now retries forever on the capped cadence (bounded, low-frequency)
  rather than going silent — acceptable, and the right default until the H-8 alerting half lets an
  operator be paged to hard-disable it. (Auto-hard-disable after N failures is a named follow-up.)
- **No migration** (`db/models.py` untouched; `test_migrations.py` not triggered).
- **Not person-affecting** — pure connector scheduling; touches no ER/merge/score/guard/graph path.
  No per-run human sign-off. `human_fork: false`.

## Intended behaviour change (flagged for the judge)

`tests/integration/test_ingest_driver.py::test_driver_records_error_and_does_not_leave_instance_running`
currently asserts a failed instance ends `status == "error"`. That contract is exactly the bug. The
test is updated to assert the NEW contract — `status == "enabled"`, `next_run` advanced to the backoff
value, and the `task_run` row still `status == "error"` (failure stays visible). This is an
intended-behaviour change, not a silent weakening; every other assertion in that suite is unchanged.

## Reversibility

Reversible (scheduling policy). Reversal cost: low — revert `_finalize` + drop two settings. Revisit
trigger: if a future operator-driven hard-disable / alerting slice (H-8 alerting half) lands, fold the
"retry forever" default into "retry until N failures, then hard-disable + page".
