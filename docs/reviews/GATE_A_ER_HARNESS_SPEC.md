# Gate A — ER Measurement Harness + EM Weights

> Status: **PROPOSED** (BUILD gate) · 2026-06-25 · Extends **ADR 0016** · Closes the
> *measurement* half of audit gap **G7**. Owner ADR: **0043**.
> Branch: `gate/a-er-measurement-harness` (off `master@0dcf2c0`).
>
> **NOTE — naming:** This is *not* the already-shipped "Gate A" ingest-driver (ADR 0029).
> That name is reused only as a fleet gate label; the owning decision here is **ADR 0043**.
> Do not touch ADR 0029.

---

## 0. TL;DR

Today every Splink weight and the `0.92` merge threshold are **hand-set and unmeasured**
(ADR 0016). We have **no instrument** to tell whether the resolver over-merges — and
over-merge (fusing two distinct real people/orgs into one canonical node) is the
**catastrophic, currently-unmeasured ER failure**. This gate builds that instrument:

- a **gold pair set** + table/migration,
- an **evaluation harness** (Splink PR analysis + **B³** + **CEAFe** cluster metrics + an
  **`over_merge_rate`**),
- an **EM-training capability** exposed as a *measured candidate model* (loadable, scored,
  **not** swapped into the live scoring path),
- a **cost-sensitive recommended threshold** derived from the measured PR curve and emitted
  as a **report value only**.

Every later ER gate (re-weighting, new sources, LogicV2 re-scoring) regresses against this
harness. It is the regression instrument the rest of the ER roadmap depends on.

**Central design constraint — the person-affecting split:**

- **Slice-1** changes **no person-affecting value**. Person-neutral, autonomously buildable
  and mergeable. It *derives and proposes* a non-`0.92` threshold but writes it nowhere.
- **Slice-2** is **person-affecting** (it would change the live merge decision for real
  individuals). It is `human_fork=true`: it **pauses** and presents the harness's measured
  evidence for the user to **sign off** before any live value changes. **It is NOT merged
  without explicit human sign-off.**

---

## 1. Why this gate (problem statement)

ADR 0016 locked **expert-set** Splink m/u weights and a conservative `DEFAULT_MERGE_THRESHOLD
= 0.92` for v0, explicitly because Phase 1 had a single source and no labelled pairs. ADR 0016
itself flags the consequence as audit gap **G7**: *"weights are uncalibrated against real match
outcomes; accuracy on a second source is unknown … calibrate before Phase 3."*

The unmeasured risk is **asymmetric**. A *missed* merge (under-merge) leaves two duplicate
nodes — recoverable, low blast radius. An *over-merge* fuses two distinct legal/biological
persons into one canonical entity. Under our append-only / no-un-merge invariant an over-merge
is **not cleanly reversible**, it corrupts every downstream score and edge that referenced the
fused node, and — because it concerns a real person — it is exactly the class of change CLAUDE.md
says must be human-gated. **We currently have no metric that even detects it.**

This gate closes the **measurement** half of G7. It does *not* (in slice-1) change any live
weight or threshold — it builds the ruler, not the cut. The cut (slice-2) is gated on a human.

---

## 2. Verify-before-code (BLOCKING)

Per the execution model, **no implementation may begin until `VERIFIED_API.md` exists** at the
repo root and records — **verbatim, against PRIMARY Splink 4.x docs** (not memory, not this
spec) — the exact signatures the harness binds. orient captured the installed signatures
(Splink **4.0.16**); the builder must **confirm** each against the primary docs and paste the
verbatim signature + the doc URL/source path into `VERIFIED_API.md`:

- `linker.training.estimate_u_using_random_sampling(max_pairs=..., seed=...)`
  (`splink/internals/linker_components/training.py:168`)
- `linker.training.estimate_parameters_using_expectation_maximisation(blocking_rule,
  fix_u_probabilities=True, ...)` (`training.py:220`)
- `linker.evaluation.accuracy_analysis_from_labels_table(labels, *,
  output_type="precision_recall" | "threshold_selection" | ..., ...)` (`evaluation.py:186`)
- `linker.table_management.register_labels_table(df)` — labels-table schema is
  `source_dataset_l | unique_id_l | source_dataset_r | unique_id_r | clerical_match_score`.
- `scipy.optimize.linear_sum_assignment` (for CEAFe).

**Namespacing is load-bearing.** These methods hang off `linker.training.*` /
`linker.evaluation.*` / `linker.table_management.*`, **not** the bare `Linker`. A binding to
the wrong namespace is a DENY.

The judge **DENIES** the gate if any bound Splink/scipy call has no verbatim entry in
`VERIFIED_API.md`, or if an entry was paraphrased rather than copied.

---

## 3. Scope — exact files / areas

### Slice-1 (person-NEUTRAL — autonomously buildable + mergeable)

Production:
- `src/worldmonitor/resolution/gold.py` — **NEW**. Gold-pair set construction (§7).
- `src/worldmonitor/resolution/eval.py` — **NEW**. Evaluation harness: PR analysis wrapper,
  **B³**, **CEAFe**, **`over_merge_rate`**, the cost-sensitive recommended-threshold report (§6).
- `src/worldmonitor/resolution/splink_model.py` — **EDIT (additive only)**. Add an EM-training
  entry point that returns a **measured candidate** settings/model (loadable + evaluable). It
  **MUST NOT** alter the existing `score_pairs(...)` live path, its weights, or its blocking.
- `src/worldmonitor/db/models.py` — **EDIT (additive only)**. New `er_gold_pair` table model.
- `src/worldmonitor/db/migrations/versions/0005_er_gold_pair.py` — **NEW**. `Revises:
  0004_drop_tenant_id`. Creates `er_gold_pair` **only** (no edit to 0001–0004).
- `pyproject.toml` — **EDIT (one line)**. Promote `scipy` from a transitive (via splink) to a
  **declared** dependency (CEAFe needs `scipy.optimize.linear_sum_assignment`).

Tests:
- `tests/test_eval_harness.py` — **NEW**. The primary failing-test-first invariant suite (§5).
- `tests/unit/test_gold.py` — **NEW** (optional, recommended). Unit tests for gold-set sampling.
- `tests/unit/test_cluster_metrics.py` — **NEW** (optional, recommended). Unit tests pinning
  B³ / CEAFe / `over_merge_rate` on hand-computed fixtures.

Docs / config:
- `VERIFIED_API.md` — **NEW** (§2; blocking gate).
- `docs/reviews/GATE_A_ER_HARNESS_SPEC.md` — this file.
- `docs/decisions/0043-er-measurement-harness-em-weights.md` — **NEW** (ADR, PROPOSED).
- `docs/decisions/0016-splink-expert-set-weights.md` — **EDIT (note only)**: add a forward
  note "extended by ADR 0043 (measurement harness)". Do **not** relitigate 0016.
- `docs/GATE_LEDGER.md` — **EDIT**: update the **G7** row (measurement half CLOSED by this
  gate; threshold/weight promotion remains OPEN under slice-2 sign-off).
- `.claude/gate.scope` — **OVERWRITE** the stale Gate-0 scope.

### Slice-2 (person-AFFECTING — GATED, requires HUMAN SIGN-OFF; NOT auto-merged)

Production:
- `src/worldmonitor/resolution/merge.py` — **EDIT**: replace `DEFAULT_MERGE_THRESHOLD = 0.92`
  (`merge.py:32`) with the harness-derived value, **and/or** wire the EM candidate model into
  the live `score_pairs` path. **`human_fork=true`.** This change is written **only after**
  the user signs off the value on the basis of the slice-1 harness report.

> Anything not in these two lists is **out of scope** (§9).

---

## 4. The deletion / addition surface (no deletions)

This gate **deletes nothing**. It is purely additive in slice-1:

- `gold.py`, `eval.py`, `0005_er_gold_pair.py`, `test_eval_harness.py` are new files.
- `splink_model.py`, `models.py`, `pyproject.toml` get additive edits only.
- The existing `score_pairs` weights/blocking/threshold are **frozen** in slice-1 (§9).

The **only** value-changing edit in the whole gate is slice-2's single line at `merge.py:32`
(and/or the EM-weight promotion), which is gated on human sign-off.

---

## 5. Failing-test-first design (`tests/test_eval_harness.py`)

This is a normal BUILD gate, so a **non-vacuous failing test is required**. The test-author
writes `tests/test_eval_harness.py` so it **FAILS on `master@0dcf2c0`** (the metric is absent)
and **PASSES** once `eval.py` lands. Required cases:

1. **Planted-over-merge ⇒ metric fires (the primary invariant).** Construct a predicted
   clustering that fuses two gold-distinct entities into one cluster. Assert:
   - **B³ precision < 1.0**, and
   - **`over_merge_rate` > 0**.
   This **FAILS pre-fix** because `eval.py` / the metrics do not exist; **passes post-fix**.

2. **Perfect-clustering property test.** For any clustering identical to the gold partition,
   assert **B³ = CEAFe = 1.0** (precision = recall = F1 = 1.0). A property/parametrised test
   over several random-but-correct partitions.

3. **Blocking-conditional assertion (the adversarial target).** A gold pair that falls **outside
   every blocking rule** is invisible to `accuracy_analysis_from_labels_table` — the *pairwise*
   metric silently omits it (Splink can only score pairs that blocking generated). Assert that:
   - the pairwise PR analysis **does not** report this pair (documents the caveat), **but**
   - the **gold-set-driven** cluster metrics + **`over_merge_rate`** (which iterate the gold
     partition, not Splink's blocked candidate set) **still catch** a blocking-conditional false
     positive. The adversarial target is precisely a **blocking-conditional over-merge the
     pairwise metric hides** — the harness must not be blind to it.

Supporting unit tests (`tests/unit/test_cluster_metrics.py`) pin B³ / CEAFe / `over_merge_rate`
on small hand-computed fixtures so a future refactor cannot silently change the math.

---

## 6. Metric definitions (authoritative)

All metrics are computed over the **gold partition** (a set of clusters / equivalence classes of
record ids) vs the **predicted partition** (the resolver's output clusters). They are
gold-set-driven, **not** limited to Splink's blocked candidate set — that is what lets them catch
blocking-conditional errors (§5.3).

### 6.1 B-cubed (B³) — Bagga & Baldwin 1998

Per-record precision and recall, averaged over all records. For record `r`, let `P(r)` be its
predicted cluster and `G(r)` its gold cluster:

- `Precision(r) = |P(r) ∩ G(r)| / |P(r)|`
- `Recall(r)    = |P(r) ∩ G(r)| / |G(r)|`
- `B³ precision = mean_r Precision(r)`, `B³ recall = mean_r Recall(r)`,
  `B³ F1 = harmonic_mean(B³ precision, B³ recall)`.

**B³ precision < 1.0 ⇔ at least one over-merge** (a predicted cluster mixes records from ≥2
gold clusters). This is the headline over-merge signal.

### 6.2 CEAFe — Luo 2005 (entity-based CEAF, φ4 similarity)

Find the optimal one-to-one alignment between predicted and gold clusters maximising total
similarity, where for aligned clusters `g, p`:

- `φ4(g, p) = 2·|g ∩ p| / (|g| + |p|)`.

The optimal alignment is solved with **`scipy.optimize.linear_sum_assignment`** on the negated
similarity matrix (Hungarian algorithm). Then:

- `CEAFe precision = Σ φ4(aligned) / Σ_p φ4(p, p)`  (= Σ_p |p| normalisation)
- `CEAFe recall    = Σ φ4(aligned) / Σ_g φ4(g, g)`
- `CEAFe F1 = harmonic_mean(precision, recall)`.

CEAFe complements B³: it penalises both fragmentation and conflation at the *entity* level and
is not dominated by large clusters the way pairwise metrics are.

### 6.3 `over_merge_rate`

The **fraction of predicted clusters that conflate ≥2 distinct gold entities**:

- `over_merge_rate = |{ p ∈ predicted : p contains records from ≥2 gold clusters }| / |predicted|`.

It is a **direct, interpretable catastrophic-merge counter**, computed over the gold partition,
so it fires even when the offending pair was never blocked (§5.3). `over_merge_rate = 0` on a
correct clustering; `> 0` whenever any cluster fuses distinct gold entities.

### 6.4 Cost-sensitive recommended threshold (REPORT VALUE ONLY)

From the measured PR curve (`accuracy_analysis_from_labels_table(..., output_type=
"threshold_selection")` / `"precision_recall"`), `eval.py` computes a **cost-sensitive**
recommended threshold that weights a **false merge** (over-merge) more heavily than a **false
split** (the asymmetry of §1). Concretely, choose the threshold minimising
`cost = c_fp · FP + c_fn · FN` with `c_fp ≫ c_fn` (the exact ratio is recorded in ADR 0043).

**This value is RETURNED / REPORTED ONLY.** `eval.py` MUST NOT write it into `merge.py`,
`DEFAULT_MERGE_THRESHOLD`, or any live config. The harness must be **able to derive a
non-`0.92` threshold** from the measured curve (this is what satisfies the Workflow-B end-state
goal "DENY if threshold remains 0.92" — see §10); the live value changes only in slice-2 under
sign-off.

---

## 7. Gold-set design (`gold.py` + `er_gold_pair` + `0005` migration)

**Goal:** a small, reproducible, labelled set of record pairs (match / non-match) that
exercises the metrics — especially the over-merge / blocking-conditional cases.

- **Sampling:** **stratified uncertainty sampling** over the **0.5–0.95** Splink-score band
  (the decision-boundary region where the model is least certain and labels are most
  informative), plus a deliberately seeded **OS-Pairs-style** set of known hard cases (the
  OpenSanctions ER pairs idiom: same-name-different-entity, transliteration variants,
  shared-but-clashing ids).
- **Must include**, by construction:
  - at least one **true over-merge trap** (two distinct gold entities that the resolver is
    tempted to fuse), and
  - at least one **blocking-conditional** gold pair that lies **outside every blocking rule**
    (so §5.3 is exercisable).
- **Determinism:** all sampling is seeded; `gold.py` is reproducible run-to-run.

### `er_gold_pair` table (`models.py`, additive)

Mirror the existing `ResolverJudgement` shape (`models.py:153`):

| column        | type            | note                                               |
|---------------|-----------------|----------------------------------------------------|
| `id`          | `String(64)` PK | row id                                             |
| `left_id`     | `String(255)`   | canonically ordered `left_id <= right_id`          |
| `right_id`    | `String(255)`   |                                                    |
| `label`       | `String(16)`    | `"match"` \| `"non_match"`                          |
| `source`      | `String(32)`    | how the pair was sampled (e.g. `uncertainty`, `os_pairs`) |
| `clerical_score` | `Float` (nullable) | maps to Splink's `clerical_match_score` for the labels table |
| `created_at`  | `DateTime(tz)`  | `server_default=func.now()`                        |

Add a unique constraint `uq_er_gold_pair (left_id, right_id)` (same idiom as
`uq_resolver_judgement_pair`).

### `0005_er_gold_pair.py` migration

- `Revises: 0004_drop_tenant_id`; `revision = "0005_er_gold_pair"`.
- `upgrade()` creates `er_gold_pair` + its unique constraint **only**.
- `downgrade()` drops the table.
- **Drift guard:** `models.py` and the `0005` head **must agree byte-for-byte** —
  `tests/integration/test_migrations.py` (ADR 0030) asserts `fresh(head) ≡ create_all(models)`
  and `alembic check` clean. Write the model edit and `0005` as **one atomic change**; run
  `alembic check` locally before pushing. This is the gate's #1 mechanical risk.

---

## 8. EM-training capability (`splink_model.py`, additive)

Add a function (e.g. `train_candidate_model(...)`) that, on a frame of entities:

- builds a Splink `Linker` (reusing `_flatten` / the comparison + blocking definitions),
- runs `linker.training.estimate_u_using_random_sampling(max_pairs=..., seed=...)` then
  `linker.training.estimate_parameters_using_expectation_maximisation(blocking_rule,
  fix_u_probabilities=True, ...)` (signatures per `VERIFIED_API.md`),
- returns the **trained settings / candidate model** as a loadable artefact that `eval.py` can
  score against the gold set.

**Hard constraint:** this is a **measured candidate**. `score_pairs(...)` — the live path that
`pipeline.py:291` calls and `merge.py` consumes — keeps its **expert-set weights and blocking
unchanged**. The EM model is *evaluated*, not *promoted*, in slice-1. Promotion is slice-2.

---

## 9. Out of scope (hard stops)

- **No live value change in slice-1.** Not `DEFAULT_MERGE_THRESHOLD`, not the `score_pairs`
  weights, not the blocking rules, not the EM model into the live path.
- **No edits to ADR 0016's decision** (only a forward note). Do not relitigate expert-set v0.
- **No new datastore / no parallel model** (CLAUDE.md). Gold pairs live in Postgres
  (`er_gold_pair`); metrics are pure Python over partitions.
- **No edits to migrations 0001–0004.** The delta is `0005` only.
- **No LogicV2 / nomenklatura re-scorer** (its own future ADR; out of this gate).
- **Slice-2 is not merged autonomously.** It pauses for human sign-off (§10).

---

## 10. Acceptance criteria → tests → slices

### APPROVE (all must hold)

| # | Criterion | Proof |
|---|-----------|-------|
| A1 | `VERIFIED_API.md` exists and records **verbatim** every bound Splink/scipy signature | §2; judge inspects file + diffs against bound call sites |
| A2 | EM candidate model **loads + is evaluable** against the gold set | `train_candidate_model` returns a scorable model; harness scores it |
| A3 | **B³ and CEAFe unit-verified** on a **perfect** clustering (= 1.0) AND a **planted-error** clustering (precision < 1.0) | `tests/unit/test_cluster_metrics.py`; `test_eval_harness.py` cases 1–2 |
| A4 | **`over_merge_rate`** computed on the gold set; **> 0** on the planted over-merge, **0** on a correct clustering | `test_eval_harness.py` case 1 + property case 2 |
| A5 | A **non-`0.92`** cost-sensitive threshold is **derivable** from the measured PR curve and returned as a report value | `eval.py` recommended-threshold path; harness asserts it is computed (not stubbed) |
| A6 | The **blocking-conditional caveat is asserted**: the pairwise metric hides the unblocked pair, the gold-set metrics still catch it | `test_eval_harness.py` case 3 |
| A7 | `er_gold_pair` + `0005` migration land; **drift guard green** | `tests/integration/test_migrations.py` |
| A8 | `scipy` is a **declared** dependency | `pyproject.toml` diff |
| A9 | The **existing resolution suites stay green** (frozen behaviour, §11) | full CI: `quality` + `security` + integration |

### DENY if any of:

- **D1** — any cluster metric (B³, CEAFe, `over_merge_rate`) is **stubbed / hard-coded /
  returns a constant** instead of computed from the partitions.
- **D2** — the **blocking-conditional caveat is unhandled** (the harness is blind to an
  unblocked over-merge, or §5.3 is missing/xfailed).
- **D3** — the **person-affecting threshold (or EM live-weight) is auto-promoted** without
  explicit human sign-off (any slice-1 edit to `merge.py:32` or to the live `score_pairs`
  weights). The end-state Workflow-B criterion *"DENY if threshold remains 0.92"* is satisfied
  by slice-1 **deriving + proposing** a non-`0.92` value and slice-2 **promoting it under
  sign-off** — it is **not** satisfied by slice-1 silently writing the value.
- **D4** — any bound Splink/scipy call is missing from `VERIFIED_API.md`, or bound to the wrong
  namespace (e.g. on the bare `Linker` instead of `linker.training.*` / `linker.evaluation.*`).
- **D5** — `score_pairs` weights/blocking changed in slice-1, or a frozen test (§11) is removed,
  loosened, or xfailed.

---

## 11. Frozen tests (must pass UNCHANGED)

The existing resolution suites prove the merge-guard / sign-off / canonical-id / negative-evidence
behaviour that slice-1 must not perturb (slice-1 is additive). They must stay green byte-for-byte:

- `tests/unit/test_resolution.py`
- `tests/unit/test_resolution_canonical_id.py`
- `tests/unit/test_resolution_negative_judgement.py`
- `tests/unit/test_resolution_anchor_conflict.py`
- `tests/unit/test_resolution_distinguishing_evidence.py`
- `tests/unit/test_resolution_multiscript.py`
- `tests/unit/test_resolution_merge_incompat.py`
- `tests/integration/test_resolution_pipeline.py`
- `tests/integration/test_resolution_batching.py`
- `tests/integration/test_b6_resolve_incompat.py`
- `tests/integration/test_b6_signoff_poison.py`
- `tests/integration/test_migrations.py` (drift guard — must stay green **with** `0005`)

The judge diffs these: a removed assert, an added skip/xfail, or a loosened tolerance is a DENY.

---

## 12. Locked invariants (must hold across the gate)

- **G1 — provenance on every node AND edge.** Untouched: the harness reads, never writes, the
  graph. Gold pairs are id references, not graph mutations.
- **Append-only / no un-merge.** Untouched. The harness *measures* clustering; it never merges,
  splits, or deletes. This is precisely *why* over-merge measurement matters (over-merge is not
  cleanly reversible under append-only).
- **Canonical-canonical only via the guard.** Untouched. `merge.py`'s guard + return-to-block
  sign-off state machine are not behaviourally changed in slice-1; slice-2 only changes the
  *threshold value* the guard consumes, under sign-off.
- **ER thresholds affecting a real person need human sign-off; no silent in-place mutation**
  (CLAUDE.md / ADR 0016). Enforced as the slice-1 / slice-2 split and DENY D3.
- **ADR-0036 deterministic canonical id.** Untouched (the harness does not mint canonical ids).

---

## 13. Slice plan (each individually mergeable + CI-green)

**slice-1 — MEASUREMENT HARNESS (person-NEUTRAL, autonomously mergeable):**
`gold.py` + `er_gold_pair` model + `0005` migration; `eval.py` (PR wrapper + B³ + CEAFe +
`over_merge_rate` + cost-sensitive recommended-threshold **report**); additive EM
`train_candidate_model` in `splink_model.py` (candidate only, live `score_pairs` frozen);
`scipy` declared; `tests/test_eval_harness.py` (+ unit metric tests). **Mergeable** when:
quality + security + integration green; `VERIFIED_API.md` present; drift guard green;
frozen tests (§11) unchanged; metrics computed (not stubbed); blocking-conditional caveat
asserted. **Changes no person-affecting value.** Land first.

**slice-2 — GATED PROMOTION (person-AFFECTING, `human_fork=true`, HUMAN SIGN-OFF REQUIRED):**
replace `DEFAULT_MERGE_THRESHOLD = 0.92` (`merge.py:32`) with the harness-derived value
and/or promote the EM weights into the live `score_pairs` path. This slice **PAUSES** and
presents the slice-1 harness report — B³ / CEAFe / `over_merge_rate` at candidate thresholds,
and the cost-sensitive recommendation — for the user to **sign off the new value**. It is
**versioned with rollback** and **NOT merged without explicit human sign-off**. It only
becomes mergeable after the user authorises the specific new value; that authorisation is
recorded (ADR 0043 + the gate.scope metadata). Land second, post-sign-off.

> `land_order`: slice-1 first (builds the instrument), slice-2 second (cuts under sign-off).
> They are independent in code, but slice-2 is **meaningless** without slice-1's measured report
> and **forbidden** without the human's sign-off.

---

## 14. Risks (human checkpoint)

1. **Migration drift (mechanical, highest-probability).** `models.py` and the `0005` head must
   agree byte-for-byte or `test_migrations.py` (ADR 0030) goes red. Mitigation: write the model
   edit and `0005` as one atomic change; run `alembic check` locally before pushing.
2. **Blocking-conditional blind spot (correctness, the adversarial heart).** If the metrics are
   computed only over Splink's blocked candidate set rather than the full gold partition, the
   harness will silently miss exactly the over-merge it exists to catch (§5.3). Mitigation: the
   §5.3 assertion is mandatory; metrics iterate the gold partition, and DENY D2 guards it.
3. **Premature promotion (governance, the person-affecting line).** The strongest temptation is
   to "finish the gate" by writing the derived threshold into `merge.py:32` autonomously — which
   would silently change the merge decision for real people, violating CLAUDE.md and DENY D3.
   Mitigation: the slice-1 / slice-2 split; slice-2 is `human_fork=true` and **must** pause for
   sign-off.

**Human sign-off requirement (explicit):** slice-2 — promoting the PR-curve-derived threshold
into `DEFAULT_MERGE_THRESHOLD` and/or the EM weights into the live `score_pairs` path — is
**person-affecting** and **must not be merged without explicit human sign-off** on the specific
new value, on the basis of the slice-1 harness report, versioned with rollback.
