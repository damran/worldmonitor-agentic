# 82 — Gate 3b cutover plan (3b-planning-proper)

- **Status:** PLANNING (2026-07-12) — the four deliverables `docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md`
  §7 assigns to **3b-planning-proper**: the **exclusion-surface audit** (§7-11), the **one-time
  reconciliation** (§7-12), the **driver LOWs** (§7-13), and the **write-path-retirement carve-outs**
  (§7-14, plus the §7-8 superseded-node disposition that lands here because P2/ADR 0107 chose mechanism
  (a)). Its decision record is the PROPOSED **ADR 0114**; this document is the detailed working artifact.
- **What this document is NOT:** the cutover. Gate 3b proper — retire the direct write, make
  rebuild-from-the-log the routine path — is **human-gated, irreversible, and LAST** (ADR 0095;
  81 §The-target). This plan is authored so that when the human runs it, the unverified surface is
  minimal and named, and the reconciliation that licenses the irreversible step is sound.
- **Inputs:** ADR 0095 (statement-log = SoR), ADR 0099/0100/0101/0102 (spine, fold engine, incremental
  correctness, rebuild-diff guard), ADR 0106 (context-claim capture), ADR 0107 (erasure reaches the SoR),
  ADR 0108 (sign-off durability), ADR 0110/0111/0112 (WPI), ADR 0113 (Gate 2b backfill), and an
  adversarial red-team of the audit itself (§7, this doc), which surfaced two person-affecting CRITICALs
  the first-pass audit missed. Every code claim below is anchored to `file:line` on master `9494335`.

---

## 1. The target and the irreversibility

Today **Neo4j is the live system of record**; the statement/decision/context-claim log + fold engine are
**built and dormant** (`resolution/projector.py`, exercised only against ephemeral targets — ADR 0100 D4).
Gate 3b inverts this: `resolution/projector.py::project(full_rebuild=True)` becomes the **sole sanctioned
writer of the live entity graph**, and the three direct writers (`pipeline.py:540`, `signoff.py:304`,
`signoff.py:387`) are retired. Afterwards **the Postgres log is the one truth and Neo4j is a rebuildable
projection**.

"Irreversible" in the sense that bites (81 §The-target): once the direct writer is gone, **anything the
log cannot reproduce is gone from a rebuild**, and **anything the log cannot forget is a GDPR liability
forever**. The four gate conditions (consult §1) are therefore:

1. Rebuild destroys nothing load-bearing → the **exclusion-surface audit** (§3) + the **live→fold**
   reconciliation (§4).
2. Rebuild resurrects nothing erased → the **fold→live erased-value** checks (§4) + the erasure-convergence
   precondition (§4 R1b).
3. Forgetting reaches the live graph → the erasure live-prune survives write-path retirement (§5).
4. The unverified surface at cutover is minimal and named → this whole document.

---

## 2. Cutover strategy: SWAP, not in-place  (the central mechanic; ADR 0114 open-decision D-1)

There are two ways to first-populate the live `"neo4j"` checkpoint from the log:

- **In-place** — point `project(full_rebuild=True)` at the *current live* Neo4j (`checkpoint_id="neo4j"`).
  The projector is **MERGE-only and never deletes** (`ftmg_fork/transform.py`; verified — no
  DELETE/DETACH/REMOVE anywhere in the write path). So every node the legacy direct writer ever wrote
  under a now-**superseded** alias id is **orphaned** (the fold stops emitting it; MERGE never removes it),
  and its **CREATE-once edges** (`ftmg_fork/transform.py` entity-links + edge-schema are first-write-wins,
  never refreshed) linger with stale properties.
- **Swap** — `project(full_rebuild=True)` into a **fresh, wiped, fenced** target (exactly what the
  diff-guard already provisions, `driver.py:417-419`), verify it against live via the §4 reconciliation,
  then promote it to be the live instance (URI cutover / connection-drain). Current-live is never mutated
  until the swap; **orphaned superseded nodes and frozen edge props vanish for free**.

**Recommendation: SWAP.** The red-team confirmed the in-place orphan problem is **not** correctness-neutral:
the alias-on-read redirect the "leave orphans live" default relies on — `writer.py::resolve_node_id` /
`get_entity_by_alias` — is **DEAD CODE** (zero production callers; grep-verified). The real read surface
(`graph/queries.py:20,30,42,67` behind every `api/graph.py` route and every `mcp/server.py` tool) issues
**raw `MATCH (n:Entity {id})`** with no ledger resolution. So under in-place cutover:

- `GET /graph/entities/{superseded-id}` returns the **stale orphan**, not the survivor;
- `get_neighbors` / any `:Entity` count **double-counts** orphan + survivor (the orphan's CREATE-once edges
  were never deleted).

Swap makes this class of defect structurally impossible and simultaneously resolves §7-8 (no orphans to
delete) and the edge-prop-freeze staleness. **Reversal cost of choosing swap:** it needs URI-cutover /
connection-drain tooling that **does not exist yet** (a Gate-3b build item, small). **Revisit trigger:**
if the swap tooling proves disproportionate, fall back to in-place **plus** either (a) wiring
`resolve_node_id` into `queries.py` (revives the dead redirect) or (b) a one-shot orphan DETACH-DELETE GC
pass — both larger than the swap they replace. This is a human-gate decision (ADR 0114 D-1); the plan
builds toward swap.

---

## 3. Exclusion-surface audit (§7-11)

**Two instruments, different jobs.** The **recurring divergence predicate**
(`divergence.py::measure_divergence`) is the only production-wired surface: it is **one-directional**
(live→fold), a per-prop **subset** test after `survivor_of` normalisation, and it **excludes** a fixed axis
set. The **equivalence signature** (`graph_signature`, `tests/property/test_prop_fold_engine.py`) is
**test-only**, byte-exact and bidirectional, but valid **only in the single-batch null-divergence regime**.
"All fold tests green in CI" certifies the fold in a controlled fixture; it does **not** certify the real
corpus. The irreversible gate therefore cannot lean on the recurring guard's blind spots — each must be
named and dispositioned.

| Axis | Divergence predicate | Cutover-moment disposition |
|---|---|---|
| `id` (node id / edge endpoints) | EXCLUDED — used only as the `F.id==survivor_of(L.id)` join key (`divergence.py:97,147`) | **ACCEPTABLE** (the id difference *is* the replayed live-alias→fold-survivor judgement). Operator verifies once: `canonical_id_ledger` completeness so `survivor_of` maps every live id, and no id is materialised more than once (see same-id row). |
| `caption` | EXCLUDED (`:98`) | ACCEPTABLE — a single FtM pick, not a union-monotone set (D6-iii). Its inputs (the name values) are fully compared. No manual verify. |
| **anchors** — `CANONICAL_ID_FIELDS` bare keys (wikidata_id, geonames_id, lei, opencorporates_id) | EXCLUDED (`:99`, pick-semantics, ADR 0106 Sub-fork A) | **⚠ TOP REAL-DATA-LOSS RISK. MUST operator-verify** one-time **anchor parity** (live vs fold) over the real corpus. Safe ONLY IF Gate P1 capture landed AND the **Gate 2b backfill actually RAN** (a pre-P1/pre-backfill anchored node with no `context_claim` row loses its anchor on rebuild, unflagged). |
| `datasets` (node) | EXCLUDED (`:100`) | ACCEPTABLE as the documented **E4 semantic redefinition** (node `datasets` := source-id set of folded claims; ADR 0113 D4). Record in ADR 0114 + the API/MCP response-schema changelog. |
| `prov_*` scalars | EXCLUDED (`prov_` prefix, `:101`) | ACCEPTABLE — provenance **presence** is G1-guaranteed at write (`write_entities` fails closed; the fold always stamps, `projector.py:234-240`). Note the representative-pick shift under E1 as accepted. |
| `prov_witnesses` (per-prop witness map) | EXCLUDED as part of `prov_*` | **MUST spot-check once**: per-prop `live_witnesses ⊆ fold_witnesses`. The one excluded axis that IS evidence AND is monotone-comparable (D6-ii convenience exclusion, not a derived pick). |
| **topic labels** (`:Sanction`, `:Role_pep`, … 73 codes) | **NEVER COMPARED — and NOT a property either.** `registry.topic` ∈ ftmg `ENTITY_IGNORE_PROP_TYPES`, so `topics` is dropped from node props and materialised only as a **Neo4j label** (`generate_topic_labels`). `measure_divergence` compares neither labels (D6-i) nor this non-property. | **⚠ CRITICAL — MUST add a live↔fold topic-label PARITY check as a hard cutover precondition** (both directions; see §3.1). Person-affecting: the catastrophic-merge **sensitivity guard reads these labels from the live graph** (`sensitivity.py::_risk_within_khop`, `r:Sanction OR r:Role_pep …`), and `gds.py:29` reads them. |
| schema labels (`:Company`, `:Person`, …) | NEVER COMPARED (D6-i) | **MUST check `live_labels ⊆ fold_labels`** (the LOSS direction — see §3.1). A survivor whose `common_schema` computes too general (`projector.py:173-184`) can drop a `:Company` label a mixed-schema live node accumulated via additive MERGE. |
| same-id multiplicity (>1 live node sharing one id) | BLIND — each of N live nodes is individually "explained" by the one fold node (`fold_nodes_by_id` dict collapses the lookup; no `n.id` UNIQUE constraint, `constraints.py:24-30`) | **MUST operator-verify** a one-time count: no id materialised more than once on the live graph. Wire this as a hard reconciliation R-gate (§4), not just prose. |
| id-less nodes / id-less-endpoint edges | BLIND — both instruments filter `n.id IS NOT NULL` (`snapshot.py`, `test_prop_fold_engine.py`) | **MUST count once** (`MATCH (n) WHERE n.id IS NULL`). An id-less node is un-foldable and silently vanishes on rebuild. |
| fold-side extras (in fold, absent from live) | BLIND BY DESIGN (one-directional; the fold is a resolved superset, ADR 0100 D2) | **MUST perform the §4 two-directional reconciliation** — enumerate every fold-side extra and explain it as legitimate E1, else BLOCK. |
| edge property currency | edge non-excluded props ARE subset-compared (`:157-163`), but CREATE-once edges are never refreshed by a re-fold | **Neutralised by the swap** (§2). Under in-place, accept documented edge-prop staleness or refresh edge writes. |
| text/html/json/checksum/mimetype props (`notes`, `description`, `summary`, …) | Never projected to the graph on EITHER side (`registry.text` etc. ∈ `ENTITY_IGNORE_PROP_TYPES`); symmetric, no divergence | ACCEPTABLE for the *graph* comparison, but **RECORD**: these live only in the Postgres log post-cutover and are invisible to any "the graph is the product" consumer (they ARE captured as statement rows, `statements.py:93`). Not a loss (they're in the SoR), but a wire-visibility note for the API/MCP changelog. |

### 3.1 The label check must run in the LOSS direction (red-team HIGH)

A naive one-time check `fold_labels ⊆ live_labels` detects fold-**invented** labels but **stays TRUE
exactly when the fold DROPS a label** — the failure it must catch. The loss-detecting direction is
`live_labels ⊆ fold_labels`, split into **topic labels** and **schema labels**, both compared. Steady-state
topic-label loss ≈ 0 (topics ARE captured as statement rows, so the fold re-emits the label), so this is a
guard-blind-spot / audit-completeness fix rather than a live fold bug — **but concrete un-logged paths
exist** where the loss is real: pre-ADR-0113 legacy nodes, and `backup.py::_import_neo4j` DR-restored
labels that have **zero backing statement rows**. Under an in-place cutover those labels silently drop and
no instrument flags it; **under the swap they are simply not re-materialised** — which is why the parity
check is a hard precondition regardless of strategy.

---

## 4. One-time reconciliation runbook (§7-12)

The recurring guard is one-directional; the cutover reconciliation is **more**. It is a **read-only
dress rehearsal** (folds into a fenced isolated target, never live) whose PASS licenses the irreversible
step. Executable via a new pure, Neo4j-free, `@given`-tested module **`resolution/reconciliation.py`**
(mirrors `divergence.py`; the runbook must not be hand-typed REPL snippets for an irreversible
person-affecting op).

**Preconditions**
- **R0 — quiesce** the spine (no concurrent ingest/sign-off) for the whole run. (`project()` takes **no**
  WPI-3 spine-writer lock and runs three READ-COMMITTED SELECTs with no shared snapshot — a concurrent
  writer can spuriously trip `IncompleteAliasedSurvivorError` and desync `survivor_of` from the measure.)
- **R1 — Gate 2b backfill RUN**, `assert_backfill_complete` green on the **REAL** Postgres (not merely
  merged). A `full_rebuild` before the backfill silently omits all pre-dual-write legacy data (**loss**).
- **R1b — erasure convergence** (red-team CRITICAL, §7 finding a): confirm no crashed/aborted erasure left
  the log un-scrubbed while the live graph was already pruned. Re-run `scrub_stock` to convergence and
  assert **rebuild-contains-no-erased-source** *before* the authoritative fold. `erase_source` has no live
  caller today, so this is latent — but Gate 3b + any future live eraser co-activate it.
- **R2 — LOW-1 + (scoped) LOW-3** in place so the instrument's own numbers are trustworthy (§6).

**Direction (i) live→fold — "rebuild loses nothing load-bearing"**
- **R4** `measure_divergence(live, fold, survivor_of).total == 0` (MUST-PASS).
- **R5** resolve every historically-unexplained live element to a **named benign class**; any residual not
  attributable to one = **BLOCK**.
- **R6** exclusion-surface audit (§3): confirm each excluded axis diverges only for its one documented
  reason; run the **anchor-parity**, **prov_witnesses**, **topic-label** and **schema-label** spot-checks.

**Direction (ii) fold→live — "rebuild resurrects nothing erased" + no silent extras**
- **R7** enumerate **fold-side extra nodes/edges** (`enumerate_fold_extras`, NEW).
- **R8** classify each as legitimate **log-ahead-of-live recovery** (accept, per-item, sensitivity-gated)
  or flag.
- **R9** **HARD BLOCK** any fold-side-extra dataset that matches an erase-audit `source_id`
  (`TaskRun(kind="erase")`).
- **R9b — co-present erased-value check** (red-team CRITICAL, §7 finding a): R9 only covers extra *nodes*.
  A **multi-source node value-pruned in place** (`ops.py:152` keeps the node) whose staged log scrub never
  committed passes R4 (live⊆fold) **and** is not a fold-extra — so the erased value silently resurrects.
  For every **co-present** node, compare its fold value-set against the erase-audit records; any erased
  value present in the fold = **HARD BLOCK**.
- **R9c — fold→live co-present value check** (red-team HIGH): flag any non-excluded **fold** value with no
  live counterpart, then classify as E1-superset vs unexplained. Catches a **wrong-but-logged catastrophic
  merge** faithfully replayed (a bad `canonical_id_ledger` alias folds A into the wrong survivor; both
  sides normalise through `survivor_of` so R4 is blind). Add an explicit **ledger-correctness spot-check**:
  the reconciliation certifies *fold == log*, never *log == reality*.
- **R10** fold-side extra **edges** (same taxonomy); edge-prop staleness neutralised by swap.

**Direction (iii) counts**
- **R11/R12 — node & edge count reconciliation** (`reconcile_counts`, NEW) must **balance to exactly 0**:
  `|live| − |fold|` decomposes into (E1 lingering-alias/cross-batch collapse) + (accepted live-only drops
  ≈0 post-WPI-1) − (fold-side extras).
- **R11b — per-id multiplicity term** (red-team HIGH): `reconcile_counts` MUST carry a **separate per-id
  multiplicity term** and NOT let `alias_collapse = |live| − |distinct survivors|` **absorb** a same-id
  duplicate (numerically identical: 2 live → 1 fold). A duplicate carrying an un-logged **anchor**
  (excluded from R4) otherwise vanishes count-clean. Wire A1's "no id materialised more than once" check
  as a hard R-gate here.

**PASS = all MUST-gates green + every residual named benign + zero erased-value resurrection (R9/R9b) +
zero unexplained fold→live value (R9c) + count residual exactly 0 (R11/R12) + per-id multiplicity 0
(R11b) + label parity clean (R6/§3.1).** Anything else BLOCKS the irreversible step.

---

## 5. Retirement carve-out registry (§7-14 + §7-8)

The write-channel inventory is **provably exhaustive** at `9494335` (six independent sweeps: `execute_write`;
`.session()`; `GraphDatabase.driver`/`execute_query`; every Cypher write verb incl. ftmg-generated
strings; apoc/GDS-mutate/LOAD CSV; connectors/MCP/API/tests-with-live-client) — **12 write sites, no
misses.** `Neo4jClient` is the sole driver boundary (`GraphDatabase.driver` only at `neo4j_client.py:34`).

| Site | Disposition |
|---|---|
| `graph/writer.py::write_entities` (the function) | **SPLIT** — the function SURVIVES (it is what `project()→:453` calls); what is RETIRED is its use as a *direct, non-log-mediated* writer. |
| `pipeline.py:540` promote/drain → `write_entities(neo4j, …)` | **RETIRE** — remove/gate; the spine writes in the same path make the promoted cluster reconstructable. |
| `signoff.py:304` `approve()` → `write_entities` | **RETIRE** — Gate P3 (ADR 0108) already co-commits statement + decision(`decided_by`) + ledger rows, so the approved merge is reconstructable. |
| `signoff.py:387` `reject()` → `write_entities` | **RETIRE** — Gate P3 co-commits each rejected member's rows. |
| `projector.py:453` `project()` → `write_entities(target, …)` | **PROMOTE-TO-LIVE** — re-target from the isolated diff instance onto the live client (`checkpoint_id="neo4j"`). Becomes the SOLE sanctioned entity writer (ADR 0095). |
| `graph/ops.py::erase_source_graph` (`:152` SET-prune, `:162` sole-source DETACH DELETE, `:169` edge DELETE) | **SURVIVE-CARVEOUT** — P2 mechanism (a), ADR 0107; the system's only node/edge deletion capability. |
| `graph/ops.py::set_node_values` (`:267`) + `erasure_scrub.py::prune_live_to_fold` | **SURVIVE-CARVEOUT** — value/anchor-prune half of the erasure carve-out. |
| `runner/driver.py:417` diff-target wipe + `project()→diff` | **DIFF-ISOLATED — SURVIVES** (§7-14). |
| `graph/constraints.py::ensure_constraints` (`:27`) | **DDL-ONE-TIME — SURVIVES** (bootstrap). Decide a first-class boot hook so anchor-uniqueness exists BEFORE the first authoritative fold (today it's established only as a side effect of DR restore). |
| `graph/gds.py::degree_centrality` (`:39/:55` project/drop) | **ANALYTICS-EPHEMERAL** — writes no persisted state. **Dead code (zero callers) → recommend DELETE at cutover** to shrink the audited surface. |
| `backup.py::_import_neo4j` (`:307` wipe, `:323` node MERGE, `:337` edge CREATE) | **OPEN / HUMAN GATE** — a raw-Cypher DR restore that reloads live from its OWN prior JSON export, **bypassing the log**. Not a §7-14-named survivor. Recommend **retire in favour of "restore Postgres, then `project(full_rebuild=True)`"** (ADR 0095 single-source-of-truth), or keep it as a fast DR bypass **with a mandatory post-restore reconcile-to-log step**. |

### 5.1 Fail-closed writer guard (harden beyond the naive allowlist — red-team MEDIUM)

- **Primary (structural CI gate, no DB):** an AST-walk of `src/worldmonitor` that finds every call to a
  Neo4j write method (`.execute_write`, `.session()`), resolves the enclosing function, and asserts it is
  in the allowlist above.
- **Enforce the retirement:** assert `write_entities` is CALLED ONLY from `projector.py::project`. The
  cutover PR that removes `pipeline.py:540` / `signoff.py:304,387` flips this green.
- **Chokepoint:** assert the bolt driver is constructed only in `graph/neo4j_client.py`.
- **⚠ Plug the `.driver` hole:** `Neo4jClient.driver` is a **public field** (`neo4j_client.py:26`), so
  `client.driver.session().run(...)` / `client.driver.execute_query(..., routing_=WRITE)` bypasses BOTH
  the method-name allowlist AND the chokepoint. The guard MUST also forbid `.driver` attribute access
  outside `neo4j_client.py` (or name-mangle/privatise the field). Not exploited today (grep-clean), but
  the guard exists precisely to keep it that way.

### 5.2 §7-8 superseded-node disposition

**Made moot by the swap cutover** (§2): a wipe-then-authoritative-rebuild materialises only survivors, so
no orphan is ever created and there is nothing to GC. If the human gate chooses **in-place** instead, the
reversible default (documented alias-on-read staleness) is **not** available as-is, because that redirect
is dead code (§2). In-place then REQUIRES either wiring `resolve_node_id` into `graph/queries.py` or a
one-shot DETACH-DELETE orphan GC. The **escalation to a projector delete step** is reserved for P2's own
revisit trigger (mechanism (b), seq-bearing erasure-event rows — ADR 0107), which would also give
superseded-node deletion a principled per-node home.

---

## 6. Driver LOWs (§7-13)

| LOW | Disposition |
|---|---|
| **LOW-1 — single ledger read** | **BUILD NOW.** `_run_projection_diff` reads the ledger inside `project()` (`build_survivor_of` at `projector.py:374`) AND again via `build_survivor_of(session)` at `driver.py:424`, in a **separate session** — two reads, an inconsistency window, and the completeness check does a **third** `_load_alias_map` (`projector.py:387`). Thread ONE `survivor_of` closure through the fold + the measure. Reversible, additive, on the dormant guard. |
| **LOW-2 — handshake-refusal observability** | **BUILD NOW.** Both fences (`driver.py:377` textual, `:404` D3 identity) only `logger.error`+raise and leave `_latest_projection_divergence = None`, so an operator can't distinguish "never ran" from "keeps refusing to wipe". Add a refusal **counter/gauge** surfaced via the existing Prometheus collector, so a mis-aliased diff target is visible on the dashboard. |
| **LOW-3 — snapshot streaming** | **SPEC + DEFER the risky part.** `read_graph_snapshot` materialises the whole graph in two unpaged reads — a real scale blocker for production DR. **BUT** the naive fix (lazy `session.run()` iterated record-by-record) is **WRONG for the LIVE read** (red-team HIGH): it holds the read transaction open across Python processing, so under Neo4j default READ-COMMITTED a long streamed scan **observes concurrent commits** the tight eager `execute_query` (EagerResult) would not → the recurring guard's gauge becomes **timing-dependent** (spurious `ProjectionDivergenceHigh`, or a false green if a node is skipped between batches). **Correct scoping:** keep the **EAGER** read for the LIVE snapshot (or quiesce the recurring guard, not just the one-time cutover); stream ONLY the **isolated diff/fold target** (no concurrent writer). Also: the node-read and edge-read are **two separate transactions** — streaming widens that window too. And the equivalence test that certifies the swap MUST be **id-multiplicity-aware** (compare a sorted multiset of `(id, labels, props)`, never a dict keyed by `id` — there is no `n.id` UNIQUE constraint, so a dict collapses duplicates and hides exactly the failure streaming risks). Given the correctness surface, LOW-3 is a **focused follow-up gate**, not a build-now. |

---

## 7. Red-team findings folded in (adversarial audit of this plan, 2026-07-12)

An independent adversarial pass (three skeptics, reproduced against `9494335`) found the first-pass audit
**incomplete** on person-affecting axes. Verified first-hand and now closed in §3–§6:

- **CRITICAL (exclusion) — topic labels** are invisible to every proposed instrument, yet feed the
  catastrophic-merge sensitivity guard from the live graph → §3 topic-label parity precondition.
- **CRITICAL (reconciliation) — erased-value resurrection on a co-present multi-source node** passes R4/R7/R9
  → §4 R9b + R1b.
- **HIGH — the audit's own label check was the wrong direction** (`fold ⊆ live` passes on loss) → §3.1.
- **HIGH — count reconciliation masks a per-entity error** via same-id multiplicity absorbed into
  `alias_collapse` → §4 R11b.
- **HIGH — LOW-3 streaming changes the divergence result** under concurrent writes → §6 scoping.
- **HIGH — no fold→live value check** lets a wrong-but-logged catastrophic merge be replayed → §4 R9c.
- **MEDIUM** — text/html property class un-projected (§3); LOW-3 equivalence test blind to multiplicity
  (§6); node/edge two-snapshot window (§6); `.driver` allowlist hole (§5.1); GDPR orphan-reachability
  rests on the provenance-match erase leg only (record in ADR 0114).
- **Credit:** the write-channel inventory (§5) is provably exhaustive — no missed live writer.

---

## 8. Pre-cutover checklist (operator; the "green over N cycles" gate, §7-10)

Human-gated, and several items are **operator-blocked** (need a real-seed host + keys), not code-blocked:

- [ ] Gate 2b backfill **RUN**; `assert_backfill_complete` green on the real Postgres (§4 R1). *(operator)*
- [ ] Per-cohort fidelity spike over the real-seed corpus (ADR 0113 SF-4, blocked-on-real-seed). *(operator)*
- [ ] Erasure convergence: `scrub_stock` to convergence + rebuild-contains-no-erased-source (§4 R1b). *(operator)*
- [ ] Rebuild-and-diff guard **enabled against an isolated target** and **green over N cycles**
  (`projection_diff_enabled=True` + a distinct `projection_diff_neo4j_uri`; §7-10 depends on the anchored /
  sign-off / erased corpora being present). *(operator)*
- [ ] `resolution/reconciliation.py` instruments + LOW-1/LOW-2 merged and `@given`-green (code, this track).
- [ ] Fail-closed writer guard merged, incl. the `.driver` hole (§5.1) (code, the cutover PR).
- [ ] Swap tooling (URI cutover / connection-drain) built (code, the cutover PR) — or the in-place fallback
  §5.2 explicitly chosen at the human gate.
- [ ] The §4 reconciliation PASSES on the real corpus (§4). *(operator + human sign-off)*
- [ ] ADR 0114 cosigned; ADR 0114 flipped PROPOSED→ACCEPTED **in the cutover PR** (person-affecting,
  irreversible — human sign-off).

## 9. Reversibility

3b-planning-proper (this doc, ADR 0114 PROPOSED, the reconciliation instruments, LOW-1/LOW-2) is fully
reversible: docs, a PROPOSED draft, and additive Neo4j-free measurement code on a default-off guard. The
**cutover itself is the irreversible step** and is deliberately NOT executed here — it awaits the operator
preconditions (§8) and the human cutover sign-off. Revisit trigger for the whole F1 inversion is unchanged
from ADR 0095: the fold/projection maintenance cost exceeding the merged-node/DR/erasure pain it removes,
with the scheduled rebuild-and-diff job as the early-warning signal.
