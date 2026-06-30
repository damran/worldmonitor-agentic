# Gate B — Landing-zone GC safety (review remediation)

- **Source:** adversarial review 2026-06-29 of PRs #138–145 (`docs/reviews/` session notes;
  MEMORY `session-review-2026-06-29-backlog`). Three **medium**-severity findings against the
  landing-zone orphan GC (ADR 0083 / PR #143). **0 critical / 0 high** — this is hardening, not a
  live-incident fix. Default-off ⇒ no behaviour change for any current deployment.
- **ADR of record:** `docs/decisions/0086-landing-gc-safety.md` (PROPOSED).
- **Touches the provenance invariant** (G1: provenance on every node AND edge). Landing bytes ARE
  provenance (raw pointer). Per CLAUDE.md build discipline a gate touching the provenance invariant
  **MUST** add a `@given` property/metamorphic test in `tests/property/`. Non-negotiable.

## Scope (exact files)

| File | Change |
|------|--------|
| `src/worldmonitor/settings.py` | New `@model_validator(mode="after")` grace-window guard (Change 1). No new field. |
| `src/worldmonitor/runner/gc.py` | Rename `GcStats.bytes_freed → orphan_bytes` (Change 3); document the er_queue-never-hard-deleted invariant + add the explicit reference-set guard/assertion (Change 2b); extract the pure orphan classifier so the property test pins a pure decision (Change 2c support). |
| `src/worldmonitor/metrics/collector.py` | Field access `gc.bytes_freed → gc.orphan_bytes` (line ~165). Gauge name `worldmonitor_landing_orphan_bytes` is **unchanged** (already correct). |
| `src/worldmonitor/runner/driver.py` | In scope for the rename ripple (imports `GcStats`); no field access today, change only if the rename surfaces one. |
| `tests/property/test_prop_landing_gc_reference_safety.py` | **NEW** — the mandatory `@given` reference-safety suite (Change 2c). |
| `tests/unit/test_landing_gc.py` | Grace-guard unit tests (Change 1) + rename updates (Change 3) + reference-set-all-statuses test (Change 2b). |
| `docs/decisions/0086-landing-gc-safety.md` | The ADR. |
| `docs/reviews/GATE_B_LANDING_GC_SAFETY_SPEC.md` | This spec. |
| `docs/GATE_LEDGER.md` | One row for ADR 0086. |

**Out of scope:** any change to ER-queue lifecycle / hard-delete behaviour; Neo4j provenance-pointer
unioning into the reference set (explicitly deferred — see ADR 0086 §Alternatives); auto-tuning the
grace window; the maintenance-cadence wiring; the Prometheus gauge **names**. No schema change.
Not person-affecting.

## Locked invariants this gate must hold

- **G1 provenance on every node AND edge.** Landing bytes are the raw-pointer provenance for every
  entity derived from them. The GC must never delete an object that is still referenced — directly
  enforced by the reference-check-before-age-check ordering (preserved) and the new reference-set
  guard (Change 2).
- **Append-only / no silent provenance loss.** Deletion stays opt-in (`landing_gc_delete_enabled`),
  report-only by default, and the grace guard (Change 1) makes a delete-enabled config with an
  unsafe grace window **un-loadable** (fail-closed) rather than silently destructive.
- **Canonical↔canonical only via the guard** — N/A to this gate (no merge path touched); preserved
  by non-interference (the GC never touches the ER/merge path).

---

## Change 1 — Grace-window guard (fail-closed at config validation)

**Hazard.** `run_ingest` does `landing.put(key, …)` BEFORE the windowed `session.commit()`. The
maximum wall-clock gap between that put and the commit that creates its referencing row is bounded
by `ingest_timeout_seconds` (the run deadline). If `landing_gc_min_age_seconds < ingest_timeout_seconds`
(or `== 0`) while deletion is enabled, a GC pass can sweep a put-before-commit object of a *still
in-flight* ingest — destroying provenance for a record that is about to be committed.

**Decision: fail-closed at `Settings` construction (a `model_validator(mode="after")`), not clamp.**
A silent clamp hides the misconfiguration; an operator who set `min_age=60` with `timeout=1800`
should be told, not quietly given `1800`. The validator only fires when **deletion is enabled**
(report-only mode is purely read and safe at any grace).

**Rule (enforced iff `landing_gc_delete_enabled is True`):**

| Condition | Result |
|-----------|--------|
| `landing_gc_min_age_seconds == 0` | `ValueError` — grace window disabled is unsafe with deletion. |
| `ingest_timeout_seconds == 0` (deadline disabled → unbounded in-flight window) | `ValueError` — no finite grace is provably safe; set a finite `ingest_timeout_seconds` or disable deletion. |
| `0 < landing_gc_min_age_seconds < ingest_timeout_seconds` | `ValueError` naming both values. |
| `landing_gc_min_age_seconds >= ingest_timeout_seconds (> 0)` | OK (boundary `==` allowed). |
| `landing_gc_delete_enabled is False` (any grace) | OK — report-only is read-only. |

Default config (`min_age=86400`, `timeout=1800`, delete off) is unaffected.

**Acceptance criteria (Change 1):**
1. `Settings(landing_gc_delete_enabled=True, landing_gc_min_age_seconds=60, ingest_timeout_seconds=1800)`
   raises `ValueError` whose message names both `landing_gc_min_age_seconds` and `ingest_timeout_seconds`.
2. `Settings(landing_gc_delete_enabled=True, landing_gc_min_age_seconds=0)` raises `ValueError`.
3. `Settings(landing_gc_delete_enabled=True, ingest_timeout_seconds=0, landing_gc_min_age_seconds=86400)`
   raises `ValueError`.
4. `Settings(landing_gc_delete_enabled=False, landing_gc_min_age_seconds=0)` constructs OK (report-only).
5. `Settings(landing_gc_delete_enabled=True, landing_gc_min_age_seconds=1800, ingest_timeout_seconds=1800)`
   constructs OK (boundary).
6. Default `Settings()` constructs OK (existing `U-8a/U-8b` defaults test still passes).

**Named tests (`tests/unit/test_landing_gc.py`):**
`test_grace_below_timeout_rejected_when_delete_enabled`,
`test_grace_zero_rejected_when_delete_enabled`,
`test_ingest_timeout_zero_with_delete_rejected`,
`test_grace_unconstrained_when_delete_disabled`,
`test_grace_equals_timeout_accepted`.

---

## Change 2 — Reference-set invariant (make the load-bearing dependency explicit)

**Hazard.** `gc_landing_orphans` decides "referenced" from `ErQueueItem.source_record ∪
IngestDeadLetter.source_record` only. It **omits Neo4j provenance pointers**. This is safe **only
because `er_queue` rows are never hard-deleted** — a resolved/processed candidate's row (and its
`source_record`) persists, so the landing object it points at stays referenced forever. That
invariant is currently **undocumented and load-bearing**: if anyone adds a hard-delete (or a
status-filtered reference query), the GC would start orphaning live provenance.

**Required:**
- **(2a) Document.** A code comment in `gc.py` at the reference-set build naming the invariant
  ("ER-QUEUE-NEVER-HARD-DELETED: the ErQueueItem reference query is UNFILTERED — all rows, all
  statuses. Safe to omit Neo4j provenance pointers ONLY while er_queue rows are never hard-deleted.
  If a hard-delete is ever added, the GC MUST also union Neo4j `prov_source_id` pointers"), plus the
  same in ADR 0086.
- **(2b) Explicit guard.** The `ErQueueItem` reference query MUST remain status-unfiltered. Add a
  test that fails if a `WHERE`/status filter is introduced: `test_reference_set_covers_all_er_statuses`
  builds `ErQueueItem` rows across every status value and asserts every `source_record` is treated as
  referenced (none becomes an orphan candidate), regardless of status or object age.
- **(2c) Property test (mandatory `@given`).** See the property contract below.

**Supporting refactor (to make the property a pure decision):** extract the classification core into
a pure helper, e.g.
`select_orphan_candidates(objects, referenced_uris, *, now, min_age_seconds) -> list[dict]`,
that `gc_landing_orphans` calls. No behaviour change — the helper is the existing loop (lines
~140–164) lifted out so the property can exercise it without S3/DB I/O. The `bucket`-relative URI is
passed in as already-built strings.

### Primary property-test contract

**File:** `tests/property/test_prop_landing_gc_reference_safety.py`

**P-REF — a referenced object is NEVER an orphan candidate (the G1 safety core).**
- *Inputs* (`@given`): a list of landing objects each `{Key, Size, LastModified}` with arbitrary age
  (including very old, well past any grace); a referenced-URI set drawn as a subset of those objects'
  URIs (plus arbitrary extra URIs).
- *Oracle:* `for obj in objects: if uri(obj) in referenced_uris: obj not in select_orphan_candidates(...)` —
  for ANY `min_age_seconds >= 0` and ANY age. Reference beats age, unconditionally.

**P-MM-MONOTONE — adding a reference can only remove candidates (metamorphic).**
- *Inputs:* objects + two referenced sets `R ⊆ R'` (R' = R plus extra URIs).
- *Oracle:* `candidates(R') ⊆ candidates(R)` — enlarging the reference set never creates a new
  candidate. (Proves no path makes a referenced object *more* deletable.)

**P-ER-STATUS — an object referenced only by a resolved/processed ER row is never a candidate.**
- *Inputs:* `ErQueueItem` rows with `status` drawn from the full status domain (pending, resolved,
  processed, error, …) whose `source_record` matches some old landing object; a fake/in-memory
  session returning those rows; a fake landing store returning those objects.
- *Oracle:* `gc_landing_orphans(session, landing, min_age_seconds=0, delete=False)` reports those
  objects as **referenced**, `orphaned == 0` for them, regardless of row status or object age. This
  is the exact review-finding contract ("an object referenced only by a resolved/processed ER row is
  never selected as an orphan candidate").

Use `settings(deadline=None)` (per CLAUDE.md flake note — config + classification per example is
heavier than a pure micro-function). Reuse `tests/property/strategies.py` where a generator already
exists; otherwise add a small local strategy for the object dicts.

**Acceptance criteria (Change 2):** all three properties pass at `max_examples >= 150`;
`test_reference_set_covers_all_er_statuses` passes; the `gc.py` invariant comment + ADR §Reference-set
invariant exist; the `ErQueueItem` reference `select` has no status `WHERE` clause (asserted by the
all-statuses test failing if one is added).

---

## Change 3 — Rename `GcStats.bytes_freed → orphan_bytes`

**Rationale.** The value is "bytes of orphan *candidates identified*", computed even when
`delete=False` / dry-run — it is **not** necessarily bytes *freed*. The Prometheus gauge already uses
the correct noun (`worldmonitor_landing_orphan_bytes`); this aligns the struct field with the metric.

**Ripple:** `gc.py` (field, local var `bytes_freed`, the log line, the constructor kwarg, the
docstring), `collector.py:165` (`gc.bytes_freed → gc.orphan_bytes`), `driver.py` (import only — no
field access expected; change only if one surfaces), and all `tests/` references (unit U-1/U-5/I-1/I-2
currently assert `stats.bytes_freed`).

**Acceptance criteria (Change 3):** no remaining `bytes_freed` token in `src/` or `tests/`
(`grep -rn bytes_freed src tests` is empty); `GcStats(... orphan_bytes=...)` constructs; the
`worldmonitor_landing_orphan_bytes` gauge value is sourced from `gc.orphan_bytes`; gauge **name**
unchanged so the ADR 0078 alert-rules parity test still passes.

**Named tests:** update `test_gcstats_dataclass` (U-1) to assert `orphan_bytes`; update U-5 / I-1 / I-2
assertions.

---

## Slice breakdown (independent, individually mergeable)

Each slice is a standalone PR with green CI; they share no merge-order dependency (the rename and the
guard touch disjoint code regions; the property test imports the pure helper added in its own slice).

- **Slice B1 — Grace-window guard (Change 1).** `settings.py` validator + 5 unit tests. Pure config
  validation; no GC code touched. Smallest, ships first.
- **Slice B2 — Reference-set invariant + property suite (Change 2).** `gc.py` invariant comment +
  pure-classifier extraction (no behaviour change) + `test_prop_landing_gc_reference_safety.py`
  (P-REF, P-MM-MONOTONE, P-ER-STATUS) + `test_reference_set_covers_all_er_statuses`. This is the
  invariant-bearing slice and carries the mandatory `@given` tests.
- **Slice B3 — Rename `bytes_freed → orphan_bytes` (Change 3).** Mechanical rename across `gc.py`,
  `collector.py`, `driver.py` (if needed), and tests. Independent of B1/B2; if it lands after B2 the
  rename also covers the new property test's field reads (or B2 already uses the new name if it lands
  second — coordinate at author time, but neither blocks the other functionally).

The ADR (0086) and `GATE_LEDGER.md` row land with the first slice merged (or its own doc-only PR);
each slice references ADR 0086.
