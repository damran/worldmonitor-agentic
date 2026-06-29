# Gate H-8b — Periodic maintenance cadence + resolve wall-clock timeout + lock-skip escalation

- Gate: **H-8b** (Stage-4 hardening; the second of the H-8 remaining halves)
- Branch: `feat/h8b-maintenance-resolve-liveness` (off `master` @ `e532853`)
- ADR: `docs/decisions/0075-periodic-maintenance-and-resolve-liveness.md` (accepted)
- Audit: `docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md` §H-8 (lines 163-167); B-4d judge B4D-M1
- Scope contract: `.claude/gate.scope`
- Person-affecting: **NO** (driver/queue scheduling + loop liveness — no ER/merge/score/guard). No human sign-off.
- Migration: **NONE** (no `db/models.py` change; skip counter is in-memory; timeout rides `ResolveStats`).

---

## 1. GAP (verified against current code)

1. **Maintenance is startup-only.** `run_forever` calls `recover_stale()` + `prune_task_runs()` +
   `prune_dead_letters()` once in its preamble (`driver.py:423-425`) before `while True`. A long-lived
   driver never prunes mid-uptime → `task_run` / `ingest_dead_letter` grow until disk fills.
2. **Resolve is unbounded.** `resolve_pending` drains in an unbounded `while True:` (`pipeline.py:110-136`)
   with no wall-clock deadline; a large backlog / L-5 block runs arbitrarily long.
3. **Lock-skip is silent.** `run_resolution` (`driver.py:288-290`) `logger.info`s and returns `[]` when
   the non-blocking `_resolve_lock` is held — no counter, no escalation. A wedged pass silently starves.

## 2. BUILD — three additive parts (one PR; see ADR 0075 §D1–D3)

### D1 — periodic maintenance cadence (`driver.py`, `settings.py`)
- New setting `maintenance_cadence_seconds: int = Field(default=3600, gt=0)` (in the ADR-0029 cadence block).
- `run_maintenance(self, *, now: datetime) -> None`: calls `self.prune_task_runs(now=now)` and
  `self.prune_dead_letters(now=now)` — **and only those** (NOT `recover_stale`).
- `_maintenance_due(self, now: datetime, last_maintenance: datetime | None) -> bool` (pure) =
  `last_maintenance is None or (now - last_maintenance).total_seconds() >= self._settings.maintenance_cadence_seconds`.
- `run_forever`: keep `self.recover_stale()` at startup; drop the two startup `prune_*` calls; add
  `last_maintenance: datetime | None = None` and, in the loop, `if self._maintenance_due(now,
  last_maintenance): self.run_maintenance(now=now); last_maintenance = now` (via `asyncio.to_thread`,
  mirroring the resolve gate). First tick fires → boot prune preserved.

### D2 — cooperative resolve wall-clock timeout (`pipeline.py`, `settings.py`, `driver.py`)
- New setting `resolve_timeout_seconds: float = Field(default=600.0, ge=0)` (`<= 0` disables; mirror
  `ingest_timeout_seconds` exactly, including the doc comment style).
- `ResolveStats`: add `stopped_reason: str = "exhausted"` (mirror `IngestStats.stopped_reason`; values
  `"exhausted"` | `"timeout"`). Keep the dataclass `frozen=True, slots=True`.
- `resolve_pending`: add `timeout: float | None = None`; `deadline_s = timeout if timeout is not None
  else settings.resolve_timeout_seconds`; `start = time.monotonic()` before the `while True`; **after**
  the per-batch `session.commit()` (`pipeline.py:130`), `if deadline_s > 0 and (time.monotonic() -
  start) >= deadline_s: stopped_reason = "timeout"; break`. Return `stopped_reason` in `ResolveStats`;
  `logger.warning` on early stop (mirror ingest's early-stop WARNING).
- `driver._resolve` may pass `timeout=self._settings.resolve_timeout_seconds` explicitly or rely on the
  settings default inside `resolve_pending` (either is fine; the optional param exists for tests).

### D3 — lock-skip escalation + loop-liveness backstop (`driver.py`, `settings.py`)
- New setting `resolve_lock_skip_alert_threshold: int = Field(default=3, gt=0)`.
- `__init__`: `self._consecutive_resolve_skips = 0`.
- `run_resolution`: failed acquire → `self._consecutive_resolve_skips += 1`; if `>=
  self._settings.resolve_lock_skip_alert_threshold` → `logger.warning(...)` else `logger.info(...)`;
  `return []`. Successful acquire → `self._consecutive_resolve_skips = 0` (inside the `try`, before work).
- `_resolve_wait_timeout(self) -> float | None` (pure) = `None` if `self._settings.resolve_timeout_seconds
  <= 0` else `self._settings.resolve_timeout_seconds + self._settings.driver_tick_seconds`.
- `run_forever` (pragma glue): wrap the resolve `to_thread` in `asyncio.wait_for(..., timeout=
  self._resolve_wait_timeout())`; on `asyncio.TimeoutError` log an escalation and continue (do not
  re-raise; the loop stays alive). `None` timeout = no backstop (today's awaited behaviour).

`.env.example`: document all three new vars under a `# --- Driver maintenance + resolve liveness (Gate
H-8b / ADR 0075) ---` block (UPPER_SNAKE: `MAINTENANCE_CADENCE_SECONDS`, `RESOLVE_TIMEOUT_SECONDS`,
`RESOLVE_LOCK_SKIP_ALERT_THRESHOLD`), mirroring the existing cadence block.

## 3. LOAD-BEARING INVARIANTS (the gate's positive contract) — see `.claude/gate.scope` INV-1..8

## 4. FAILING-TEST-FIRST (RED pre-build → GREEN post-build)

Driver tests in `tests/integration/test_ingest_driver.py` (reuse `_harness`/`_NOW`/`_add_instance`,
`model_copy(update=...)` to override settings, inject `now=`). Resolve-timeout test in
`tests/integration/test_resolution_batching.py` (closest batch-drain analog). Settings tests in
`tests/unit/test_settings.py`.

**RED today:** `run_maintenance`, `_maintenance_due`, `_resolve_wait_timeout`,
`self._consecutive_resolve_skips`, `ResolveStats.stopped_reason`, and the three settings **do not
exist** → `AttributeError` / `TypeError` / `ValidationError`.

**GREEN tests to add:**
- *`_maintenance_due` (pure):* `None`→True; `elapsed < cadence`→False; `elapsed >= cadence`→True.
- *`run_maintenance` (integration):* seed an OLD finished `task_run` + an OLD `ingest_dead_letter` **and**
  a `running` `task_run`; one `run_maintenance(now=_NOW)` prunes both old rows **and leaves the `running`
  row untouched** (proves `recover_stale` is NOT wrapped — INV-2).
- *maintenance cadence gate (integration or pure):* assert `_maintenance_due` flips on the new setting,
  and that `recover_stale` is not invoked by `run_maintenance` (e.g. spy/asserting the running row stays).
- *resolve timeout stops the drain, loses no work (integration):* seed `> resolve_batch_size` pending
  items across ≥2 batches; run a resolve pass with a deadline that trips after batch 1 (monkeypatch
  `time.monotonic`, or `resolve_timeout_seconds` small with a slowed batch); assert `stopped_reason ==
  "timeout"`, the first batch's results are committed to the graph, and the remaining items are still
  `pending` (resume next tick). With `resolve_timeout_seconds = 0` the pass drains to exhaustion
  (`stopped_reason == "exhausted"`) exactly as today.
- *lock-skip escalation (integration):* hold `_resolve_lock`; call `run_resolution` `threshold` times →
  the counter increments and a WARNING is emitted on the threshold-th call (assert via `caplog`);
  release + one successful `run_resolution` → counter resets to 0.
- *`_resolve_wait_timeout` (pure):* `resolve_timeout_seconds=0`→None; `=600, tick=30`→630.0.
- *settings (unit):* default/override/reject trio for each of the three knobs; `resolve_timeout_seconds`
  `allows_zero_to_disable`; `gt=0` knobs reject `0`/negative with `ValidationError`.

## 5. ACCEPTANCE CRITERIA
- The three settings exist with the §2 defaults/bounds and are documented in `.env.example`.
- `run_maintenance` + `_maintenance_due` exist; the loop prunes on cadence; the boot prune is preserved;
  `recover_stale` is unchanged and stays startup-only.
- `resolve_pending` honours a between-batch deadline; `ResolveStats.stopped_reason` reports it; committed
  batches persist and remaining items stay `pending`; `<=0` disables.
- `run_resolution` escalates after `resolve_lock_skip_alert_threshold` consecutive skips and resets on
  success; `_resolve_wait_timeout` returns the looser backstop / `None`.
- All §4 RED tests pass; **every other** existing `test_ingest_driver.py` / resolution test stays
  byte-identical and green; `tests/integration/test_migrations.py` (ADR 0030 drift guard) untouched;
  `ruff format --check .` + `ruff check` + `pyright` clean repo-wide.

## 6. FROZEN (KEEP-GREEN) — a removed assert / added skip|xfail / loosened tolerance is a judge DENY
- ADR-0054/0074 retry/backoff/hard-disable: `_finalize` failure branch, `_backoff_seconds`,
  `_consecutive_ingest_failures` — untouched. The H-8a tests (`test_ingest_driver.py:377-489`) stay green.
- `recover_stale` RESET semantics + `test_driver_recovers_stale_running_on_startup`.
- `prune_task_runs` / `prune_dead_letters` DELETE semantics +
  `test_prune_task_runs_removes_old_finished_only` / `test_prune_dead_letters_removes_old_only`.
- `test_driver_resolution_pass_resolves_and_does_not_overlap` (serialization / no-overlap STAYS;
  D3 ADDS skip-escalation, it must not change no-overlap).
- The B-4c heartbeat touch in `run_forever`; the due-query; the idempotent-enqueue path.
- All resolution / sign-off / merge-guard / provenance tests — PRESERVED VACUOUSLY (logic untouched).
- `tests/integration/test_migrations.py` — no migration in this gate.

## 7. PERSON-AFFECTING ASSESSMENT
**NOT person-affecting → no per-run human sign-off** (`human_fork: false`). Pure scheduling/maintenance/
loop-liveness. A timeout only bounds *how far* one pass drains (per-batch committed, remainder resumes):
it changes nothing about what its *committed batches* merge and runs the same guard/threshold/sign-off
on every merge; the deferred remainder's *cross-pass* batch grouping stays within the already-accepted
ADR-0026 cross-batch dedup limitation. It never touches the guard, thresholds, the canonical graph, or
the canonical-id ledger. No `@given` invariant property test is mandated (no
ER/merge/canonical-id/provenance/sensitivity invariant is touched — see CLAUDE.md build discipline).

## 8. OUT OF SCOPE (hard stops)
- `/metrics` + alerting transport (H-8c / ADR 0076). No Prometheus endpoint, no Gauge/Counter code.
- A true resolve watchdog/kill (abandon-not-kill only; ADR 0027 precedent).
- `recover_stale` advancing `next_run` / breaking a crash-loop (recover_stale logic is frozen here).
- The HA/multi-replica lease (ADR 0029 fork X2); landing-zone GC (M-6); DuckDB-Linker close + finer
  blocking (L-5); any `db/models.py` / migration change.

## 9. VERDICT
Deterministic, fork-free, reversible scheduling policy. Build as one PR; the checker reproduces INV-1..8
against the diff; the judge gates the merge. `human_fork: false`.
