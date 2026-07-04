# ADR 0043 — ER measurement harness + EM weights (extends ADR 0016)

> Status: **ACCEPTED** · 2026-06-25 · **Extends ADR 0016** (does not supersede it) ·
> Closes the *measurement* half of audit gap **G7**.
> Gate spec: `docs/reviews/GATE_A_ER_HARNESS_SPEC.md`.
> `human_fork`: **slice-1 = false** (autonomous) · **slice-2 = true** (human sign-off required).

## Context

ADR 0016 locked **expert-set** Splink m/u weights and a conservative
`DEFAULT_MERGE_THRESHOLD = 0.92` for ER v0, explicitly deferring EM training and calibration
("calibrate before Phase 3") and naming the consequence as audit gap **G7**: the weights are
**uncalibrated against real match outcomes** and accuracy on a second source is unknown.

We still have **no instrument** to measure ER quality. In particular we cannot measure
**over-merge** — the fusion of two distinct real persons/orgs into one canonical node. Over-merge
is the **catastrophic** ER failure: under our append-only / no-un-merge invariant it is not
cleanly reversible, it corrupts every downstream edge and score that referenced the fused node,
and — because it concerns a real person — it is exactly the change CLAUDE.md requires to be
human-gated. Every later ER decision (re-weighting, a second source, a LogicV2 re-scorer) needs a
regression instrument to measure against. **This ADR builds that instrument.**

This ADR does **not** relitigate ADR 0016's expert-set v0 decision. It **extends** it: it adds
the measurement layer ADR 0016 itself called for, and it stages the eventual EM/threshold upgrade
through the **propose → evaluate → gate → promote** loop ADR 0016 and CLAUDE.md require.

## Decision

### 1. Build an evaluation harness (`resolution/eval.py`)

Wrap Splink's labelled accuracy analysis
(`linker.evaluation.accuracy_analysis_from_labels_table`, labels registered via
`linker.table_management.register_labels_table`) to emit a **precision/recall curve**, and add
**cluster-level** metrics computed over the gold partition vs the predicted partition:

- **B-cubed (B³)** — Bagga & Baldwin 1998. Per-record precision/recall averaged over records;
  `B³ precision < 1.0` ⇔ at least one over-merge. The headline over-merge signal.
- **CEAFe** — Luo 2005 (entity-based CEAF, φ4 similarity). Optimal one-to-one cluster alignment
  solved with **`scipy.optimize.linear_sum_assignment`** (Hungarian). Penalises conflation and
  fragmentation at the entity level; complements B³ (not dominated by large clusters).
- **`over_merge_rate`** — fraction of predicted clusters that conflate ≥2 distinct gold
  entities. A direct, interpretable catastrophic-merge counter.

**Why B³ + CEAFe specifically:** they are the two standard, peer-reviewed coreference/ER cluster
metrics with complementary failure-sensitivities. B³ is interpretable and over-merge-sensitive at
the record level; CEAFe is entity-level and robust to the large-cluster bias that pairwise F1 and
(to a lesser degree) B³ suffer. Together with `over_merge_rate` they give a redundant,
cross-checkable read on the one failure we most fear. They are computed over the **gold
partition**, not Splink's blocked candidate set, so they catch **blocking-conditional**
over-merges the pairwise PR analysis structurally cannot see.

### 2. Build a reproducible gold-pair set (`resolution/gold.py` + `er_gold_pair`)

A small, seeded, labelled set of record pairs built by **stratified uncertainty sampling** over
the 0.5–0.95 Splink-score band (the decision boundary, where labels are most informative) plus a
seeded **OS-Pairs-style** set of known-hard cases. Persisted in a new Postgres table
`er_gold_pair` (new migration `0005_er_gold_pair`, revises `0004_drop_tenant_id`). The gold set
**must include**, by construction, a true over-merge trap and at least one gold pair lying
**outside every blocking rule** (so the blocking-conditional caveat is testable).

### 3. Add EM-training as a measured CANDIDATE (`resolution/splink_model.py`, additive)

Add an EM-training entry point
(`linker.training.estimate_u_using_random_sampling` →
`linker.training.estimate_parameters_using_expectation_maximisation(..., fix_u_probabilities=True)`)
that returns a **loadable, evaluable candidate model**. The candidate is **scored against the gold
set**, but **NOT swapped into the live `score_pairs` path**. The expert-set v0 weights and blocking
remain the production model in slice-1.

### 4. Cost-sensitive recommended threshold (REPORT VALUE ONLY)

From the measured PR curve, derive a **cost-sensitive** recommended threshold that weights a
**false merge (over-merge)** more heavily than a **false split**, reflecting the §1 asymmetry:
choose the threshold minimising `cost = c_fp · FP + c_fn · FN` with `c_fp ≫ c_fn`. The chosen
ratio is **`c_fp : c_fn = 10 : 1`** (an over-merge of two real persons is treated as an order of
magnitude worse than a missed duplicate; this ratio is itself revisitable in slice-2 under
sign-off). This value is **returned/reported only** — `eval.py` writes it nowhere live. The
harness **must be able to derive a non-`0.92`** threshold from the measured curve; the live value
changes only in slice-2.

### 5. Promote `scipy` to a declared dependency (`pyproject.toml`)

`scipy` is currently present only transitively via splink. CEAFe binds
`scipy.optimize.linear_sum_assignment` directly, so it must be a **declared** dependency.

### 6. The person-affecting split — `human_fork` on slice-2

Per CLAUDE.md ("ER thresholds … always need human sign-off; never silent in-place mutation") and
ADR 0016's own propose → evaluate → gate → promote clause:

- **Slice-1 (this ADR's buildable core)** is **person-neutral** — it builds the ruler and
  *proposes* a threshold. `human_fork = false`; autonomously buildable and mergeable.
- **Slice-2** — replacing `DEFAULT_MERGE_THRESHOLD = 0.92` (`merge.py:32`) with the derived value
  **and/or** promoting EM weights into the live `score_pairs` path — is **person-affecting**.
  `human_fork = true`. It **pauses** and presents the slice-1 harness evidence (B³ / CEAFe /
  `over_merge_rate` at candidate thresholds + the cost-sensitive recommendation) for the user to
  **sign off the specific new value**. It is **versioned with rollback** and **NOT merged without
  explicit human sign-off**.

## Alternatives considered

- **Pairwise PR / F1 only (no cluster metrics).** Rejected: pairwise metrics are blind to
  blocking-conditional over-merges (a fused pair never blocked is simply absent from the labels
  analysis) and are dominated by large clusters. They cannot reliably surface the catastrophic
  failure. B³ + CEAFe + `over_merge_rate` over the gold partition do.
- **MUC metric.** Rejected: link-based MUC is notoriously insensitive to over-merge (it rewards
  merging) — the opposite of what we need.
- **Auto-promote the EM weights / derived threshold once measured.** Rejected: this is a silent
  in-place mutation of a person-affecting parameter, forbidden by CLAUDE.md and ADR 0016. Hence
  the slice-2 `human_fork`.
- **Train EM and replace expert-set weights now (skip the candidate stage).** Rejected:
  premature — we still have effectively one rich source; EM output would be neither reproducible
  nor demonstrably better until measured against the gold set. Measure first, promote under
  sign-off later. (This keeps ADR 0016's deferral intact rather than overturning it.)
- **Store gold pairs in a new datastore (DuckDB/CSV).** Rejected: CLAUDE.md forbids a parallel
  datastore; `er_gold_pair` in Postgres reuses the existing `ResolverJudgement` idiom and the
  Alembic drift-guard discipline.

## Consequences

- ✅ First quantitative read on ER quality; over-merge becomes **measurable** (closing the
  measurement half of G7). Every later ER gate now has a regression instrument.
- ✅ EM weights become a **measured, promotable** candidate rather than an unmeasured guess —
  satisfying the versioned, rollback-able model store ADR 0016 said EM would need.
- ✅ The live merge decision for real persons is **unchanged** until a human signs off (slice-2),
  honouring the self-improvement gate.
- ⚠️ The gold set is small and seeded; it is a **regression** instrument, not a population-level
  accuracy estimate. Broadening it (more sources, larger n) is future work.
- ⚠️ `scipy` becomes a first-class dependency (already present transitively; now pinned).
- ⚠️ G7 is **not fully closed** by this ADR: the *promotion* half (live threshold / weights)
  stays OPEN under slice-2's human sign-off. The Gate Ledger G7 row reflects this split.

## Relationship to other ADRs

- **Extends ADR 0016** (expert-set weights v0) — adds the measurement layer and stages the EM
  upgrade through the gate. ADR 0016's decision is **not** relitigated.
- Independent of **ADR 0029** (ingest-driver "Gate A") — unrelated; named differently by
  coincidence of the fleet gate label.
- Consumes, but does not change, **ADR 0036** (deterministic canonical id) and the **ADR 0031**
  return-to-block sign-off state machine; the merge guard's *behaviour* is untouched in slice-1.
