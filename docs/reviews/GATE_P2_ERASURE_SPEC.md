# GATE P2 — right-to-forget reaches the SoR — BUILD SPEC

- **Owning decision:** ADR 0107 (`docs/decisions/0107-erasure-reaches-sor.md`), status **PROPOSED**,
  DECIDED lines filled by the P2 planning gate (2026-07-11) against a first-hand read of master `b1b061c`
  (Gate P1 #174 + Gate P3 #176 merged) and a 3-lens plan-verify (all FIX_FIRST — folded in below).
  **person_affecting: true → user cosign REQUIRED before the code build; `human_cosign` stays PENDING
  until the accept flip at gate end.**
- **Source:** the Fable log-capture consult (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md` §6, §7-4, §7-5)
  and the pre-cutover gate sequence (`docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md` Gate P2 + §7 owner
  map). Anchors verified first-hand against master: `erasure.py`, `graph/ops.py`, `graph/constraints.py`,
  `resolution/statements.py`, `resolution/projector.py`, `resolution/divergence.py`, `db/models.py`,
  `llm/egress_audit.py`, and the P1 DB-level append-only detector (`tests/integration/test_context_claim_lane.py`).
- **Governance:** `human_fork: false`, `person_affecting: true`, `human_cosign: PENDING`. P2 changes *what
  an erasure actually erases* → person-affecting **by construction**. The main loop requests the cosign
  **before the code build** (findings disclosed — the four D-i…D-iv items below) and stamps ADR 0107's
  dated `human_cosign` line + flips PROPOSED → ACCEPTED at the accept flip (gate end). **A judge DENIES on
  an un-cosigned-at-merge person-affecting diff (ADR 0097 §5) — 0107 is correctly tagged `true`; do NOT
  stamp the cosign at planning, do NOT flip Status.**
- **Branches (planner writes files only — creates no branch):** the **planning docs** (this spec + the ADR
  0107 fill) ship as their **own docs-only PR** from a docs branch (autonomously mergeable — committing the
  filled PROPOSED decision-space is the 0107/0108 committed-draft precedent; the 0107 index row is
  unchanged, still PROPOSED, so **no README regen in the planning PR**). **`.claude/gate.scope` is a
  gitignored (`.gitignore:46`) LOCAL working file for the scope-guard hook — NOT a committed PR artifact;
  it is regenerable from §7 (INV-ERASE-*) + §8 (FROZEN), which are the committed contract the checker uses.**
  The **code** PR follows **after** the cosign, on `gate/p2-erasure-reaches-sor` cut from `origin/master`.
  **P2 adds exactly ONE migration — `0013` (the `dataset` index, SF-1); every other schema is FROZEN.**

The test-author writes RED tests first; the builder makes them GREEN without weakening any FROZEN invariant.

---

## 0. Cosign disclosures (the main loop discloses these BEFORE the build — ADR 0107 §Decided)

- **D-i (SF-3 load-bearing):** P2 keeps `graph/ops.py`'s direct prune (option a) and does **not** give the
  projector a delete path (option b). This closes P2's full erasure mandate but leaves the
  incremental-projector anchor-retraction bound (ADR 0106 §Verification) + superseded-node deletion to Gate
  3b (safety net: `full_rebuild` correct + guard-green over N cycles).
- **D-ii (append-only carve-out widened):** P2 extends the sanctioned erasure exception to the SoR spine —
  it DELETEs `statement`/`context_claim` rows and redacts `decision.member_ids`, lanes the current
  `erasure.py` never touched. **Proven confined by a POSITIVE test** (§4 IT-ERASE-appendonly).
- **D-iii (erasure made more complete):** P2 removes erased values the current prune LEAVES (co-witnessed
  props) and erased anchor values the current prune never touches (**REMOVE-only**) — a person-affecting
  behaviour change in the correct direction (discharges ADR 0095 :63).
- **D-iv (schema migration):** P2 adds migration `0013` (index on `statement.dataset` +
  `context_claim.dataset`).

---

## 1. Verified current state (do not re-derive; confirm if editing)

| Fact | Location |
|---|---|
| `erase_source(*, neo4j, session, landing, source_id, authorized_by)` deletes landing objects, redacts `er_queue`/dead-letter, calls `erase_source_graph`; audits ONE `TaskRun(kind="erase")` whose `stats = ErasureResult.as_dict()` carries `source_id` + `authorized_by` + 7 counts; DB writes staged on `session` for the CALLER to commit; **landing + graph removals applied IMMEDIATELY (cross-store non-atomic — documented)** | `erasure.py:137-196,150-153,168-190` |
| `erase_source_graph` builds `new_props = dict(props)` from the node's **FULL current props** then `SET n = $props` (`graph/ops.py:124-156`) — the read-current-props-then-merge idiom SF-4 MUST mirror so nothing (`prov_*`/`id`/`caption`/`datasets`) is dropped | `graph/ops.py:124-156` |
| The prune is **prop-granular**: sole-source node → DETACH DELETE; multi-source survivor → drop `source_id` from each witness set, REMOVE only props it was the **sole** witness of (co-witnessed props keep ALL values), rebuild `prov_*`; edges with `prov_source_id==source_id` DELETEd whole | `graph/ops.py:79-183,119-158` |
| **`prov_witnesses` is `{prop: [datasets]}` — prop-granular, NO per-value attribution** → a co-witnessed prop retains erased-source-**only** VALUES (surprise #1) | `graph/ops.py:61-76,106-136` |
| **Bare anchor keys are NOT in the witness map** → `erase_source_graph` NEVER removes an erased source's anchor value (surprise #1) | `graph/ops.py` (operates on `prov_witnesses`+`prov_*` only) |
| **Every `CANONICAL_ID_FIELDS` prop carries a Neo4j `REQUIRE n.<anchor> IS UNIQUE` constraint** → SF-4's anchor prune MUST be **REMOVE-only** (a SET of a surfaced value can crash the erasure, surprise #8) | `graph/constraints.py:24-30` |
| `statement.dataset` = `String(255)` **no index**; `context_claim.dataset` = `String(255)` **no index** (SF-1) | `db/models.py:336,557` |
| The P1 writer stamps `dataset = prov.source_id or member.id or ""` → an empty-`source_id` member's rows are keyed by `member.id` (SF-1 fallback residual) | `statements.py:196` |
| `decision` has **NO `dataset` column**; `member_ids` = plain **JSONB** (no `MutableList`) → REASSIGN, never in-place `.remove()` (SF-2) | `db/models.py:352-396,384` |
| The projector reads `decision` rows **ONLY** to advance `last_decision_seq` — `member_ids` never consumed into node reconstruction (SF-2 safety) | `projector.py:346-349,417` |
| `reconstruct_entities(statement_rows, survivor_of, context_claim_rows=())` is a **PURE** fold (no DB, no Neo4j) — reusable read-only as the SF-4 parity oracle; groups by statement rows | `projector.py:83-239` |
| The production divergence guard fires on `ProjectionDivergence.total`; `_excluded` = {`id`, `caption`, `CANONICAL_ID_FIELDS`, `datasets`, `prov_*`} → **anchors are EXCLUDED** (SF-6 anchor-oracle fence) | `divergence.py:67-69,96-102` |
| The P1 DB-level append-only detector monitors ONLY the normal write paths (seed/resolve/approve/project); never invokes the scrub | `tests/integration/test_context_claim_lane.py:435-542` |
| `LlmEgressRecord.entity_manifest` is a caller-declared JSONB canonical-id list; durable audit DORMANT (`llm_egress_durable_enabled=False`); no `dataset` column (F2 rider = defer) | `db/models.py:439-496`, `llm/egress_audit.py:84-99` |
| Existing erasure behaviour + the idempotent second-run zero-count assertion are pinned by | `tests/integration/test_erasure.py` (incl. `:372` zero-run), `test_erasure_graph.py`, `test_erasure_fixes.py` |

---

## 2. The gate — three independent, individually-mergeable slices

All three are the **sanctioned erasure exception** (ADR 0049 / CLAUDE.md — the one carve-out to append-only),
confined to the erasure entry points and never reachable from any writer/pipeline/agent path — **PROVEN by
the positive confinement test** (§4 IT-ERASE-appendonly), not merely asserted. The FROZEN append-only
writers (`statements.py`) are byte-unchanged; the P1 DB-level detector stays green.

### Slice P2-a — the three-lane log scrub + decision redaction (SF-1 + SF-2 + SF-5)

**2.a.1 New module `resolution/erasure_scrub.py`** (mirrors the `statements.py` spine idiom, but for the
sanctioned delete). Public surface:

```
def scrub_log_lanes(session, source_id) -> LogScrubResult:
    # 1. erased_member_ids = DISTINCT entity_id over (statement ∪ context_claim) rows WHERE dataset = source_id
    #    — computed BEFORE any delete (surprise #2: compute-then-delete ordering is load-bearing).
    # 2. touched_survivors = { survivor_of(canonical_id) for the reached rows }  (build_survivor_of(session)).
    # 3. Reach predicate (plan-verify MEDIUM — fallback-keyed closure): DELETE statement + context_claim rows
    #    WHERE (dataset = source_id) OR (entity_id IN erased_member_ids)  — so a member with >= 1
    #    dataset=source_id row also loses its member.id-keyed fallback rows.
    # 4. Decision redaction: erased_refs = erased_member_ids ∪ { entity_id of every reached (deleted) row }.
    #    For each decision row whose member_ids ∩ erased_refs: REASSIGN member_ids to the pruned list
    #    (row.member_ids = [id for id in row.member_ids if id not in erased_refs]) — NEVER in-place .remove()
    #    (plain JSONB, no MutableList → an in-place mutation silently will NOT persist; reassign or
    #    flag_modified(row, "member_ids")). Row preserved (kind/score/decided_by/evidence/surviving members).
    # Returns erased_member_ids + touched_survivors + per-lane counts (for the additive TaskRun stats).
```

- **Only `session.execute(delete(...))` / column reassignment** — no writer path. Caller commits (rides
  the existing `erase_source` transaction). **Never** deletes/redacts `canonical_id_ledger`,
  `ResolverJudgement`, `SignOff`, `MergeAudit` (the ADR-0049 no-un-merge carve-out preserved).
- **Irreducible residual (SF-1):** a member whose rows are *all* `member.id`-keyed (empty `source_id`
  everywhere) is unreachable-by-source from the log — a P1-writer defect. P2 **does NOT** re-open the
  FROZEN P1 writer; it **names the non-empty-`source_id` enforcement as a P2 dependency** (§9), routed to a
  WPI slice, so no new pure-fallback rows appear. The `(dataset OR entity_id)` reach closes every
  source-linkable row (D-iii for those).

**2.a.2 Stock-scrub driver** (SF-5), in the same module:
```
def scrub_stock(session, *, neo4j) -> list[LogScrubResult]:
    # sources = { r.stats["source_id"] for r in session.query(TaskRun).filter(kind="erase", status="ok") }
    #   — read PYTHON-SIDE (plan-verify LOW: NOT a Postgres-only `stats->>'source_id'`, which breaks the
    #   Docker-free SQLite unit lane). for each: scrub_log_lanes(...) + the SF-4 live prune (2.b);
    #   idempotent (a second run finds nothing).
```
One-off, operator-invoked (never autonomous). Verified by the SF-6 rebuild-contains-no-erased-source check.

### Slice P2-b — the SF-4 live value/anchor prune-to-fold (PROVENANCE-PRESERVING; anchors REMOVE-only)

**2.b.1 A thin, provenance-preserving value-set writer in `graph/ops.py`** (keeps `graph/ops.py` free of a
`resolution` import → no cycle). **The signature MUST read the node's current props so provenance is never
dropped (plan-verify HIGH-1):**
```
def set_node_values(neo4j, node_id, *, compared_props: dict[str, list[str]], remove_anchor_keys: list[str]) -> None:
    # 1. READ the node's FULL current props: MATCH (n:Entity {id:$id}) RETURN properties(n)  (mirror
    #    graph/ops.py:88-93/124).
    # 2. new_props = dict(current_props); for prop, values in compared_props: if values: new_props[prop]=values
    #    else: new_props.pop(prop, None). For key in remove_anchor_keys: new_props.pop(key, None).
    # 3. SET n = $new_props  (full-property-replace — mirror graph/ops.py:152-156; injection-safe
    #    LiteralString, no dynamic-property-name Cypher).
    # FORBIDDEN: a bare `SET n = <partial map>` built from only compared_props/anchors — it would WIPE
    #    prov_source_id / prov_witnesses / datasets / id / caption (they live in the full current props).
    #    Equivalent alt: `SET n += <non-empty sets>` + explicit literal-key `REMOVE n.<prop>` for now-empty
    #    props and each anchor key. Either way provenance is PRESERVED.
```

**2.b.2 Orchestration in `resolution/erasure_scrub.py`** (imports the FROZEN pure `reconstruct_entities`
read-only):
```
def prune_live_to_fold(session, neo4j, touched_survivors) -> None:
    survivor_of = build_survivor_of(session)
    for s in touched_survivors:
        # read s's REMAINING (post-scrub) statement ∪ context history for its preimage
        fold_entity = reconstruct_entities(remaining_stmt_rows, survivor_of, context_claim_rows=remaining_ctx)
        # compared_props := fold_entity value-sets for every NON-_excluded prop (GDPR + guard-green)
        # remove_anchor_keys := the anchor keys whose live value came (solely) from the erased source —
        #   i.e. present on the live node but ABSENT from get_anchors(fold_entity). REMOVE-ONLY (SF-4 / HIGH-2):
        #   NEVER SET a new/changed anchor value (graph/constraints.py UNIQUE → a surfaced conflict-resolved
        #   value would ConstraintValidationFailed and abort the erasure). GDPR-complete + guard-neutral.
        graph.ops.set_node_values(neo4j, s, compared_props=compared_props, remove_anchor_keys=remove_anchor_keys)
```
- **`reconstruct_entities` is the single source of truth** for the compared-prop target — do NOT hand-roll
  a second value-diff.
- **Anchors: REMOVE-only.** The erasure only removes an erased-source anchor value; it never surfaces a new
  one (HIGH-2). Anchor removal is verified by a DIRECT live-node property read (SF-6), not `measure_divergence`.
- Edges carry `prov_*` only (no witness map) → every edge is sole-source → whole-edge DELETE
  (`erase_source_graph`) is complete; **no co-witnessed-edge-value case exists** (no edge value prune needed).

### Slice P2-c — wire the scrub into `erase_source` + the round-trip properties (SF-6 + SF-7)

**2.c.1 `erasure.py::erase_source`** — after `erase_source_graph(...)` and before building `ErasureResult`,
call `scrub_log_lanes(session, source_id)` (staged on `session`, caller commits) then
`prune_live_to_fold(session, neo4j, touched_survivors)`. **Extend `ErasureResult` ADDITIVELY** (plan-verify
LOW-c): add the per-lane scrub-count fields + `as_dict()` keys, **retaining ALL existing `_COUNT_KEYS` /
`_STATS_KEYS` with no assertion removed** (coordinate with `test_erasure.py`'s locked keys); a second
idempotent erase yields **scrub-count == 0** so `test_erasure.py:372`'s zero-run assertion still holds.
- **Ordering:** `erase_source_graph` (sole-source deletes + prop-granular prune) → log scrub
  (compute-then-delete) → `prune_live_to_fold` (completes value/anchor removal on survivors) → audit.
- **Cross-store non-atomicity contract (plan-verify LOW):** `prune_live_to_fold` writes Neo4j immediately;
  `scrub_log_lanes` stages Postgres for the caller's commit (the SAME split `erase_source` already
  documents). A commit failure after the Neo4j prune leaves the live graph pruned but the log un-scrubbed
  → a `full_rebuild` would resurrect onto the pruned live graph **until the idempotent retry re-scrubs +
  re-prunes to convergence**. State this contract in the module docstring; pin it by IT-ERASE-idempotent.

**2.c.2** The `@given` + integration suites (§3, §4).

---

## 3. Property invariants (@given — RED-first)

NAME · STATEMENT · GENERATOR · ORACLE · NON-VACUITY. New file `tests/property/test_prop_erasure_scrub.py`.
Container-backed examples MUST wrap any per-example engine in `try/finally` dispose (memory:
given-red-tests-leak-connections). All P-ERASE-* are **RED before the builder wires §2**.

**P-ERASE-1 — the round-trip asserts BOTH surfaces, with TWO vacuity fences (Slice P2-c; real Postgres +
ISOLATED Neo4j fold target).**
- *Statement:* after `erase_source(..., source_id)` on a seeded corpus, then `project(full_rebuild=True)`
  into a FRESH isolated target: **(i)** the fresh rebuild contains **nothing** of the erased source — zero
  `statement`/`context_claim` rows reached by the SF-1 predicate; no reconstructed node/edge value equal to
  an erased-source-**only** value; no `decision.member_ids` referencing an erased member; **AND (ii)** the
  LIVE graph no longer holds the erased values — sole-source nodes gone, co-witnessed erased-only values
  removed, erased anchor values removed.
- *Generator:* `@given` over corpora that MUST include: a **sole-source node**, a **multi-source survivor
  with a co-witnessed prop holding an erased-source-only value** (the SF-4 hard case), an **erased-source
  anchor** on a surviving node, and a **`decision` row referencing an erased member**.
- *Oracle:* independent DB reads (reached-row counts == 0; `decision.member_ids` reads) + fold value reads
  + **DIRECT live-node property reads**.
  - **Compared props** MAY use `measure_divergence(live, fold, survivor_of).total == 0`.
  - **Anchors MUST use a DIRECT live-node property read** (query `n.wikidata_id`/`n.geonames_id`/… on the
    live node) — **`measure_divergence` is REJECTED as the anchor oracle** (it `_excludes`
    `CANONICAL_ID_FIELDS` → goes GREEN with a residual erased anchor still on the node; plan-verify MEDIUM).
- *Vacuity fences (both mandatory):* (1) a **fresh-target-only** oracle is REJECTED — the test MUST read
  the LIVE graph too (against master, (i) resurrects on rebuild AND (ii) the co-witnessed erased value +
  erased anchor survive live — both RED). (2) the **anchor-oracle** fence above.
- *Non-vacuity:* an impl that scrubs the log but skips `prune_live_to_fold` → (ii) fails on the
  co-witnessed value AND the direct anchor read; an impl that does the live prune but not the log scrub →
  (i) resurrects on rebuild; an impl that deletes the decision row instead of redacting → the SF-2
  belief-revision assertion (P-ERASE-3) fails; an impl whose value-prune drops `prov_*` (bare partial SET)
  → a G1/provenance assertion fails (see P-ERASE-4).

**P-ERASE-2 — `full_rebuild` over the scrubbed log is erased-free unconditionally (Slice P2-c; the DR path).**
- *Statement:* after a scrub, `project(full_rebuild=True)` into a fresh target yields NO node/edge/value of
  the erased source, **regardless of interleaving** (a scrub between two folds does not resurrect anything).
- *Generator:* `@given` over {scrub-before-fold, scrub-between-two-folds} × corpora.
- *Oracle:* fold value reads == erased-free.
- *Non-vacuity:* an impl that only prunes the live graph (never scrubs the log) → the fresh full_rebuild
  resurrects the erased rows → fail. **P-FOLD-2 stays byte-FROZEN** (no-deletion regime unchanged).

**P-ERASE-3 — decision-row redaction preserves the judgement, removes the reference (Slice P2-a).**
- *Statement:* for a `decision` row whose `member_ids` intersects the erased refs, after the scrub: the row
  still EXISTS with byte-identical `kind`/`score`/`decided_by`/`evidence`; `member_ids` has exactly the
  erased ids removed and the surviving ids intact; a `full_rebuild` reconstructs the survivor node
  identically (proving the projector never consumed `member_ids`).
- *Generator:* `@given` over decisions with {all-members-erased, some-erased, none-erased}.
- *Oracle:* DB reads of the decision row pre/post + fold node parity.
- *Non-vacuity:* an impl that DELETEs the decision row fails (row absence); one that in-place `.remove()`s
  the JSONB without reassignment fails (SQLAlchemy does not persist it → the erased id survives).

**P-ERASE-4 — the live value-prune preserves G1 provenance (Slice P2-b; plan-verify HIGH-1).**
- *Statement:* after `erase_source`, every surviving pruned node still carries a non-empty `prov_source_id`
  and its (pruned) `prov_witnesses`/`datasets`/`caption`/`id` — the value-prune removed erased values
  WITHOUT wiping provenance (G1 upheld: node-provenance never becomes empty).
- *Generator:* `@given` over multi-source survivors with a co-witnessed erased-only value.
- *Oracle:* direct live-node reads of `prov_*` + `id`.
- *Non-vacuity:* an impl using a bare `SET n = <partial map>` (compared_props/anchors only) → `prov_*`
  wiped → fail.

---

## 4. Unit + integration tests

- **`tests/integration/test_erasure_scrub.py` (NEW, `pytest.mark.integration`, real Postgres + Neo4j — Docker
  IS available locally, run it):**
  - **IT-ERASE-flow:** seed a corpus (multi-source survivor with a co-witnessed erased-only value + an
    erased-source anchor + a sole-source node + a decision referencing an erased member), `erase_source(...)`,
    then assert BOTH surfaces (SF-6): zero reached `statement`/`context_claim` rows; the decision row
    redacted (SF-2); the live survivor node lacks the erased value (compared prop) AND the erased anchor
    (DIRECT `n.wikidata_id` read, NOT `measure_divergence`); `prov_*` preserved (P-ERASE-4); then
    `project(full_rebuild=True)` into an isolated target contains nothing of the erased source.
  - **IT-ERASE-stock:** simulate the dual-write window (several `TaskRun(kind="erase")` rows + still-present
    log rows), run `scrub_stock(...)` (Python-side source read), assert each source scrubbed once,
    idempotent on a second run, verified by rebuild-contains-no-erased-source.
  - **IT-ERASE-signoff-lane:** a P3 sign-off-approved survivor whose members include an erased source →
    the scrub reaches its sign-off `statement`/`context` rows AND redacts its `decided_by="operator:…"`
    decision row (P2 layers on P3 uniformly).
  - **IT-ERASE-idempotent (cross-store recovery, plan-verify LOW):** (a) a plain second `erase_source` is a
    precise no-op across all lanes (scrub-count == 0). (b) a run with an **injected post-Neo4j /
    pre-Postgres-commit failure** leaves the live graph pruned but the log un-scrubbed → a `full_rebuild`
    momentarily resurrects onto the pruned live graph; a **retry re-scrubs + re-prunes to convergence**
    (resurrection-then-recovery PROVEN, not assumed).
  - **IT-ERASE-appendonly (POSITIVE confinement, plan-verify governance MEDIUM):** a `before_cursor_execute`
    listener over the engine — **(a)** the FULL normal pipeline (seed → `resolve_pending` → `signoff.approve`
    → `project`) issues **ZERO** DELETE/UPDATE against `statement` / `context_claim` / `decision`
    (table-qualified token match, the P1 detector idiom extended to the two SoR lanes + decision), **AND
    (b)** `scrub_log_lanes(...)` DOES emit exactly those DELETE/UPDATEs — so "append-only EXCEPT the
    sanctioned erasure scrub" is demonstrated in BOTH directions (this is INV-ERASE-APPENDONLY-CARVEOUT's
    test — a tautology-proof, not the trivially-green P1 detector).
- **`tests/integration/test_erasure.py` (extend):** the existing landing/queue/dead-letter/graph behaviour
  stays green **unchanged** (incl. `:372`'s idempotent zero-run); add assertions that the scrub rows are
  gone + the ADDITIVELY-extended `ErasureResult`/`TaskRun.stats` scrub counts are present (all existing
  `_STATS_KEYS` retained, no assertion removed).
- **`tests/unit/test_erasure_scrub.py` (NEW, Docker-free where possible):** the `erased_member_ids`
  derivation ordering (compute-before-delete); the `(dataset OR entity_id)` reach predicate; the
  decision-redaction REASSIGNMENT (in-place `.remove()` fails to persist); the `set_node_values`
  read-current-props merge (a stubbed node retains `prov_*`).
- **`test_migrations.py` (drift guard) runs UNCHANGED and passes** — it auto-detects migration `0013`; do
  NOT edit it.

---

## 5. Builder task list (ordered)

**Planning docs (this planner's output; ship FIRST as a docs-only PR from a docs branch, no cosign):** ADR
0107 DECIDED-lines fill (done) + this spec. **`.claude/gate.scope` is a gitignored local working file —
NOT part of the docs PR** (regenerable from §7/§8). Merge on green (docs-only). The 0107 index row is
unchanged (still PROPOSED) — **no `README.md` regen in the planning PR.**

**Slice P2-a (code; after cosign):** 1) migration `0013` + `index=True` on `statement.dataset` +
`context_claim.dataset` (byte-agree). 2) `resolution/erasure_scrub.py` — `scrub_log_lanes` (compute-then-
delete, `(dataset OR entity_id)` reach, decision reassign-redact) + `scrub_stock` (Python-side source read).
3) make P-ERASE-3, IT-ERASE-stock, IT-ERASE-appendonly, the scrub-helper unit tests GREEN.

**Slice P2-b (code):** 1) `graph/ops.py::set_node_values` read-current-props-then-merge writer (FORBID bare
partial SET). 2) `erasure_scrub.prune_live_to_fold` (reuse `reconstruct_entities`; anchors REMOVE-only).
3) make the SF-4 live-prune assertions + P-ERASE-4 GREEN.

**Slice P2-c (code):** 1) wire `scrub_log_lanes` + `prune_live_to_fold` into `erasure.py::erase_source` +
ADDITIVELY extend `ErasureResult`. 2) make P-ERASE-1 (both fences), P-ERASE-2, IT-ERASE-flow,
IT-ERASE-signoff-lane, IT-ERASE-idempotent (incl. cross-store recovery), and the extended `test_erasure.py`
GREEN.

**All three slices = ONE code PR** on `gate/p2-erasure-reaches-sor` (recommended). At the accept flip
(post-cosign): stamp ADR 0107's `human_cosign` dated line, flip PROPOSED → ACCEPTED, re-run
`uv run python scripts/gen_adr_index.py` (the 0107 row status flips), `--check` passes. **The accept flip
must not occur on a PENDING cosign line.**

Cosign gate: the main loop asks the user **before the code build** (person_affecting:true), discloses
D-i…D-iv (§0), and stamps ADR 0107's `human_cosign` dated line at the accept flip / merge.

---

## 6. Acceptance criteria (all measurable)

- **FULL** `uv run pytest -m "not integration"` GREEN repo-wide (the `quality` job runs exactly this).
- **FULL local integration suite GREEN** (`uv run pytest -m integration`) — explicitly including
  `test_erasure_scrub.py` (incl. IT-ERASE-appendonly + IT-ERASE-idempotent cross-store recovery), the
  extended `test_erasure.py`, the unchanged `test_erasure_graph.py` / `test_erasure_fixes.py`, the unchanged
  P1 `test_context_claim_lane.py` (DB-level detector still green), and the unchanged `test_migrations.py`.
- All new `@given` properties GREEN: P-ERASE-1 (BOTH surfaces + BOTH vacuity fences), P-ERASE-2, P-ERASE-3,
  P-ERASE-4 (provenance preserved).
- **FROZEN-adjacent suites stay green unchanged:** `test_prop_statement_spine.py`,
  `test_prop_context_claim_capture.py` (incl. the P-CTX-3 writer append-only spy),
  `test_prop_fold_engine.py` (**P-FOLD-2 byte-unchanged**), `test_prop_projection_divergence.py`,
  `test_prop_signoff_spine.py`, and every IT-PROJ / IT-SIGN.
- `ruff format --check .` (REPO-WIDE) clean; `ruff check .` clean; `uv run pyright` clean.
- **Exactly ONE new migration:** `git diff` adds `db/migrations/versions/0013_*.py` and changes
  `db/models.py` ONLY to add `index=True` to `statement.dataset` + `context_claim.dataset`; no other model
  changes; **no erasure-event table** (SF-3(a)); `test_migrations.py` passes.
- **`ErasureResult`/`_STATS_KEYS` extension is ADDITIVE** — all existing count keys retained, no assertion
  removed; `test_erasure.py:372`'s idempotent zero-run still holds (a second erase → scrub-count == 0).
- **Erasure-completeness claim — bounded honestly:** after `erase_source`, BOTH (i) a fresh `full_rebuild`
  and (ii) the live graph are erased-free (values + anchors, anchors verified by a DIRECT live-node read).
  This is **NOT** "the divergence guard goes green on the corpus": the incremental-projector
  anchor-retraction bound + superseded-node deletion stay deferred to Gate 3b (D-i); the zero-prop-member
  decision residual stays WPI-1; a pure-source-less P1 fallback row is forward-eliminated by the named
  P1-writer dependency (SF-1 / §9).
- `quality` + `security` (+ `adr-index`) CI green before merge; `gh pr checks <N> --watch` before any merge.
- ADR 0107 `human_cosign` stamped (dated) at the accept flip; the checker + judge reproduce the
  `person_affecting: true` self-tag against the diff and DENY on an un-cosigned-at-merge diff (ADR 0097 §5).

---

## 7. Invariants the checker MUST reproduce (INV-ERASE-*)

- **INV-ERASE-3LANE** — `erase_source` (and `scrub_stock`) DELETE `statement` + `context_claim` rows reached
  by `(dataset==source_id) OR (entity_id ∈ erased_member_ids)` AND redact the erased refs out of
  `decision.member_ids`; NO other lane row is touched; `canonical_id_ledger` +
  `ResolverJudgement`/`SignOff`/`MergeAudit` are preserved (ADR-0049 no-un-merge carve-out). (P-ERASE-1 /
  IT-ERASE-flow)
- **INV-ERASE-DECISION-REDACT** — a decision row's `member_ids` has exactly the erased refs removed (derived
  from the pre-scrub reached rows — compute-before-delete); the row itself is preserved
  (`kind`/`score`/`decided_by`/`evidence`/surviving members intact); the redaction REASSIGNS the JSONB list
  (in-place `.remove()` does NOT persist); a `full_rebuild` reconstructs the survivor identically. (P-ERASE-3)
- **INV-ERASE-LIVE-VALUE** — after erase, the live graph holds NO erased-source-only value: sole-source
  nodes DETACH-DELETEd; a value contributed ONLY by the erased source is removed from a co-witnessed prop
  (compared props == `reconstruct_entities`' row-granular result); an erased-source anchor value is
  **REMOVEd (REMOVE-only, never SET — the Neo4j UNIQUE constraint)** and asserted by a **DIRECT live-node
  property read**, NOT `measure_divergence` (which excludes anchors). (P-ERASE-1 clause (ii))
- **INV-ERASE-PROV-PRESERVED** — the live value-prune reads the node's CURRENT full props and merges (no
  bare `SET n = <partial map>`) so `prov_source_id`/`prov_witnesses`/`datasets`/`id`/`caption` are never
  wiped (G1 upheld). (P-ERASE-4)
- **INV-ERASE-NONRESURRECT** — a `full_rebuild` over the scrubbed log into a FRESH target contains nothing
  of the erased source. (P-ERASE-1 clause (i) / P-ERASE-2)
- **INV-ERASE-BOTH-SURFACES** — P-ERASE-1 asserts BOTH (i) fresh-rebuild AND (ii) live-graph erased-free,
  with BOTH vacuity fences (fresh-target-only REJECTED; `measure_divergence`-as-anchor-oracle REJECTED).
- **INV-ERASE-STOCK** — the one-off stock scrub enumerates every erasure since the dual-write window from
  `TaskRun(kind="erase").stats["source_id"]` (read Python-side) and scrubs each once (idempotent), verified
  by rebuild-contains-no-erased-source. (IT-ERASE-stock)
- **INV-ERASE-FOLD-DR** — `full_rebuild` over the scrubbed log is erased-free unconditionally; **P-FOLD-2
  stays proven byte-unchanged**; the incremental-after-scrub bound is documented + the non-`seq`
  `refold_pending` re-fold-trigger contract is recorded for the projector-wiring gate (no consumer in P2).
  (P-ERASE-2 / SF-7)
- **INV-ERASE-CROSS-STORE-RECOVER** — erasure is cross-store-non-atomic (Neo4j immediate / Postgres staged);
  a post-Neo4j / pre-commit failure is recovered by an idempotent retry (resurrection-then-recovery proven).
  (IT-ERASE-idempotent)
- **INV-ERASE-APPENDONLY-CARVEOUT** — the scrub's DELETE/UPDATE is confined to the erasure entry points and
  unreachable from any writer/pipeline/agent path; the append-only writers (`statements.py`) are
  byte-unchanged; the P1 DB-level detector stays green; `erase_source` still requires `authorized_by`. This
  is **PROVEN by a POSITIVE test** (IT-ERASE-appendonly): the normal pipeline emits ZERO DELETE/UPDATE
  against `statement`/`context_claim`/`decision`, AND `scrub_log_lanes` DOES emit exactly those.
- **INV-ERASE-MIGRATION** — the ONLY schema change is migration `0013` (index on `statement.dataset` +
  `context_claim.dataset`; model + migration byte-agree, ADR 0030); **no erasure-event table** (SF-3(a));
  no other table/column added.
- **INV-ERASE-F2-DEFER** — `llm_egress` is NOT a P2 scrub lane; the `entity_manifest × erasure` design is a
  NAMED precondition on the F2 durable-audit **enablement** gate (`llm_egress_durable_enabled` stays False).
- **INV-FROZEN** — every FROZEN glob (§8) is byte-unchanged, EXCEPT `db/models.py` (ONLY `index=True` on the
  two `dataset` columns) and `graph/ops.py` (ONLY the additive `set_node_values` writer; `erase_source_graph`
  byte-unchanged). `resolution/statements.py` stays FROZEN (the P1-writer non-empty-`source_id` dependency
  is routed to a WPI slice, §9).

---

## 8. FROZEN (byte-unchanged — the checker verifies `git diff` touches none of these)

- **`resolution/projector.py`** — P2 imports `reconstruct_entities` + `build_survivor_of` **read-only** as
  the SF-4 parity oracle; **no delete path is added** (SF-3(a)).
- **`resolution/divergence.py`** — SF-4 is prune-to-match, NOT guard-explains; the `_excluded` predicate is
  unchanged (P2 aligns the live graph to it; anchors stay excluded → the SF-6 direct-read fence).
- **`resolution/statements.py`** — the append-only writers are byte-unchanged; the scrub is a SEPARATE
  module; the P1-writer non-empty-`source_id` enforcement is a NAMED dependency routed OUT to a WPI slice
  (§9), NOT built here.
- **`resolution/merge.py`, `resolution/canonical.py`, `resolution/pipeline.py`, `resolution/referents.py`,
  `resolution/signoff.py`** — P2 READS `signoff.py`/`pipeline.py`; edits none.
- **The merge guard** (`resolution/guard.py` / `resolution/review.py` / `guard.sensitivity`),
  **`resolution/eval.py`, `resolution/gold.py`, `resolution/silver.py`**.
- **`graph/writer.py`, `graph/ftmg_fork.py`, `graph/constraints.py`, `ontology/anchors.py`** — the live
  write path + anchor projection + the UNIQUE anchor constraints are unchanged; P2 reuses `get_anchors`
  read-only (and respects the constraint via REMOVE-only, SF-4).
- **`graph/ops.py::erase_source_graph`** — byte-unchanged; P2 adds ONLY the additive `set_node_values`
  writer beside it.
- **`llm/**` (incl. `egress_audit.py`), `mcp/**`, `authz/**`, `api/**`, `runner/**`, `metrics/**`** — no
  wiring change; F2 rider defers `llm_egress`.
- **Existing migrations `0001`–`0012`** — immutable; **P2 adds ONLY `0013`.**
- **P1/P3 property suites** — `test_prop_statement_spine.py`, `test_prop_context_claim_capture.py`,
  `test_prop_fold_engine.py` (**P-FOLD-2 byte-unchanged**), `test_prop_projection_divergence.py`,
  `test_prop_signoff_spine.py`, and `test_context_claim_lane.py`'s DB-level append-only detector — stay
  green unchanged.

---

## 9. OUT OF SCOPE (do NOT build here — see `81_PRECUTOVER_GATE_SEQUENCE.md`)

- **The seq-bearing erasure-event lane + a projector delete path** (SF-3(b)) — the documented (a → b)
  revisit path; NOT built in P2 (would un-freeze the projector + add a table/migration).
- **The incremental-projector anchor-retraction bound** (ADR 0106 §Verification) + **superseded-node
  deletion** (§7-8) — need the projector delete path; travel together to **Gate 3b** (safety net:
  full_rebuild-correct + guard-green over N cycles).
- **The P1-writer non-empty-`source_id` enforcement** — a NAMED P2 dependency (SF-1; ADR 0106 §Verification
  minor finding (i)) that forward-eliminates pure-source-less fallback rows; routed to a **WPI slice** so
  `statements.py` stays FROZEN. P2's `(dataset OR entity_id)` reach closes every source-linkable row now.
- **`llm_egress` erasure** — a NAMED precondition on the F2 durable-audit **enablement** gate (F2 rider).
- **The zero-prop-zero-anchor member decision residual** — **WPI-1** (§7-3).
- **Divergence guard N-cycle green / the exclusion-surface audit / cutover mechanics** — Gate
  3b-planning-proper.
- Any change to ER/thresholds/merge/guard/gold/scores, the live graph WRITE path, or the append-only writers.

---

## Surprises (code facts the ADR skeleton did not anticipate — disclose at cosign)

1. **`erase_source_graph` is value-INCOMPLETE on the live graph** — co-witnessed erased-only property
   VALUES survive (prop-granular witness map, `graph/ops.py:106-136`) and erased ANCHOR values are never
   touched (anchors are not in the witness map). The skeleton framed the granularity gap as a *divergence*
   artefact; it is also a **GDPR-completeness gap**. SF-4 closes both. **Load-bearing.**
2. **The `decision` lane has NO `dataset` column** → reached only via `member_ids ∩ (erased_member_ids ∪ …)`,
   forcing **compute-erased-member-set-BEFORE-DELETE** ordering.
3. **The projector reads `decision` rows ONLY for the watermark** (never `member_ids`) → SF-2 redact-in-place
   cannot corrupt reconstruction (the required confirmation).
4. **`statement.dataset` + `context_claim.dataset` are UNINDEXED** → SF-1 requires migration `0013` (D-iv).
5. **The projector is dormant/isolated and the guard uses `full_rebuild`** → SF-7's incremental-after-delete
   staleness only affects the dormant fold engine; `full_rebuild` over the scrubbed log is correct.
6. **`erase_source` has no live API/runner/MCP caller** → SF-5's stock source of truth is
   `TaskRun(kind="erase").stats["source_id"]` (read Python-side); the scrub is exercised via the erasure
   entry points.
7. **P2 extends the append-only erasure carve-out to the SoR spine** — PROVEN confined by a POSITIVE test
   (the normal pipeline emits ZERO DELETE/UPDATE against the three lanes AND `scrub_log_lanes` DOES emit
   exactly those), not just left to the trivially-green P1 detector. The **P1 detector stays green**.
8. **The bare anchor keys carry a Neo4j UNIQUE constraint** (`graph/constraints.py:24-30`) → the SF-4 anchor
   prune is **REMOVE-only** (never SET). A prune that surfaced a previously omit-on-conflict surviving value
   (`SET n.wikidata_id = Q2` while another node holds `Q2`) would `ConstraintValidationFailed` and abort the
   erasure mid-transaction. REMOVE-only is GDPR-complete + guard-neutral; the "align-to-fold ADD" option is
   **deleted**.
9. **The value-prune live write MUST read-current-props-then-merge** (`SET n = $full_props`, never a bare
   `SET n = <partial map>`) so it never drops `prov_*`/`id`/`caption`/`datasets` (plan-verify HIGH-1;
   pinned by P-ERASE-4).
10. **Erasure is cross-store-non-atomic** (Neo4j immediate / Postgres staged, the split `erase_source`
    already has) → a post-Neo4j / pre-commit failure needs an idempotent retry to converge; pinned by
    IT-ERASE-idempotent.
11. **The P1 writer's `dataset = source_id or member.id or ""` fallback** makes a pure-source-less member's
    rows unreachable-by-source; the `(dataset OR entity_id)` reach closes source-linkable rows, the named
    P1-writer dependency (§9) forward-eliminates the rest.
