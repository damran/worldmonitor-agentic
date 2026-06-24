# Gate B-6 — Resolution / sign-off integrity

**Gate id:** B-6
**Title:** Resolution / sign-off integrity — transitive cross-schema member isolation (H-2) +
sign-off poison-row guard
**Branch:** `gate/b6-resolution-integrity` (off master HEAD `a6259c6`, post-B-5)
**ADR:** `docs/decisions/0041-resolution-signoff-integrity.md` (status: proposed)
**Human fork:** NONE (`human_fork=false`) — see §9.
**Single gate:** yes (two independent, individually-mergeable slices under one theme).

---

## 1. Problem statement

Two distinct correctness/robustness defects in the resolution module let a single bad input
corrupt or wedge the resolved-entity graph. Both were reproduced against the real code; the H-2
chain was reproduced at runtime via `.venv/bin/python` driving the actual
`cluster_and_merge`/`_merge_entities`.

### Finding H-2 — schema-incompatible members of a transitive cluster are silently swallowed

`src/worldmonitor/resolution/merge.py`:

- `_merge_entities` (lines **213-234**) loops over the cluster's `member_ids` and calls
  `merged.merge(by_id[member_id])`. When a member has **no common FtM schema** with the merge
  base it raises `followthemoney InvalidData ("No common schema")`; the `except InvalidData:`
  branch only `logger.warning`s and **drops the member**, returning a merged entity that silently
  excludes it. There is no channel surfacing the dropped set.
- `cluster_and_merge` (lines **196-209**) constructs the `ResolvedCluster` with the FULL
  `member_ids = tuple(sorted(members))` (line 197) **regardless** of which members were actually
  merged. The dropped members therefore remain in `member_ids`.

This arises on a **transitive cluster**: `Person p1 ~ Person p2` (compatible) and
`Person p2 ~ Company c1` (incompatible). Splink/`score_pairs` scores A~B and B~C separately and
**never compares A~C**, so `score_pairs`' schema-compat gate cannot prevent the cluster being
gathered; FtM merge then raises on the cross-schema member.

**Runtime reproduction (from the design dossier, confidence HIGH):** forcing the transitive cluster
through `StoredJudgement` positives, the real `cluster_and_merge` produced exactly the H-2 chain:

- (a) the WARNING fired (twice — `sorted(member_ids)[0]=='c1'` Company became the merge base, so
  **both** Persons were the dropped members; the failure is symmetric — whichever schema is the
  base, the minority members are silently dropped);
- (b) the dropped members **still remain** in the returned cluster: `member_ids == ('c1','p1','p2')`
  while the merged entity is `schema=Company` containing only `c1` (`p1`/`p2` NOT merged in),
  `is_merge` True;
- (c) downstream blast radius confirmed against current code:
  - `build_referent_map([cluster])` returns `{'c1':canon,'p1':canon,'p2':canon}` — it redirects the
    dropped Person ids `p1`/`p2` onto a **Company** canonical node; any edge naming `p1`/`p2` (e.g.
    `Directorship.director`) would be **rewritten onto a wrong-schema node** (referents.py:42-43 →
    `rewrite_referents`);
  - `record_merge` (pipeline.py:340) audits `source_ids=['c1','p1','p2']` — including the
    silently-dropped ones;
  - `_set_status(cluster.member_ids, 'resolved')` (pipeline.py:341) marks the dropped rows resolved;
  - the leftover safety sweep (pipeline.py:349-362) keys on `clustered_ids` which still contains
    `p1`/`p2`, so they are **not** quarantined.
  - Confirmed `Person.merge(Company)` raises `InvalidData`; a singleton's `canonical_id` is its own
    id, score 1.0, entity its own node. Baseline `tests/unit/test_resolution*.py` + `test_referents.py`
    = **32 passed**.

**Impact:** a wrong-schema merge is written, the dropped member loses its own correct-schema node,
its edges are rewired onto the wrong node, and the skip is **only logged — not durably auditable**.

### Finding (sign-off poison-row wedge) — one malformed `raw_entity` wedges sign-off for a whole tenant

`src/worldmonitor/resolution/signoff.py`:

- `_outbound_edges` (lines **~155-172**) scans **ALL** `ErQueueItem` rows for the tenant with **no
  status filter** (line 163) and calls `make_entity(row.raw_entity)` **unguarded** (line 166).
- `_member_rows` (lines **145-152**) filters `status == 'pending_review'` (line 149) but still calls
  `row.raw_entity.get("id")` / `make_entity` unguarded on those rows.
- `pipeline._quarantine` (~pipeline.py:201) sets `status='invalid'` **without clearing the bad
  `raw_entity`**, so a quarantined poison row keeps a malformed payload sitting in the tenant's queue.
- `approve()`/`reject()` both call these scans → **one poison row's `InvalidData` aborts sign-off
  for EVERY parked merge of that tenant**. The B-2 per-input isolation pattern was never applied to
  sign-off.

**Impact:** availability — a single malformed queue row wedges the human-review path for the entire
tenant.

---

## 2. Scope (exact files / areas)

### Production (allow-list)

- `src/worldmonitor/resolution/merge.py` — **slice-1**.
- `src/worldmonitor/resolution/pipeline.py` — **slice-1**.
- `src/worldmonitor/resolution/signoff.py` — **slice-2**.
- `src/worldmonitor/db/models.py` — **DOCSTRING ONLY** (extend the `IngestDeadLetter.stage` doc list
  to mention the new `'resolve-incompat'` and `'signoff-poison'` stages). **NO column / schema /
  Alembic change.** `stage` stays `String(16)`; the `ingest_dead_letter` table is reused as-is.

### Tests (allow-list)

- `tests/unit/test_resolution_merge_incompat.py` — **NEW, slice-1**.
- `tests/integration/test_b6_resolve_incompat.py` — **NEW, slice-1** (Docker/testcontainers → CI-only,
  integration-marked).
- `tests/integration/test_b6_signoff_poison.py` — **NEW, slice-2** (Docker/testcontainers → CI-only,
  integration-marked).

### Docs / config

- `docs/reviews/GATE_B6_SPEC.md` (this file).
- `docs/decisions/0041-resolution-signoff-integrity.md`.
- `.claude/gate.scope`.

Everything else is **out of scope** (§8).

---

## 3. Fix design (authoritative — from the dossier)

### Slice-1 (H-2), `merge.py` + `pipeline.py`:

1. `_merge_entities` (213-234): change return type from `FtmEntity` to
   `tuple[FtmEntity, tuple[str, ...]]`. In the `except InvalidData:` branch keep the warning AND
   append `member_id` to a local `dropped` list. Return `(merged, tuple(dropped))`. The base
   `member_ids[0]` is always mergeable into itself, so it is never in `dropped`.
2. `ResolvedCluster` (60-73): add ONE defaulted field `merge_incompatible: bool = False`
   (frozen+slots dataclass). The default keeps all **3** existing construction sites valid
   (merge.py:203, `tests/unit/test_referents.py:18`, `tests/unit/test_resolution_anchor_conflict.py:165`
   — all use keyword args and omit it). Doc: "True for a singleton re-emitted because it was
   schema-incompatible with its transitive cluster (H-2)."
3. `cluster_and_merge` loop (196-209): call `merged, dropped = _merge_entities(...)`. If `not dropped`,
   append the cluster exactly as today. If `dropped`: compute
   `kept = tuple(m for m in member_ids if m not in set(dropped))`, **re-derive**
   `kept_canon = _canonical_id(kept)` and `kept_entity, _ = _merge_entities(kept_canon, kept, by_id)`
   (content-addressed id from the ACTUAL merged set — ADR-0036), append the kept cluster with
   `score=_cluster_score(kept, pair_scores)`; then for each `d in sorted(dropped)` append
   `ResolvedCluster(canonical_id=d, member_ids=(d,), entity=by_id[d], score=1.0, merge_incompatible=True)`.
4. `pipeline._resolve_batch`: add a **non-status-mutating** durable recorder — when
   `cluster.merge_incompatible` is True, write an `IngestDeadLetter(stage="resolve-incompat", ...)`
   for that member's queue rows (looked up via `items_by_entity_id.get(cluster.member_ids[0], [])`)
   **WITHOUT** setting `item.status`, so the singleton still resolves normally to `'resolved'` with
   its own correct-schema node and a self-referent (no-op). Stage string is EXACTLY
   `"resolve-incompat"` (16 chars — the rejected `'resolve-merge-incompat'` is 22 and overflows).
   The leftover/`resolve-noid` sweep (349-362) is unchanged and still correct (the demoted member is
   now in `clustered_ids` via its singleton, so not double-quarantined).

### Slice-2 (sign-off poison-row guard), `signoff.py`:

Wrap the per-row `make_entity()`/`.get()` in `_member_rows` AND `_outbound_edges` in `try/except`;
skip + durably dead-letter the offending row (`IngestDeadLetter`, stage `"signoff-poison"` = 14
chars) and continue, mirroring the pipeline Stage-1 isolation. Verify the `status == 'pending_review'`
filter still selects **exactly** the right member rows (do not broaden/narrow which rows count as
members or edges). Clean-input `approve`/`reject` behavior MUST be unchanged.

---

## 4. Acceptance invariants → tests → slices

Each invariant is asserted by a named test; before/after is the observable change.

| INV | Assertion (abridged) | Slice | Test | Before → After |
|-----|----------------------|-------|------|----------------|
| **INV-1** | After `cluster_and_merge` on a transitive Person~Person~Company cluster, NO returned cluster's `member_ids` contains entities of incompatible schemas; the dropped id is absent from every multi-member (`is_merge`) cluster. | slice-1 | `tests/unit/test_resolution_merge_incompat.py` | before: dropped id present in the merge (`('c1','p1','p2')`) → after: absent from all merges |
| **INV-2** | Each dropped member is re-emitted as its OWN singleton `ResolvedCluster`: `canonical_id==id`, `member_ids==(id,)`, `entity.schema==original`, `score==1.0`, `merge_incompatible is True`; an ordinary cluster keeps `merge_incompatible False`. | slice-1 | `tests/unit/test_resolution_merge_incompat.py` | before: no such singleton (member swallowed) → after: its own correct-schema node |
| **INV-3** | `build_referent_map` over the result maps the dropped id to ITSELF (id→id no-op), never to the wrong-schema canonical id; `rewrite_referents` leaves edges naming it on its own node. | slice-1 | `tests/unit/test_resolution_merge_incompat.py` | before: dropped_id → wrong Company canonical → after: dropped_id → dropped_id |
| **INV-4** | Through `resolve_pending`/`_resolve_batch`: the dropped member's row ends `status 'resolved'` with its own correct-schema node; the merged cluster's `MergeAudit.source_ids` does NOT contain the dropped id; an `IngestDeadLetter` row exists at stage `'resolve-incompat'` carrying the dropped member's source_record. | slice-1 | `tests/integration/test_b6_resolve_incompat.py` | before: dropped row resolved as part of the merge, audited in the merge, referent-rewired to wrong node, only a log → after: separate node, excluded from source_ids, dead-lettered |
| **INV-5** | Idempotency/determinism: re-running the same batch yields the same kept-cluster `canonical_id` (SHA-256 of the ACTUAL kept set, not the pre-drop set) and the same singleton ids; crash+retry converges (ADR-0036). | slice-1 | `tests/unit/test_resolution_merge_incompat.py` (+ integration convergence in `test_b6_resolve_incompat.py`) | before: canonical_id derived from a set including never-merged ids → after: derived only from genuinely-merged ids, stable |
| **INV-6** | `approve()`/`reject()` succeed for a valid parked merge even when an UNRELATED queue row in the same tenant carries a poison `raw_entity`: canonical/member nodes + outbound edges written exactly as for clean inputs; SignOff/judgement/MergeAudit transition completes. | slice-2 | `tests/integration/test_b6_signoff_poison.py` | before: one malformed `raw_entity` raises in `_member_rows`/`_outbound_edges` and aborts the whole approve/reject (wedges sign-off tenant-wide) → after: poison row skipped, sign-off completes |
| **INV-7** | A poison row encountered during sign-off is durably recorded (`IngestDeadLetter` at a ≤16-char stage e.g. `'signoff-poison'`, replayable) rather than silently swallowed; clean-input approve/reject behavior (`entities_written`/`edges_written`, audit transitions, idempotent re-run) is unchanged. | slice-2 | `tests/integration/test_b6_signoff_poison.py` (+ `tests/integration/test_signoff.py` FROZEN) | before: poison row crashes or is silently skipped with no audit → after: skipped AND dead-lettered, clean path identical |

---

## 5. Slice plan

Two independent, individually-mergeable slices (disjoint production files; they share only the inert
`ingest_dead_letter` table). **Recommend slice-1 first** (data-integrity > availability); either may
land first.

- **slice-1 (H-2 incompatible-member isolation).** `merge.py`: `_merge_entities` returns
  `(merged, dropped_ids)`; `ResolvedCluster` gains a defaulted `merge_incompatible: bool=False`
  (keeps all 3 construction sites valid); `cluster_and_merge` rebuilds the KEPT cluster with a
  re-derived content-addressed canonical id and re-emits each dropped member as its own correct-schema
  singleton. `pipeline._resolve_batch` records a non-status-mutating `IngestDeadLetter` at stage
  `'resolve-incompat'` before the singleton resolves normally. Files: `merge.py`, `pipeline.py`
  (+ `models.py` docstring). Mergeable when unit + integration green; `cluster_and_merge` return type
  stays `list[ResolvedCluster]`.
- **slice-2 (sign-off poison-row guard).** `signoff.py`: harden the two full-tenant queue scans
  (`_member_rows`, `_outbound_edges`) so one malformed `raw_entity` cannot crash approve/reject for
  the whole tenant — per-row `try/except`, skip + dead-letter (`'signoff-poison'`), continue. Confirm
  the `status == 'pending_review'` member filter is unchanged (do not broaden/narrow). Files:
  `signoff.py` (+ `models.py` docstring). Mergeable when integration green; clean-input approve/reject
  behavior unchanged (`test_signoff.py` frozen).

**Land order:** independent (disjoint files; shared only the inert dead-letter table). Either may land
first; slice-1 recommended first.

---

## 6. Blast-radius analysis

**Production:**

- `merge.py` — `_merge_entities` return type widens to `(FtmEntity, tuple[str,...])` (internal helper,
  not part of the public API); `ResolvedCluster` gains one **defaulted** field; `cluster_and_merge`
  return type is **unchanged** (`list[ResolvedCluster]`) — all external callers are unaffected.
- `pipeline.py` — `_resolve_batch` gains a non-status-mutating dead-letter write; the leftover/safety
  sweep (349-362) is untouched and still correct.
- `signoff.py` — the two private scan helpers gain per-row isolation; the public approve/reject
  signatures and clean-path semantics are unchanged.
- `models.py` — docstring-only edit to the `IngestDeadLetter.stage` list. **No schema/migration.**

**Downstream (slice-1) that the fix corrects, not breaks:**

- `referents.build_referent_map` / `rewrite_referents` — now receives only correct-schema clusters;
  the dropped id maps id→id (no-op). `referents.py` is **NOT edited**.
- `pipeline.record_merge` / `MergeAudit.source_ids` — now excludes the dropped id (audit is correct).
- The leftover/`resolve-noid` sweep — the demoted member is in `clustered_ids` via its singleton, so
  it is NOT double-quarantined; genuinely id-less rows still hit `resolve-noid`.
- `needs_review`/merge-guard — runs on the KEPT cluster (the one actually written); singletons are
  correctly never parked (review.py:44 returns `(False,'')` for a singleton).

**Untouched:** `referents.py`, `audit.py`, graph writer/constraints, API, MCP, connectors, enrichers,
`DEFAULT_MERGE_THRESHOLD=0.92`, Splink weights, `cluster_and_merge` return type.

---

## 7. Risks (from the dossier)

1. **Singleton re-emit vs canonical-id determinism (ADR-0036):** the KEPT cluster's canonical id MUST
   be re-derived from the actual kept set (`kept_canon = _canonical_id(kept)`), not the original
   pre-drop `member_ids`, or a crash+retry would diverge. **Guarded by INV-5.**
2. **Leftover/safety sweep double-quarantine (pipeline.py:349-362):** the dropped member is now in
   `clustered_ids` via its singleton, so it must NOT also be caught by the `resolve-noid` sweep;
   genuinely id-less rows must still hit `resolve-noid`. **Check against
   `test_b2_poison_batch_isolation.py`.**
3. **Dead-letter stage length (`models.py String(16)`):** `'resolve-incompat'` is exactly 16 chars;
   the rejected `'resolve-merge-incompat'` (22) would overflow; the slice-2 stage must also be ≤16
   (`'signoff-poison'` = 14). A stage >16 chars raises on insert.
4. **Status-mutation ordering (slice-1):** the `IngestDeadLetter` for a `merge_incompatible` member
   must be recorded WITHOUT setting `item.status`, so the singleton path still resolves the row to
   `'resolved'` (a self-referent no-op). Recording it as a status-mutating `_quarantine` would mark
   the row `'invalid'` and prevent its own correct-schema node being written.
5. **Sign-off status filter (slice-2):** `_member_rows` filters `status == 'pending_review'`; confirm
   hardening does NOT change WHICH rows are considered members (keep matching only the parked rows by
   `raw_entity` id) — broadening could pull in unrelated rows; narrowing could miss a needed status.
   Verify against `test_signoff.py` before/after.
6. **`needs_review` on the kept cluster:** re-merging the kept set produces a possibly-different
   cluster than the original; `needs_review`/guard must run on the KEPT cluster (the one actually
   written), so sensitivity parking still fires correctly.
7. **Backward compatibility of the new defaulted `ResolvedCluster` field:** all 3 construction sites
   (merge.py:203, test_referents.py:18, test_resolution_anchor_conflict.py:165) use keyword args and
   omit `merge_incompatible`; the frozen+slots default keeps them valid — confirmed.
8. **Idempotent dead-letter on retry:** a re-run of the same batch must not accumulate duplicate
   `'resolve-incompat'` rows in a way that breaks the drain or audit expectations (rows are keyed by
   uuid; acceptable, but INV-5's determinism check notes re-runs are convergent at the node level).

---

## 8. Out of scope (hard stops)

- **NO schema / migration / Alembic change.** `models.py` is **DOCSTRING-ONLY**; `stage` stays
  `String(16)`; the `ingest_dead_letter` table is reused. Any new column or migration is out.
- **NOT** `referents.py`, `audit.py`, graph writer/constraints, API, MCP, connectors, enrichers,
  review.py logic (only its existing singleton behavior is relied on, not changed).
- **NOT** `DEFAULT_MERGE_THRESHOLD=0.92`, Splink weights, the `score_pairs` signature, or any
  individual-affecting score.
- **NOT** the `cluster_and_merge` return type (stays `list[ResolvedCluster]`).
- **NOT** the two frozen ResolvedCluster construction-site tests, nor any frozen suite in §9 — they
  must pass UNCHANGED.
- **NOT** audit items B-4 / H-3 / H-5 and any other backlog item.
- **NO** new dead-letter stage string > 16 chars.

---

## 9. Frozen tests (must pass UNCHANGED)

- `tests/unit/test_referents.py` — constructs `ResolvedCluster` without `merge_incompatible`; the
  defaulted field guarantees backward compatibility.
- `tests/unit/test_resolution_anchor_conflict.py` — `ResolvedCluster` constructed at line 165 without
  `merge_incompatible`.
- `tests/unit/test_resolution.py`, `tests/unit/test_resolution_canonical_id.py`,
  `tests/unit/test_resolution_negative_judgement.py` — `cluster_and_merge` keeps return type
  `list[ResolvedCluster]`; canonical-id determinism and H-1 negative-judgement behavior unchanged.
- `tests/unit/test_resolution_distinguishing_evidence.py` (B-3 / ADR-0039),
  `tests/unit/test_resolution_multiscript.py` (ADR-0035) — scoring untouched.
- `tests/integration/test_signoff.py` — slice-2 must not alter clean-input approve/reject/judgement/
  audit behavior; all four flows stay green.
- `tests/integration/test_b2_poison_batch_isolation.py` — slice-1's singleton re-emit changes which
  ids land in `clustered_ids`; the existing resolve-row/resolve-batch/resolve-noid quarantine
  semantics must not regress (a dropped member is now in `clustered_ids` via its singleton, so it must
  NOT be double-quarantined).
- `tests/integration/test_b1_crash_recovery.py`, `tests/integration/test_b1_signoff_idempotency.py` —
  ADR-0036 idempotency / crash recovery must hold for the re-derived kept canonical id and for sign-off.
- `tests/integration/test_resolution_pipeline.py`, `tests/integration/test_resolution_batching.py`,
  `tests/integration/test_referent_rewriting.py` — pipeline/batch drain + referent rewriting unchanged.

**MAY legitimately need updating:** none expected — the H-2 design states baseline
`test_resolution*`/`test_referents` = 32 passed against the prototype. If any integration test asserts
the dropped member is audited inside a merge's `source_ids`, **that assertion is the bug** and would be
corrected — flag for the builder, do NOT silently weaken.

---

## 10. Human sign-off note (`human_fork=false`)

This gate is **correctness/robustness only**. Neither change touches `DEFAULT_MERGE_THRESHOLD=0.92`,
Splink weights, or any individual-affecting score; both are strictly **precision/containment-favoring**
(slice-1 PREVENTS a wrong cross-schema merge from being written; slice-2 PREVENTS a crash). There is
therefore **NO OPEN product/architecture fork** and **no person-affecting ER-policy decision** — the
human does not need to decide anything before building.

CLAUDE.md's gating still applies via the **gate process**: a fresh-context Opus judge approves the
builder records, the frozen suites must be green, and CI (`quality` + `security`) must be green before
merge. No individual-affecting score is auto-promoted.

### Pre-existing, out-of-gate working-tree changes (escalated to the human)

Uncommitted/untracked FLEET-SETUP files present at session start are NOT part of gate B-6:
`.claude/agents/*`, `.claude/hooks/*`, `.claude/settings.json`, `.claude/council-broker.log`,
`.claude/fleet.*`, `orchestrator/`, `scripts/council/`, `scripts/dev/fleet_*.sh`, `scripts/smoke/`,
`scripts/dev/{local_ci,orient,spawn_worker}.sh`. The scope-guard hook STRIPS `.claude/` paths (inert
to it); the non-`.claude/` ones are DELIBERATELY NOT added to the glob allow-list (adding them would
silently widen the B-6 blast radius). They must NOT be touched, staged, or committed on the
`gate/b6` branch — HARD STOP, escalated to the human (same handling as B-3/B-5).
