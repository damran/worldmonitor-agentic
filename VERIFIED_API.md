# VERIFIED_API.md — verified external-API bindings (cumulative)

> **Verify-before-code artefact (BLOCKING).** Every external library call the resolver/harness binds
> is recorded here **verbatim**, confirmed against the installed package via `inspect.signature`
> (the authoritative runtime — exactly what executes) and, where available, the primary docs. No
> implementation may bind a symbol absent from this file, or bound to the wrong module/namespace.
> Sections are added per gate (Gate A: Splink/scipy · Gate B-front: nomenklatura/rigour).

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

---

# Gate B-front — nomenklatura / rigour (anchor-preferred stable IDs)

Verified 2026-06-25 against the installed **nomenklatura 4.10.0** + **rigour 2.1.2** via
`inspect.signature` (authoritative runtime). The API is namespaced under `nomenklatura/resolver/`
— `Identifier` is NOT a top-level export. `merge.py` imports `import nomenklatura as nk` and
`from nomenklatura import Judgement`.

## nomenklatura — `Resolver` (`nomenklatura/resolver/resolver.py`)

```python
# verbatim from inspect.signature (installed 4.10.0)
Resolver.make_default(engine: Optional[sqlalchemy.engine.base.Engine] = None) -> "Resolver[SE]"   # classmethod
Resolver.decide(self, left_id: Union[str, Identifier], right_id: Union[str, Identifier], judgement: Judgement, user: Optional[str] = None, score: Optional[float] = None) -> Identifier
Resolver.get_canonical(self, entity_id: str) -> str
Resolver.get_referents(self, canonical_id: str, canonicals: bool = True) -> Set[str]
Resolver.get_judgement(self, entity_id: Union[str, Identifier], other_id: Union[str, Identifier]) -> Judgement
Resolver.connected(self, node: Identifier) -> Set[Identifier]
Resolver.remove(self, node_id: Union[str, Identifier]) -> None
Resolver.explode(self, node_id: Union[str, Identifier]) -> Set[str]
Resolver.begin(self, load_edges: bool = True) -> None
Resolver.commit(self) -> None
Resolver.rollback(self) -> None
```
- `get_canonical` returns `max(connected(node)).id` **iff that max is canonical**, else the node's own
  id. The "max" is by `Identifier.__lt__` = `(weight, id)` → a connected **QID wins** (weight 3).
- `get_referents(canonical_id)` = the set of ids that resolve to `canonical_id` (the superseded-id
  traceability primitive — the gate's `canonical_alias` ledger mirrors this durably).
- `decide(..., Judgement.POSITIVE, ...)` canonicalises (mints an `Identifier` only if no connected node
  is already canonical); this is the membership step the ADR-0037 resolve uses (already bound today at
  `merge.py:147,178`). `remove`/`explode` are the split primitives (slice-2).
- **`get_canonical`/`Identifier` weighting is QID-ONLY** — LEI/regNo/taxNo are weight-1 raw ids the
  resolver never deterministically prefers. → the richer durable precedence (QID>LEI>regNo>taxNo) is
  derived OUTSIDE the resolver in `resolution/canonical.py` (spec §3). The resolver decides membership;
  `canonical.py` decides the durable id.

## nomenklatura — `Identifier` (`nomenklatura/resolver/identifier.py`)

```python
Identifier.PREFIX = "NK-"
Identifier.make(value: Optional[str] = None) -> "Identifier"     # classmethod -> f"{PREFIX}{value or shortuuid.uuid()}"
# __init__: weight=1; weight=2 if id.startswith("NK-"); weight=3 if is_qid(id); canonical = weight > 1
# __lt__: orders by (weight, id)
```
- The nomenklatura canonical-id prefix is **`NK-`** (distinct from our `wmc-` fingerprint and our new
  `wm-mint-`/`qid:`/`lei:`/`regno:`/`taxno:` durable forms). We do NOT reuse `NK-`.

## nomenklatura — `Judgement` (`nomenklatura/judgement.py`)

```python
class Judgement(Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    UNSURE = "unsure"
    NO_JUDGEMENT = "no_judgement"
```
- British spelling `Judgement`; member `NO_JUDGEMENT`. Positive-before-negative ordering in
  `get_judgement` is the ADR-0037 transitive primitive.

## rigour — QID validation (`rigour.ids.wikidata`)

```python
from rigour.ids.wikidata import is_qid
is_qid("Q42") -> True          # a Wikidata QID
is_qid("5493001KJTIIGC8Y1R12") -> False   # an LEI is not a QID
```
- `pick_anchor`'s QID tier validates with `is_qid` (same check nomenklatura's `Identifier` uses for
  its weight=3). FtM props `wikidataId` / `leiCode` / `registrationNumber` / `taxNumber` are all FtM
  type `identifier`; regNo/taxNo are normalised via the FtM `identifier` type per ADR 0039.

---

# Gate C — followthemoney StatementEntity (value-level provenance)

Verified 2026-06-25 against installed **followthemoney 4.9.2** via `inspect.signature`. `StatementProxy`
does **NOT** exist (binding it is a DENY). `StatementEntity`/`Statement`/`Dataset` import top-level.

```python
from followthemoney import StatementEntity, Statement, Dataset
# StatementEntity -> followthemoney.statement.entity.StatementEntity
# Statement       -> followthemoney.statement.statement.Statement

Statement.__init__(self, entity_id: str, prop: str, schema: str, value: str, dataset: str,
    lang: Optional[str] = None, original_value: Optional[str] = None, first_seen: Optional[str] = None,
    external: bool = False, id: Optional[str] = None, canonical_id: Optional[str] = None,
    last_seen: Optional[str] = None, origin: Optional[str] = None)
#   dataset = the source  (= Provenance.source_id)
#   origin  = raw pointer (= Provenance.source_record)
#   first_seen = timestamp (= Provenance.retrieved_at); canonical_id defaults to entity_id

StatementEntity.merge(self, other: EntityProxy) -> StatementEntity
#   other is a StatementEntity -> re-canonicalizes each statement to self.id + add_statement
#     (per-prop SET union) -> ALL sources' (prop,value,dataset) statements aggregate under self.id;
#     lineage INTACT. other is a plain ValueEntity -> unsafe_add, lineage-LOSING.
#     >>> Gate C MUST feed it StatementEntity built with per-statement dataset/origin/first_seen. <<<
StatementEntity.from_statements(dataset: Dataset, statements: Iterable[Statement]) -> StatementEntity
StatementEntity.add_statement(self, stmt: Statement) -> None   # per-prop set union
StatementEntity.get_statements(self, prop, quiet: bool = False) -> List[Statement]
StatementEntity.statements                                      # property -> all statements
Dataset(data: dict)                                             # from_statements needs a Dataset
```
- **Value-set-invariance (the §9 fence, achievable):** `add_statement`'s per-prop `set` union yields a
  fused VALUE set identical to `ValueEntity.merge`'s union — so switching to StatementEntity adds lineage
  WITHOUT changing which values survive. The gate requires a test asserting byte-for-byte value-set parity.

---

# Gate D — followthemoney-graph (ftmg) edge materialization (abstract Thing-range)

Verified 2026-06-25 against installed **followthemoney-graph 0.1.0** via `inspect.signature`. Imported
only in `src/worldmonitor/graph/writer.py`. The fork (`graph/ftmg_fork/`) is a THIN OVERRIDE of two
generators; everything else is imported from upstream ftmg.

```python
# verbatim from inspect.signature (installed 0.1.0); source ftmg/transform.py
generate_entity_links(config: ftmg.config.Configuration, proxy: followthemoney.entity.ValueEntity) -> Generator[QueryBatch, None, None]   # transform.py:198 — PRIMARY override (Sanction.entity drop site, lines 227-229)
generate_edge_entity(config: ftmg.config.Configuration, proxy: followthemoney.entity.ValueEntity)  -> Generator[QueryBatch, None, None]   # transform.py:291 — secondary override (UnknownLink drop site, lines 317-322)
generate_node_entity(config, proxy)   -> Generator[QueryBatch, None, None]   # import unchanged
generate_topic_labels(config, proxy)  -> Generator[QueryBatch, None, None]   # import unchanged
QueryBatch          # NamedTuple, fields = ('query', 'params')
ENTITY_LABEL = "Entity"   # the base node label every node carries — the fork's MATCH-label fallback when the range schema is abstract

from followthemoney import registry
registry.entity.name == "entity"   # the prop-type check: a prop points to another entity iff prop.type == registry.entity (or prop.type.name == "entity")
```
- **Upstream behaviour being overridden (the drop):** `generate_entity_links` already filters
  `prop.type == registry.entity` (transform.py:220) but then keys the target lookup on the RANGE SCHEMA —
  `config.nodes.schemata.get(prop.range.name)` (227) — which is `None` for the abstract `Thing` because
  `ftmg/config.py:67-70` registers only `not schema.edge and not schema.abstract` (and `config.py:73`
  *raises* if you try to register an abstract schema). So the fork CANNOT register `Thing`; it must fall
  back to the `ENTITY_LABEL="Entity"` MATCH label when the range is abstract/absent. Same shape for
  `generate_edge_entity` (317-322) for `UnknownLink`.
- **Idempotent edge:** project via the `MERGE (s)-[:REL]->(t)` form keyed on (source durable id, target
  durable id, rel-type) — re-projection is idempotent. Endpoints are the durable canonical ids (ADR 0044);
  the new edge carries the asserting entity's `prov_*` (G1) and is realigned by `writer._align_entity_link_ids`.
