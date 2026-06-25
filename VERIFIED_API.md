# VERIFIED_API.md — Gate A (ER Measurement Harness + EM Weights)

> **Verify-before-code artefact (BLOCKING).** Every Splink / scipy call the harness binds is
> recorded here **verbatim**, confirmed against BOTH (a) the installed package via
> `inspect.signature` (the authoritative runtime — exactly what executes) and (b) the PRIMARY
> Splink 4.x docs. No implementation may bind a Splink/scipy symbol absent from this file, or
> bound to the wrong namespace. (Gate spec §2; judge DENY D4.)

## Environment (verified 2026-06-25)

- **splink 4.0.16** (`pyproject.toml` pins `splink>=4.0`; installed in `.venv`)
- **scipy 1.18.0** (currently transitive via splink → **promote to a declared dep** this gate)
- **duckdb 1.5.4** (already declared)
- Splink is imported only in `src/worldmonitor/resolution/splink_model.py:24`
  (`from splink import DuckDBAPI, Linker, SettingsCreator, block_on`).

## Namespacing (load-bearing)

Training / evaluation / table-management methods hang off **accessor sub-objects** of a
constructed `Linker`, NOT the bare `Linker`:

- `linker.training.*`   (internal class `LinkerTraining`)
- `linker.evaluation.*` (internal class `LinkerEvalution` — note: misspelt upstream, but the
  public accessor is `linker.evaluation`; never reference the class name directly)
- `linker.table_management.*` (internal class `LinkerTableManagement`)
- `linker.inference.*` (already used: `linker.inference.predict(...)`)
- `linker.misc.*` (internal class `LinkerMisc`) — trained-model (de)serialisation

A binding to the wrong namespace (e.g. `linker.estimate_u_*` on the bare `Linker`) is a DENY.

---

## Splink — training (`linker.training.*`)

Primary doc: <https://moj-analytical-services.github.io/splink/api_docs/training.html>
Source: `splink/internals/linker_components/training.py`

```python
# verbatim from inspect.signature (installed 4.0.16); confirmed against primary docs
linker.training.estimate_u_using_random_sampling(max_pairs: float = 1000000.0, seed: int = None) -> None
```
- `max_pairs`: max pairwise comparisons to sample for the u estimate (larger = more accurate, slower).
- `seed`: **set for reproducible u probabilities** (the gold harness MUST pass a fixed seed).
- Returns `None`; mutates the linker's estimated u params in place.

```python
linker.training.estimate_parameters_using_expectation_maximisation(
    blocking_rule: Union[str, BlockingRuleCreator],
    estimate_without_term_frequencies: bool = False,
    fix_probability_two_random_records_match: bool = False,
    fix_m_probabilities: bool = False,
    fix_u_probabilities: bool = True,
    populate_probability_two_random_records_match_from_trained_values: bool = False,
) -> EMTrainingSession
```
- `blocking_rule`: a `block_on(...)` creator (or SQL string) generating the pairs EM trains on,
  e.g. `block_on("country")`.
- **`fix_u_probabilities` defaults `True`** → EM updates only **m**; therefore u MUST be estimated
  first via `estimate_u_using_random_sampling(...)`. This is the spec's "EM for m, direct/u-first"
  order: (1) `estimate_u_using_random_sampling(seed=...)`, then (2) one or more
  `estimate_parameters_using_expectation_maximisation(block_on(...))` calls.
- Returns an `EMTrainingSession` (iteration history); the trained m/u live on the linker's settings.

---

## Splink — evaluation (`linker.evaluation.*`)

Primary doc: <https://moj-analytical-services.github.io/splink/api_docs/evaluation.html>
Source: `splink/internals/linker_components/evaluation.py`

```python
linker.evaluation.accuracy_analysis_from_labels_table(
    labels_splinkdataframe_or_table_name: "str | SplinkDataFrame",
    *,
    threshold_match_probability: float = 0.5,
    match_weight_round_to_nearest: float = 0.1,
    output_type: Literal["threshold_selection", "roc", "precision_recall", "table", "accuracy"] = "threshold_selection",
    add_metrics: List[Literal["specificity", "npv", "accuracy", "f1", "f2", "f0_5", "p4", "phi"]] = [],
) -> "Union[ChartReturnType, SplinkDataFrame]"
```
- The harness calls this with **`output_type="table"`** — the dataframe-bearing form — and reads
  the returned `SplinkDataFrame` (`fp` / `fn` / `match_probability` columns) to build the PR curve
  and derive the cost-sensitive recommended threshold. (The named `"precision_recall"` /
  `"threshold_selection"` types return *charts*, not frames, so `"table"` is the correct value for
  programmatic PR-curve access. The full `output_type` value set is confirmed verbatim via
  `inspect.signature` above.)
- **PAIRWISE + BLOCKING-CONDITIONAL caveat (gate spec §5.3):** this metric is computed over the
  pairs Splink's blocking actually generated. A gold pair outside every blocking rule is INVISIBLE
  here — so the cluster metrics (B³/CEAFe/over_merge_rate) MUST iterate the full gold partition,
  not this table.

```python
linker.evaluation.prediction_errors_from_labels_table(
    labels_splinkdataframe_or_table_name: "str | SplinkDataFrame",
    include_false_positives: bool = True,
    include_false_negatives: bool = True,
    threshold_match_probability: float = 0.5,
) -> SplinkDataFrame
```
- Optional helper for error analysis (false-merge / false-split inspection).

---

## Splink — table management (`linker.table_management.*`)

```python
linker.table_management.register_labels_table(input_data, overwrite=False)
```
- `input_data`: a (pandas) dataframe of clerical labels; returns a registered `SplinkDataFrame`
  to pass to `accuracy_analysis_from_labels_table`.
- **Labels-table schema (primary docs):** `source_dataset_l | unique_id_l | source_dataset_r |
  unique_id_r | clerical_match_score`. `source_dataset` / `unique_id` must match the settings
  dict; **for `dedupe_only` links the `source_dataset_*` columns may be omitted** (our model is
  `link_type="dedupe_only"`, `splink_model.py:407`).

---

## Splink — misc (`linker.misc.*`)

Source: `splink/internals/linker_components/misc.py`

```python
# verbatim from inspect.signature (installed 4.0.16)
linker.misc.save_model_to_json(out_path: "str | None" = None, overwrite: bool = False) -> "dict[str, Any]"
```
- Serialises the trained settings (m/u weights, blocking, comparisons) to a JSON-serialisable
  `dict`. `train_candidate_model` (`splink_model.py:458`) returns this dict — with `out_path=None`
  it returns the dict **without writing a file** — as the loadable/evaluable EM candidate artefact;
  tests reconstitute a `Linker` from it. Bound on `linker.misc.*` (not the bare `Linker`).

## scipy — CEAFe optimal alignment

Primary doc: <https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linear_sum_assignment.html>

```python
from scipy.optimize import linear_sum_assignment
linear_sum_assignment(cost_matrix, maximize=False)  # -> (row_ind, col_ind)
```
- CEAFe maximises total φ4 similarity → call with `maximize=True` on the φ4 matrix, OR negate and
  use the default. Returns aligned `(row_ind, col_ind)` index arrays.
