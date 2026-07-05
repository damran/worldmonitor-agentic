# Gate 3a-ii-A — Incremental fold correctness (F3 fix + P-FOLD-2 + F4 edge equivalence)

- Gate: **3a-ii-A** (statement-log projector line; the fold-correctness half of Gate 3a-ii)
- Branch: `feat/gate-3a-ii-a-incremental-fold` (off `origin/master` @ the 3a-i merge, `e5d544e`)
- ADR: `docs/decisions/0101-incremental-fold-correctness-and-rebuild-diff-guard.md` (PROPOSED → flips
  ACCEPTED at gate approval; Decisions A1/A2/A3)
- Scope contract: `.claude/gate.scope` (INV-1..8 below)
- Person-affecting: **NO** — dormant/isolated projector; re-projects existing merge decisions; makes
  none; writes only the ephemeral isolated test target, never the live graph. Header carries the ADR-0097
  human co-sign because the diff edits a `resolution/**` module.
- Migration: **NONE** — `seq` + `projection_checkpoint` already exist (migration 0010, Gate 3a-i).
- `@given` property suite: **MANDATORY** (touches the fold invariant = ER/merge projection faithfulness).

---

## 1. GAP (from ADR 0100 §"Adversarial-verification findings")

1. **F3 (HIGH).** `project(full_rebuild=False)` folds ONLY the delta (`projector.py:244-297`) and writes
   `SET n += props` (`graph/writer.py:46-47`). `SET n += props` overwrites any key **present** in the
   thin re-emit, so a survivor whose multi-valued prop accumulated `{A, B}`, when a later batch
   re-observes only `{A}`, is **clobbered to `{A}`**. (Keys *absent* from the delta are preserved; the
   clobber bites a *present-but-thinner* re-observation.) ADR 0100 mandated the fix: *"3a-ii must re-read
   each touched survivor's full statement history before writing."*
2. **P-FOLD-2 missing.** No property proves incremental-fold == full-rebuild-fold
   (`test_prop_fold_engine.py` has P-FOLD-1/3/4 only; the incremental path is exercised solely by the
   0-delta no-op sub-case `:324-342`).
3. **F4 (MED).** IT-PROJ-2 (`test_projector.py:349-452`) proves edge byte-equivalence **vacuously** — its
   `_candidates()` corpus has **0 edges** (`_EXCL={"datasets"}` at `:408`, assert `:427`). IT-PROJ-3
   (`:460-598`) asserts an edge *exists* + carries `prov_*`, not byte-equivalence.

## 2. BUILD

### 2.1 F3 fix — `src/worldmonitor/resolution/projector.py`, `project()` incremental branch ONLY

Between the delta read and the fold, re-read each touched survivor's **full** statement history and fold
*that*. `full_rebuild=True` is **unchanged**. Surgical algorithm (see ADR 0101 §Decision A1):

- Delta read `WHERE seq > watermark ORDER BY seq` — **unchanged** (`:244-248`). Keep the name for the
  delta set; it drives `statements_read`, `statements_deduped`, and the watermark.
- `survivor_of` from the full ledger — **unchanged** (`:271-294`).
- `touched = { survivor_of(r.canonical_id) for r in delta_rows }`.
- If `touched` is empty → `fold_rows = []` (no-op; preserves the P-FOLD-3 0-delta case).
- `preimage = touched ∪ { alias for alias in alias_map if survivor_of(alias) in touched }`
  (`alias_map` at `:276-280`, `survivor_of` at `:282-294`).
- `fold_rows = select(StatementRecord).where(StatementRecord.canonical_id.in_(preimage)).order_by(seq)`.
  `canonical_id` is indexed (`models.py:326`) → index-backed `IN`.
- `entities = reconstruct_entities(fold_rows, survivor_of)` — the pure fold is **unchanged**.
- `write_entities(target, entities)`; advance watermark to `max(delta_rows.seq)`; commit — **Neo4j-first
  ordering unchanged** (`:299-322`).

**MUST-hold semantics (see INV-3/INV-4):**
- `statements_read` and `statements_deduped` stay defined over the **DELTA** (not `fold_rows`).
  `full_rebuild=True` keeps them over all rows. A NEW additive `ProjectionResult` field
  (`statements_refolded: int`) MAY report the re-read count — but MUST NOT change `statements_read`.
- Watermark = `max(delta_rows.seq)`, **never** `max(fold_rows.seq)` (the re-read pulls old low-seq rows;
  re-reading does not un-consume the log).
- No change to `write_entities`, `reconstruct_entities`, the ledger read, the checkpoint ordering, or the
  `full_rebuild` path. No ftmg touched.

### 2.2 F4 edge equivalence — `tests/integration/test_projector.py` (+ `graph_signature` in both copies)

- Extend the `graph_signature` helper with an **additive** `exclude_edge_props: frozenset[str] =
  frozenset()` param (currently only `exclude_node_props` is honoured — edge props are compared in full,
  `test_projector.py:122-134`). Default empty ⇒ existing callers unaffected. Apply the same additive edit
  to the **duplicate** copy in `tests/property/test_prop_fold_engine.py:79-134` (the two are "kept in sync
  manually", per the module docstring `:71-73`).
- Add a NEW single-batch, single-source, **edge-bearing** fixture (an `Ownership` edge between two
  Companies) — do **NOT** mutate the shared `_candidates()` (`test_statement_spine.py` depends on it
  verbatim). Single batch ⇒ E1 null ⇒ owner/asset resolve identically in the direct and fold paths.
- Add `test_single_batch_edge_fold_vs_direct_equivalence` (IT-PROJ-4): resolve_pending writes the direct
  graph; capture `direct = graph_signature(exclude_node_props={"datasets"}, exclude_edge_props={"datasets"})`;
  wipe; `project(full_rebuild=True)`; capture `fold` the same way; assert **`fold == direct`**, with a
  **non-empty edge set** on both sides (`len(direct[1]) >= 1`) so edge equivalence is non-vacuous.
  E4-on-edges: excluding `datasets` from edge props mirrors the node exclusion; if the edge carries no
  `datasets` the exclusion is a harmless no-op (the test observes the direct edge's actual props).

### 2.3 Property tests — `tests/property/test_prop_fold_engine.py` (+ optional `strategies.py` helper)

Reuse the module's `_SETTINGS` (`deadline=None`, container round-trips) and `graph_signature`.

- **P-FOLD-2** and **P-FOLD-5** (§4). Prefer **direct `StatementRecord` appends** for deterministic
  control of batch boundaries and the thin-re-emit trigger (P-FOLD-4 already appends rows directly,
  `:423-465`), rather than `resolve_pending` (which always writes full observations and can trigger
  cross-batch merges). A small **additive** edge/multi-value helper MAY be added to
  `tests/property/strategies.py` (do **NOT** alter existing strategies/comparators — other suites depend
  on them).

## 3. LOAD-BEARING INVARIANTS — see `.claude/gate.scope` INV-1..8

G1 (provenance on every projected node AND edge) · append-only (projector only READS the log) ·
canonical-canonical only via the guard (projector makes NO merge decision) · determinism + incremental
loss-freeness · isolation/reversibility · person_affecting:false honesty.

## 4. FAILING-TEST-FIRST (RED → GREEN) — the PRIMARY invariants, stated precisely

Write these FIRST; they are RED until the F3 fix + `graph_signature` edit land.

### P-FOLD-2 — incremental fold == full-rebuild fold (the headline `@given`)

- **Name:** `test_p_fold_2_incremental_equals_full_rebuild`.
- **Universally-quantified statement:** for any log `L` and any ordered partition of `L` into batches
  `B1..Bk` (by `seq`) that is **supersession-monotonic** (no batch supersedes a canonical id that was a
  survivor node in an earlier batch), the graph produced by appending each `Bi` and calling
  `project(full_rebuild=False)` after each equals — **byte-identically, no exclusions** — the graph
  produced by one `project(full_rebuild=True)` over all of `L`.
- **Generator shape:** draw a small set of stable survivor ids and, per batch, a set of
  `(survivor, prop, value)` observations (some batches re-observe an existing survivor's existing prop
  with a **thinner** value subset — the F3 trigger); emit them as `StatementRecord` rows with
  monotonically increasing `seq` (append order = batch order). Enforce the bound by **never** writing a
  mid-sequence supersession alias among already-projected survivors (draw the ledger up-front or use no
  aliases). 2..4 batches, small value sets, `unique_by` survivor where needed.
- **Oracle:** fold incrementally into `clean_graph` (append batch → `project(False)`), capture
  `sig_incr = graph_signature(clean_graph)`; wipe; `project(full_rebuild=True)` over the whole log,
  capture `sig_full`; assert `sig_incr == sig_full` (full signature, NO exclusion — both sides are the
  fold, so `datasets`/anchors reconstruct identically). One isolated target (the P-FOLD-1 pattern).

### P-FOLD-5 — thin re-observation does not clobber accumulated multi-valued props (the F3 regression witness)

- **Name:** `test_p_fold_5_thin_incremental_no_clobber`.
- **Universally-quantified statement:** for any survivor `S` whose property `p` accumulated a value set
  `V` with `|V| >= 2` across the log, appending a batch that re-observes `p` with a **strict subset**
  `V' ⊂ V` and running `project(full_rebuild=False)` leaves `S`'s node carrying the **full** `V` — equal
  to what `project(full_rebuild=True)` produces for `S`.
- **Generator shape:** draw `S`, a prop `p`, and `V` (`>=2` values, `unique`); seed `V` as distinct
  `StatementRecord` rows (distinct `statement_id`s), `project(full_rebuild=False)` (node `p` = `V`); then
  append ONE row re-observing `p = v` for some `v ∈ V` (higher `seq`, a thinner re-emit touching `S`);
  `project(full_rebuild=False)` again.
- **Oracle:** after the thin incremental fold, `node(S).props[p]` (sorted) `== sorted(V)`, and equals the
  `full_rebuild` node for `S`. (Pre-fix RED: the thin delta-fold clobbers `p` down to `{v}`.)

### IT-PROJ-4 — single-batch edge byte-equivalence (F4)

- **Name:** `test_single_batch_edge_fold_vs_direct_equivalence`. Statement + oracle per §2.2. Assert
  `fold == direct` (nodes AND edges, excluding `datasets` on both) AND `len(direct[1]) >= 1` (edges
  present → non-vacuous).

### Regression witnesses that MUST stay GREEN

- `test_prop_fold_engine.py::test_p_fold_3_idempotent_redelivery` — the 0-delta incremental sub-case
  (`:324-342`, `result.statements_read == 0`). The fix's empty-`touched` no-op path MUST keep this green.
- `test_p_fold_1_determinism`, `test_p_fold_4_dedup_supersession_convergence` (full_rebuild path,
  unchanged), IT-PROJ-1/2/3, `test_statement_spine.py`, `test_resolution_pipeline.py`,
  `test_migrations.py`, `test_no_autogenerate_drift`.

## 5. ACCEPTANCE CRITERIA (all must hold)

- `project(full_rebuild=False)` re-reads each touched survivor's FULL statement history (via
  `canonical_id IN preimage`) and folds *that* before writing; the `SET n += props` write restores full
  multi-valued props. `full_rebuild=True` byte-unchanged.
- **P-FOLD-2** green: incremental fold == full-rebuild fold, byte-identical, over the
  supersession-monotonic regime. **P-FOLD-5** green: thin re-observation does not shrink an accumulated
  multi-valued prop. Both are `@given`.
- **IT-PROJ-4** green: single-batch fold-vs-direct edge byte-equivalence with a **non-empty** edge set,
  excluding `datasets` on nodes AND edges (E4-on-edges). `graph_signature` gains `exclude_edge_props`
  (additive) in **both** copies.
- `statements_read` / `statements_deduped` semantics UNCHANGED (delta-based); P-FOLD-3 0-delta
  `statements_read == 0` stays green. Watermark = `max(delta_rows.seq)`.
- Only `resolution/projector.py` changes in production code. `statements.py` / `merge.py` /
  `pipeline.py` / `graph/writer.py` / the guard / `db/models.py` / every migration are **byte-unchanged**
  (person_affecting:false honest). No `driver.py` / `collector.py` / `settings.py` / alert-YAML change
  (those are 3a-ii-B).
- `docs/decisions/0101-*.md` present; builder re-runs `uv run python scripts/gen_adr_index.py` so
  README gains the `0101` row and `uv run python scripts/gen_adr_index.py --check` passes.
- Full `pytest -m "not integration"` green locally (the `quality` job runs it); integration suite green
  locally (Docker available here); `ruff format --check .` repo-wide + `ruff check` + `pyright` clean;
  `quality` + `security` (+ `alert-rules`, unaffected) green before self-merge.

## 6. FROZEN (KEEP-GREEN)

- `reconstruct_entities` (the pure fold, `projector.py:73-206`) — F1/F2 fixes, common-schema, global
  referent rewrite, G1 provenance, witness map: **byte-unchanged**. The fix is upstream of it (which rows
  it is given), not inside it.
- `write_entities` / ftmg / `ftmg_fork` — unchanged (`SET n += props` is correct once the re-emit is
  complete). The checkpoint Neo4j-first ordering and the `full_rebuild` read path — unchanged.
- The person-affecting write path (statements/merge/pipeline/writer/guard) and the ER/threshold/score/
  erasure surfaces — **untouched**.

## 7. PERSON-AFFECTING ASSESSMENT

**NOT person-affecting.** The change re-reads + re-folds already-persisted statements before an
idempotent write to an isolated ephemeral target; it makes no merge/ER decision, touches no
threshold/score/guard/erasure, and never writes the live graph. `human_fork: false`,
`person_affecting: false`; the ADR carries the ADR-0097 human co-sign (resolution/** diff). The `@given`
suite (P-FOLD-2/P-FOLD-5) is the mandated invariant harness for the fold surface.

## 8. OUT OF SCOPE (do NOT build — 3a-ii-B / later)

- **The rebuild-and-diff guard** (`run_maintenance` hook, cached divergence, `worldmonitor_projection_divergence`
  gauge in `collector.py`, `ProjectionDivergenceHigh` alert + promtool fixture, `projection_diff_*`
  settings) — that is **3a-ii-B** (ADR 0101 §Decision B; blocked on the human's B4 steer).
- **Retroactive supersession node-deletion** — the LOW backlog; the projector only `MERGE`s. P-FOLD-2 is
  bounded to the supersession-monotonic regime precisely to exclude it.
- **3b** cutover / **Gate 2b** backfill / any driver wiring, settings flag, compose profile, or new CI job
  for the projector / any change to clustering, thresholds, scoring, the guard, referent rewrite, the
  writer, statements, merge, or pipeline / the `roadmap` doc drift (`docs/40_ROADMAP.md:56` still says
  "Gate 0 CURRENT" — a non-blocking truth-up for a later docs sweep, NOT this gate).

## 9. VERDICT

Reversible, non-person-affecting, dormant-projector correctness fix executing the ADR-0100 F3 FIX
MANDATE, proven by the mandatory `@given` P-FOLD-2/P-FOLD-5 and a non-vacuous edge-equivalence anchor.
One focused PR; checker reproduces INV-1..8; judge gates; `human_fork: false`.
