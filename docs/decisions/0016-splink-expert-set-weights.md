# ADR 0016 — Splink ER model: expert-set weights (v0), EM-trained later

> Status: **LOCKED** (v0) · June 2026 · Supersedes nothing. Refines decision #4 (ER = Splink + nomenklatura).
> **Extended by [ADR 0043](0043-er-measurement-harness-em-weights.md) (2026-06-25):** Gate A adds the
> measurement harness (gold set + B³/CEAFe + over_merge_rate) and an EM-trained *candidate* model. The
> v0 expert-set weights and the 0.92 threshold this ADR locked remain the LIVE path; promoting the EM
> weights / a calibrated threshold is the person-affecting, human-sign-off slice-2 of Gate A.

## Context
Entity resolution v0 (PR #12) needs a pairwise scoring model. Splink/Fellegi-Sunter requires `m` and `u`
probabilities per comparison level. These can be **estimated unsupervised via Expectation-Maximisation
(EM)** from the data, or **set by hand (expert-set)**. At Phase 1 there is exactly one source
(OpenSanctions) and no labelled pairs, so EM has little data to estimate from and its output would be
neither reproducible nor auditable across runs.

## Decision
Use **expert-set m/u weights** for v0: hand-tuned, transparent comparison levels for `name`
(exact / jaro-winkler ≥.92 / ≥.82 / else), `country`, `birth_date`, and `wikidata_id`, with a
conservative high-confidence merge threshold of **0.92** and a prior of 0.001
(`resolution/splink_model.py:44-149`, `resolution/merge.py` `DEFAULT_MERGE_THRESHOLD`). Per CLAUDE.md's
"transparent weighted first, then Bayesian." **EM training is a deferred, gated upgrade** for when there
is enough multi-source data to estimate parameters reliably.

## Status
**LOCKED** for v0. The upgrade to EM-trained weights is **OPEN** and gated: it is a self-improvement
change to a person-affecting system, so it must go through propose → evaluate → gate → promote with
versioning + rollback + human sign-off (CLAUDE.md self-improvement rule).

## Consequences
- ✅ Reproducible, auditable, debuggable scoring; no silent drift between runs.
- ✅ No dependency on labelled data we don't have.
- ⚠️ Weights are uncalibrated against real match outcomes; accuracy on a second source is unknown
  (audit gap **G7**). Calibrate **before Phase 3** scaling.
- ⚠️ The weights live in Python, not a versioned store — when EM training lands, the model must become
  versionable with rollback (needed anyway for the gated self-improvement loop).
