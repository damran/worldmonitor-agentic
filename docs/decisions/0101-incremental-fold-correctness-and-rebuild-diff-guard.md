# 0101 — Incremental fold correctness (F3 / P-FOLD-2 / F4) + the rebuild-and-diff guard roadmap

- **Status:** ACCEPTED (2026-07-05)
- **Date:** 2026-07-05
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** Mithat — Gate 3a-ii-A user cosign 2026-07-05 (person_affecting:false waiver on the
  `resolution/**` projector diff, per ADR 0097 §4/§5; a dormant/isolated fold-correctness fix that makes
  no merge/ER decision and never writes the live graph). See §Governance.
- **Realises:** ADR 0095 build-sequence **step 3** (the *rebuild-and-diff* half — the fold-determinism
  guard) and the **F3 FIX MANDATE** ADR 0100 recorded. **Supersedes:** nothing. **Builds on:** ADR 0100
  (the dormant fold engine + `seq`/`projection_checkpoint`), ADR 0044 (the canonical-id ledger),
  ADR 0078 (the alert-rule + INV-PARITY collector surface).

## Context

Gate 3a-i (ADR 0100) landed the **dormant, isolated** fold engine: `resolution/projector.py` folds the
Postgres statement + decision log into an **ephemeral, isolated** Neo4j (never the live graph), reusing
`graph.writer.write_entities` unchanged. Its adversarial-verification review (ADR 0100
§"Adversarial-verification findings") shipped two fixes (F1 mixed-schema, F2 survivor determinism) and
**backlogged three correctness gaps to 3a-ii**, plus the deferred cross-batch parity property:

- **F3 (HIGH) — incremental delta-fold is lossy.** `project(full_rebuild=False)` folds **only** the
  delta rows (`WHERE seq > watermark`, `projector.py:244-297`) and writes via `SET n += props`
  (`graph/writer.py:46-47`). `SET n += props` overwrites any key **present** in the thin re-emit, so a
  survivor whose multi-valued property accumulated `{A, B}` across the log, when a later batch
  re-observes only `{A}`, is **clobbered down to `{A}`**. (Keys *absent* from the delta are preserved;
  the clobber bites a *present-but-thinner* re-observation of an existing property.) ADR 0100 recorded
  the fix mandate directly: *"3a-ii must re-read each touched survivor's full statement history before
  writing."* Incremental is exercised today only for the 0-delta no-op (`test_prop_fold_engine.py:324-342`).
- **F4 (MED) — edge byte-equivalence is unproven.** The mandatory single-batch fold-vs-direct anchor
  IT-PROJ-2 (`test_projector.py:349-452`) runs on `_candidates()`, which has **0 edges**, so edge
  equivalence is proven **vacuously** (`edge_sigs` compares two empty tuples). IT-PROJ-3
  (`:460-598`) asserts an edge *exists* + carries `prov_*`, **not** byte-equivalence.
- **P-FOLD-2 (the deferred property).** ADR 0100 deferred the cross-batch parity property to 3a-ii.
- **The rebuild-and-diff guard (ADR 0095).** ADR 0095 names *"a scheduled full-rebuild-and-diff job
  [as] the DR story and the fold-determinism guard,"* and names the one genuine risk: *"Fold/projection
  determinism … where this design can rot into a mutated projection."* That guard is unbuilt.

**A load-bearing clarification of what P-FOLD-2 means in 3a-ii.** ADR 0100 discussed "P-FOLD-2" in the
context of *fold-vs-**direct**-write* parity, where the cross-batch class **E1** (the fold applies the
**global** referent rewrite; the live writer only rewrites within-batch and resolves the rest
alias-on-read) makes the two legitimately diverge. **This ADR pins P-FOLD-2 to the property the F3 fix
actually makes true: fold-incremental == fold-full.** That is a *pure projector self-consistency*
property — **both sides are the projector**, so both apply the same global `survivor_of` (rebuilt from
the whole ledger on every call, `projector.py:271-280`), both reconstruct `datasets`/anchors the same
way, and there is **no E-class divergence and no exclusion**: the two graphs must be **byte-identical**.
E1 only distinguishes fold-vs-**direct**, which is the concern of the **3a-ii-B** rebuild-and-diff guard
(§Decision B) — and is exactly why that guard's divergence measure is the hard part, not this one.

## Decomposition decision — SPLIT into 3a-ii-A (now) and 3a-ii-B (follow-on)

**Decision: ship 3a-ii as two independently-mergeable slices. 3a-ii-A is this gate; 3a-ii-B is the
immediate follow-on, spec'd in §Decision B and roadmapped, NOT built here.**

- **3a-ii-A — fold correctness (deliverables F3 + P-FOLD-2 + F4).** Pure `resolution/projector.py`
  engine change + property/integration tests + one additive test-strategy helper. Touches **no**
  `db/models.py`, **no** migration (the `seq`/`projection_checkpoint` substrate already exists from
  3a-i), **no** `runner/driver.py`, **no** `metrics/collector.py`, **no** `settings.py`, **no** alert
  YAML. This is the ADR-0100 FIX MANDATE and the highest-value, lowest-blast-radius work.
- **3a-ii-B — the operational rebuild-and-diff guard (deliverable 2).** The `run_maintenance` hook, the
  cached divergence stat, the `collector.py` gauge, the alert rule + promtool fixture, and the settings
  surface for an opt-in isolated fold target.

**Why split (not one PR):**
1. **"One focused feature per PR" (CLAUDE.md).** A is a resolution-engine correctness fix; B is
   driver/metrics/alerting observability. Different review lenses, different blast radii.
2. **Coupling direction is one-way.** P-FOLD-2 *proves* the F3 fix; F4 *extends* the same equivalence
   anchor — both are the same engine change. The guard (B) merely **consumes** the proven engine (it
   folds via the exact `project()` A hardens). B needs A; A does not need B. A can merge and stand alone.
3. **Repo grain.** The line is already sub-sliced this way (Gate 2 → 2a/2b; Gate 3 → 3a-i/3a-ii/3b).
   A/B keeps each step small and reversible.
4. **B has a genuine design surface that must not gate A.** B's divergence measure and its
   isolated-target provisioning (Neo4j Community is single-database, ADR 0094 D5 — there is **no** free
   shadow DB, and the projector must **never** write live) are a real design conversation (§Decision B).
   Splitting lets the crisp, buildable correctness fix land now while B is planned deliberately.

`.claude/gate.scope` is written for **3a-ii-A only**.

---

## Decision A1 — F3 fix: incremental fold re-reads each touched survivor's full history

On `project(full_rebuild=False)`, between reading the delta and folding, **re-read the complete
statement history of every survivor the delta touches**, and fold *that* (not the delta) so the
`SET n += props` write restores the full multi-valued properties. The clobber only ever bit a *thinner*
re-emit; a *complete* re-emit is a superset, so `+=` is correct and idempotent.

**Algorithm (surgical; only the incremental branch changes):**

1. Read delta rows `WHERE seq > watermark ORDER BY seq` — **unchanged** (`projector.py:244-248`). These
   identify *what changed* and drive the watermark + counts.
2. Build `survivor_of` from the **full** ledger — **unchanged** (`projector.py:271-294`).
3. `touched = { survivor_of(r.canonical_id) for r in delta_rows }`.
4. If `touched` is empty → **no-op** (`fold_rows = []`; preserves the P-FOLD-3 0-delta case, below).
5. `preimage = touched ∪ { alias for alias in alias_map if survivor_of(alias) in touched }` — every
   `canonical_id` (survivor *or* superseded alias) whose survivor is a touched survivor. (`alias_map`
   already exists at `projector.py:276-280`; `survivor_of` at `:282-294`.)
6. `fold_rows = SELECT * FROM statement WHERE canonical_id IN preimage ORDER BY seq` — the **full**
   history. `canonical_id` is indexed (`models.py:326`) so the `IN` is index-backed.
7. `entities = reconstruct_entities(fold_rows, survivor_of)` — the pure fold is **unchanged**.
8. `write_entities(target, entities)`; then advance the watermark to `max(delta_rows.seq)` and commit —
   **Neo4j-first ordering unchanged** (`projector.py:299-322`).

`full_rebuild=True` is **unchanged** (reads all rows, folds all).

**Invariants the fix MUST preserve (checker reproduces):**
- **`statements_read` / `statements_deduped` stay defined over the DELTA** (the `WHERE seq >` set), not
  the re-read set. `P-FOLD-3` asserts `result.statements_read == 0` on a 0-delta incremental run
  (`test_prop_fold_engine.py:338`) — this MUST keep passing. The full-history re-read count is internal;
  it MAY be surfaced as a NEW additive `ProjectionResult` field (e.g. `statements_refolded`) but MUST
  NOT change `statements_read`'s meaning.
- **Watermark = `max(delta_rows.seq)`**, never `max(fold_rows.seq)` (the re-read pulls OLD low-seq rows;
  re-reading them for a re-fold does not un-consume the log — the watermark is the consumption frontier).
- **The write path stays `write_entities` unchanged** (no ftmg touched; `SET n += props` is correct once
  the re-emit is complete).

**Reversal cost:** low — revert the incremental branch to the delta-only fold (dormant module, no live
caller). **Revisit trigger:** if the re-read of a very high-degree survivor (a canonical with thousands
of aliases/statements) makes an incremental pass too slow, move to a bounded/paged re-fold or a
per-survivor materialised fold-state — a performance revisit, not a correctness one.

## Decision A2 — P-FOLD-2: incremental fold == full-rebuild fold (byte-identical)

Add the mandatory `@given` property **P-FOLD-2**: folding a log **incrementally across N batches** (each
appended, then `project(full_rebuild=False)`) yields a graph **byte-identical** to a single
`project(full_rebuild=True)` over the whole log. No E-class exclusion (fold-vs-fold; §Context).

**The documented OUT-of-scope boundary — retroactive supersession node-deletion.** If a batch
introduces a ledger alias that supersedes a canonical id that was **already projected as a survivor
node** in an earlier batch, the full rebuild produces no node under the now-superseded id, but the
incremental path — which only `MERGE`s, never `DELETE`s — leaves a **stale** node there. Retroactive
deletion of a now-empty survivor is the **LOW backlog** ADR 0100 recorded, and it stays **OUT** of
3a-ii. P-FOLD-2 is therefore stated over the **supersession-monotonic** regime: no batch may supersede
an id that was a survivor in an earlier batch (the survivor set only grows / gains properties). The
generator enforces this trivially (direct-append recipe: never write a mid-sequence supersession among
already-projected survivors). This is a *chosen*, documented bound — not a silently-narrowed property.

## Decision A3 — F4: edge byte-equivalence + edge-level `datasets` (E4-on-edges)

Give the single-batch fold-vs-direct equivalence anchor a **real edge-bearing corpus** so edge
byte-equivalence is proven **non-vacuously**, and handle the `datasets` divergence on **edge**
properties the same way ADR 0100 handles it on node properties (E4).

- Add a NEW single-batch, single-source, edge-bearing fixture (an `Ownership` edge; do **not** mutate
  the shared `_candidates()` — `test_statement_spine.py` depends on it verbatim). Single batch ⇒ E1 is
  null (owner/asset resolve identically in the direct and fold paths), so the comparison is clean.
- Extend the `graph_signature` helper (in **both** copies — `tests/integration/test_projector.py` and
  `tests/property/test_prop_fold_engine.py`, which are "kept in sync manually") with an **additive**
  `exclude_edge_props: frozenset[str] = frozenset()` parameter (default empty → existing callers
  unaffected). The new edge-equivalence test excludes `datasets` from **both** node and edge props
  (E4 on nodes AND edges). If the edge carries no `datasets` property the exclusion is a harmless no-op —
  confirmed by the test observing the direct-write edge's actual properties.

---

## Decision B (ROADMAP — 3a-ii-B, spec only; NOT built in this gate)

The scheduled **full-rebuild-and-diff guard**: on a cadence, fold the **whole** log
(`project(full_rebuild=True)`) into an **isolated** ephemeral Neo4j, diff it against the **live** graph,
compute a divergence measure, cache it on the driver, emit a gauge, and alert. Design, with **sensible
reversible defaults picked per the CLAUDE.md reversibility mandate** (this is default-off observability,
not data-shape lock-in), plus the **one genuine sequencing question** flagged for the human at
3a-ii-B planning:

**B1 — Isolated target provisioning (opt-in, default-off ⇒ dormant).** Neo4j Community is
single-database (ADR 0094 D5): there is **no** free shadow DB on the live instance, and the projector
must **never** write live (the invariant 3a-i established). So the guard folds into an
**operator-provisioned separate Neo4j** addressed by new settings
(`projection_diff_neo4j_uri/user/password`, all default-empty) gated by
`projection_diff_enabled: bool = False`. Unset ⇒ the guard is a **no-op** and the cached divergence
stays `None` (the gauge reports a sentinel / is emitted as "never run", mirroring the GC cached-stats
"0 until first pass" pattern, `collector.py:145-166`). This keeps 3a-ii-B **additive, reversible, and
dormant-by-default** exactly like 3a-i.

**B2 — The divergence measure (one-directional; the crisp default).** Define
**`divergence = |{ live graph elements (nodes + edges) NOT explained by the fold }|`**, measured as:
for each live node `L`, `L` is *explained* iff `survivor_of(L.id)` is a fold node id whose (non-anchor,
non-`datasets`) property values are a **superset** of `L`'s; edges analogously after endpoint
resolution. Rationale: ADR 0100 D2 makes the fold a **resolved superset** of the live graph (every live
element maps onto a fold element; the fold may *additionally* consolidate cross-batch), so the
**cross-batch class E1 contributes 0** to this one-directional measure — the guard does not false-alarm
on the very divergence ADR 0100 calls *expected*. A **non-zero** value means the live graph contains a
node/edge the log **cannot** reproduce — genuine projection rot / an un-logged mutation — which is
precisely the ADR-0095 risk. E2 (anchors) and E4 (`datasets`) are excluded from the value comparison
(they are legitimately not in the log). This is chosen over a naive symmetric-difference measure, which
would fire constantly on any multi-batch corpus because of E1.
- **Measurement-harness honesty:** the guard's divergence measure is a **new, self-contained** metric,
  not a claim on the ER golden set — it does **not** consume `resolution/eval.py`/`gold.py` (ADR 0043)
  and makes no B³/CEAFe claim. It is a projection-integrity gauge, and its acceptance is "0 on a
  fresh/single-batch corpus that IT-PROJ-2 already proves fold-equivalent; monotone-meaningful under the
  one-directional definition."

**B3 — Caching + gauge + alert (established patterns).** Cache the latest divergence on the driver like
`_latest_gc_stats` (`driver.py:104`, accessor injected into the collector `:519`). Emit a new
`worldmonitor_projection_divergence` gauge from **`collector.py`** — this is mandatory, not optional:
the **INV-PARITY** test (`tests/unit/test_prometheus_alerts.py:74-93`) regex-derives the valid alert-expr
metric set from `collector.py` string literals, so a `ProjectionDivergenceHigh` alert whose metric is
not emitted from `collector.py` fails CI. Add one **warning** alert (`ProjectionDivergenceHigh`,
`worldmonitor_projection_divergence > <tunable> for: <window>`) to
`deploy/prometheus/alerts/worldmonitor.rules.yml` with a fire + no-fire fixture case in
`deploy/prometheus/tests/worldmonitor.rules.test.yml` (the `alert-rules` CI job runs promtool,
`.github/workflows/quality.yml:54-85`). The existing "exactly two critical" contract
(`test_prometheus_alerts.py:449-466`) stays true — the new alert is `warning`.

**B4 — THE ONE QUESTION FOR THE HUMAN AT 3a-ii-B PLANNING (not a 3a-ii-A blocker).** *Build the diff
guard now (accepting that it requires an operator to stand up a second, throwaway Neo4j for the fold
target), or defer it until Gate 2b (backfill) lands?* Deferral is attractive because (a) before 2b the
live graph and the fold can only be compared cleanly on the fresh post-2a regime, where IT-PROJ-2
already proves equivalence, so the guard's marginal signal is thin; and (b) 3b cutover needs a
DR-rebuild target anyway, which is the same second instance. This is a genuine **sequencing/ops** call,
not a code fork — it is **reversible** (default-off), so it does not stop 3a-ii-A, but it is worth the
human's steer before 3a-ii-B is built. **Recommendation: land 3a-ii-A now; raise B4 with the user when
3a-ii-B is planned.**

---

## Governance (ADR 0097 §4/§5) — person_affecting: false, justified

**`person_affecting: false`, and the checker+judge reproduce this self-tag from the diff and DENY if a
person-affecting path is touched untagged.** The narrow, checker-verifiable claim for **3a-ii-A**:

- The **only** production file that changes is `resolution/projector.py`, and only its **incremental
  branch** (`full_rebuild=False`). The projector is **dormant/isolated**: no driver wiring, no settings
  flag, no compose profile, and it writes **only** the ephemeral, isolated test target — **never the
  live graph**.
- It **makes no merge/ER decision.** It faithfully **re-projects** decisions already taken by the
  existing, byte-unchanged, guarded merge path. It changes **no** ER threshold, **no** clustering/merge
  outcome, **no** individual-affecting score, **no** guard behaviour, **no** erasure path. A parked
  (`pending_review`) cluster wrote no statements, so it still produces **no** projected node — canonical
  fusion still routes only through the guard.
- **No file in the person-affecting write path changes:** `resolution/statements.py`,
  `resolution/merge.py`, `resolution/pipeline.py`, `graph/writer.py`, the merge guard, `db/models.py`,
  and every migration are **byte-unchanged**. The change is a re-read + re-fold of *already-persisted*
  statements before an *idempotent* write — additive and behaviour-preserving for `full_rebuild`.
- Because the diff still edits a `resolution/**` module while self-tagging non-sensitive, ADR 0097
  requires the explicit **human co-sign** carried in the header (as ADR 0100 did) — cosigned by the
  user (Mithat, 2026-07-05) at the PROPOSED→ACCEPTED flip.

3a-ii-B (when built) is likewise **person_affecting: false** (read-only projection-integrity
observability; opt-in, default-off; the fold target is a separate ephemeral Neo4j, never live).

## Reversibility

**Reversible** (additive; the projector stays dormant until 3b cutover).

- **A1 (full-history re-read).** Reversal cost: low — revert the incremental branch. **Revisit trigger:**
  a very high-degree survivor makes the re-read slow → bounded/paged re-fold.
- **A2 (P-FOLD-2 bound).** Reversal cost: none (a test). **Revisit trigger:** retroactive-supersession
  node-deletion is built (leaves the LOW backlog) → widen the property past the supersession-monotonic
  bound.
- **A3 (F4 edge equivalence + `exclude_edge_props`).** Reversal cost: none (additive test surface).
- **B (rebuild-and-diff guard).** Reversal cost: `projection_diff_enabled=False` fully disables it;
  drop the settings + gauge + alert. **Revisit trigger:** after 2b backfill + real cross-batch operation,
  reconsider whether the one-directional measure should become two-directional, and revisit B4.

**Overall revisit trigger (ADR 0095's):** the fold/projection maintenance cost exceeds the
merged-node/DR/erasure pain it removes — the 3a-ii-B guard is the early-warning signal (once built).

## Deferred (explicitly not built in 3a-ii-A)

- **3a-ii-B** — the whole rebuild-and-diff guard (§Decision B).
- **Retroactive supersession node-deletion** (the LOW backlog; the guard/cutover owns removing a
  now-empty superseded node — 3a-ii only `MERGE`s).
- **3b** — cutover (project into the live graph) + retire the direct write path (human-gated).
- **Gate 2b** — backfill of pre-2a graph nodes into the log.

## Alternatives rejected

- **One PR for all three deliverables.** Rejected (§Decomposition): mixes a resolution-engine
  correctness fix with driver/metrics/alerting observability; violates one-focused-PR; A stands alone
  and B only consumes A.
- **F3 fix by re-reading only the delta's own `canonical_id`s (not the survivor preimage).** Rejected:
  misses statements filed under a superseded alias that folds into the same touched survivor — the
  re-fold would be incomplete under re-canonicalisation.
- **F3 fix by making `write_entities` `SET n = props` (replace, not merge).** Rejected: a replace would
  destroy anchors/`prov_witnesses` the additive `SET n += props` deliberately preserves (ADR 0060), and
  would still be wrong for untouched survivors under a full replace of the node.
- **Naive symmetric-difference divergence measure (3a-ii-B).** Rejected (§B2): fires constantly on any
  cross-batch corpus because of E1 (the fold is legitimately more-resolved); the one-directional
  "live-not-explained-by-fold" measure is the crisp rot signal.
- **Co-writing a shadow subgraph inside the live Neo4j (3a-ii-B).** Rejected: touches the live DB and
  breaks the "never write live" invariant; Neo4j Community is single-database anyway (ADR 0094 D5).

## Consequences

- The projector's incremental mode becomes **loss-free** and provably equal to a full rebuild
  (P-FOLD-2), closing the last HIGH fold-determinism gap ADR 0095 named — while staying dormant,
  isolated, and behaviour-preserving for the live path.
- Edge byte-equivalence is proven **non-vacuously** (F4), so the fold-vs-direct anchor is real.
- The rebuild-and-diff guard is **designed and roadmapped** with reversible defaults and one flagged
  sequencing question, ready to build as 3a-ii-B once the human steers B4.

## ADR-index coupling

Adding this PROPOSED ADR requires the **3a-ii-A builder** to re-run
`uv run python scripts/gen_adr_index.py` so `docs/decisions/README.md` gains the `0101` row (else the
`adr-index` CI check goes red); `docs/decisions/README.md` is in the 3a-ii-A gate scope for exactly
this reason. The header uses the canonical list dialect (`Status` / `Date` / `human_fork` /
`person_affecting` on the header lines the generator parses), so the regenerated row reads
`PROPOSED | 2026-07-05 | false | false` until accept-time.
