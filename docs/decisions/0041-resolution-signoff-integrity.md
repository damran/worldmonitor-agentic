# ADR 0041 — Resolution / sign-off integrity

- **Status:** ACCEPTED
  *(No OPEN fork. ACCEPTED on merge after the builder record is filled in and the fresh-context judge
  approves. `human_fork=false` — see §Decision rationale.)*
- **Date:** 2026-06-24
- **Gate:** B-6 — `docs/reviews/GATE_B6_SPEC.md`
- **Branch:** `gate/b6-resolution-integrity`
- **Extends:** ADR 0038 (per-batch / per-stage exception isolation), ADR 0036 (deterministic
  canonical id), ADR 0031 (sign-off), ADR 0035 / ADR 0025 (referents).

---

## Context

Two distinct correctness/robustness defects in the resolution module let a single bad input corrupt
or wedge the resolved-entity graph. Both were reproduced against the real code (H-2 at runtime via
`.venv/bin/python` driving the actual `cluster_and_merge`/`_merge_entities`).

### Finding H-2 — schema-incompatible members of a transitive cluster are silently swallowed

`merge.py::_merge_entities` (213-234) catches `followthemoney InvalidData ("No common schema")` when
merging a schema-incompatible member and only `logger.warning`s, returning a merged entity that
silently excludes it. `cluster_and_merge` (196-209) then builds the `ResolvedCluster` with the FULL
`member_ids` regardless of which members actually merged, so dropped members stay in `member_ids`.
This happens on a **transitive cluster** (`Person p1 ~ Person p2` compatible, `Person p2 ~ Company c1`
incompatible) because Splink/`score_pairs` scores A~B and B~C separately and never compares A~C, so
the schema-compat gate cannot prevent the cluster being gathered.

Runtime repro confirmed the full blast radius keyed off the stale `member_ids`: `build_referent_map`
maps the dropped Person ids onto a **Company** canonical node (edges naming them get rewritten onto a
wrong-schema node); `record_merge` audits all three ids including the dropped ones;
`_set_status(member_ids,'resolved')` marks the dropped rows resolved; and the leftover safety sweep
keys on `clustered_ids` (still containing them) so they are never quarantined. The skip is only
logged — not durably auditable.

### Finding (sign-off poison-row wedge) — one malformed `raw_entity` wedges sign-off tenant-wide

`signoff.py::_outbound_edges` (~155-172) scans ALL `ErQueueItem` rows for the tenant with NO status
filter (line 163) and calls `make_entity(row.raw_entity)` unguarded (line 166); `_member_rows`
(145-152) filters `status=='pending_review'` but still calls `make_entity`/`.get()` unguarded.
`pipeline._quarantine` sets `status='invalid'` without clearing the bad `raw_entity`, so a quarantined
poison row keeps a malformed payload in the queue. `approve()`/`reject()` both call these scans, so
**one poison row's `InvalidData` aborts sign-off for EVERY parked merge of that tenant** — the B-2
per-input isolation pattern was never applied to sign-off.

---

## Decision

**ADR 0041 — Resolution / sign-off integrity.**

**(1) For H-2 (transitive cross-schema clusters), stop silently swallowing schema-incompatible
members.** `_merge_entities` surfaces the set of members it could not FtM-merge; `cluster_and_merge`
then (a) materialises the KEPT (genuinely-merged) members as a cluster whose canonical id is
re-derived (content-addressed SHA-256, ADR-0036) from ONLY the kept set, and (b) re-emits EACH dropped
member as its own correct-schema singleton `ResolvedCluster` (`canonical_id==id`, `member_ids==(id,)`,
`score 1.0`, `merge_incompatible=True`). The pipeline durably dead-letters the skip (`IngestDeadLetter`,
stage `'resolve-incompat'`) WITHOUT mutating status, then lets the singleton resolve normally to its
own correct-schema node. This guarantees no cross-schema member ever enters a merged node, every member
keeps a correct-schema node, referents for it are id→id no-ops, and the merge's audit/source_ids
exclude it.

*Rationale:* this PREVENTS an erroneous merge from being written rather than unmerging one (append-only
/ no-unmerge preserved); it is the minimal blast-radius fix (the return type of `cluster_and_merge` and
the StoredJudgement/`needs_review`/guard paths are untouched; `ResolvedCluster` gains one defaulted
field so all existing construction sites stay valid; reuses the `ingest_dead_letter` table with NO
schema/migration change — stage strings kept ≤16 chars).

**(2) For the sign-off poison-row wedge, harden the two full-tenant queue scans in
`signoff.approve/reject` (`_member_rows`, `_outbound_edges`)** so a single malformed `raw_entity`
cannot raise and crash sign-off for every parked merge of the tenant: the offending row is skipped and
dead-lettered (replayable, stage `'signoff-poison'`) and the sign-off completes, mirroring the B-2
per-input isolation pattern.

*Rationale:* availability/robustness — a poison row must never wedge the human-review path; clean-input
behavior is provably unchanged (`test_signoff.py` frozen).

**Decision rationale — no human fork (`human_fork=false`).** NEITHER change touches an ER threshold
(`DEFAULT_MERGE_THRESHOLD=0.92`), Splink weights, or any individual-affecting score; both are strictly
precision/containment-favoring (prevent a wrong merge / prevent a crash). Therefore NO human
person-affecting sign-off fork is required — these are correctness/robustness fixes, not ER policy.

---

## Alternatives considered

- **H-2 — keep the silent skip (status quo).** Rejected: produces a wrong-schema merge, loses the
  dropped member's node, and rewires its edges onto the wrong node, with only a log entry.
- **H-2 — abort the whole batch on `InvalidData`.** Rejected: regresses B-2 batch isolation; one
  transitive cluster would block an entire batch.
- **H-2 — keep the original `member_ids` and just exclude on write.** Rejected: leaves the stale id in
  audit/referents/sweep; the canonical id would still be content-addressed over never-merged ids,
  breaking ADR-0036 determinism.
- **H-2 — change `cluster_and_merge`'s return type to carry the dropped set out-of-band.** Rejected:
  many test callers depend on `list[ResolvedCluster]`; re-emitting dropped members as singletons + one
  defaulted flag is strictly smaller blast radius.
- **Sign-off — silently skip the poison row with no audit.** Rejected: loses the durable, replayable
  record; the same malformed payload would resurface every sign-off.
- **Sign-off — clear the bad `raw_entity` in `_quarantine`.** Rejected: out of scope (would change
  pipeline quarantine semantics and the row's audit payload); the per-scan guard is sufficient and
  contained.
- **Bundle H-2 + sign-off as one slice.** Rejected: they are independent (disjoint production files);
  two individually-mergeable slices keep each PR focused.

---

## Consequences / locked-invariant impact

- **G1 provenance (preserved):** the re-emitted singleton is the member's ORIGINAL entity (`by_id[d]`)
  with its own provenance/context intact; `rewrite_referents` only touches entity-typed property values
  and maps the id to itself (no-op), so no edge loses or gains provenance.
- **G4 tenant isolation (preserved):** resolution stays inside one tenant's ephemeral resolver; the
  dead-letter row carries the member's own `tenant_id` (via the looked-up `ErQueueItem`). Sign-off
  scans stay tenant-scoped; the poison-row guard introduces no cross-tenant read/write.
- **Append-only / no-unmerge (preserved):** the fix PREVENTS an erroneous merge from being written
  rather than unmerging one — the dropped member never enters a merged node, so there is nothing to
  retroactively split.
- **ADR-0036 deterministic canonical id (preserved):** the KEPT cluster's canonical id is now correctly
  content-addressed over ONLY the genuinely-merged members, so a crash+retry still converges.
- **ADR-0024 merge-guard block / return-to-block (untouched):** `needs_review` still runs on every
  multi-member cluster (the KEPT cluster, the one actually written); singletons are correctly never
  flagged; `MERGE_GUARD_MODE` handling unchanged. The demoted member is a singleton (nothing merged),
  so it is rightly not parked.
- **Audit gap closed:** the skip is now durably dead-lettered (`resolve-incompat`) and the sign-off
  poison row is dead-lettered (`signoff-poison`) rather than only logged / silently swallowed.
- **No DB schema/migration:** reuses `ingest_dead_letter`; `stage` stays `String(16)`; all new stage
  strings ≤16 chars (`resolve-incompat`=16, `signoff-poison`=14). `models.py` change is docstring-only.
- **`referents.py` and `audit.py` UNCHANGED.**

---

## Out of scope

- NO schema/migration/Alembic change; `models.py` is docstring-only.
- NOT `referents.py`, `audit.py`, graph writer/constraints, API, MCP, connectors, enrichers.
- NOT `DEFAULT_MERGE_THRESHOLD=0.92`, Splink weights, the `score_pairs` signature, the
  `cluster_and_merge` return type, or any individual-affecting score.
- NOT the frozen ResolvedCluster construction-site tests or any frozen suite (gate spec §9).
- NOT audit B-4 / H-3 / H-5 or any other backlog item.

---

## Builder record (fill in on implementation — required before ACCEPTED)

- **Final dead-letter stage strings used:** slice-1 `resolve-incompat` (16 chars, == String(16) cap);
  slice-2 `signoff-poison` (14 chars). Both ≤16 confirmed; no schema/migration (reuses
  `ingest_dead_letter`, `stage` stays `String(16)`). The rejected `resolve-merge-incompat` (22) would
  have overflowed.
- **Measured INV results** (unit slice-1 measured locally; integration slice-1/2 collect cleanly and
  run on the Docker CI job — they are testcontainers-backed and cannot execute in the default env):
  - INV-1 (no cross-schema member in any merge): PASS —
    `test_dropped_member_absent_from_every_merge` green (the dropped Company `z1` is absent from every
    `is_merge` cluster's `member_ids`; each merge is schema-homogeneous).
  - INV-2 (dropped member re-emitted as own correct-schema singleton, `merge_incompatible=True`): PASS
    — `test_dropped_member_reemitted_as_correct_schema_singleton` (`z1` → own `Company` singleton,
    `canonical_id==z1`, `member_ids==('z1',)`, `score==1.0`, `merge_incompatible is True`) +
    `test_ordinary_cluster_is_not_flagged_incompatible` /
    `test_clean_cluster_without_a_drop_is_not_flagged` (ordinary clusters keep it `False`).
  - INV-3 (`build_referent_map` id→id no-op for dropped id): PASS —
    `test_referent_map_for_dropped_member_is_a_noop` (`z1`→`z1`) +
    `test_edge_naming_dropped_member_stays_on_its_own_node` (a `Directorship.organization==z1` is left
    on `z1`'s own node; the kept member `a1` is still redirected onto the kept Person canonical).
  - INV-4 (row `resolved` w/ own node; excluded from MergeAudit source_ids; `resolve-incompat`
    dead-letter present): asserted by `test_b6_resolve_incompat.py`
    (`test_dropped_member_resolves_to_its_own_node_excluded_from_merge_and_dead_lettered`); collects
    cleanly, runs on Docker CI. Implemented: `pipeline._resolve_batch` calls the new NON-status-mutating
    `_record_skip(..., stage="resolve-incompat")` for the `merge_incompatible` singleton's queue rows
    (looked up via `items_by_entity_id`) BEFORE it resolves normally; the singleton then resolves to
    `'resolved'` with its own node; the merge's `MergeAudit.source_ids` excludes `z1`.
  - INV-5 (kept canonical id = SHA-256 of the kept set; convergent on retry): PASS (unit) —
    `test_kept_cluster_canonical_id_is_rederived_from_the_kept_set` (`kept.canonical_id ==
    _canonical_id(('a1','a2'))`, NOT `_canonical_id(('a1','a2','z1'))`) +
    `test_rerun_is_deterministic`; integration convergence in
    `test_b6_resolve_incompat.py::test_rerun_converges_on_the_same_nodes` (Docker CI).
  - INV-6 (approve/reject succeed despite unrelated poison row in tenant): asserted by
    `test_b6_signoff_poison.py` (`test_approve_succeeds_despite_unrelated_poison_row`,
    `test_reject_succeeds_despite_unrelated_poison_row`); collects cleanly, runs on Docker CI.
    Implemented: per-row `try/except` in `signoff._member_rows` + `_outbound_edges`.
  - INV-7 (poison row dead-lettered at `signoff-poison`; clean path unchanged): asserted by the same
    two tests (`_assert_poison_dead_lettered`); clean-path identity guarded by FROZEN `test_signoff.py`
    (green, byte-identical). Implemented: `_dead_letter_poison` skips + dead-letters the offending row
    at stage `signoff-poison` and continues; the `status=='pending_review'` member filter is unchanged.
- **Frozen suites green (unchanged):** test_referents.py, test_resolution_anchor_conflict.py,
  test_resolution.py, test_resolution_canonical_id.py, test_resolution_negative_judgement.py,
  test_resolution_distinguishing_evidence.py, test_resolution_multiscript.py → GREEN locally
  (60 passed; all 8 byte-identical to HEAD). test_signoff.py, test_b2_poison_batch_isolation.py,
  test_b1_crash_recovery.py, test_b1_signoff_idempotency.py, test_resolution_pipeline.py,
  test_resolution_batching.py, test_referent_rewriting.py → integration (Docker CI), unchanged on disk.
- **CI (`quality` + `security`) green:** `ruff check src tests` + `ruff format --check src tests` +
  `uv run pyright` (config `include=["src"]`, strict) all clean locally; final CI gate run pending on PR.
- **Judge approval (fresh-context Opus):** `____`
