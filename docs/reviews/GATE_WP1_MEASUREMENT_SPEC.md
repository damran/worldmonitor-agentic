# WP-1 — ER measurement executable end-to-end (spec)

> 2026-07-18 · plan `stateless-tumbling-koala` WP-1 · closes re-review findings #1 and #2
> (`docs/fable-review/90_REREVIEW_2026-07-11.md`) and the roadmap's "Label-sufficiency report"
> (`docs/40_ROADMAP.md` G7 lane). Measurement-only: NO live-path change, NO threshold write,
> promotion stays human-gated (G7). Lean fleet: test-author → builder → ONE checker.

## Problem

The harness is a ruler nobody can pick up: `eval.py`'s `bcubed`/`ceafe`/`over_merge_rate`/
`precision_recall_table`/`recommended_threshold` have **zero non-test call sites**; nothing reads
`er_gold_pair` back into a `Partition`; there is no report entrypoint. Separately,
`silver.py::build_silver_pairs` evaluates all O(n²) pairs (`silver.py` `for i… for j…`), infeasible
at real-corpus scale.

## Deliverables

### D1 — `src/worldmonitor/resolution/measure.py` (NEW, strictly read-only)

1. `load_labelled_pairs(session, *, sources: Sequence[str] | None = None) -> list[GoldPair]` —
   read `er_gold_pair` rows (optionally filtered by `source`), returned as
   `resolution.gold.GoldPair`s.
2. `gold_partition(pairs: Sequence[GoldPair]) -> dict[str, str]` — union-find over the `match`
   pairs; EVERY id mentioned in any pair (including `non_match`-only ids) appears in the
   partition (singleton unless united). Deterministic labels (e.g. the lexicographically
   smallest member id of each component). Non-match edges add members but never unite.
3. `predicted_partition(session, record_ids: Iterable[str]) -> dict[str, str]` — each record id
   mapped through the `canonical_id_ledger` (alias → surviving `canonical_id`; reuse
   `resolution/canonical.py`'s resolve helper if one exists, else read the table directly,
   latest row per alias). An id with no ledger row maps to itself (its own singleton cluster).
4. `metric_confidence_intervals(gold, predicted, *, n_boot=200, seed=0)` — bootstrap over GOLD
   clusters (resample clusters with replacement; restrict both partitions to the sampled
   records; recompute B³ P/R/F1, CEAFe F1, over_merge_rate) → per-metric (point, lo, hi) at
   2.5/97.5 percentiles. Seeded + deterministic; stdlib `random.Random(seed)` (NO
   `Date.now`-style nondeterminism).
5. `SufficiencyReport` dataclass + `build_sufficiency_report(session, *, n_boot=200, seed=0)`:
   - labels by `source` × `label` (counts), distinct record ids covered;
   - boundary coverage: pairs carrying a `clerical_score`, and of those the count/fraction in
     `[0.5, 0.95)` (the G7 decision band; silver rows have `clerical_score=None` by N3 and are
     reported as scoreless);
   - cluster metrics + CIs over (gold vs predicted) — computed ONLY when the ledger yields at
     least one non-trivial mapping; otherwise `metrics=None` with an explicit
     `reason="no resolved corpus in the ledger yet"` (never crash on an empty deploy);
   - `as_dict()` (JSON-ready) and `as_text()` (the human report).
6. CLI: `python -m worldmonitor.resolution.measure [--json] [--boot N] [--seed N]` — builds the
   report from process settings and prints it. Read-only: the module contains NO
   `session.commit`/`add`/`delete` and never imports `merge.py`; `recommended_threshold` remains
   a report value (DENY D3 unchanged — this module does not even call it unless a Splink linker
   is wired, which it is NOT in v1: PR-curve reporting stays a follow-up; do not fake it).

### D2 — `silver.py` anchor-key candidate blocking (EXACT-equivalent)

Replace the `for i in range(n): for j in range(i+1, n)` full cross-product with candidate
generation: for each `prop` in `ANCHOR_PROPERTIES`, let `S_prop` = entities with ≥1 value for
`prop`; the candidate set is the union over props of within-`S_prop` pairs (canonical-keyed,
deduplicated). Then run the UNCHANGED per-pair classification on each candidate.

Equivalence argument (the property test pins it): a pair outside every `S_prop` has no anchor
prop with both sides non-empty ⇒ `has_shared == has_conflict == False` in the naive loop ⇒
abstain ⇒ emitting nothing for it is byte-identical output. Jurisdiction-scoped candidates that
fail corroboration classify to abstain exactly as before (harmless in the candidate set).

- N1/N2/N3 and the ADR 0085 classification order are UNTOUCHED; every existing silver test must
  pass unmodified.
- Docstring records the residual: labelled NEGATIVES are inherently pairwise within an anchor
  block; if any `|S_prop|` grows past ~10k, negative sampling needs its own ADR (revisit
  trigger).

### D3 — docs

- `docs/runbooks/OPERATOR_SESSION.md` §3: replace the placeholder block with the real CLI line.
- `docs/40_ROADMAP.md`: check the "Label-sufficiency report" box (cite this PR).

## Tests (test-author writes FIRST, separate from the builder)

- `tests/unit/test_measure.py`: union-find correctness (chains, non-match singletons,
  determinism); ledger mapping (alias→canonical, missing→self); report shape on a seeded SQLite
  corpus (counts by source/label, boundary band, scoreless silver); empty-ledger → metrics=None
  with reason; CLI smoke (`main()` with injected sessionmaker or settings monkeypatch, asserts
  no DB write); CI determinism for a fixed seed.
- `tests/property/test_prop_silver_blocking.py`: `@given` equivalence — a NAIVE reference
  implementation (the current double loop, copied into the test as the oracle) vs the blocked
  `build_silver_pairs`, byte-identical `list[GoldPair]` output over generated corpora covering:
  shared/conflicting/absent anchors per tier, same/distinct/missing sources, corroborating/
  disjoint/absent jurisdictions, contradiction pairs, id-less entities. Use
  `settings(deadline=None)` (heavy-@given rule) and bounded corpus sizes (≤ 12 entities).
- SQLite idiom: the `@compiles(JSONB, "sqlite")` shim + `make_engine("sqlite:///:memory:")`
  exactly as `tests/unit/test_backfill.py`.

## Invariants for the checker

1. Measurement-only: `git diff` touches only `resolution/measure.py`, `resolution/silver.py`,
   tests, the two docs; NO change under `merge.py` / `splink_model.py` / `pipeline.py` /
   `graph/` / thresholds; no new write path (grep the new module for commit/add/delete/execute-
   with-INSERT).
2. Silver equivalence: independently re-run the property suite; adversarially probe with a
   hand-built contradiction + same-source-positive + jurisdiction-abstain corpus and diff the
   two implementations' outputs directly.
3. N1/N2/N3 intact (existing silver tests unmodified and green).
4. Full `pytest -m "not integration"` green; `ruff format --check .` repo-wide; pyright clean
   on the touched src files.
