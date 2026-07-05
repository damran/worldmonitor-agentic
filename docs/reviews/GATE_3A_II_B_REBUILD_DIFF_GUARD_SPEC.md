# Gate 3a-ii-B — the scheduled projection rebuild-and-diff guard

- Gate: **3a-ii-B** (statement-log projector line; the operational guard half of Gate 3a-ii)
- Branch: `feat/gate-3a-ii-b-rebuild-diff-guard` (off `origin/master` @ the 3a-ii-A merge, `bbac0ae`)
- ADR: `docs/decisions/0102-projection-rebuild-diff-guard.md` (PROPOSED → flips ACCEPTED at gate
  approval; realises ADR 0101 §Decision B; records the B4 resolution)
- Scope contract: `.claude/gate.scope` (INV-1..8 below)
- Person-affecting: **NO** — read-only w.r.t. the live graph (the guard NEVER writes live; the D3 fence
  guarantees it); makes no merge/ER decision; changes no threshold/score/guard/erasure; observability
  only, default-off. The ADR header carries the completed ADR-0097 human cosign (Mithat, 2026-07-05) because the
  diff edits a `resolution/**` module (`projector.py` + the new `resolution/divergence.py`).
- Migration: **NONE** — the guard reuses the existing `ProjectionCheckpoint` table under a new `id`
  value (`"projection-diff"`); `String(64)` PK, `db/models.py:413`.
- `@given` property suite: **MANDATORY** — this gate touches the **projection-integrity invariant** (the
  divergence measure that certifies the fold reproduces the live graph).

---

## 1. WHAT THIS GATE BUILDS (from ADR 0102 §D1–D10 — build to these, do NOT relitigate)

1. **`src/worldmonitor/resolution/divergence.py` (new, PURE — no Neo4j import).** Snapshot dataclasses +
   the `ProjectionDivergence` result dataclass + `measure_divergence(...)` (D6/D7).
2. **`src/worldmonitor/graph/snapshot.py` (new, read-only).** `read_graph_snapshot(client)` — the one
   src-side whole-graph reader (D9).
3. **`src/worldmonitor/resolution/projector.py`** — additive `checkpoint_id` param on `project()` (D5) +
   the pure `build_survivor_of` export the guard reuses (D9). `reconstruct_entities` and the write path
   stay byte-unchanged.
4. **`src/worldmonitor/runner/driver.py`** — the guard hook + fence + cadence + cache + collector wiring
   + error isolation (D3/D8/D9/D10).
5. **`src/worldmonitor/metrics/collector.py`** — the `worldmonitor_projection_divergence` gauge (+ the
   companion `..._last_run_timestamp` liveness gauge) with the `-1` sentinel, behind an optional ctor
   kwarg (D7).
6. **`src/worldmonitor/settings.py`** — the `projection_diff_*` settings block (D2). No `model_validator`;
   `validate_production_secrets()` untouched (FROZEN, ADR 0061).
7. **`deploy/prometheus/alerts/worldmonitor.rules.yml`** + **`deploy/prometheus/tests/worldmonitor.rules.test.yml`**
   — the `ProjectionDivergenceHigh` **warning** alert + promtool fire/no-fire fixtures (D7).
8. **Tests** (§4) + **`docs/decisions/0102-*.md`** present + re-run `gen_adr_index.py` so
   `docs/decisions/README.md` gains the `0102` row.

---

## 2. THE PRECISE DEFINITIONS (the intellectual core — state exactly for the builder)

### 2.1 Snapshots (`resolution/divergence.py`, frozen dataclasses)

- `NodeSnapshot(id: str, labels: frozenset[str], props: dict[str, frozenset[str]])`
- `EdgeSnapshot(type: str, src: str, dst: str, props: dict[str, frozenset[str]])`
- `GraphSnapshot(nodes: tuple[NodeSnapshot, ...], edges: tuple[EdgeSnapshot, ...])`

Every property value is a **set of strings** (Neo4j scalar → `{str(v)}`; list → `{str(x) for x in v}`,
mirroring `graph_signature`'s `_stable_val` list handling). The reader (§2.4) does the coercion; the
measure treats props as opaque string value-sets.

### 2.2 The measure (`measure_divergence(live, fold, survivor_of, *, computed_at) -> ProjectionDivergence`)

**Excluded compared-property predicate** `_excluded(p)`: `p == "id"` OR `p.startswith("wm_anchor_")` (E2)
OR `p == "datasets"` (E4) OR `p.startswith("prov_")` (prov scalars + `prov_witnesses`, D6-ii) OR
`p == "caption"` (D6-iii — picked scalar, not union-monotone; its inputs, the name values, stay
compared). Labels are **not** props and are **not** compared (D6-i).

**Per-prop subset test** `_props_subset(live_props, fold_props, survivor_of)`: for every prop `p` in
`live_props` with `not _excluded(p)`, require
`{survivor_of(v) for v in live_props[p]} ⊆ {survivor_of(v) for v in fold_props.get(p, frozenset())}`;
return `False` on the first failure.

**Node loop.** Index fold nodes by id (`{F.id: F}`). A live node `L` is **explained** iff a fold node
`F` exists with `F.id == survivor_of(L.id)` AND `_props_subset(L.props, F.props, survivor_of)`. Count the
unexplained.

**Edge loop.** Index fold edges by key `(E.type, survivor_of(E.src), survivor_of(E.dst))` →
list of fold edges. A live edge `L` is **explained** iff some fold edge `F` under key
`(L.type, survivor_of(L.src), survivor_of(L.dst))` satisfies `_props_subset(L.props, F.props,
survivor_of)`. Count the unexplained.

`ProjectionDivergence(unexplained_nodes, unexplained_edges, live_nodes=len(live.nodes),
live_edges=len(live.edges), computed_at)`, `total = unexplained_nodes + unexplained_edges`.

`survivor_of` is the identity on literals and already-survivor ids, so normalising both sides is a free
no-op for name/date values and idempotent on the fold side — this is what makes the measure E1-tolerant
without a second referent implementation.

### 2.3 The `build_survivor_of` export (`projector.py`, D9)

Extract `projector.py:275-298` (the F2 supersession-only ledger read + the cycle-guarded fixed-point
`survivor_of`) verbatim into `build_survivor_of(session: Session) -> Callable[[str], str]`; have
`project()` call `survivor_of = build_survivor_of(session)`. **Pure, behaviour-preserving** — the ledger
query, the `alias != canonical` filter, the ORDER BY, and the walk are unchanged.

### 2.4 The reader (`graph/snapshot.py`, D9)

`read_graph_snapshot(client) -> GraphSnapshot` runs two `execute_read` queries (never a write):
`MATCH (n) WHERE n.id IS NOT NULL RETURN n.id AS nid, labels(n) AS lbls, properties(n) AS props` and
`MATCH (a)-[r]->(b) WHERE a.id IS NOT NULL AND b.id IS NOT NULL RETURN type(r) AS rtype, a.id AS src,
b.id AS dst, properties(r) AS rprops`, coercing values to `frozenset[str]` per §2.1.

### 2.5 The driver guard (`driver.py`, D3/D8/D9/D10)

`run_maintenance` gains, **last** and in its **own `try/except`**:

```
if settings.projection_diff_enabled and settings.projection_diff_neo4j_uri and _projection_diff_due(now):
    self._last_projection_diff = now                      # honour cadence even on failure
    try:
        self._latest_projection_divergence = self._run_projection_diff(now=now)   # cache ONLY on success
    except Exception:
        logger.exception("projection diff guard failed; continuing (ADR 0102)")
```

`_run_projection_diff(now)`:
1. **GATE 1, THE TEXTUAL FENCE FIRST** — if `_same_neo4j_target(live_uri, settings.projection_diff_neo4j_uri)`
   for EITHER live source (`settings.neo4j_uri` or the live client's own `uri`):
   `logger.error(...)` and `raise ProjectionDiffMisconfiguredError(...)` **before** any client construction
   or wipe (so no wipe, no fold, no cached stat).
2. `pw = <the SecretStr's secret value>`; `diff = Neo4jClient.connect(uri=..., user=..., password=pw)`
   (short local binding — keeps the secret-scan hook's `password=<long-token>` heuristic from
   false-positive-matching a code reference); `try: ... finally: diff.close()`.
3. **GATE 2, THE IDENTITY HANDSHAKE (authoritative, BEFORE the wipe)** — `_database_id(client)` reads
   `CALL db.info() YIELD id` from BOTH the live and diff connections; equal ids OR either id unreadable ⇒
   `logger.error` + `raise ProjectionDiffMisconfiguredError` (fail-closed — defeats DNS-alias/port-forward
   aliasing the textual gate cannot see; hardened after the adversarial-verify CRITICAL finding).
4. `diff.execute_write("MATCH (n) DETACH DELETE n")` (wipe-before-rebuild, D4).
5. `with self._sessions() as session: project(session, diff, full_rebuild=True, checkpoint_id="projection-diff")`.
6. `live_snap = read_graph_snapshot(self._neo4j)`; `fold_snap = read_graph_snapshot(diff)`.
7. `with self._sessions() as session: survivor_of = build_survivor_of(session)`.
8. `return measure_divergence(live_snap, fold_snap, survivor_of, computed_at=now)`.

`_same_neo4j_target(a, b) -> bool` (pure, module-level in `driver.py`): `True` iff the `strip()`+lower+
trailing-`/`-stripped strings are equal **OR** the parsed `(canonical host, port)` tuples are equal —
hosts canonicalized via `_canonical_host` (loopback/unspecified class → one token; IP textual
normalization; trailing-dot strip); absent port → `7687`. Fail-closed / bias to more refusals (D3).

`_projection_diff_due(now)`: `self._last_projection_diff is None or (now - self._last_projection_diff)
.total_seconds() >= settings.projection_diff_cadence_seconds`.

`__init__` gains `self._latest_projection_divergence: ProjectionDivergence | None = None` and
`self._last_projection_diff: datetime | None = None` (mirror `_latest_gc_stats`, `:104`). `run_forever`'s
`DriverMetricsCollector(...)` call gains `projection_divergence=lambda: self._latest_projection_divergence`
(mirror `gc_stats=`, `:519`).

### 2.6 The collector (`collector.py`, D7)

New optional ctor kwarg `projection_divergence: Callable[[], ProjectionDivergence | None] | None = None`
(mirror `gc_stats`, `:58/:67`). In `collect()`:
`div = self._projection_divergence() if self._projection_divergence is not None else None`, then
`yield _gauge("worldmonitor_projection_divergence", "...", div.total if div is not None else -1)` and
`yield _gauge("worldmonitor_projection_divergence_last_run_timestamp", "...", div.computed_at.timestamp()
if div is not None else 0)`. The metric name literal MUST appear as a quoted string in the source
(INV-PARITY).

### 2.7 Settings (`settings.py`, D2) — verbatim block after the landing-GC block (~:234)

```python
projection_diff_enabled: bool = False
projection_diff_neo4j_uri: str = ""
projection_diff_neo4j_user: str = ""
projection_diff_neo4j_password: SecretStr = SecretStr("")
projection_diff_cadence_seconds: float = Field(default=86400.0, gt=0)
```

### 2.8 The alert (`worldmonitor.rules.yml`, D7)

```yaml
- alert: ProjectionDivergenceHigh
  expr: worldmonitor_projection_divergence > 0
  for: 15m
  labels:
    severity: warning
  annotations:
    summary: "..."          # one line
    description: >          # folded block; must render byte-identically in the promtool fixture
      ...
```

promtool fixtures: **fire** (`5` for 16m → warning, verbatim summary/description), **no-fire** proving
`-1` (never-run sentinel) does NOT fire, **no-fire** `0`.

---

## 3. LOAD-BEARING INVARIANTS — see `.claude/gate.scope` INV-1..8

Never-write-live (fence + read-only snapshot) · checkpoint isolation (`checkpoint_id="projection-diff"`
never moves `"neo4j"`) · dormancy (disabled/empty-URI ⇒ no client, no wipe, no fold, stat None, gauge
`-1`) · one-directional E1-tolerance (P-DIV-1) · rot sensitivity (P-DIV-2) · error isolation ·
alert-contract (warning; `-1` never fires) · append-only (guard only READS the log) · person_affecting:
false honesty.

---

## 4. FAILING-TEST-FIRST (RED → GREEN) — the PRIMARY invariants, stated precisely

Write these FIRST. The pure-measure properties (P-DIV-1/2) and the driver unit tests are **Docker-free**;
only the integration anchors need containers. **Reminder:** any incidental `@given` test that creates a
per-example SQLAlchemy engine MUST wrap its body in `try/finally: engine.dispose()` (the 3a-ii-A
connection-leak lesson) — but P-DIV-1/2 operate on **in-memory snapshots with no engine**, so this does
not apply to them.

### P-DIV-1 — no false alarm / E-tolerance (headline `@given`, pure)

- **Name:** `test_p_div_1_no_false_alarm_on_e_legit_transformations`.
- **Universally-quantified statement:** for ANY fold `GraphSnapshot` and ANY `survivor_of` (alias→survivor,
  survivor→self), and ANY live snapshot derived from the fold by **E-legit transformations** —
  per node: `id` set to the fold id **or** an alias that `survivor_of` maps back to it; a subset of values
  dropped from any multi-valued prop; arbitrary `wm_anchor_*` props added; `datasets` added/altered;
  arbitrary `prov_*` and labels; and any compared prop value replaced by an alias whose `survivor_of` is a
  value present in the fold node's set — and per edge: endpoints set to fold endpoints **or** aliases
  mapping back, a subset of edge-prop values dropped, arbitrary `datasets`/`prov_*` — the measure yields
  `total == 0`.
- **Generator shape:** draw a small set of stable survivor ids; per survivor a set of literal value-sets
  per prop; a set of edges over those survivors; then draw an alias map (each alias → one survivor,
  `unique` aliases). Build the fold snapshot from the survivors; build the live snapshot by applying the
  transformations above. Keep sets small (`max_size` ~4).
- **Oracle:** `measure_divergence(live, fold, survivor_of, computed_at=<fixed>).total == 0`.

### P-DIV-2 — rot sensitivity (`@given`, pure)

- **Name:** `test_p_div_2_rot_is_detected`.
- **Universally-quantified statement:** starting from a fold-derived live snapshot with `total == 0`,
  (a) injecting `k` distinct **new nodes** whose `survivor_of(id)` is absent from the fold ⇒ `total`
  increases by **exactly `k`**; (b) injecting `k` distinct **new edges** (type/endpoints) with no fold
  counterpart ⇒ `total` increases by **exactly `k`**; (c) adding to one explained node a compared-prop
  value absent from (and not `survivor_of`-equal to any value in) the fold node's set ⇒ that node flips
  and `total` increases by **exactly 1**. Every single injection ⇒ `total >= 1`.
- **Generator shape:** reuse the P-DIV-1 fold/live builders; draw `k` (1..3) fresh ids/edges disjoint from
  the fold; for (c) draw one explained node + a fresh value token.
- **Oracle:** the exact deltas above.

### UNIT — measure + snapshot + fence (`tests/unit/test_projection_divergence.py`, Docker-free)

- `measure_divergence` example tests: identical graphs → 0; one exclusion class each (add `wm_anchor_*`,
  `datasets`, `prov_*` to a live node → still 0; add a differing label → still 0, D6-i); a missing fold
  node → 1; a thinner live value-set → 0; an extra live value → 1; an edge endpoint alias that resolves
  back → 0; an edge with no fold counterpart → 1.
- `_same_neo4j_target` table: `bolt://neo4j:7687` vs `bolt://neo4j:7687` → True; vs `bolt://neo4j:7687/`
  → True; vs `BOLT://NEO4J:7687` → True; vs `neo4j://neo4j:7687` (scheme variant, same host:port) → True;
  vs `bolt://neo4j:7688` (different port) → False; vs `bolt://other:7687` (different host) → False.
- `read_graph_snapshot` MAY be exercised in the integration anchor (it needs a real graph); a unit test
  MAY stub `execute_read` to assert value-coercion + read-only (no `execute_write` call).

### UNIT — driver guard dormancy / fence / error-isolation (`tests/unit/test_driver_projection_diff.py`, Docker-free)

Construct an `IngestDriver` with a stub/`_StubNeo4j`-style live client and SQLite sessions. Monkeypatch
`worldmonitor.runner.driver.Neo4jClient.connect` to a **fail-if-called** spy and (where relevant)
`worldmonitor.runner.driver.project` / `read_graph_snapshot`.

- **Dormancy:** `projection_diff_enabled=False` (default) ⇒ `run_maintenance(now=...)` does NOT call
  `Neo4jClient.connect`, does NOT wipe, does NOT call `project`; `_latest_projection_divergence` stays
  `None`. Repeat with `projection_diff_enabled=True` + **empty** `projection_diff_neo4j_uri` ⇒ same.
- **MISCONFIG FENCE:** `projection_diff_enabled=True` + `projection_diff_neo4j_uri == settings.neo4j_uri`
  ⇒ `run_maintenance` does NOT call `Neo4jClient.connect` and does NOT wipe; an error is logged; the stat
  stays `None`. (Assert `_same_neo4j_target(settings.neo4j_uri, settings.projection_diff_neo4j_uri)` is
  `True` for the configured pair.)
- **ERROR ISOLATION:** monkeypatch the diff path (e.g. `Neo4jClient.connect` or `project`) to raise;
  `run_maintenance(now=...)` returns **without propagating**; `_latest_projection_divergence` stays
  `None` (no stat cached on failure); the prunes (which run before the guard) still executed.
- **CADENCE:** with the guard enabled+URI set and the diff path monkeypatched to a benign stub returning a
  `ProjectionDivergence`, a first `run_maintenance` runs it and caches the stat; a second call within
  `projection_diff_cadence_seconds` does NOT re-run it (`_projection_diff_due` False).

### UNIT — collector sentinel (lives in `tests/unit/test_projection_divergence.py` — a NEW file, so
### the RED collection failure never breaks the existing `test_metrics_collector.py` module)

- Collector built **without** `projection_divergence` ⇒ `worldmonitor_projection_divergence == -1` and
  `worldmonitor_projection_divergence_last_run_timestamp == 0`.
- Collector built **with** `projection_divergence=lambda: ProjectionDivergence(2, 3, 10, 5, <ts>)` ⇒
  `worldmonitor_projection_divergence == 5.0` and the timestamp gauge `== <ts>.timestamp()`.
- The existing `assert names >= _EXPECTED_NAMES` superset assertion (`:195`) stays green (extra gauges are
  additive); do NOT convert it to exact equality.

### UNIT — alert structure (`tests/unit/test_prometheus_alerts.py`, mostly auto-covered)

The existing suite auto-covers the new alert: INV-PARITY (the metric must be emitted from `collector.py`),
INV-STRUCTURE (expr/for/severity/annotations), exactly-two-critical (new alert is **warning** → criticals
stay `{DriverDown, ResolutionWedged}`), seven-alerts subset (an 8th alert passes the `missing = expected -
actual` check). No edit is required for these to stay green; add an assertion for `ProjectionDivergenceHigh`
presence only if desired.

### INTEGRATION (Docker, local) — `tests/integration/test_projection_diff.py`

Reuse the second-container pattern (`conftest.py:136-152` `neo4j_gds_client`) for the diff target and the
existing live-graph fixture (`clean_graph`) + `postgres_dsn`, and the `_candidates()`/`_queue_item()` seed
shape (single-batch, single-source ⇒ E1/E2/E3 null, so divergence is 0 on a faithful fold).

- **IT-DIV-1 — end-to-end zero divergence + checkpoint isolation.** Seed `_candidates()`; `resolve_pending`
  into `clean_graph` (the live graph); pre-seed a `ProjectionCheckpoint(id="neo4j", last_statement_seq=<N>)`
  row (to prove isolation); wipe the **diff** container; `project(session, diff, full_rebuild=True,
  checkpoint_id="projection-diff")`; `live = read_graph_snapshot(clean_graph)`; `fold =
  read_graph_snapshot(diff)`; `survivor_of = build_survivor_of(session)`; assert
  `measure_divergence(live, fold, survivor_of, computed_at=<now>).total == 0`. Then assert the
  `"neo4j"` checkpoint row is **unchanged** and a `"projection-diff"` row now exists (D5 checkpoint
  isolation). (Prefer driving the fold directly via `project(...)` here rather than through the whole
  driver, so the checkpoint-isolation assertion is unambiguous.)
- **IT-DIV-2 — rot is detected.** After IT-DIV-1's zero state, inject rot into the **live** container (an
  extra node with a fresh id **not** in the log, or an extra compared-prop value on an existing node);
  re-read the live snapshot; assert `measure_divergence(...).total >= 1`.

### Regression witnesses that MUST stay GREEN

`test_prop_fold_engine.py` (P-FOLD-1..5), `test_projector.py` (IT-PROJ-1..4 — `project()`'s default
`checkpoint_id` keeps them byte-behaviour-identical), `test_metrics_collector.py` (superset assertion),
`test_prometheus_alerts.py` (two-critical + seven-alerts subset + INV-PARITY), `test_statement_spine.py`,
`test_resolution_pipeline.py`, `test_migrations.py`, `test_no_autogenerate_drift`.

---

## 5. ACCEPTANCE CRITERIA (all must hold — measurable, no promissory claims)

- `measure_divergence` implements §2.2 exactly; **P-DIV-1** (`total == 0` under E-legit transforms) and
  **P-DIV-2** (exact-`k` rot sensitivity) are green `@given` properties, run Docker-free.
- `project()` gains the additive `checkpoint_id` param (default `_TARGET_ID`); the guard passes
  `"projection-diff"` and **never** moves the `"neo4j"` row (proven by IT-DIV-1). `build_survivor_of` is
  a pure extraction; `project()` consumes it; IT-PROJ-1..4 + P-FOLD-* stay green.
- The driver guard is **dormant** by default (no client/wipe/fold when disabled or empty-URI; stat `None`;
  gauge `-1`), **fenced** (diff-URI == live-URI ⇒ no wipe, error logged — the misconfig-fence unit test is
  present and green), **isolated on error** (a diff failure never aborts `run_maintenance`, no stat
  cached), and **cadenced** (at most once per `projection_diff_cadence_seconds`).
- The collector emits `worldmonitor_projection_divergence` (`-1` sentinel when dormant, `total` when
  populated) + the companion liveness gauge, via an **optional** ctor kwarg; the existing collector unit
  tests keep passing unchanged.
- `ProjectionDivergenceHigh` (warning, `> 0`, `for: 15m`) is in `worldmonitor.rules.yml` with promtool
  fire + `-1` no-fire + `0` no-fire fixtures; `test_prometheus_alerts.py` (two-critical, seven-alerts,
  INV-PARITY) stays green; the `alert-rules` CI job (promtool check+test) passes.
- **IT-DIV-1** (zero divergence + checkpoint isolation) and **IT-DIV-2** (rot detected) green locally
  against real Postgres + two real Neo4j containers.
- `docs/decisions/0102-*.md` present; builder re-runs `uv run python scripts/gen_adr_index.py` so README
  gains the `0102` row and `uv run python scripts/gen_adr_index.py --check` passes.
- Full `uv run pytest -m "not integration"` green locally (the `quality` job runs it); the integration
  suite green locally (Docker available here); `ruff format --check .` **repo-wide** + `ruff check` +
  `pyright` clean; `quality` + `security` + `alert-rules` green before self-merge.
- The `human_cosign` header line carries the COMPLETED, dated user cosign (Mithat 2026-07-05, an
  explicit ack obtained after the adversarial verify + fix round — never a promissory placeholder).

---

## 6. FROZEN (KEEP-GREEN / BYTE-UNCHANGED)

The **person-affecting write path** and every substrate this gate must not touch:
`resolution/statements.py`, `resolution/merge.py`, `resolution/pipeline.py`, the merge guard,
`graph/writer.py`, `graph/ftmg_fork.py`, `resolution/canonical.py`, `resolution/referents.py`,
`db/models.py`, **every** `db/migrations/**` file, and `resolution/eval.py` / `gold.py`. Within
`projector.py`, `reconstruct_entities` (the pure fold — F1/F2, common-schema, referent rewrite, G1
provenance, witness map), the write path, and the `full_rebuild` fold-selection logic stay
**byte-unchanged**; only the additive `checkpoint_id` param + the `build_survivor_of` extraction land.
`validate_production_secrets()` (ADR 0061) is **not** touched.

---

## 7. OUT OF SCOPE (do NOT build — later gates)

- **Gate 3b cutover** (project into the live graph) + retiring the direct write path (human-gated).
- **Gate 2b backfill** of pre-2a graph nodes into the log (the pre-2b thin-signal caveat, ADR 0102 D1,
  closes when 2b lands).
- **A two-directional divergence measure** (fold-not-in-live) — deferred (fires on E1); revisit after 2b.
- **Retroactive supersession node-deletion** — the ADR-0100 LOW backlog; the fold only `MERGE`s.
- **A compose service / DR-restore runbook** for the diff target (the operator provisions it out-of-band).
- **Any change to clustering, ER thresholds, scoring, the merge guard, referent rewrite, the writer,
  statements, merge, pipeline, `db/models.py`, or migrations.**
- **The `docs/40_ROADMAP.md` drift** truth-up (a later docs sweep, not this gate).

---

## 8. PERSON-AFFECTING ASSESSMENT

**NOT person-affecting.** The guard is read-only w.r.t. the live graph (fenced against ever wiping it;
reads it via `execute_read` only), makes no merge/ER decision, touches no threshold/score/guard/erasure,
and writes only the operator-designated **isolated** diff target. `human_fork: false`, `person_affecting:
false`. Because the diff edits `resolution/**` modules (`projector.py` + new `resolution/divergence.py`),
ADR 0102 carries the completed ADR-0097 human cosign (Mithat, 2026-07-05 — an explicit ack obtained
after the adversarial verify + fix round; the 3a-ii-A judge DENIED an undated promissory cosign, so no
placeholder survives to merge). The mandatory `@given` P-DIV-1/P-DIV-2 are the invariant harness for
the projection-integrity surface.

## 9. VERDICT

Reversible, non-person-affecting, default-off/dormant projection-integrity guard realising ADR 0101
§Decision B with the user's B4 build-now steer, proven by the mandatory pure `@given` P-DIV-1/P-DIV-2 and
a two-container end-to-end anchor. One focused PR; checker reproduces INV-1..8; judge gates on the cosign;
`human_fork: false`, `person_affecting: false`.
