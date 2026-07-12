# GATE 2b — statement/context-claim log backfill — BUILD SPEC

> Reflects the **recommended defaults** in ADR 0113 (`docs/decisions/0113-statement-log-backfill.md`).
> Four decisions are **pending user scoping** (ADR 0113 §"Open decisions") and gate the build: (1)
> person_affecting classification + fleet, (2) SF-1 backfill source, (3) fidelity-spike scope, (4) riders.
> This spec assumes the recommended defaults; the load-bearing pivot is SF-1 (source), which changes §2/§3
> reconstruction details but not the invariants (§7). **Do not build until the user cosigns** (ADR 0113 is
> person_affecting per the recommended default).

## 0. Cosign disclosures (the main loop discloses these BEFORE the build — ADR 0113 §Open decisions)

- **D-i — SF-1 source deviates from the consult.** The consult named landing re-map; the code favours
  `er_queue.raw_entity` (byte-faithful, no `map()`-drift). Confirm the substrate before building.
- **D-ii — person-affecting movement of historical data into the permanent SoR.** Backfill materialises
  possibly-person-referencing pre-2a contributions into the never-forgetting log. Forget-safety (SF-3) is
  the mandatory guard; the SF-6-style rebuild-contains-no-erased-source property proves it.
- **D-iii — erasure reaches effectively-*more* (correct direction).** The backfill self-heals the P2
  over-removal residual by restoring surviving values erasure wrongly dropped. Never a leak.
- **D-iv — the fidelity spike is not fully runnable now.** Per-cohort fidelity needs a real-seed corpus
  (operator-blocked); the mechanism ships with synthetic/property coverage and the spike is marked
  blocked-on-real-seed. No fidelity validation is claimed on a promissory note.

## 1. Verified current state (do not re-derive; confirm if editing)

- Pre-2a contributions have **no spine rows** → `project(full_rebuild=True)` **raises
  `IncompleteAliasedSurvivorError`** for every pre-2a aliased survivor (`projector.py:385-395`,
  `spine_integrity.py:95-99`). Gate 2b is the prerequisite that makes the first clean `full_rebuild` possible.
- **No `UNIQUE(statement_id)`**; dedup is projector-side (`projector.py:148-154`). `statement_id` =
  `sha1(f"{dataset}.{entity_id}.{prop}.{value}")` (normal) / `sha256(canonical_id ␀ entity_id ␀ "wm:exists"
  ␀ dataset)` (WPI-1 existence claim).
- `statement.dataset` + `context_claim.dataset` are **already `NOT NULL` + indexed** (`0013`). Rider-1 (ADR
  0112) already makes every written `dataset` a real `source_id`.
- `er_queue.raw_entity` is byte-faithful (resolution parses it via `make_entity`), never hard-deleted
  (`gc.py:185-193`), redacted-to-shell on erase (`erasure.py:110-133`). Landing raw objects are retained by
  reference-GC (ADR 0083) and prefix-deleted on erase (`erasure.py:199-200`).
- Live-graph read-back **cannot** reproduce faithful rows (prop-granular witnesses; per-member `entity_id`
  lost at merge).

## 2. The gate — three slices (independently reviewable; may ship as one PR)

### Slice 2b-a — the backfill reader + row synthesis (SF-1 default: `er_queue` primary)

New module `src/worldmonitor/resolution/backfill.py`:
- `iter_backfill_members(session) -> Iterator[tuple[FtmEntity, str]]` — for each **non-redacted**
  `ErQueueItem` (skip `raw_entity.get("erased")` shells), `make_entity(raw_entity)` → member; resolve
  `canonical_id = build_survivor_of(session)(member.id)` over the ledger. Landing re-map (`connector.map()`
  over `raw_pointer`) is the fallback for a shell/gap (SF-1 fallback; wire behind a flag, off by default).
- `backfill_spine(session, *, dry_run=False) -> BackfillResult` — group members by `canonical_id`,
  reconstruct a minimal `ResolvedCluster`-shaped input + `by_id`, and project through the **existing frozen**
  `fuse_statement_rows` / `_existence_claim_rows` / `fuse_context_claim_rows` / `record_decision` (inherits
  rider-1 skip + WPI-1 existence claim + exact `statement_id`). **Pre-filter** on the existing `statement_id`
  set to skip already-written post-2a rows (SF-2). Returns counts (rows written/skipped-duplicate/
  skipped-unreachable/existence-claims/erased-source-excluded).

### Slice 2b-b — forget-safety (SF-3, person-affecting; mandatory)

- Load the erase-audit exclusion set from `TaskRun(kind="erase", status="ok").stats["source_id"]`
  (Python-side, SQLite-safe — mirror `scrub_stock`, `erasure_scrub.py:453-463`) and **exclude those
  `source_id`s** from `backfill_spine` (skip any member whose `Provenance.source_id` is in the set).
- **Post-backfill re-scrub:** after the backfill commits, run `scrub_stock(session)` once so any row a race
  re-introduced for an already-erased source is re-closed (backfilled rows are `dataset == source_id`
  reachable).

### Slice 2b-c — the completion gate + fidelity harness (SF-4)

- `assert_backfill_complete(session)` — `find_incomplete_aliased_survivors(...) == ∅` (the WPI-2 discharge)
  AND a whole-graph divergence spike over the `divergence._excluded` axes == 0 (NOT the IT-PROJ equivalence
  signature). Fails loud if either is non-empty.
- Fidelity harness: consume `resolution/eval.py` where a metric is measurable now; mark the **per-cohort
  fidelity spike** `blocked-on-real-seed` (operator host/keys) — do not claim it validated.
- SF-7: define the log-completeness-boundary property as `P-ERASE-5` (or fix the dangling
  `erasure_scrub.py:323` citation) — a one-line doc/comment rider.

## 3. Property invariants (`@given` — RED-first; mandatory per build discipline)

- **P-BACKFILL-1 (completeness / WPI-2 discharge):** over a synthetic pre-2a corpus (graph + ledger + no
  spine rows), `full_rebuild` RAISES before backfill and, after `backfill_spine`,
  `find_incomplete_aliased_survivors == ∅` and `full_rebuild` reconstructs every survivor. **Metamorphic
  negative:** omit one survivor's members from the backfill source ⇒ it stays in the incomplete set.
- **P-BACKFILL-2 (byte-faithful dedup / idempotence):** running `backfill_spine` twice writes zero new rows
  the second time (pre-filter on `statement_id`); a post-2a row already in the log is not re-written.
- **P-BACKFILL-3 (rider-1 stamped-ness on the backfill path):** every backfilled `statement.dataset` /
  `context_claim.dataset` equals a contributing member's real `source_id`; a member with empty `source_id`
  is skipped-and-logged.
- **P-BACKFILL-4 (forget-safety, SF-3):** a source in the erase-audit exclusion set contributes **zero**
  backfilled rows; a redacted `raw_entity` shell contributes zero; after backfill + re-scrub, a
  `full_rebuild` into a fresh target contains **nothing** of any erased source (the SF-6-style
  rebuild-contains-no-erased-source oracle). This is the person-affecting invariant.

## 4. Unit + integration tests

- `tests/property/test_prop_backfill.py` — P-BACKFILL-1..4 (heavy container-backed examples wrap per-example
  engines in `try/finally` + dispose; `deadline=None`).
- `tests/integration/test_backfill.py` — real Postgres + Neo4j: seed a pre-2a graph (write direct + ledger,
  NO spine rows), run `backfill_spine`, assert `full_rebuild` no longer raises and is divergence-clean over
  the excluded axes; assert the erase-audit exclusion + post-backfill re-scrub path (erase a source, then
  backfill, then rebuild-contains-no-erased-source).
- `tests/unit/test_backfill.py` — the reader (`iter_backfill_members` skips shells; `build_survivor_of`
  resolves singleton self-rows + merge aliases), the dedup pre-filter, the exclusion-set loader.

## 5. Builder task list (ordered)

1. `backfill.py` reader + `backfill_spine` (SF-1 default `er_queue`; landing fallback behind an off flag).
2. Wire the erase-audit exclusion + post-backfill `scrub_stock` re-scrub (SF-3).
3. `assert_backfill_complete` + the divergence-spike completion gate (SF-4).
4. SF-7 `P-ERASE-5` doc/citation fix.
5. Green the property + integration + unit suites (RED-first from the test-author).

## 6. Acceptance criteria (all measurable)

- After `backfill_spine` on a pre-2a corpus: `find_incomplete_aliased_survivors == ∅` AND `full_rebuild`
  succeeds AND the whole-graph divergence (excluded-axes measure) == 0.
- `backfill_spine` is idempotent (2nd run: 0 new rows).
- No backfilled row carries an empty/`member.id`-keyed `dataset`.
- An erased source contributes 0 backfilled rows; rebuild-contains-no-erased-source holds after backfill +
  re-scrub.
- The per-cohort fidelity spike is documented **blocked-on-real-seed** (not claimed validated).
- Full `pytest -m "not integration"` + local `-m integration` green; `ruff format --check .` repo-wide +
  `ruff check` + `pyright` clean; all 7 CI checks green.

## 7. Invariants the checker MUST reproduce (INV-BACKFILL-*)

- **INV-BACKFILL-COMPLETE** — after backfill, no incomplete aliased survivor; `full_rebuild` reconstructs
  the whole graph (the WPI-2 obligation is discharged, self-verifying).
- **INV-BACKFILL-FAITHFUL** — backfilled rows reproduce the exact dual-write `statement_id` (projected
  through the frozen writers), so they dedup against post-2a rows at the projector.
- **INV-BACKFILL-IDEMPOTENT** — re-running writes no new rows (SF-2 pre-filter).
- **INV-BACKFILL-STAMPED** — every backfilled `dataset` == a real `source_id` (rider-1 inherited).
- **INV-BACKFILL-FORGET-SAFE** (person-affecting) — no erased source is resurrected: erase-audit exclusion +
  redacted-shell skip + post-backfill re-scrub + rebuild-contains-no-erased-source.

## 8. FROZEN (byte-unchanged — the checker verifies `git diff` touches none)

`resolution/statements.py` (the writers — the backfill *calls* them, does not edit them),
`resolution/merge.py`, `resolution/projector.py` (`reconstruct_entities` + `find_incomplete_aliased_survivors`
consumed read-only), `resolution/spine_integrity.py`, `resolution/spine_lock.py`, `resolution/divergence.py`,
`resolution/erasure_scrub.py` logic (only the SF-7 `P-ERASE-5` doc line, if taken), `erasure.py`,
`graph/writer.py`, `graph/ops.py`, `db/models.py` (**NO schema change** under the recommended defaults),
`db/migrations/**` (**NO new migration**), the provenance model, `ontology/**`, `authz/**`, `api/**`,
`llm/**`, `settings.py`.

## 9. OUT OF SCOPE (do NOT build here)

Gate 3b cutover; the per-cohort real-seed fidelity spike; enricher (E2) capture; the `origin_datasets`
column (SF-6 declined) + the belt-and-suspenders `CHECK` (SF-5a declined) — taking either re-introduces a
migration; the projector delete path (anchor-retraction bound + superseded-node deletion, Gate 3b);
`llm_egress` erasure.

## Surprises (code facts the ADR skeleton did not anticipate — disclose at cosign)

1. **`er_queue.raw_entity` beats landing** for byte-faithfulness (resolution's own member source) — the
   consult never evaluated it. SF-1 default deviates from the consult accordingly.
2. **Both value-bearing substrates are already forget-safe** — `erase_source` purges landing (prefix delete)
   and `er_queue` (redact-to-shell); the stores it does not touch (`merge_audit`, ledger) carry no claim
   values. So forget-safety is achievable by substrate choice + shell-skip + erase-audit exclusion.
3. **The stamped-ness + index riders are largely already done** — indexes exist (0013); rider-1 closed the
   write path. Gate 2b's stamped-ness work is a backfill-side obligation, not new schema.
4. **`P-ERASE-5` is a dangling citation** (`erasure_scrub.py:323`) — Gate 2b is its natural owner.
