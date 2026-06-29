# 0080 — External-benchmark FLOOR for the ER measurement harness

- **Status:** accepted (reversible defaults — the external leg of the non-circular label on-ramp the
  user decided 2026-06-29)
- **Date:** 2026-06-29
- **Gate:** G7 label on-ramp, **slice 3 of 4**. ADR [0079](0079-canonical-anchor-silver-labels.md)
  (slice 2, MERGED) gave the harness (ADR [0043](0043-er-measurement-harness-em-weights.md)) a
  non-circular *internal* label source (canonical-anchor SILVER). This slice adds an **independent,
  external** ruler: a public, third-party-labelled benchmark used to **report a precision / recall /
  over-merge FLOOR** for our matcher. It is a **sanity floor, NOT a promotion input.**
- **Touches:** new `src/worldmonitor/resolution/benchmark.py` (the importers + `evaluate_floor` + the
  contamination guard); `tests/property/test_prop_benchmark.py` (the mandatory `@given` invariant test);
  `tests/unit/test_benchmark.py` (example + math + guard + febrl mapper); `pyproject.toml` (a new
  **optional** `benchmark` dependency group, `recordlinkage`, folded into `dev` only). **No schema
  change, no migration, no new runtime dependency, no write to `er_gold_pair`.** **Not person-affecting**
  (`human_fork: false`) — it computes a returned-only report value, never a threshold / EM weight /
  merge / graph value.

## Context

**G7 = promoting calibrated Splink ER thresholds / EM weights into the LIVE decision path.** That is
person-affecting and stays **human-sign-off-gated** (CLAUDE.md self-improvement gate) — it is **not** in
scope here. The on-ramp's job is to give the harness labels that are **NOT a function of the model's own
score**, so a future (human-gated) calibration is not graded against its own opinion.

Slice 2 (ADR 0079) built the *internal* non-circular signal (a canonical id shared across ≥2 distinct
sources ⇒ `match`; conflicting same-type ids ⇒ `non_match`). That signal is high-precision but
**corpus-bound and partial in recall**, and — critically — it is **derived from the same OpenSanctions
data we ingest**, so on its own it cannot answer *"how does our matcher compare to an independent,
externally-labelled standard?"*. That is what an **external benchmark floor** provides: a public set
labelled by a third party, scored by our matcher, reported as a floor number with a published reference
point to sanity-check against.

**Decided benchmarks (user, 2026-06-29; WorldMonitor is NON-COMMERCIAL):**

- **PRIMARY — OpenSanctions "OS-Pairs".** ~755k analyst-labelled pairs, line-JSON
  `{"judgement": "positive"|"negative", "left": <FtM entity>, "right": <FtM entity>}`. It is
  **FtM-native** (the same L2 ontology this repo speaks — no parallel model), multilingual, and the
  closest public analog to our own ER task. Licence **CC BY-NC 4.0** — fine for our non-commercial,
  self-hosted project. File: `https://data.opensanctions.org/contrib/training/pairs-20251209.json.gz`
  (~409 MB gzip). Docs: <https://www.opensanctions.org/docs/opensource/pairs/>. **Published reference
  point:** nomenklatura's `RegressionV1` scores **91.33 % F1** on this set (arXiv 2603.11051) — the
  yardstick our floor is read against (we expect to sit *below* a tuned regression model; a floor far
  below it flags a real defect).

- **SECONDARY — Febrl synthetic person data** via the `recordlinkage` package
  (`load_febrl1..4`, `return_links=True`). **BSD/ANUOS** licence (commercial-OK), **zero PII**
  (fully synthetic), Person-only, English-only. It is the licence-clean, PII-free fallback that keeps a
  floor available even under a future commercial pivot, and exercises the Person path independently of
  OpenSanctions.

## The two load-bearing properties

1. **Independence (the floor must not grade itself).** `evaluate_floor` takes the matcher as an
   **injected `score_fn`**; `benchmark.py` references **no** scoring symbol and bakes in **no** model. The
   real run wires `resolution.splink_model.score_pairs` from *outside* the module (an ops/eval step);
   tests pass a stub. This is the import-purity invariant (INV-IMPORT-PURITY), the slice-3 analog of
   ADR 0079's N1.

2. **Non-contamination (the floor must be independent of our training/silver labels).** OS-Pairs and our
   canonical-anchor silver labels **both draw on OpenSanctions**, so a naive floor would double-count
   entities the matcher (or a future calibration) already saw. The **contamination guard**
   (`drop_contaminated`) excludes every benchmark pair whose entity identity overlaps our silver/gold
   partition and **reports the count dropped** — never silently truncates.

## Decision

**New pure module `src/worldmonitor/resolution/benchmark.py`.** No DB, no graph, no persistence. The
floor is computed and **returned in memory**; nothing is written to `er_gold_pair` or anywhere live.

### D1 — The common shape

```python
BENCHMARK_SOURCE = "external_benchmark"          # the floor's source tag (sub-source records which set)

@dataclass(frozen=True, slots=True)
class BenchmarkPair:
    left: FtmEntity            # built via ontology.ftm.make_entity (FtM-native)
    right: FtmEntity
    label: str                 # "match" | "non_match"
    source: str = BENCHMARK_SOURCE
    sub_source: str = ""       # "os_pairs" | "febrl1".."febrl4"
```

### D2 — OS-Pairs importer (`load_os_pairs`)

```python
OS_PAIRS_URL = "https://data.opensanctions.org/contrib/training/pairs-20251209.json.gz"

def load_os_pairs(source: str | os.PathLike[str] | Iterable[str]) -> Iterator[BenchmarkPair]: ...
def fetch_os_pairs(cache_dir: Path | None = None) -> Path: ...      # download-on-demand; NOT used in tests
```

- `load_os_pairs` accepts a **local path** (`.json` or `.json.gz`, transparently decompressed) **or a
  line-iterable**, so tests pass a tiny in-memory fixture and **never touch the network**. Each line is
  one JSON object; `left`/`right` are FtM entity dicts built through
  `ontology.ftm.make_entity` (extra OpenSanctions keys like `caption`/`datasets` are ignored by FtM).
- **Judgement map (INV-JUDGEMENT-MAP):** `"positive" -> "match"`, `"negative" -> "non_match"`. **Any
  other value** (`"unsure"`, `"no_judgement"`, missing) is **skipped, never coerced** into a label.
- `fetch_os_pairs` is the **pull-only, download-on-demand** helper: it streams `OS_PAIRS_URL` (via the
  already-present `httpx`) into a local cache path (default under a user cache dir / `WM_BENCHMARK_CACHE`)
  iff absent, and returns the path. It is **never called by tests**; the bulk file is **`.gitignored` and
  never committed** (licence + hermeticity). Attribution to OpenSanctions / CC BY-NC 4.0 lives in the
  module header.

### D3 — Febrl importer (`load_febrl`)

```python
def load_febrl(dataset: str = "febrl1", *, negatives: int = 0, seed: int = 0) -> Iterator[BenchmarkPair]: ...
def _febrl_record_to_entity(record_id: str, row: Mapping[str, Any]) -> FtmEntity: ...   # testable mapper
```

- `recordlinkage` is **lazy-imported inside the function**; if absent it raises a clear, actionable
  `ImportError` naming the optional group (`pip/uv ... benchmark`). It is **never** a runtime dependency.
- Uses `recordlinkage.datasets.load_febrl{1..4}(return_links=True)` → `(DataFrame, links MultiIndex)`.
  Each record row maps to an FtM **`Person`** via `_febrl_record_to_entity` (given/surname → `name`,
  date_of_birth → `birthDate`, etc.). Each gold link yields a `match` `BenchmarkPair`; a **seeded**
  sample of `negatives` non-link pairs yields `non_match` pairs (default `negatives=0` ⇒ matches only,
  the deterministic minimum). The split-out `_febrl_record_to_entity` is the mapper a hermetic unit test
  exercises with a synthetic row, with **no `recordlinkage` import required**.

### D4 — Contamination guard (`identity_keys` + `drop_contaminated`) — LOAD-BEARING

```python
def identity_keys(entity: FtmEntity) -> frozenset[str]:
    # {entity.id} ∪ { values of every silver.ANCHOR_PROPERTIES property on `entity` }

def drop_contaminated(
    pairs: Iterable[BenchmarkPair],
    our_keys: AbstractSet[str],
) -> tuple[list[BenchmarkPair], int]:          # (kept, n_dropped) ; logs n_dropped ; never silent
```

- `our_keys` is the set of identity keys present in **our** silver/gold partition — assembled by the
  caller (the ops/eval step) from `er_gold_pair` entity ids **plus** the canonical anchors of those
  records. The guard stays pure and DB-free so it is unit-testable in isolation.
- For each pair, if `identity_keys(left) | identity_keys(right)` intersects `our_keys`, the pair is
  **dropped and counted**. The guard guarantees `len(kept) + n_dropped == len(input)` (no silent
  truncation) and **logs** the dropped count to `logging.getLogger(__name__)` (stderr discipline, never
  stdout). `identity_keys` reuses `silver.ANCHOR_PROPERTIES` so the anchor set has a **single source of
  truth** shared with the silver deriver.

### D5 — `evaluate_floor` (the floor number)

```python
@dataclass(frozen=True, slots=True)
class FloorMetrics:
    precision: float
    recall: float
    f1: float
    over_merge_rate: float
    n: int                       # pairs scored AFTER decontamination
    n_dropped_contaminated: int

def evaluate_floor(
    pairs: Iterable[BenchmarkPair],
    score_fn: Callable[[FtmEntity, FtmEntity], float],
    threshold: float,
    *,
    contamination_keys: AbstractSet[str] = frozenset(),
) -> FloorMetrics: ...
```

- Runs `drop_contaminated(pairs, contamination_keys)` **first** (so the floor is *always* on
  decontaminated data; `n_dropped_contaminated` is surfaced, never hidden). For each kept pair,
  `predicted_match = score_fn(left, right) >= threshold`, compared to `label == "match"`:
  `precision = TP/(TP+FP)`, `recall = TP/(TP+FN)`, `f1` via **`eval._harmonic_mean`** (reused, not
  re-implemented, so the divide-by-zero guard is shared).
- **`over_merge_rate` (pairwise floor) = `FP / (TP+FP)`** — the fraction of pairs our matcher *predicts
  match* that the benchmark calls **distinct** (the catastrophic-merge floor signal); `0.0` when there
  are no predicted matches. This is the pairwise analog of `eval.over_merge_rate` (which is cluster-level
  over the gold partition); the docstring states the distinction explicitly.
- `score_fn` is **injected** (INV-IMPORT-PURITY): tests pass a stub; the real run wires
  `score_pairs([left, right])` from outside the module. `benchmark.py` contains no scoring logic.

### D6 — Out of the live path

This gate computes a returned-only report. It writes **nothing** to `er_gold_pair`, `merge.py`, any
threshold, any EM weight, the Splink model, or the graph. Promotion of any value remains the separate,
**human-sign-off-gated** slice — explicitly forbidden here.

## Alternatives considered

- **A commercial-licensed ER benchmark (e.g. a vendor record-linkage corpus):** rejected — WorldMonitor
  is non-commercial and self-hosted; CC BY-NC (OS-Pairs) + BSD (Febrl) cover us without a paid licence.
- **Skip the external benchmark, trust silver alone:** rejected — silver is derived from the same
  OpenSanctions data we ingest; an *independent* third-party-labelled floor is the whole point of this
  slice (it answers a question silver structurally cannot).
- **Persist benchmark pairs into `er_gold_pair` (a `source="external_benchmark"` partition):** rejected
  for this gate — the floor is a *report*, not a training/calibration label, and persisting it risks a
  future promotion step quietly consuming externally-licensed, potentially-contaminated rows. Keeping the
  floor in-memory enforces the sanity-only boundary. (Revisit trigger 4.)
- **Bake `score_pairs` into `evaluate_floor`:** rejected — that reintroduces the self-grading problem and
  couples the floor to one model. Injection keeps the floor honest and the module import-pure.
- **Make `recordlinkage` a hard dependency:** rejected — it pulls scikit-learn/jellyfish and is only
  needed for the optional Febrl floor. It goes in an **optional** group, lazy-imported, with an actionable
  error when absent.
- **Run the full 755k OS-Pairs set in CI / a test:** rejected — that is a ~409 MB download and a heavy
  scoring run; it is an **ops/eval activity** (documented below), never a unit test. Tests use a tiny
  synthetic fixture.

## Consequences

- The harness gains an **independent external floor**: a public, third-party-labelled precision / recall
  / over-merge number read against the published `RegressionV1` 91.33 % F1 reference — a sanity check
  that silver (internal) cannot provide.
- **Invariant-touching ⇒ a `@given` property test is mandatory** (CLAUDE.md build discipline). The
  load-bearing ones are the contamination-guard soundness property and the floor-math oracle property
  (§Property-test plan).
- **No new runtime dependency, no schema change, no migration, no `er_gold_pair` write.** `recordlinkage`
  is dev/optional only.
- **Sovereignty-safe:** `fetch_os_pairs` is pull-only download-on-demand; the bulk file is `.gitignored`
  and never committed; our data never leaves. Attribution is in the module header.
- The floor is **sanity-only:** by construction it cannot move a threshold (returned-only, no live
  write). Promotion stays the separate human-sign-off slice.

## Out of scope (recorded)

- **Slice 1** — running the connectors on the host to build the real multi-source corpus (an ops run).
- **Slice 4** — the label-sufficiency report (labels-by-source + boundary coverage + metric CIs).
- **The full 755k OS-Pairs scoring run** — an **ops/eval activity** (`fetch_os_pairs` → `load_os_pairs` →
  `evaluate_floor` with `score_fn = score_pairs` and `contamination_keys` from `er_gold_pair`), run on
  the host, **not** in tests.
- **Any promotion** — threshold / EM-weight / `merge.py` change. Person-affecting,
  human-sign-off-gated (CLAUDE.md); **strictly forbidden in this gate.** The live ER decision path is
  untouched.
- **Persisting the floor / benchmark pairs to the DB**, and committing **real OpenSanctions or Febrl bulk
  data** — both excluded (sanity-only boundary + licence/hermeticity).

## Property-test plan (`tests/property/test_prop_benchmark.py`, all `@given`)

Synthetic FtM entities / `BenchmarkPair`s only — **no** real OpenSanctions or Febrl bulk data.

- **P-GUARD-SOUND (load-bearing)** — for any generated pair list + `our_keys`, `drop_contaminated`
  returns `(kept, n_dropped)` with `len(kept) + n_dropped == len(input)`, **every** kept pair's
  `identity_keys` are disjoint from `our_keys`, and **every** dropped pair intersected `our_keys` (no
  under-drop, no over-drop, exact count).
- **P-GUARD-EMPTY** — with `our_keys == ∅`, nothing is dropped (`n_dropped == 0`, `kept == input`).
- **P-JUDGEMENT-TOTAL** — over lines with judgement drawn from
  `{positive, negative, unsure, no_judgement, <garbage>}`, `load_os_pairs` emits a pair **iff** the
  judgement is `positive`/`negative`, with the correct label; all others are skipped.
- **P-FLOOR-MATH** — for a generated labelled set and an **oracle** `score_fn` (returns 1.0 for `match`,
  0.0 for `non_match`) at `threshold=0.5`: `precision == recall == f1 == 1.0`, `over_merge_rate == 0.0`,
  `n == len(pairs)`. For an **inverted** oracle: `precision == recall == 0.0`. General check:
  `over_merge_rate == FP/(TP+FP)` (or 0.0 when `TP+FP == 0`).
- **P-CONTAM-IN-FLOOR** — injecting a key from a pair into `contamination_keys` drops exactly that pair
  from the floor (`n_dropped_contaminated` increments by one; `n` decrements by one) and never changes
  the metric for the remaining pairs.
- **P-IMPORT-PURITY (proves INV-IMPORT-PURITY)** — `inspect.getsource(benchmark)` references no scoring
  symbol (`score_pairs`, `match_probability`, `probability`, `ScoredPair`); `evaluate_floor`'s signature
  carries `score_fn` (matcher is injected, not imported).

## Unit-test plan (`tests/unit/test_benchmark.py`, all Docker-free + offline)

- **OS-Pairs import** — a tiny hand-authored fixture (a few lines in the OS-Pairs JSON shape: one
  `positive`, one `negative`, one `unsure`) parsed via `load_os_pairs(<list-of-lines>)`: correct
  label mapping, `source == "external_benchmark"`, `sub_source == "os_pairs"`, FtM `left`/`right` built,
  the `unsure` line skipped. **No real OpenSanctions data.**
- **`evaluate_floor` math** — a small known labelled set + a deterministic stub `score_fn`: assert
  `precision`/`recall`/`f1`/`over_merge_rate`/`n`/`n_dropped_contaminated` against hand-computed values.
- **Contamination guard** — a set with one pair whose entity id (or anchor) is in `our_keys`: that pair
  is dropped, `n_dropped == 1`, the rest survive.
- **Febrl mapper (ALWAYS runs)** — `_febrl_record_to_entity` on a synthetic record dict yields a valid
  FtM `Person` with the expected `name`/`birthDate` (no `recordlinkage` import needed).
- **Febrl loader (gated)** — under `pytest.importorskip("recordlinkage")`, `load_febrl("febrl1")` yields
  `match` pairs with `sub_source == "febrl1"` over the bundled offline dataset; and a separate test
  asserts `load_febrl` raises the actionable `ImportError` when `recordlinkage` is monkeypatched absent.

## Reversibility

**Reversible** (a report-value module + an optional dev dependency; no live-path or data-shape lock-in).
Per the CLAUDE.md reversible-decision discipline the sensible defaults are picked and we proceed — no
human fork is manufactured.

**Reversal cost: low** — delete `benchmark.py` and the `benchmark` dependency group; nothing in the
app/driver imports them (the floor is an ops/eval call, not wired into any live path; no DB rows are
written, so there is nothing to clean up).

**Reversible defaults recorded:**
- the **benchmark choice** (OS-Pairs primary + Febrl secondary) and the pinned OS-Pairs **snapshot URL**;
- the **contamination rule** (`identity_keys` = entity id ∪ `silver.ANCHOR_PROPERTIES` values; drop on
  any overlap);
- the **`over_merge_rate` definition** (pairwise `FP/(TP+FP)`);
- `recordlinkage` as an **optional/dev** dependency (lazy-imported);
- Febrl `negatives=0` default and the seeded negative-sampling scheme.

**Revisit triggers:**
1. **Commercial pivot** ⇒ CC BY-NC OS-Pairs is no longer usable ⇒ **drop OS-Pairs, keep Febrl** (BSD) and
   acquire a paid/commercial-OK ER benchmark; the importer seam + `evaluate_floor` are reused unchanged.
2. OpenSanctions publishes a **newer pairs snapshot** ⇒ bump `OS_PAIRS_URL` (a one-line change; the floor
   is a moving sanity check, not a frozen artefact).
3. The floor sits **far below** the 91.33 % F1 reference on decontaminated data ⇒ that is a real matcher
   defect to investigate (the floor did its job) — open a finding, not a threshold move.
4. A move to **promote** any value derived from the floor, or to **persist** benchmark pairs as
   calibration labels ⇒ that is the separate, human-sign-off slice — open a new ADR; this one does not
   authorise it.
5. Contamination drop-rate is **surprisingly high/low** (the floor would be tiny / suspiciously clean) ⇒
   revisit `identity_keys` (e.g. widen/narrow the anchor set, or key on `referents`).

## Slice plan (independently mergeable)

- **Slice 3a — the guard + the floor (the heart).** `identity_keys` + `drop_contaminated` +
  `evaluate_floor`/`FloorMetrics` + `BenchmarkPair` + `tests/property/test_prop_benchmark.py`
  (P-GUARD-*, P-FLOOR-MATH, P-CONTAM-IN-FLOOR, P-IMPORT-PURITY) + the `evaluate_floor`/guard unit tests.
  Stands alone (no importer needed to test the math/guard).
- **Slice 3b — OS-Pairs importer.** `OS_PAIRS_URL` + `load_os_pairs` + `fetch_os_pairs` +
  `tests/unit/test_benchmark.py` OS-Pairs cases + `tests/property` P-JUDGEMENT-TOTAL. Order-independent.
- **Slice 3c — Febrl importer + optional dep.** `load_febrl` + `_febrl_record_to_entity` + the
  `benchmark` dependency group in `pyproject.toml` + the febrl unit tests + `docs/GATE_LEDGER.md` +
  `docs/40_ROADMAP.md` + flip this ADR to `accepted` on merge.
