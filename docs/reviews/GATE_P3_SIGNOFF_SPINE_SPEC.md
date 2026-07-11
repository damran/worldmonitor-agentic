# GATE P3 — sign-off spine durability — BUILD SPEC

- **Owning decision:** ADR 0108 (`docs/decisions/0108-signoff-spine-durability.md`), status PROPOSED,
  DECIDED lines filled by the P3 planning gate (2026-07-11) after a 3-lens plan-verify (all FIX_FIRST —
  folded in below). **person_affecting: true → user cosign REQUIRED before the code build; `human_cosign`
  stays PENDING until the accept flip at gate end.**
- **Source:** the Fable log-capture consult (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md` §6b, §7-2 — the
  discovered **CRITICAL**) and the pre-cutover gate sequence (`docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md`
  Gate P3). Anchors verified first-hand against master (Gate P1 merged, #174): `resolution/signoff.py`,
  `resolution/statements.py`, `resolution/canonical.py`, `resolution/pipeline.py`, `resolution/merge.py`,
  `resolution/projector.py`, `resolution/divergence.py`, `metrics/collector.py`, `runner/driver.py`,
  `db/models.py`, and FtM `Statement.id` behaviour (run under `uv run python`).
- **Governance:** `human_fork: false`, `person_affecting: true`, `human_cosign: PENDING`. P3 alters the
  mechanics of the **human sign-off path** → person-affecting **by construction**. The main loop requests the
  user cosign **before the code build** (findings disclosed) and stamps ADR 0108's dated `human_cosign` line
  + flips PROPOSED → ACCEPTED at the **accept flip** (gate end). A judge DENIES on an un-cosigned-at-merge
  person-affecting diff (ADR 0097 §5) — 0108 is correctly tagged `true`; the discipline is: **do NOT stamp
  the cosign at planning, do NOT flip Status.**
- **Branches (planner writes files only — creates no branch):** the **planning docs** (this spec + the ADR
  0108 fill) ship as their **own docs-only PR** from a docs branch (autonomously mergeable — committing the
  filled PROPOSED decision-space is the 0107/0108 committed-draft precedent). The **code** PR follows **after**
  the cosign, on `gate/p3-signoff-spine` cut from `origin/master`. **No new migration** (verified:
  `statement`+`decision` = `0009`, `canonical_id_ledger` = `0006`, `context_claim` = `0012`;
  `decision.decided_by` already `String(255)`).

The test-author writes RED tests first; the builder makes them GREEN without weakening any FROZEN invariant.

---

## 1. Verified current state (do not re-derive; confirm if editing)

| Fact | Location |
|---|---|
| `approve()` writes the graph at `:291` (`[canonical, *edges]`, before commit), then judgements/audit-flip/`sign_off`/P1 context claims, then the SOLE `session.commit()` at `:302` | `resolution/signoff.py:284-302` |
| `reject()` writes the graph at `:347` (`[*members, *edges]`), then judgements/audit-flip/`sign_off`, then the SOLE `session.commit()` at `:354`; members keep their OWN ids (no rewrite) | `resolution/signoff.py:337-354` |
| **Outbound edges are SEPARATE queue entities** (own ids, own provenance) whose source endpoint ∈ members; approve() referent-rewrites them to the canonical at `:287-288`, reject() leaves them unrewritten; NEITHER mutates the edge queue rows' status | `resolution/signoff.py:185-210,287-288,294-295,350-351` |
| **The production guard fires on `ProjectionDivergence.total = unexplained_nodes + unexplained_edges`** over the full snapshot — the Prometheus gauge + the driver's diff | `resolution/divergence.py:67-69`, `metrics/collector.py:184`, `runner/driver.py:426` |
| P1 already co-commits `record_context_claims(...)` at `approve():301` (reject unchanged in P1) | `resolution/signoff.py:298-301` |
| The parked cluster's `canonical_id` is ALREADY the durable id (rekeyed BEFORE the park at `:388-392`); `resolve_durable_id` returns `fallback_id` (the `wmc-` fingerprint) when unanchored and `rekey_cluster` is then a no-op | `resolution/pipeline.py:388-392`, `resolution/canonical.py:320-324`, `resolution/merge.py:269` |
| The pipeline promote block co-commit shape P3 mirrors: `record_durable_id` (is_merge) → `record_statements` → `record_decision` (is_merge) → `record_context_claims`, one commit | `resolution/pipeline.py:472-497`, `resolve_pending:153` |
| `record_statements(session, cluster, by_id)` = pure `session.add`, caller-commits, INSERT-only; works for a singleton cluster | `resolution/statements.py:108-123`, `fuse_statement_rows:50-105` |
| `record_decision(session, cluster, *, reason)` HARDCODES `decided_by="auto:resolver"` and no-ops for a singleton | `resolution/statements.py:125-158` |
| `record_durable_id(...)` writes the self-row + one alias per member; `record_canonical`/`record_alias` `session.flush()` (the fail-closed delta) and skip-if-exists (idempotent) | `resolution/canonical.py:327-360,243-254,257-272` |
| `fuse_context_claim_rows` SKIPS a member with no stamped provenance / no `retrieved_at` (anchor parity is provenance-stamped-only) | `resolution/statements.py:182-190` |
| `_merge_members` does **NOT** stamp a Tier-1 witness map (the pipeline's `_merge_entities` DOES, `merge.py:415-417`) ⇒ the direct sign-off node has no `prov_witnesses`; the fold reconstructs one (fold strictly richer) | `resolution/signoff.py:241-247` |
| The projector reads `decision` rows ONLY to advance `last_decision_seq` — never consumed into node reconstruction | `resolution/projector.py:345-349,83-239` |
| A context-claim-only survivor (zero statement rows) yields NO fold node — `reconstruct_entities` groups by statement rows (graceful no-op) | `resolution/projector.py:159,126,137-141` |
| FtM `Statement.id` = deterministic content hash over **(dataset, entity_id, prop, value)** — NOT schema, NOT canonical_id (verified first-hand) | `uv run python` probe; `followthemoney.Statement` |
| `measure_divergence(...)` with `_excluded` = {id, caption, `CANONICAL_ID_FIELDS`, datasets, `prov_*`} is the guard-green oracle | `resolution/divergence.py:72-102,128-171` |
| P1 integration test (f) pins the no-op VIA `signoff.approve()` — its "0 statement rows" precondition BREAKS under P3 | `tests/integration/test_context_claim_lane.py:608-669` |
| `uq_er_queue_dedup` is on `(source_record, entity_id)` — two rows CAN share an `entity_id` (derive the member set with `set(...)`) | `db/models.py:64` |
| `decision.decided_by` = `String(255)`, NOT NULL; `sign_off.approver` = `String(255)` | `db/models.py:388,221` |
| Existing sign-off behaviour + B-1 idempotency + poison isolation are pinned by | `tests/integration/test_signoff.py`, `test_b1_signoff_idempotency.py`, `test_b6_signoff_poison.py`; `_ownership(edge_id, owner, asset)` helper at `test_signoff.py:83` |

---

## 2. The gate — two independent, individually-mergeable slices

Both slices are additive `session.add`-family calls inside the **existing** sign-off transaction; the graph
write and the approve/reject decision are byte-unchanged. Recommended as **ONE code PR** after cosign (small
gate, P1 precedent), but the split lets a reviewer land `approve()` (the CRITICAL person-affecting core)
first. Slice P3-a owns the one-line `statements.py` param; Slice P3-b needs no `statements.py` change.

**SF-EDGE reminder (do NOT capture edges in the block).** `approve()`/`reject()` write outbound edge
entities, but P3 records **no** statement rows for them: an edge reconstructs from its OWN pipeline
promotion, and `record_durable_id`'s member→canonical aliases (approve) are what rewrite the fold edge's
endpoint to match the live rewrite. A still-`pending` edge is transiently unexplained until its own promotion
(pre-existing ingestion-ordering property — §Surprises). This keeps the block small and correct.

### Slice P3-a — `approve()` co-commit (the CRITICAL; carries the person-affecting weight)

**2.a.1 `statements.record_decision` — additive `decided_by` param.** Add a keyword parameter with a
backward-compatible default so the pipeline call is byte-behaviour-identical:
```
def record_decision(session, cluster, *, reason: str, decided_by: str = "auto:resolver") -> None:
    ...
    session.add(DecisionRecord(..., decided_by=decided_by, ...))   # was the hardcoded "auto:resolver"
```
Everything else in `statements.py` (the singleton `is_merge` guard, `evidence` shaping,
`fuse_statement_rows`, `record_statements`, `fuse_context_claim_rows`, `record_context_claims`,
`fuse_statement_entity`) stays **byte-unchanged**. Update ONLY the `record_decision` docstring's
`decided_by="auto:resolver"` note to reflect the reserved human path (ADR 0099 / 0108 SF-3).

**2.a.2 `signoff.approve()` — additive spine block.** After `record_context_claims(...)` at `:301` and
**before** `session.commit()` at `:302` (purely additive — do NOT edit the existing `:284`/`:291`/`:293-301`
lines), mirroring the pipeline promote block:
```
# Gate P3 (ADR 0108): co-commit the SoR spine writes beside the P1 context capture — statements (so a
# rebuild reconstructs this human-approved merge), a decision row attributing it to the operator, and the
# ledger self-row + member aliases (so survivor_of resolves the collapsed members AND rewrites the outbound
# edges' endpoints, SF-EDGE). SAME transaction as the SignOff/judgement rows: a crash before session.commit()
# rolls them ALL back (SF-2); the graph write above is idempotent on the B-1 re-run.
members = [make_entity(r.raw_entity) for r in member_rows]
signoff_cluster = ResolvedCluster(
    canonical_id=canonical_id,
    member_ids=tuple(sorted({m.id for m in members if m.id})),   # set(): uq_er_queue_dedup allows a shared id
    entity=canonical,
    score=audit.score,
)
by_id = {m.id: m for m in members if m.id}
if signoff_cluster.is_merge:
    record_durable_id(session, canonical_id, member_ids=list(signoff_cluster.member_ids), prior_id=None)
record_statements(session, signoff_cluster, by_id)
if signoff_cluster.is_merge:
    record_decision(session, signoff_cluster, reason=reason, decided_by=f"operator:{approver}")
```
- **No edge capture** — the outbound edges written at `:291` reconstruct from their own pipeline promotion;
  `record_durable_id`'s aliases rewrite their endpoints at fold time (SF-EDGE).
- `prior_id=None`: the parked cluster is already rekeyed to its durable id; the prior `wmc-` fingerprint was
  never materialised in the graph. The durable id is an `wm-anchor-…` id when the members anchor, **else the
  `wmc-` fingerprint itself** — a `wmc-` ledger self-row is VALID (INV-SIGN-LEDGER).
- The `is_merge` guards are **defensive dead code** (build-time correction, 2026-07-11): `needs_review`
  short-circuits on `not cluster.is_merge` and every park axis requires a merge, so `approve()`/`reject()`
  only ever see 2+-member merges — there is **no reachable parked-singleton case**. The guards stay (cheap,
  harmless), but the parked-singleton test examples are removed as unreachable (see ADR 0108 §Decided P-SIGN-1).
- Imports (signoff.py): `from worldmonitor.resolution.merge import ResolvedCluster`;
  `from worldmonitor.resolution.canonical import record_durable_id`; extend the statements import to
  `record_context_claims, record_decision, record_statements`. (No import cycle — `merge`/`canonical` do not
  import `signoff`.)

### Slice P3-b — `reject()` co-commit (the member-write equivalent)

**2.b.1 `signoff.reject()` — additive per-member statement block.** After `session.add(_signoff_row(...))`
at `:353` and **before** `session.commit()` at `:354`, reusing the `members` list already built at `:342`:
```
# Gate P3 (ADR 0108): co-commit statement rows for EACH rejected member kept as its own entity, so a rebuild
# reconstructs the reject-written member nodes (else a full rebuild silently drops them — consult §6b). Each
# member is its own canonical/survivor: NO merge decision, NO ledger alias. Outbound edges (unrewritten,
# endpoints = member ids) reconstruct from their own pipeline promotion (SF-EDGE). SAME transaction (SF-2).
for member in members:
    if member.id is None:
        continue
    member_cluster = ResolvedCluster(
        canonical_id=member.id, member_ids=(member.id,), entity=member, score=1.0,
    )
    record_statements(session, member_cluster, {member.id: member})
```
Imports: `ResolvedCluster` + `record_statements` (shared with P3-a; whichever slice merges first adds them).
No `record_decision`, no `record_durable_id`, no edge capture.

---

## 3. Property invariants (@given — RED-first)

NAME · STATEMENT · GENERATOR · ORACLE · NON-VACUITY. New file `tests/property/test_prop_signoff_spine.py`.
Container-backed examples MUST wrap any per-example engine in `try/finally` dispose (memory:
given-red-tests-leak-connections). All P-SIGN-* are **RED before the builder wires §2**. **The guard oracle
is `measure_divergence(...).total == 0` (nodes AND edges) — never `unexplained_nodes` alone.**

**P-SIGN-1 — approved-merge fold round-trip (Slice P3-a; real Postgres + ISOLATED Neo4j fold target).**
- *Statement:* for a parked merge promoted by `approve()`, after `project(full_rebuild=True)` into an
  isolated target: (i) `measure_divergence(live, fold, survivor_of).total == 0` (the sign-off survivor AND
  its statement-bearing outbound edges are explained); (ii) `get_anchors(fold_node) == get_anchors(direct_node)`
  **for provenance-stamped members** (clause (ii) inherits P1's INV-CTX-PROV — an unstamped member's anchor
  is intentionally NOT reconstructed); (iii) exactly ONE `decision` row for the survivor with `kind="merge"`,
  `decided_by == f"operator:{approver}"`, `set(member_ids) == set(source_ids)`, `score`; (iv) the
  `canonical_id_ledger` holds the self-row (`canonical == alias == survivor`) + one alias per collapsed
  member, and `survivor_of(member_id) == survivor`.
- *Generator:* `@given` over parked merges — MUST include an **anchored** example, an **unanchored (`wmc-`
  durable id / `wmc-` self-row)** example, and an **edge-bearing** example (an `Ownership` whose `owner` is a
  reviewed member, itself independently promoted/statement-bearing in the setup — reuse `_ownership`).
  **No parked-singleton example** (build-time correction: singletons are never parked — see §2/§Surprises). The
  rows, ZERO ledger aliases, `.total == 0`).
- *Oracle:* `measure_divergence` (guard predicate) + independent DB reads of `decision`/`canonical_id_ledger`;
  anchors re-derived from the direct node.
- *Non-vacuity:* an impl omitting `record_statements` → survivor unexplained, `.total > 0` (fail); omitting
  `record_durable_id` → a member id (and the edge endpoint) does not resolve to the survivor → `.total > 0`
  (fail); omitting the decision → assertion (iii) fails; writing `decided_by="auto:resolver"` → fail.

**P-SIGN-2 — reject round-trip to member nodes (Slice P3-b; real Postgres + isolated fold).**
- *Statement:* for a parked merge rejected by `reject()`, after `full_rebuild`: `measure_divergence.total ==
  0` (each member folds to its OWN node with the member's own statements, and each statement-bearing outbound
  edge folds correctly — endpoints stay member ids, matching the unrewritten live edge); NO `decision` row;
  NO `canonical_id_ledger` alias for the members (`survivor_of(member.id) == member.id`).
- *Generator:* `@given` over rejected parked merges, incl. an **edge-bearing** example.
- *Oracle:* `measure_divergence.total` + `SELECT count(*)` of `decision`/alias rows for the members == 0.
- *Non-vacuity:* an impl writing a decision row for reject fails; one writing a ledger alias fails; one
  omitting `record_statements` → member nodes unexplained, `.total > 0` (fail).

**P-SIGN-3 — co-commit atomicity, RED-first with the positive control folded in (P3-a for approve; P3-b for reject).**
- *Statement:* on a **successful** commit the spine rows are **PRESENT** (statement + decision + ledger +
  `sign_off`) AND `merge_audit.decision` is terminal; when the sign-off `session.commit()` is forced to raise
  (monkeypatch the session's `commit`), a FRESH session sees **ZERO** new spine rows and `merge_audit.decision
  == "pending_review"` (all rolled back together), while the graph MAY hold the idempotent node(s). A
  `session.commit` **call-count spy asserts exactly 1** commit on the success path (the "no second commit"
  guardrail — a two-commit impl's first commit would itself raise under the forced failure, so the spy, not
  the row count, is the discriminator).
- *Generator:* `@given` over parked merges; deterministic failure injection.
- *Oracle:* success-path row presence + commit-count == 1; failure-path fresh-session row count == 0 +
  `pending_review`.
- *Non-vacuity:* the success/positive control writes the rows (so the test is not trivially green against
  today's zero-spine-row master); an impl that used a SECOND commit for the spine writes fails the spy.

**P-SIGN-4 — B-1 idempotent re-run convergence (Slice P3-a + P3-b).**
- *Statement:* running `approve()` (resp. `reject()`) TWICE — the second either after a rolled-back first
  attempt (crash-before-commit ⇒ a full re-run) OR after a fully-committed first attempt (⇒ the
  `already_applied` no-op) — converges: a `full_rebuild` fold after the second run has the identical
  equivalence signature to the fold after a single successful run; the survivor resolves identically; the
  survivor's `decision`-row count is exactly 1 in BOTH cases.
- *Generator:* `@given` over parked merges × a boolean {crash-first, commit-first}.
- *Oracle:* fold-signature equality (1 run vs 2 runs) + `decision`-row count == 1.
- *Non-vacuity:* an impl whose crash-first re-run double-writes and diverges the fold fails; one that raises
  on the commit-then-rerun instead of `already_applied` no-op fails.

---

## 4. Unit + integration tests

- **`tests/unit/test_statements.py` (extend):** `record_decision(..., decided_by="operator:alice")` writes
  that exact string; the DEFAULT call still writes `"auto:resolver"` (the pipeline byte-behaviour pin); a
  singleton cluster still writes NO decision row regardless of `decided_by`.
- **`tests/integration/test_signoff_spine.py` (NEW, `pytest.mark.integration`, real Postgres + Neo4j — Docker
  IS available locally, run it):**
  - **IT-SIGN-approve:** drive a real park (`resolve_pending` block-mode on a sensitive/oversized anchored
    corpus that ALSO promotes an `Ownership` edge whose `owner` is a reviewed member) → `approve(...,
    approver="op-x")`; assert `statement` rows for the survivor, a `decision` row with
    `decided_by="operator:op-x"`/`kind="merge"`, ledger self-row + member aliases; then
    `project(full_rebuild=True)` into an isolated target yields a NODE for the survivor (the **un-dormanting**
    — flips the P1 no-op) and **`measure_divergence.total == 0`** (node AND the rewritten edge explained),
    anchors present on the fold node.
  - **IT-SIGN-reject:** the same park → `reject(...)`; per-member `statement` rows, ZERO decision rows, ZERO
    member aliases; `full_rebuild` yields each member's OWN node + the unrewritten edge, `.total == 0`.
  - **IT-SIGN-atomic:** the P-SIGN-3 success-then-forced-failure pair at real-DB scale (approve + reject) —
    success writes the rows with commit-count 1; forced failure ⇒ a fresh session sees zero spine rows and
    `pending_review`.
  - **IT-SIGN-decided-by-bound:** `approver` of length **246** (exact fit ⇒ `decided_by` == 255 persists
    without truncation); document (comment, not a hard-failing assert) that **247** overflows — the
    cosign-disclosure edge.
  - **IT-SIGN-pending-edge (SF-EDGE honesty):** an outbound edge left `pending` at approve() time is
    transiently unexplained (`.total > 0` for the edge) until a subsequent `resolve_pending` promotes it, then
    `.total == 0` — pins the ingestion-ordering property as intended, not a P3 defect.
- **`tests/integration/test_context_claim_lane.py` (FLIP test (f)):** `test_context_only_survivor_after_
  signoff_approve_is_projector_no_op` breaks under P3 (approve now writes statement rows). Repoint it to a
  **synthetic** statement-less survivor — insert a `ContextClaimRecord` for a `canonical_id` with NO
  `StatementRecord`, then `project(full_rebuild=True)` and assert NO node (the genuine projector no-op is
  preserved). Rename to reflect the synthetic construction (e.g. `test_context_only_survivor_without_
  statements_is_projector_no_op`). The sign-off-approve un-dormanting is asserted by IT-SIGN-approve.
- **`tests/integration/test_signoff.py` (extend):** the existing approve/reject behaviour (orphan guards,
  judgements, audit flip, `sign_off`, graph writes, `already_applied`) stays green **unchanged**; add
  assertions that the additive spine rows are present after approve/reject.

---

## 5. Builder task list (ordered)

**Planning docs (this planner's output; ship FIRST as a docs-only PR from a docs branch, no cosign):** ADR
0108 DECIDED-lines fill (done), this spec, `.claude/gate.scope` (P3 contract). Merge on green (docs-only). The
0108 index row is unchanged (still PROPOSED) — no `README.md` regen in the planning PR.

**Slice P3-a (code; after cosign):** 1) `statements.record_decision` additive `decided_by` param + docstring.
2) `signoff.approve()` spine block + imports. 3) make P-SIGN-1, P-SIGN-3(approve), P-SIGN-4(approve),
`test_statements.py` unit, IT-SIGN-approve, IT-SIGN-atomic(approve), IT-SIGN-decided-by-bound,
IT-SIGN-pending-edge, and the flipped `test (f)` GREEN.

**Slice P3-b (code):** 1) `signoff.reject()` spine block. 2) make P-SIGN-2, P-SIGN-3(reject),
P-SIGN-4(reject), IT-SIGN-reject GREEN.

**Both slices = ONE code PR** on `gate/p3-signoff-spine` (recommended). At the accept flip (post-cosign): stamp
ADR 0108's `human_cosign` dated line, flip PROPOSED → ACCEPTED, re-run `uv run python scripts/gen_adr_index.py`
(the 0108 row status flips), `--check` passes. **The accept flip must not occur on a PENDING cosign line.**

Cosign gate: the main loop asks the user **before the code build** (person_affecting:true), discloses the
plan-verify findings (esp. the fail-closed behavioral delta and the PRE-P3-nodes-need-Gate-2b scope), and
stamps ADR 0108's `human_cosign` dated line at the accept flip / merge.

---

## 6. Acceptance criteria (all measurable)

- **FULL** `uv run pytest -m "not integration"` GREEN repo-wide (the `quality` job runs exactly this).
- **FULL local integration suite GREEN** (`uv run pytest -m integration`) — explicitly including
  `test_signoff_spine.py`, the extended `test_signoff.py`, **`test_b1_signoff_idempotency.py`**,
  **`test_b6_signoff_poison.py`**, the flipped `test_context_claim_lane.py`, and the unchanged
  `test_migrations.py` drift guard.
- All new `@given` properties GREEN: P-SIGN-1..4, each asserting **`measure_divergence.total == 0`**.
- **FROZEN-adjacent suites stay green unchanged:** `test_prop_statement_spine.py` (P-STMT-1/2/3),
  `test_prop_context_claim_capture.py` (P-CTX-1..7), `test_prop_fold_engine.py`,
  `test_prop_projection_divergence.py`, and every IT-PROJ.
- `ruff format --check .` (REPO-WIDE) clean; `ruff check .` clean; `uv run pyright` clean.
- **No new migration / no schema change:** `git diff` adds no file under `db/migrations/versions/` and changes
  no `db/models.py` model; `test_migrations.py` passes untouched.
- **Guard-green claim — bounded honestly (plan-verify HIGH-2):** after a **POST-P3** park+approve, the
  sign-off survivor and its **statement-bearing** outbound edges are explained by the fold
  (`measure_divergence.total == 0`). This is **NOT** "the guard goes green on the corpus": **PRE-P3
  already-written sign-off nodes** carry zero statement rows and remain unexplained until **Gate 2b** backfill
  (`81_PRECUTOVER_GATE_SEQUENCE.md` item 9); a **zero-FtM-property** sign-off canonical remains unexplained
  (WPI-1 / edge (d)); an outbound edge still `pending` at approve() time is transiently unexplained until its
  own pipeline promotion (SF-EDGE). Full §7-10 N-cycle guard-green is the operational 3b condition depending
  also on P1/P2/2b.
- `quality` + `security` (+ `adr-index`) CI green before merge; `gh pr checks <N> --watch` before any merge.
- ADR 0108 `human_cosign` stamped (dated) at the accept flip; the checker + judge reproduce the
  `person_affecting: true` self-tag against the diff and DENY on an un-cosigned-at-merge diff (ADR 0097 §5).

---

## 7. Invariants the checker MUST reproduce (INV-SIGN-*)

- **INV-SIGN-APPROVE-SPINE** — `approve()` co-commits, in the ONE existing transaction, `record_statements`
  (fused canonical) + `record_durable_id` (self-row + member aliases, is_merge) + `record_decision`
  (`kind="merge"`, is_merge). NO edge statement rows are written in the block (SF-EDGE). (P-SIGN-1)
- **INV-SIGN-REJECT-SPINE** — `reject()` co-commits `record_statements` for EACH member as its own singleton
  cluster; NO decision, NO ledger alias, NO edge capture. (P-SIGN-2)
- **INV-SIGN-DECIDED-BY** — the sign-off decision's `decided_by == f"operator:{approver}"` (distinct from
  `"auto:resolver"`); `record_decision`'s added `decided_by` param defaults to `"auto:resolver"` so the
  pipeline call is byte-unchanged. (P-SIGN-1 / unit)
- **INV-SIGN-LEDGER** — `approve()` writes the ledger self-row + one alias per collapsed member; every
  `survivor_of(member) == survivor`; **a `wmc-` durable self-row is VALID** (the unanchored fallback);
  `reject()` writes NO alias. (P-SIGN-1/2)
- **INV-SIGN-FOLD-EXPLAINED** — after approve()+`full_rebuild` **`measure_divergence(...).total == 0`** for the
  sign-off survivor AND its statement-bearing outbound edges (nodes AND edges — the guard fires on `.total`);
  after reject()+`full_rebuild` each member node + its unrewritten edge is explained (`.total == 0`). Scope:
  POST-P3 statement-bearing survivors; NOT pre-P3 nodes (Gate 2b), NOT zero-prop (WPI-1), NOT still-pending
  edges (SF-EDGE). (P-SIGN-1/2)
- **INV-SIGN-ATOMIC** — all P3 spine rows commit under the ONE existing `session.commit()` (approve `:302` /
  reject `:354`); a forced commit-failure rolls back EVERY new Postgres row together with `merge_audit`
  staying `pending_review`; **a `session.commit` call-count spy == 1 on the success path** proves no second
  commit is added. (P-SIGN-3)
- **INV-SIGN-IDEMPOTENT** — a B-1 re-run (crash-first OR commit-first) converges the fold to the identical
  survivor with no duplicate divergence; committed-then-rerun hits `already_applied`; statement rows dedup by
  content-hash `statement_id`; the survivor's decision-row count is exactly 1. (P-SIGN-4)
- **INV-SIGN-FAIL-CLOSED** — a spine-write failure (ledger flush error / `decided_by` overflow) rolls back the
  WHOLE approve()/reject() (the human decision is atomic with its SoR record); a retry is idempotent under
  B-1. This is the deliberate, disclosed behavioral delta (ADR 0108 SF-2). (P-SIGN-3 / disclosure)
- **INV-SIGN-NO-GRAPH-CHANGE** — the live graph write (entities/edges to `write_entities`), the approve/reject
  decision, orphan guards, `_record_judgements`, the audit flip and the `sign_off` row are byte-identical with
  the P3 spine writes on vs off; P3 adds NO node property and NO witness map. (non-mutation fence)
- **INV-SIGN-CONTEXT-UNDORMANT** — a sign-off-approved survivor that yielded NO fold node in P1 now yields a
  node (statements present) with its P1 context anchors attached (for stamped members); the genuine
  statement-less no-op stays a graceful no-op, re-pinned synthetically. (edge (c) / flipped test (f))
- **INV-SIGN-NO-MIGRATION** — the P3 diff adds no Alembic migration and no `db/models.py` schema change.
- **INV-FROZEN** — every FROZEN glob (§8) is byte-unchanged, EXCEPT `statements.record_decision` which may
  change ONLY to add the defaulted `decided_by` param + its docstring (so a strict INV-FROZEN checker does not
  false-flag the intended edit).

---

## 8. FROZEN (byte-unchanged — the checker verifies `git diff` touches none of these)

- **`resolution/merge.py`** (fusion + value-set + `ResolvedCluster` + `_member_statements`) — P3 READS it.
- **`resolution/canonical.py`** — P3 CALLS `record_durable_id` but the module itself is byte-unchanged.
- **`resolution/pipeline.py`** — the promote block P3 mirrors; READ, never edited (the `record_decision`
  default preserves its call).
- **`resolution/projector.py`** and **`resolution/divergence.py`** — P1 just landed them; P3 READS, never edits.
- **The merge guard** (`resolution/guard.py` / `resolution/review.py` / `guard.sensitivity`),
  **`resolution/referents.py`**, **`resolution/eval.py`**, **`resolution/gold.py`**, **`resolution/silver.py`**.
- **`graph/writer.py`** and **`graph/ftmg_fork.py`** — the live graph write is unchanged.
- **`ontology/anchors.py`** — P3 touches no anchors.
- **`llm/**`, `mcp/**`, `authz/**`, `api/**`, `runner/**`, `metrics/**`** — no wiring change; P3 reads
  `runner/driver.py` + `metrics/collector.py` for the `.total` fact only.
- **Existing migrations `0001`–`0012`** — immutable; **P3 adds NO new migration.**
- **Every `db/models.py` model** — no schema change (`decision.decided_by` already `String(255)`).
- **`resolution/statements.py`:** `fuse_statement_rows`, `record_statements`, `fuse_context_claim_rows`,
  `record_context_claims`, `fuse_statement_entity` byte-unchanged; **`record_decision` may change ONLY to add
  the defaulted `decided_by` param + its docstring** (INV-FROZEN carve-out).
- **`resolution/signoff.py` beyond the two additive spine blocks + their imports** — the graph writes, orphan
  guards, `_record_judgements`, audit flip, `_signoff_row`, `_merge_members`, `_member_rows`,
  `_outbound_edges`, `list_parked`, `_require_audit`, and the `already_applied`/refuse state machine are
  byte-unchanged.
- **P1's property suites** — `test_prop_statement_spine.py`, `test_prop_context_claim_capture.py`,
  `test_prop_fold_engine.py`, `test_prop_projection_divergence.py` — stay green unchanged.

---

## 9. OUT OF SCOPE (do NOT build here — see `81_PRECUTOVER_GATE_SEQUENCE.md`)

- **Sign-off-block edge statement capture** (SF-EDGE) — edges reconstruct from their own pipeline promotion +
  the P3 ledger alias; a still-pending edge is transiently unexplained (a pre-existing ingestion-ordering
  property, NOT a P3 defect).
- **Backfill of PRE-P3 already-written sign-off nodes** — **Gate 2b** (`81_PRECUTOVER_GATE_SEQUENCE.md` item
  9). P3 logs only future approve()/reject(); pre-P3 sign-off nodes stay unexplained on a rebuild until 2b.
  **Cosign-visible.**
- **Erasure reaching the SoR** (three-lane scrub incl. the sign-off statement/decision/context rows P3 now
  writes; live-removal mechanism; granularity reconciliation; stock scrub; P-FOLD-2 deletion bound) —
  **Gate P2** (ADR 0107).
- **The alias⇔co-commit invariant ASSERTION** (WPI-2 / §7-6) — independent of P3.
- **Zero-FtM-property sign-off canonical disposition** (edge (d) / §7-3) — **WPI-1**.
- **Restoring inbound cross-references** on approve (deferred Gate C, ADR 0031) — unchanged by P3.
- **Stamping a witness map on the direct sign-off write** — a live-graph-write change P3 deliberately avoids.
- Any change to ER/thresholds/merge/guard/gold/scores/erasure, migrations, or the live graph write path.

---

## Surprises (code facts the ADR skeleton did not anticipate — disclose at cosign)

1. **The production guard fires on `.total` = unexplained_nodes + unexplained_edges** (`divergence.py:67-69`,
   `metrics/collector.py:184`, `runner/driver.py:426`) — and `approve()`/`reject()` write **outbound edge
   entities** (`_outbound_edges`, `signoff.py:185-210,291,347`). A node-only oracle would let a dropped edge
   pass. The fix is not sign-off-block edge capture — an outbound edge reconstructs from **its own pipeline
   promotion** + P3's `record_durable_id` ledger alias (which rewrites the fold edge's endpoint member-id →
   canonical, matching the live `rewrite_referents`). A still-`pending` edge is transiently unexplained until
   its own promotion — a pre-existing ingestion-ordering property shared with the pipeline path, NOT a P3
   defect. Every P-SIGN/IT-SIGN oracle asserts `.total == 0`, with ≥1 statement-bearing-edge example.
2. **The projector never consumes `decision` rows into node reconstruction** — it reads them only to advance
   `last_decision_seq` (`projector.py:345-349`); the node folds from statement rows + the ledger + context
   claims. So the sign-off `decision` row is for **human-judgement durability / operator attribution / future
   belief-revision**, NOT node byte-parity — and a duplicate decision row (impossible under single-commit +
   `already_applied`, edge (a)) is **harmless** to the fold. P-SIGN-1 asserts the decision at the DB level,
   separately from node parity.
3. **`signoff._merge_members` does not stamp a Tier-1 witness map** (the pipeline's `_merge_entities` does,
   `merge.py:415-417`), so the direct sign-off node has no `prov_witnesses` while the fold reconstructs one —
   **the fold is strictly richer**. `graph_signature` captures all props incl. `prov_*` by default, so a
   raw-signature oracle would FALSE-diverge. P-SIGN-1 uses `measure_divergence` (which excludes
   `prov_*`/`datasets`/`caption`). Pre-existing sign-off limitation P3 leaves unchanged (the fold improves on
   it — a point in favour of cutover).
4. **P3 introduces a deliberate fail-closed behavioral delta** (plan-verify MEDIUM): the spine writes flush
   inside the single sign-off transaction (`record_durable_id` → `canonical.py:254,272`), so any spine-write
   failure now rolls back the WHOLE approve()/reject(). A human decision that pre-P3 would have committed can
   now fail-closed and require an (idempotent) retry — a safety-direction shift, disclosed for the cosign
   (INV-SIGN-FAIL-CLOSED / ADR 0108 SF-2).
5. **FtM `Statement.id` preimage is `(dataset, entity_id, prop, value)`** — not `canonical_id`, not `schema`
   (verified first-hand). The fold's D3 dedup keys on member-claim content independent of canonical; moot for
   the sign-off single-commit, but underwrites edges (a)/(b).
6. **A parked cluster is NEVER a singleton** (build-time correction, 2026-07-11 — this reverses the planner's
   original Surprise #6). `guard/sensitivity.py::needs_review` short-circuits `if not cluster.is_merge:
   return False` ("Singletons are never flagged — nothing is being merged"), and every other park axis (size,
   anchor-conflict, Chow band on a cluster score) also presupposes a merge. So every parked cluster reaching
   `approve()`/`reject()` is a 2+-member merge; `approve()`'s `is_merge=False` branch is **defensive dead
   code** (kept, harmless). No parked-singleton test example exists (removed as unreachable).
7. **The parked `audit.canonical_id` IS the durable id** — an `wm-anchor-…` id when the members anchor, ELSE
   the `wmc-` fingerprint itself (the durable fallback; `resolve_durable_id` returns `fallback_id` +
   `rekey_cluster` is a no-op when unanchored, `canonical.py:320-324`, `merge.py:269`). `prior_id=None` either
   way (the prior `wmc-`, when the durable id is an anchor id, was never materialised in the graph). A `wmc-`
   ledger self-row is VALID.
8. **`uq_er_queue_dedup` is on `(source_record, entity_id)`** (`db/models.py:64`), so two queue rows can share
   an `entity_id`; the P3 member id set uses `set(...)` and asserts `set(member_ids) == set(source_ids)`.

**Spec flag (no structural change):** P-SIGN-1 clause (ii) is the FIRST live validation of context-claim
anchor reconstruction on a sign-off node (dormant in P1). A clause-(ii) failure is a **pre-existing P1
reconstruction bug** whose fix would land in FROZEN `projector.py`/`anchors.py` — a scope change to SURFACE
(halt + re-plan), not a P3 builder task on FROZEN code. Risk low: the same `reconstruct_entities` path is
already exercised on pipeline survivors by P1's IT-PROJ / P-CTX.
