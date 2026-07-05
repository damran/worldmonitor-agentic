# 0102 — Projection rebuild-and-diff guard (Gate 3a-ii-B): scheduled full-fold divergence measure

- **Status:** ACCEPTED (2026-07-05)
- **Date:** 2026-07-05
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** Mithat — Gate 3a-ii-B user cosign 2026-07-05 (person_affecting:false waiver on the
  `resolution/**` diff, per ADR 0097 §4/§5; a default-off, dormant projection-integrity guard that
  never writes the live graph — two-gate D3 fence incl. the fail-closed db-id identity handshake —
  and makes no merge/ER decision; cosigned with disclosure of the fixed adversarial-verify findings
  [CRITICAL fence-alias bypass, HIGH caption false-alarm]). See §Governance.
- **Realises:** ADR 0101 **§Decision B (B1–B3)** — the scheduled full-rebuild-and-diff guard designed there
  — and records the **B4 resolution** (build now, dormant/default-off). **Builds on:** ADR 0100 (the
  dormant fold engine + `seq`/`projection_checkpoint`), ADR 0101 A1/A2 (loss-free incremental fold, so
  the guard consumes a proven `project()`), ADR 0078 (the alert-rule + INV-PARITY collector surface),
  ADR 0076 (the driver `/metrics` collector), ADR 0083/0086 (the `_latest_gc_stats` cache pattern the
  divergence cache mirrors), ADR 0044 (the canonical-id ledger `survivor_of` semantics). **Supersedes:**
  nothing.

## Context

ADR 0095 named *"a scheduled full-rebuild-and-diff job [as] the DR story and the fold-determinism
guard,"* and named the one genuine risk: *"Fold/projection determinism … where this design can rot into
a mutated projection."* ADR 0101 §Decision B designed that guard (B1 opt-in isolated target, B2 the
one-directional divergence measure, B3 caching+gauge+alert) but did **not** build it, leaving one
sequencing question **B4** for the human: *build now (requiring an operator to stand up a second,
throwaway Neo4j) or defer to Gate 2b?* Everything else about the guard was decided in ADR 0101 and is
**not relitigated here**.

Gate 3a-ii-A (ADR 0101 A1) landed the F3 fix, so `project()` is now loss-free and
`incremental == full-rebuild` (P-FOLD-2). The guard folds the **whole** log with
`project(full_rebuild=True)` into an isolated target and measures how much of the **live** graph the fold
**cannot explain** — a projection-integrity gauge, not an ER-quality claim.

## Decision D1 — B4 RESOLVED: BUILD NOW, dormant and default-off (user, 2026-07-05)

**The user resolved B4 on 2026-07-05: build the guard now.** It ships **dormant** (all
`projection_diff_*` settings default to the OFF/empty position); an operator stands up a second,
throwaway Neo4j and enables it later. This is a **reversible ops posture** (default-off observability,
not data-shape lock-in), so it needs no human fork beyond the recorded B4 steer + the ADR-0097 cosign
(§Governance).

**The pre-2b thin-signal caveat is accepted and is documented here as a first-class honesty note.**
Before Gate 2b backfills pre-2a graph nodes into the statement log, a **pre-2b live deployment**'s live
graph contains nodes that were written **before** the log existed and were therefore never logged. The
one-directional measure will **honestly report those pre-log nodes as unexplained** (the log genuinely
cannot reproduce them) until Gate 2b lands. On a **fresh, post-2a** deployment (everything written after
the spine existed) the measure is clean and reads 0 on a fold-equivalent corpus. Operators enabling the
guard on a pre-2b instance MUST read the divergence as *"live elements the current log cannot
reproduce"* — a superset of true rot until backfill closes the gap. This caveat is exactly why the guard
is default-off and its alert is a **warning**, not a page.

## Decision D2 — Opt-in isolated target (B1 realised): the `projection_diff_*` settings block

Neo4j Community is single-database (ADR 0094 D5): there is **no** free shadow DB on the live instance,
and the projector must **never** write live. The guard folds into an **operator-provisioned separate
Neo4j** addressed by a new settings block (placed after the landing-GC block, `settings.py` ~:234):

```python
projection_diff_enabled: bool = False
projection_diff_neo4j_uri: str = ""
projection_diff_neo4j_user: str = ""
projection_diff_neo4j_password: SecretStr = SecretStr("")
projection_diff_cadence_seconds: float = Field(default=86400.0, gt=0)
```

- **Dormancy is a runtime no-op, not a boot failure.** The guard runs only when
  `projection_diff_enabled is True` **AND** `projection_diff_neo4j_uri` is non-empty. `enabled=True` with
  an empty URI ⇒ **dormant** (matching B1: divergence stays `None`, gauge reports the `-1` sentinel).
  **No `model_validator` is added** to reject enabled-but-empty — a validator would turn a half-configured
  state into a *less-reversible* boot halt, and the B1 posture is explicitly "no-op when unset." The
  gauge sitting at `-1` **is** the signal that the guard is not running. `SecretStr` keeps the password
  out of reprs/logs; `validate_production_secrets()` (ADR 0061, FROZEN) is **not** touched — the diff
  creds are required at *use*, not at boot, exactly like `sandbox_runner_secret` (ADR 0077).

## Decision D3 — The MISCONFIG FENCE (first-class invariant): never wipe the live graph

The guard **wipes** whatever URI it is pointed at (D4). If an operator misconfigures
`projection_diff_neo4j_uri` to the **live** Neo4j, an unguarded wipe would `DETACH DELETE` the live
graph. **This is the single most dangerous line of the gate.** The guard therefore **fails closed**:
before constructing any diff client and before any wipe or fold, it compares the diff target to the live
target and, on a match, **logs an error and returns — no client, no wipe, no fold, no cached stat.**

**The fence is TWO gates (hardened after the gate's adversarial verification found a CRITICAL
hostname-alias bypass of a purely-textual fence — `localhost` vs `127.0.0.1`, the shipped default live
URI form, evaded it):**

**Gate 1 — the textual fence** (pure, no connection; runs before any client is constructed): a helper
`_same_neo4j_target(live_uri, diff_uri) -> bool` returns `True` when **either**

1. the two URIs are equal after `strip()` + lowercase + trailing-`/` removal (catches trailing-slash and
   case variants), **or**
2. their parsed **(canonical host, port)** tuples are equal — hosts canonicalized by collapsing the
   whole **loopback/unspecified equivalence class** (`localhost`, any `127.0.0.0/8`, `::1` in every
   textual form, `0.0.0.0`, `::`) to one token, normalizing IP textual variants, and stripping
   trailing-dot FQDNs; an absent port defaults to Neo4j's `7687`. This catches **scheme variants**
   (`bolt://` vs `neo4j://` vs `bolt+s://`) **and host-alias variants** (`localhost` vs `127.0.0.1` vs
   `[::1]`) that address the same instance. It is checked against **both** `settings.neo4j_uri` and the
   live client's own `uri` (strictly more refusals; honest for an embedder-injected live client).

**Gate 2 — the identity handshake (AUTHORITATIVE):** the textual fence cannot see a **DNS alias**
resolving to the live host or a **port-forward** publishing the live instance under another hostname. So
after connecting to the diff target and **before any wipe**, the guard reads the **database id**
(`CALL db.info() YIELD id`) from **both** the live and the diff connections. **Equal ids ⇒ the diff URI
reaches the live database ⇒ refuse. Either id unreadable ⇒ distinctness unproven ⇒ refuse
(fail-closed).** Identity, not addressing: this defeats every aliasing class the textual gate can miss.

On a refusal from either gate the guard raises `ProjectionDiffMisconfiguredError` (caught + logged by
the guard's own `try/except`, D10) so **no** wipe runs and **no** divergence stat is cached.

**Known false-refusal class (accepted per the bias):** a diff target **restored from a live backup** may
retain the live database id and be refused by gate 2 — the operator remedy is a fresh (empty) instance,
which is what the guard needs anyway (it wipes before folding).

**Reversal cost:** none (drop the settings + guard). **Revisit trigger:** if a future multi-database
Neo4j edition changes the meaning of `db.info()`'s id, or an operator needs a restored-clone diff
target, refine gate 2 (e.g. database-name + instance-id pair) — any refinement must stay fail-closed.

## Decision D4 — Wipe-before-rebuild

`project()` only `MERGE`s, so stale fold nodes from a prior guard run would **linger** and — under the
one-directional measure (D6) — **mask** divergence (a false negative). The guard therefore wipes the diff
target before each full fold with the established idiom
`diff_client.execute_write("MATCH (n) DETACH DELETE n")` (the same wipe `tests/conftest.py:104` /
`clean_graph` uses before every projector test). The wipe is gated behind the D3 fence, so it can only
ever touch the operator-designated isolated target.

## Decision D5 — Checkpoint isolation via an additive `checkpoint_id` param on `project()`

`project()` reads and upserts `ProjectionCheckpoint` under the fixed module constant
`_TARGET_ID = "neo4j"` (`projector.py:214/237/339`). A guard call `project(session, diff_client,
full_rebuild=True)` would **advance the shared `"neo4j"` watermark**, so a future Gate 3b live
incremental projector would silently **miss every row the guard consumed**. Fix: thread an **additive**
keyword param through `project()`:

```python
def project(session, target, *, full_rebuild=False, checkpoint_id: str = _TARGET_ID) -> ProjectionResult:
```

`checkpoint_id` is used in **both** the checkpoint read (`where(ProjectionCheckpoint.id == checkpoint_id)`)
and the upsert (`ProjectionCheckpoint(id=checkpoint_id, ...)`). The default `_TARGET_ID` keeps every
existing caller byte-behaviour-identical (P-FOLD-3 untouched). The guard passes
`checkpoint_id="projection-diff"`, writing a **separate** checkpoint row. `ProjectionCheckpoint.id` is a
plain `String(64)` PK (`db/models.py:413`), so a second distinct id is an ordinary second row — **no
migration, no schema change**.

## Decision D6 — The one-directional "explained" measure (B2 realised): the precise definition

`divergence = |{ live nodes not explained by the fold }| + |{ live edges not explained by the fold }|`,
computed over two in-memory graph **snapshots** and the ledger `survivor_of`. **Pure, Neo4j-free,
Docker-free** (`resolution/divergence.py`), so its properties are `@given`-testable without containers.

**A live node `L` is EXPLAINED iff** there is a fold node `F` with `F.id == survivor_of(L.id)` **and**
for every *compared* property `p`, `{ survivor_of(v) : v ∈ L.props[p] } ⊆ { survivor_of(v) : v ∈ F.props[p] }`
(a value-set subset after `survivor_of` normalisation). **A live edge `E=(type, src, dst, props)` is
EXPLAINED iff** there is a fold edge `F` with `F.type == E.type`,
`survivor_of(E.src) == survivor_of(F.src)`, `survivor_of(E.dst) == survivor_of(F.dst)`, and the same
per-prop value-subset rule on edge props. An unexplained node/edge is counted **once**;
`divergence = unexplained_nodes + unexplained_edges`.

**Why this is E1-tolerant by construction.** ADR 0100 D2 makes the fold a **resolved superset** of the
live graph: every live element maps onto a fold element (the fold may *additionally* consolidate
cross-batch). Joining on `survivor_of(L.id)` absorbs E1 (a live node written under an id later superseded
still joins to its survivor's fold node); normalising *every value token* through `survivor_of` before
the subset test absorbs E1 on any referent-valued property or edge endpoint (`survivor_of` is the
identity on literals and on already-survivor ids, so the normalisation is a free no-op for name/date
values and idempotent on the fold side). Dropping values (a thinner live re-observation) is a **subset**
and passes. A **non-zero** value therefore means the live graph contains a node/edge/value the log
**cannot** reproduce — genuine projection rot / an un-logged mutation — which is precisely the ADR-0095
risk. The naive symmetric-difference measure is rejected (§Alternatives): it fires constantly on any
multi-batch corpus because the fold is legitimately *more* resolved.

**Compared-property set (excluded classes).** A property `p` is **excluded** from the subset test when
`p == "id"` (the join key — legitimately differs under E1 alias-collapse), `p.startswith("wm_anchor_")`
(**E2** — anchors live in entity context, never in the log), `p == "datasets"` (**E4** — reconstructed
but batch-dependent), `p.startswith("prov_")` (see the reversible default below), or `p == "caption"`
(**D6-iii**, added after the gate's adversarial verification found a HIGH false-alarm: `caption` is a
single FtM **pick** over the name values, not a union-monotone value set — a live node's caption
reflects its *last write's* pick while the fold's caption is picked over the *whole-log* union, so a
routine cross-batch name update diverges legitimately and would fire the alert **permanently**; the
caption's **inputs** — the name values — remain fully compared, so no rot class is ceded). Everything
else — `name`, `jurisdiction`, `nationality`, `birthDate`, `topics`, and any other FtM value property —
**is** fold-reconstructed and **is** compared.

**Definition-level blind spots, named honestly (not claimed covered):** (a) **same-id multiplicity** —
N live nodes sharing one id are each *individually* explained by the one fold node, so duplicate-node
rot under an existing id is invisible to this measure; (b) **id-less elements** — the snapshot reader
keys on `n.id IS NOT NULL`, so an un-logged id-less node (and any edge touching it) never enters the
comparison. Neither is a claimed detection class of v1; both fold into the two-directional-measure
revisit (§Deferred). The **E2/E4 exclusions'** own revisit belongs to the committed **Fable consult at
Gate 3b planning** (should the log capture connector-side metadata so the graph is fully
reconstructable at cutover?).

**Two subtleties, each resolved with a documented reversible default + revisit trigger:**

- **(i) Labels — EXCLUDED from the v1 comparison.** The fold assigns one deterministic label set from the
  group's FtM `common_schema` (the 3a-i F1 fix); the live node **accumulates** labels across per-batch
  writes (Neo4j labels are additive on `MERGE`), so a cross-batch mixed-schema live node carries a
  *superset* of the fold's labels. Neither `live ⊆ fold` nor `fold ⊆ live` holds in general, so any label
  superset check would **false-alarm** on exactly the legitimate mixed-schema merge. v1 therefore
  **excludes labels from the explained check** (the join is node-id existence + value-subset). Labels are
  derived from the same statement rows the fold uses, and P-FOLD-2 / IT-PROJ-2 already prove schema/label
  equivalence in the null-divergence regime, so a label-only mismatch is an F1-class artefact, not the
  un-logged-element rot this gauge exists to catch. **Reversal cost:** none (a comparison rule).
  **Revisit trigger:** a real incident where a mislabelled live node would have been the *only* rot
  signal ⇒ add `fold_labels ⊆ live_labels` (the correct direction) as a stricter check.
- **(ii) `prov_*` scalars + `prov_witnesses` — EXCLUDED from the v1 comparison.** On a cross-batch merge
  the live node carries the batch-1 representative's `prov_*` scalars, while the fold reconstructs `prov_*`
  from the `min(entity_id)` representative over the **full** history — a legitimately *different*
  representative ⇒ different single-valued `prov_*` ⇒ a naive subset test false-alarms. Provenance
  *presence* is already guaranteed by G1 at write time (`write_entities` fails closed on an unprovenanced
  node/edge, ADR 0055/0060), so a projection-integrity gauge does not need to re-verify it. v1 therefore
  excludes the whole `prov_*` family (scalars **and** `prov_witnesses`) from the value comparison.
  **Reversal cost:** none. **Revisit trigger:** after Gate 2b + real cross-batch operation, add a
  provenance-*completeness* check that parses `prov_witnesses` and asserts, per prop,
  `live_witnesses ⊆ fold_witnesses` (a genuinely E1-tolerant set-superset), when provenance-rot detection
  becomes worth the extra surface.

## Decision D7 — Result shape, gauge, sentinel, and the warning alert (B3 realised)

- **Result dataclass** (`resolution/divergence.py`, frozen): `ProjectionDivergence(unexplained_nodes,
  unexplained_edges, live_nodes, live_edges, computed_at: datetime)` with a `total` property
  `= unexplained_nodes + unexplained_edges`. `computed_at` is **fed by the caller** (`now`), so the pure
  measure takes no clock.
- **Cache on the driver** exactly like `_latest_gc_stats`: `self._latest_projection_divergence:
  ProjectionDivergence | None` (init `None`), set **only on a successful** guard run (D10).
- **Gauge** `worldmonitor_projection_divergence` emitted from **`metrics/collector.py`** (mandatory —
  INV-PARITY, `test_prometheus_alerts.py:74-93`, regex-scans the collector source for the quoted metric
  literal). Value `= div.total` when the cached stat is present, else the **`-1` sentinel** (never-run /
  disabled). `-1` is chosen over the GC's `0` precisely so the alert `> 0` never fires on a dormant guard.
  The divergence accessor is an **optional ctor kwarg** on `DriverMetricsCollector`
  (`projection_divergence: Callable[[], ProjectionDivergence | None] | None = None`, mirroring `gc_stats`)
  so the existing collector unit tests — which construct the collector **without** it
  (`test_metrics_collector.py:189`) — keep passing and the gauge reads `-1`.
- **Companion liveness gauge** `worldmonitor_projection_divergence_last_run_timestamp` (Unix seconds;
  `div.computed_at.timestamp()` when present, else `0`) — a cheap staleness signal so an operator can see
  the guard is actually running (a divergence gauge stuck at a stale value is a footgun without it). Not
  referenced by any alert (INV-PARITY only constrains alert-referenced metrics).
- **Alert** `ProjectionDivergenceHigh` (severity **warning**) added to `worldmonitor.rules.yml`:
  `expr: worldmonitor_projection_divergence > 0`, `for: 15m` (consistent with the other `> 0`-presence
  warnings, and `-1 > 0` is false so the sentinel never fires). The existing **"exactly two critical"**
  contract (`test_prometheus_alerts.py:449-466`) stays true (the new alert is a warning); the
  seven-alerts test is a subset check, so an 8th alert is fine. promtool fixtures in
  `worldmonitor.rules.test.yml`: a **fire** case (`5` for 16m), a **no-fire** case proving the `-1`
  sentinel does **not** fire, and a **no-fire** `0` case (the `alert-rules` CI job runs
  `promtool test rules`, ADR 0088).

## Decision D8 — Cadence: default daily (`86400s`), on its own gate

A full fold is O(log size), so daily is ample. The guard has its **own** cadence
(`projection_diff_cadence_seconds`, default `86400`) rather than piggybacking the hourly maintenance
cadence: inside `run_maintenance` a `_projection_diff_due(now)` check (mirroring `_maintenance_due`,
against an instance attribute `self._last_projection_diff`) fires it at most daily. `_last_projection_diff`
is advanced when the guard block runs (due + enabled + non-empty URI), **regardless of fold outcome**, so
a broken diff target does not hammer the fold every maintenance tick; the divergence stat is cached only
on success (D10). **Reversal cost:** none (a settings default). **Revisit trigger:** the fold becomes
expensive enough that daily is too frequent (or a DR use-case wants it more often) ⇒ retune the default.

## Decision D9 — Module placement + the `build_survivor_of` export (pure, additive refactor)

- **`resolution/divergence.py` (new, pure):** the `NodeSnapshot` / `EdgeSnapshot` / `GraphSnapshot`
  dataclasses, the `ProjectionDivergence` result dataclass, and `measure_divergence(live, fold,
  survivor_of, *, computed_at)`. No Neo4j import ⇒ the property suite is Docker-free.
- **`graph/snapshot.py` (new, read-only):** `read_graph_snapshot(client) -> GraphSnapshot` — the **one**
  src-side whole-graph reader (two `execute_read` Cypher queries: all `id`-bearing nodes with
  labels+props; all edges with type+endpoints+props; values coerced to string-sets, mirroring the
  test-side `graph_signature` `_stable_val` list handling). `graph/` already imports `resolution/`
  (`writer.py` imports `resolution.canonical`), so importing the snapshot dataclasses from
  `resolution.divergence` introduces no new cycle. Uses `execute_read` only — **never** writes.
- **`resolution/projector.py`:** export the transitive `survivor_of` builder that is currently **inline**
  in `project()` (`projector.py:275-298`) as a module-level `build_survivor_of(session) ->
  Callable[[str], str]`, and have `project()` consume it (`survivor_of = build_survivor_of(session)`) — a
  **pure, behaviour-preserving** refactor (the F2 deterministic supersession-only ledger read + the
  cycle-guarded fixed-point walk move verbatim). The guard reuses `build_survivor_of` so its measure
  applies the **identical** referent semantics as the fold — no second, drifting implementation.
  `resolve_durable` (`canonical.py:275-283`) is single-hop/per-alias and is **not** usable here (the
  transitive `survivor_of` is required), which is why the export exists.
- **`runner/driver.py`:** the guard hook (`_run_projection_diff`, `_projection_diff_due`, the
  `_same_neo4j_target` fence helper, the `ProjectionDiffMisconfiguredError` exception, the
  `run_maintenance` block, the two cache attrs, and the collector-accessor wiring). The diff client is
  constructed **lazily** inside the hook only when enabled + due + fence-clear (mirroring the
  `if settings.landing_gc_enabled:` gate), via `Neo4jClient.connect(uri, user, password)` and **closed in
  a `finally`** (the guard owns it; the live `self._neo4j` is never closed).

`graph/writer.py`, `graph/ftmg_fork.py`, and `reconstruct_entities` itself stay **byte-unchanged**.

## Decision D10 — Error isolation in `run_maintenance`

`run_maintenance` has no per-task isolation (an exception aborts the tick and leaves `last_maintenance`
un-advanced). The guard block is placed **last** in `run_maintenance` and wrapped in its **own
`try/except`** so a diff-target failure (unreachable second Neo4j, fold error, fence error) can never
abort the co-located prunes/GC and never propagates to the tick loop; the divergence stat is cached
**only on success** (a failed run leaves the previous value / the `None` sentinel). This mirrors the
per-item `try/except` the ingest path already uses (`driver.py:249-253`).

## Governance (ADR 0097 §4/§5) — person_affecting: false, justified

**`person_affecting: false`, and the checker+judge reproduce this self-tag from the diff and DENY if a
person-affecting path is touched untagged.** The narrow, checker-verifiable claim:

- The guard is **read-only with respect to the live graph.** It reads the live graph via `execute_read`
  only (`read_graph_snapshot`) and **never writes it** — the D3 fence guarantees the *only* graph it ever
  wipes/folds-into is the operator-designated **isolated** diff target. It **makes no merge/ER decision**,
  changes **no** ER threshold, **no** clustering/merge outcome, **no** individual-affecting score, **no**
  guard behaviour, **no** erasure path. It is observability only.
- The production edits are: `resolution/projector.py` (the additive `checkpoint_id` param + the pure
  `build_survivor_of` extraction — both behaviour-preserving), two **new** modules
  (`resolution/divergence.py`, `graph/snapshot.py`), and additive hooks in `runner/driver.py`,
  `metrics/collector.py`, `settings.py`, plus the alert YAML + fixture. All **default-off / dormant**.
- **No file in the person-affecting write path changes** (byte-unchanged, listed as FROZEN in the spec):
  `resolution/statements.py`, `resolution/merge.py`, `resolution/pipeline.py`, `graph/writer.py`,
  `graph/ftmg_fork.py`, the merge guard, `db/models.py`, and every migration.
- Because the diff still edits a `resolution/**` module while self-tagging non-sensitive, ADR 0097
  requires the explicit **human co-sign** carried in the header — cosigned by the user (Mithat,
  2026-07-05) after the adversarial verification + fix round, with the CRITICAL/HIGH findings and
  their fixes disclosed at ask-time (the 3a-ii-A lesson: a completed, dated cosign, never a
  promissory one).

## Reversibility

**Reversible** (additive; dormant until an operator opts in; the projector never writes live).

- **D2 settings / D7 gauge+alert / D10 hook.** Reversal cost: `projection_diff_enabled=False` fully
  disables it; drop the settings + gauge + alert to remove it entirely. **Revisit trigger:** after Gate
  2b backfill + real cross-batch operation, reconsider whether the one-directional measure should become
  two-directional (§Deferred).
- **D3 fence.** Reversal cost: none. **Revisit trigger:** a multi-database Neo4j edition makes host+port
  too coarse ⇒ add a database-name match (narrower, still fail-closed).
- **D5 `checkpoint_id` param.** Reversal cost: low (revert to the module constant); default keeps all
  callers unchanged. **Revisit trigger:** none foreseen.
- **D6 label/prov exclusions.** Reversal cost: none. **Revisit triggers:** as stated in D6(i)/(ii).
- **D9 `build_survivor_of` export.** Reversal cost: none (a pure extraction; inline it back).

**Overall revisit trigger (ADR 0095's):** the fold/projection maintenance cost exceeds the
merged-node/DR/erasure pain it removes — this guard is the early-warning signal for it.

## Deferred (explicitly NOT built in 3a-ii-B)

- **Gate 2b backfill** of pre-2a graph nodes into the log (the pre-2b thin-signal caveat, D1, closes once
  it lands).
- **A two-directional divergence measure** (fold elements not present live). Rejected for v1 (§B2/ADR
  0101): it fires on E1 (the fold is legitimately *more* resolved). Revisit after 2b (D2 revisit trigger).
- **Retroactive supersession node-deletion** (the ADR-0100 LOW backlog): the fold only `MERGE`s, so a
  now-empty superseded survivor lingers in the diff target. Still LOW-backlog, still OUT.
- **Gate 3b** cutover (project into the live graph) + retire the direct write path (human-gated). The
  same second Neo4j instance the operator provisions here is the eventual 3b DR-rebuild target.
- **A compose service** for the diff target (operator provisions it out-of-band) and any DR-restore
  runbook wiring.

## Alternatives rejected

- **Defer the guard to Gate 2b (the other B4 arm).** Rejected by the user's D1 steer: the guard is
  default-off and additive, so building it now costs nothing until enabled, and it is the ADR-0095
  fold-determinism safety net that should exist before 3b cutover. The thin-signal caveat is documented
  rather than used as a reason to wait.
- **Naive symmetric-difference divergence.** Rejected (D6): fires constantly on any cross-batch corpus
  because the fold is legitimately more-resolved (E1).
- **Co-writing a shadow subgraph inside the live Neo4j.** Rejected: breaks "never write live" and Neo4j
  Community is single-database anyway (ADR 0094 D5).
- **Advancing the shared `"neo4j"` checkpoint from the guard.** Rejected (D5): silently starves a future
  3b live incremental projector; the additive `checkpoint_id` param isolates the watermark.
- **Comparing labels / `prov_*` scalars in v1.** Rejected (D6 i/ii): both false-alarm on the legitimate
  cross-batch mixed-schema / representative-shift cases; excluded with a documented revisit trigger.
- **A `model_validator` that rejects enabled-but-empty-URI.** Rejected (D2): converts a half-configured
  state into a less-reversible boot halt; the B1 posture is a runtime no-op with the `-1` gauge as the
  signal.

## Consequences

- The ADR-0095 fold-determinism / projection-rot risk gains a **default-off, operator-enablable**
  early-warning gauge + warning alert, built on the loss-free `project()` 3a-ii-A proved.
- The guard is **read-only w.r.t. the live graph**, **fenced** against ever wiping it, and **isolated**
  from the live checkpoint watermark — so it is safe to ship dormant now and enable after 3b's DR target
  exists.
- The one-directional measure is honest about the pre-2b regime (reports pre-log nodes as unexplained)
  and crisp on the post-2a regime (0 on a fold-equivalent corpus), with the two-directional refinement
  and the label/prov completeness checks explicitly deferred with revisit triggers.

## ADR-index coupling

Adding this PROPOSED ADR requires the **3a-ii-B builder** to re-run
`uv run python scripts/gen_adr_index.py` so `docs/decisions/README.md` gains the `0102` row (else the
`adr-index` CI check goes red); `docs/decisions/README.md` is in the gate scope for exactly this reason.
The header uses the list dialect the generator parses, so the regenerated row reads
`PROPOSED | 2026-07-05 | false | false` until the accept-time flip.
