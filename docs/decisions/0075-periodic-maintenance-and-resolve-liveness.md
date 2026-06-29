# 0075 — Periodic maintenance cadence + resolve wall-clock timeout + lock-skip escalation

- **Status:** accepted
- **Date:** 2026-06-29
- **Gate:** H-8b (Stage-4 hardening). The second of the H-8 remaining halves; **extends ADR
  [0054](0054-driver-connector-retry-backoff.md)** and continues after ADR
  [0074](0074-auto-hard-disable-after-n-failures.md). The remaining half — the Prometheus `/metrics`
  alerting transport — is **H-8c / ADR 0076** (not started).
- **Touches:** `runner/driver.py` (`run_forever`, `run_resolution`, `_resolve`, `__init__`),
  `resolution/pipeline.py` (`resolve_pending`, `ResolveStats`), `settings.py`, `.env.example`.
  **Not person-affecting** — driver scheduling / maintenance / loop-liveness only; touches no
  ER/merge/score/guard/graph path, no threshold, no canonical-id ledger (`human_fork: false`).

## Context

The long-running `IngestDriver` (`runner/driver.py`) has two remaining self-outage gaps from audit
**H-8** (`docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md` §H-8), both explicitly handed to this
gate by ADR 0054's verbatim "H-8 remaining halves" tail and ADR 0053's Alternative D:

1. **Maintenance is startup-only.** `recover_stale` + `prune_task_runs` + `prune_dead_letters` run
   **once** in the `run_forever` preamble (`driver.py:423-425`), before `while True`. A driver that
   stays up for weeks therefore **never prunes** `task_run` / `ingest_dead_letter` mid-uptime → both
   tables grow without bound until disk fills (the literal audit finding; also B-4d judge B4D-M1).

2. **A hung resolve pass is unbounded and silent.** `run_resolution` (`driver.py:280-301`) drains the
   ER queue via `resolve_pending`, which loops over `resolve_batch_size` batches in an **unbounded
   `while True:`** (`pipeline.py:110-136`) with **no wall-clock deadline** — a large backlog (or one
   pathological `name_fp`-prefix block, L-5) runs arbitrarily long. The pass is serialized by a
   non-blocking `threading.Lock` (`driver.py:93,288`); a tick that finds it held just
   `logger.info("…skipping this tick")` and returns — **no counter, no escalation**, so a wedged pass
   silently starves resolution with nothing surfacing it.

This gate closes both. The metrics/alerting *transport* that would carry these signals off-box stays
deferred to **H-8c / ADR 0076** — H-8b only makes the in-process signals **bounded and escalating**.

## Decision

Three additive capabilities, all driver/pipeline scheduling. **No schema change** (`db/models.py`
untouched; migration drift-guard not triggered). Three new settings, each `0`/disable preserving the
exact prior behaviour.

### D1 — Periodic maintenance cadence (the two prunes only)

- New `run_maintenance(*, now)` = `prune_task_runs(now=now)` + `prune_dead_letters(now=now)`.
- Pure gate `_maintenance_due(now, last_maintenance) -> bool` =
  `last_maintenance is None or (now - last_maintenance).total_seconds() >= maintenance_cadence_seconds`.
- `run_forever`: keep `recover_stale()` as the **startup-only** preamble call; replace the two
  startup-only prune calls with a per-tick `if self._maintenance_due(now, last_maintenance):
  run_maintenance(now=now); last_maintenance = now`. The **first tick** fires (`last_maintenance is
  None`), so the boot-time prune is preserved — the cadence is a strict superset of today.
- New setting: `maintenance_cadence_seconds: int = Field(default=3600, gt=0)`.
- The prunes' **delete semantics are unchanged** (B-4d / ADR 0053 frozen): this changes only *when*
  they run, never *what* they delete.

**`recover_stale` deliberately stays startup-only** (it is **not** wrapped by `run_maintenance`).
`recover_stale` blindly resets **every** `status=="running"` `task_run` → `error` and every
`status=="running"` instance → `enabled`. That is correct exactly once, at boot, when nothing is
genuinely in-flight. Run periodically it would race a **live** `running` row: under the single-node
asyncio loop, ingest fully finishes inside one tick before maintenance runs, but a resolve worker
abandoned by the D3 wall-clock backstop is **still alive and holding `_resolve_lock`** — resetting its
`running` `task_run` to `error` mid-flight would clobber a live row and race its own `_finalize`, for
**zero** single-node benefit (a crash on a single node kills the whole loop, so true in-life recovery
only happens on the next process start, which *is* startup). The prunes are safe to run periodically
precisely because they only touch **old, finished/terminal** rows (`prune_task_runs` filters
`status in (ok,error)` + `finished_at < cutoff`; dead-letters are write-once), which a live row never
matches. In-life stale-row recovery is the **deferred HA lease** (ADR 0029 fork X2).

### D2 — Cooperative resolve wall-clock timeout

Mirror the existing **`ingest_timeout_seconds`** pattern (`ingest.py:130,141,234-236`) exactly:

- New setting: `resolve_timeout_seconds: float = Field(default=600.0, ge=0)` — `<= 0` disables.
- `resolve_pending` gains an optional `timeout: float | None = None` override; `deadline_s = timeout
  if timeout is not None else settings.resolve_timeout_seconds`; take `start = time.monotonic()`
  before the batch loop and, **after each batch's `session.commit()`**, `if deadline_s > 0 and
  (time.monotonic() - start) >= deadline_s: stopped_reason = "timeout"; break`.
- `ResolveStats` gains `stopped_reason: str` (default `"exhausted"`, set `"timeout"` on early stop),
  mirroring `IngestStats.stopped_reason`, so the outcome is visible in `task_run.stats` (and the future
  `/metrics`). A `logger.warning` on early stop mirrors ingest.

Because every batch is **committed before the next loads** (ADR 0026), a timed-out pass loses **no**
committed work and the remaining backlog stays `pending` and **resumes on the next cadence tick** —
the timeout bounds a single pass, it does not drop data and changes **nothing** about *what* merges.

### D3 — Lock-skip escalation + loop-liveness backstop

- New setting: `resolve_lock_skip_alert_threshold: int = Field(default=3, gt=0)`.
- `__init__`: `self._consecutive_resolve_skips = 0`. In `run_resolution`: on a **failed** non-blocking
  acquire, `+= 1`; if `>= resolve_lock_skip_alert_threshold` log at **`warning`** ("resolution wedged:
  N consecutive lock-skips — a prior pass still holds the lock") else the existing `info`; `return []`.
  On a **successful** acquire, **reset to 0** before the work, so escalation fires only on genuinely
  *repeated* contention.
- **Loop-liveness backstop.** Wrap the resolve `to_thread` in `run_forever` with `asyncio.wait_for`,
  bounded by a pure helper `_resolve_wait_timeout()` = `None` when `resolve_timeout_seconds <= 0`
  (disabled → today's behaviour: the await blocks, and a wedged pass is caught coarsely by the
  heartbeat-staleness healthcheck), else `resolve_timeout_seconds + driver_tick_seconds`. The grace of
  one tick keeps the backstop **strictly looser** than the D2 cooperative deadline, so in the normal
  multi-batch case D2 breaks cleanly between batches and **no thread is abandoned**; the backstop only
  engages for a **single** batch that hangs *inside* DuckDB/Neo4j (never reaching the between-batch
  check). On `TimeoutError` the loop logs an escalation and continues — ingest + heartbeat stay alive
  (resolve-only degradation, not a whole-container restart). The abandoned worker keeps `_resolve_lock`
  until it finishes or the process restarts; the lock-skip escalation above then surfaces it tick over
  tick.

**Limitation (documented):** a wall-clock-overrun worker is **abandoned, not killed** — a Python
thread in the default executor cannot be force-cancelled. A true resolve watchdog/kill is **deferred**
(consistent with ADR 0027's deferral of hard-killing a blocked `next()`). Its `running` `task_run` row
is reconciled by `recover_stale` on the next process start (which is also when the worker dies),
exactly why D1 keeps `recover_stale` startup-only.

## Alternatives considered

- **Wrap `recover_stale` in the periodic cadence too** (what the superseded prior-session spec did):
  rejected — clobbers a live/abandoned resolve worker's `running` row for no single-node benefit (D1).
- **`asyncio.wait_for` as the *only* resolve bound** (no cooperative deadline): rejected — it abandons
  a thread on *every* overrun, so a merely-large-but-progressing backlog would deadlock resolution
  (lock held) until a human restart. The cooperative between-batch deadline drains large backlogs
  across passes without abandoning anything; the backstop is the rare single-hung-batch escape hatch.
- **Hard-kill the resolve thread on timeout:** rejected — not safely possible for a pooled thread;
  deferred watchdog (ADR 0027 precedent).
- **A separate maintenance process / external cron:** rejected — the driver already owns the loop and
  the cadence pattern (`last_resolve`); a sibling `last_maintenance` is the minimal, 12-factor change.
- **A `failure_count` / `maintenance` schema column:** rejected — no migration needed (D1 reuses the
  existing methods; the skip counter is in-memory driver state; the timeout reason rides `ResolveStats`).

## Consequences

- A long-lived driver now **prunes on a cadence** (default hourly) — `task_run` / `ingest_dead_letter`
  stay bounded mid-uptime, closing the disk-fill gap (and B-4d B4D-M1). Boot-time prune preserved.
- A resolve pass is now **wall-clock-bounded** (default 600 s) and resumes next tick with no data loss;
  a genuinely **wedged** pass frees the loop (ingest stays alive) and **escalates to `warning`** after
  a few lock-skips — no more silent starvation.
- **No migration**, **no test-contract flip** (the new defaults sit far from what the existing driver
  tests drive; every disable sentinel reproduces today's behaviour byte-for-byte).
- **Not person-affecting** — `human_fork: false`; no per-run human sign-off. Connector/queue
  *scheduling* and loop liveness only; resolution still merges exactly what it merged before.
- **Out of scope (recorded):** the `/metrics` alerting transport (H-8c / ADR 0076); a true resolve
  watchdog/kill; `recover_stale` advancing `next_run` to break a crash-loop (recover_stale logic is
  frozen here); the HA/multi-replica lease (ADR 0029 fork X2); landing-zone GC (M-6); the DuckDB-Linker
  per-batch close + finer blocking (L-5).

## Reversibility

Reversible (scheduling / loop-liveness policy). **Reversal cost: low** — revert the `run_forever`
maintenance + `wait_for` wiring and the `run_resolution`/`resolve_pending` additions, and drop the
three settings (or ship each at its disable sentinel: `resolve_timeout_seconds=0`,
`resolve_lock_skip_alert_threshold` unused without skips, `maintenance_cadence_seconds` back to
startup-only). **Revisit triggers:** (1) when **H-8c / ADR 0076** lands the `/metrics` transport, the
in-memory skip counter + `ResolveStats.stopped_reason` become the gauge/counter source and the WARN can
be paged; (2) when the **HA/multi-replica lease** (ADR 0029 fork X2) lands, periodic `recover_stale`
(behind an age/grace guard) and a true resolve watchdog/kill can be reconsidered.
