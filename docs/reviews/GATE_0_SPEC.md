# Gate 0 — Single-Tenancy Teardown (DELETION gate)

> **Type:** INVERTED / DELETION gate (reverses ADR 0017).
> **ADR:** `docs/decisions/0042-single-tenancy-teardown.md` (status: proposed; supersedes 0017).
> **Branch:** `gate/0-single-tenancy-teardown` off `master@d865f5c` (clean tree).
> **Authority:** user, 2026-06-25 — locked decision **D1: single-tenant**. `human_fork=false`
> (user-authorized; this is NOT an OPEN product/architecture fork). Still requires judge APPROVE
> + CI green (quality + security + integration) before the human's `--ff-only` merge.

---

## 1. Why

D1 makes WorldMonitor single-tenant. `tenant_id` was introduced by ADR 0017 ("tenant-scoped from
day one") to give two tenants holding the same real-world entity two distinct, isolated nodes, and
to scope every relational row, graph node/edge, and read. Under D1 there is exactly one tenant, so
`tenant_id` is a constant carried through ~110-120 call sites, an 8-table NOT NULL column, a Neo4j
property + composite constraint, two relational unique constraints, and a guard in the graph writer.
It is dead weight that every future builder must thread, and — critically — it is a *standing
contradiction* between the code (which scopes everything) and D1 (which says there is nothing to
scope). This gate removes it, and installs a drift guard so future sessions can never reload a
ground-truth that contradicts the code.

### 1.1 Result-neutrality (the central safety result)

Removing `tenant_id` changes **no result anywhere** in a single-tenant deployment. Verified by orient
and re-confirmed here against the current tree:

- **Canonical ids are unaffected.** `resolution/merge.py::_canonical_id` (`:47-57`) hashes ONLY
  `sorted(member_ids)`; `tenant_id` never feeds it. Dropping the column changes no id (ADR 0036 safe).
  The module docstring (`:34-43`) *describes* a per-tenant MERGE key but the id computation does not
  use it.
- **`uq_er_queue_dedup (tenant_id, source_record, entity_id)`** (`models.py:48`) shrinks to
  `(source_record, entity_id)`; with one tenant the `ON CONFLICT DO NOTHING` enqueue behaviour
  (`ingest.py:207-221`) is identical.
- **`uq_resolver_judgement_pair (tenant_id, left_id, right_id)`** (`models.py:179`) shrinks to
  `(left_id, right_id)`; with one tenant the idempotent judgement insert (`signoff.py:250-254`) is
  identical.
- **The ephemeral resolver / B-1 crash-recovery guarantee is provably independent of the column.**
  `merge.py::_ephemeral_resolver` (`:89-105`) builds a private in-memory `sqlite://` + `StaticPool`
  resolver per call; `tests/unit/test_resolution.py:73` proves the isolation property *passing zero
  tenant_id*. See §3.3 — this lifecycle is a **frozen behaviour**.

---

## 2. Inverted discipline (how a DELETION gate is judged)

A normal gate adds behaviour and proves it with a NEW failing-then-passing test. This gate removes a
property; the discipline is reversed:

1. **Capture green.** Before any deletion, the keep-green suites (§3.5) pass on the current tree.
2. **Delete `tenant_id`** across the deletion surface (§3) + the 0004 migration.
3. **Require still-green.** The same suites pass again — their asserted *behaviour* (counts, ids,
   edges, provenance, dead-letters, idempotency) is byte-for-byte identical; only their `tenant_id`
   *call sites* changed.
4. **Prove the property is gone, deliberately.** Delete exactly the tests that prove the removed
   property (§6) — and ONLY those. The judge verifies via
   `git diff origin/master...HEAD -- '*test_*.py' 'tests/'` that no *kept* test had an assert removed,
   a skip/xfail added, or a tolerance loosened. The only legitimate removals are the §6 set.
5. **Adversarial target (§9).** A deletion gate's failure mode is a *kept caller whose correctness
   depended on `tenant_id` for something other than isolation*. Orient found none. The judge must
   try to break that claim.

---

## 3. Deletion surface

~110-120 functional call sites across 13 source files, 3 existing migrations, 1 new migration,
1 Neo4j constraint module. Orient's surface is accurate; this spec adds **two sites orient missed**
that the grep-gate (§4) would otherwise catch: `src/worldmonitor/review.py:81` (the CLI `--tenant`
arg) and `tests/integration/test_graph_writer.py::test_writer_requires_tenant` (a negative test of
the guard being removed, §6).

### 3.1 Relational schema — `db/models.py` + migration 0004

Remove the `tenant_id: Mapped[str]` column from all 8 tables:
`ConnectorInstance:29`, `ErQueueItem:52`, `MergeAudit:74`, `IngestDeadLetter:105`, `MergeAlert:130`,
`TaskRun:153`, `ResolverJudgement:183`, `SignOff:205`. Redefine the two composite uniques:
- `uq_er_queue_dedup` (`:48`): `(tenant_id, source_record, entity_id)` -> `(source_record, entity_id)`.
- `uq_resolver_judgement_pair` (`:179`): `(tenant_id, left_id, right_id)` -> `(left_id, right_id)`.
Drop the per-table `index=True` on `tenant_id`. Update the module docstring (`:1-7`) and the
per-class docstrings that assert "every row carries `tenant_id`".

### 3.2 The 0004 migration — DROP, not keep-dead (decision, see §10)

New `src/worldmonitor/db/migrations/versions/0004_drop_tenant_id.py`, `down_revision = "0003_signoff_judgements"`:
- `op.drop_index` each `ix_<table>_tenant_id` (8 indexes).
- `op.drop_constraint` then `op.create_unique_constraint` for the two redefined uniques.
- `op.drop_column("<table>", "tenant_id")` on all 8 tables.
- A `downgrade()` that re-adds the columns/indexes/uniques (best-effort; `tenant_id` re-added
  `nullable=True` then would need backfill — note in the migration that downgrade is for schema
  symmetry only, not data round-trip, since the column's data is gone).
**Do NOT edit 0001/0002/0003** — migration history is immutable; the delta goes in 0004, exactly as
the 0002 runway delta layered on the 0001 baseline.

### 3.3 Neo4j — `graph/constraints.py` + `graph/writer.py` (+ the ephemeral resolver, KEPT)

- `constraints.py:25-34`: composite `(n.tenant_id, n.<prop>)` uniqueness -> single `(n.<prop>)`;
  drop the `entity_tenant_id` index entirely. Update the module docstring (`:1-10`).
- `writer.py`: this is the largest single change. Remove `_KEY_REWRITES` (`:45-50`),
  `_tenantize_query` (`:53-60`), `_inject_tenant`'s tenant stamping (`:63-94`), `_tenantize`
  (`:97-107`), `WriterError`'s tenant role (`:38` — keep the class if `_align_entity_link_ids`/ftmg
  still need a failure type, else remove; builder's call). Change `write_entities`'s signature
  `(client, entities, *, tenant_id)` -> `(client, entities)` and delete the `if not tenant_id: raise`
  guard (`:157-158`). ftmg is "single-tenant by design" (writer docstring `:4-6`) — removing the
  wrapper returns to ftmg's native node-key MERGE. **Provenance + anchor projection
  (`node_props_by_id`, `edge_props`, `_inject_tenant`'s `props`/`node_props_by_id` branches) MUST be
  preserved** — only the `tenant_id` stamping is removed. G1 provenance on every node AND edge is
  untouched.
- **`merge.py::_ephemeral_resolver` (`:89-105`) is KEPT verbatim in behaviour.** Its docstring's G4
  motivation is now historical, but the in-memory `sqlite://` + `StaticPool` per-call lifecycle is a
  frozen invariant (B-1 / ADR 0026 batch purity). The builder MUST NOT make it persistent or shared.

### 3.4 Reads, resolution, runner, ingest, authz, API

- `graph/queries.py:16-50`: drop `tenant_id=` params + the `{tenant_id: $tenant_id, ...}` /
  `WHERE` clauses from `get_entity`/`get_neighbors`/`get_provenance`.
- `graph/gds.py:31-45`: drop the `tenant_id` projection filter from `degree_centrality`.
- `resolution/pipeline.py`: `resolve_pending` signature (`:72-81`) drops `tenant_id`; remove the
  `ErQueueItem.tenant_id == tenant_id` predicate (`:111`), `_load_judgements`'s filter (`:151`), the
  `write_entities(..., tenant_id=...)` call (`:423`), the `IngestDeadLetter(tenant_id=...)` fields in
  `_quarantine`/`_record_skip` (`:205`,`:240`), and all `tenant_id=tenant_id` threading to
  `record_merge`/`record_merge_alert`. Behaviour (batch drain, guard, dead-letter, referent rewrite)
  is unchanged.
- `resolution/signoff.py` (~20 sites): drop `tenant_id` from `approve`/`reject`/`list_parked`/
  `_member_rows`/`_outbound_edges`/`_record_judgements`/`_node_exists`/`_any_node_exists`/
  `_dead_letter_poison`/`_signoff_row`/`_require_audit` and the corresponding `WHERE`/MATCH/`SignOff(...)`
  fields. The status-filter and the poison-row guard behaviour (slice keep-green B-6) are unchanged.
- `resolution/audit.py:13-56`: drop `tenant_id` param + the `MergeAudit(tenant_id=...)` /
  `MergeAlert(tenant_id=...)` fields from `record_merge`/`record_merge_alert`.
- `runner/driver.py`: `_resolve_tenant` (`:251-268`) and `run_resolution` (`:222-249`) currently
  `SELECT DISTINCT tenant_id WHERE status='pending'` then loop per tenant. Collapse to a SINGLE
  resolution pass: drop the distinct-tenant select, drop the per-tenant loop, call
  `resolve_pending(session=..., neo4j=...)` once when a backlog exists. Drop `TaskRun(tenant_id=...)`
  (`:181`,`:254`) and `instance.tenant_id` reads (`:186-190`). The serialization lock, ACTIVE-refusal,
  stale-reset, and finalize behaviour are unchanged. **`run_resolution` must still return a
  deterministic value (e.g. an empty/`["__all__"]` marker) so `test_ingest_driver` asserts hold —
  builder updates the test's expectation to match (behaviour-equivalent, not weakened).**
- `runner/ingest.py`: `run_ingest` signature (`:107-118`) drops `tenant_id`; remove `tenant_id` from
  the landing-key prefix (`:151-161`) — the key becomes `connector_id/dataset/{key}.json` — the
  `IngestDeadLetter(tenant_id=...)` fields (`:75-96`), and the `ErQueueItem(tenant_id=...)` insert
  value (`:207-221`). The dedup constraint reference (`uq_er_queue_dedup`) stays (now 2-col).
- `authz/oidc.py`: remove `Principal.tenant_id` (`:30`), `ORG_ID_CLAIM` (`:19`), and the
  `tenant_id=str(claims.get(ORG_ID_CLAIM, ""))` in `from_claims` (`:38`). Update the module docstring
  (`:1-7`). Auth itself (token verification) stays.
- `api/middleware.py`: drop the "tenant context" wording from the docstring (`:1-6`); the principal is
  still attached (`:70-71`) — it just no longer carries a tenant. No behavioural change to the auth
  gate.
- `api/main.py:52-57`: `/me` returns `{"subject": ..., "tenant_id": ...}`; drop the `tenant_id` key.
  `test_api_health.py` asserts on `/me` — update it to the new shape (behaviour-equivalent).
- `src/worldmonitor/review.py:34-85` **(orient-missed)**: the CLI `--tenant` arg + `tenant_id=args.tenant`
  pass-through to `signoff.list_parked`/`approve`/`reject`. Remove the `--tenant` argument and the
  pass-through. `test_driver_wiring.py` / any review CLI test updates to the new arg set.

### 3.5 What is NOT touched (behaviour frozen)

The merge guard / return-to-block sign-off STATE MACHINE, `DEFAULT_MERGE_THRESHOLD=0.92`, Splink
weights, `score_pairs`, `needs_review`, `build_referent_map`/`rewrite_referents`, the
`ResolvedCluster`/`ResolveStats` shapes, the dead-letter `stage` strings, `_canonical_id`, and
`_ephemeral_resolver`'s lifecycle. No new column, no new dead-letter stage, no schema change beyond
the column/index/unique removals in 0004.

---

## 4. Acceptance criteria (APPROVE / DENY)

The judge APPROVES iff ALL hold; any single failure is a DENY (return as the fix list):

1. **Grep gate.** `grep -rn 'tenant_id' src/worldmonitor/ --include='*.py'` returns matches ONLY
   under `db/migrations/versions/` (0001/0002/0003 history + 0004's `op.drop_column` referencing the
   column name). No live `tenant_id` predicate, parameter, column, ORM field, or Cypher property
   anywhere else in `src/`. Also grep `tenant`, `ORG_ID_CLAIM`, `_tenantize`, `_KEY_REWRITES` for
   stragglers.
2. **Kept tests green pre + post.** Every §3.5/§3.6 keep-green suite (the full list is in
   `.claude/gate.scope`) passes — including the B-1 crash-recovery and B-1 sign-off idempotency
   tests (the cross-store crash window is the highest-value regression net).
3. **Migration drift guard green.** `tests/integration/test_migrations.py` passes: fresh (alembic
   head) ≡ create_all (models.py) ≡ adopted pre-runway, and `alembic check` is clean. This is the
   gate's #1 risk (§5) — `models.py` and 0004 must agree.
4. **Drift-guard test green.** The NEW `tests/test_contract_consistency.py` passes both assertions
   (mirror-sync + claim↔code), and is non-vacuous (it would fail on the current tree, see §8.2).
5. **Only the §6 tests were removed/split.** `git diff origin/master...HEAD` shows no weakened *kept*
   test; the deletions match §6 exactly.
6. **Invariants held (§ below).** G1 provenance on every node AND edge, append-only, and
   canonical-canonical-via-the-guard are all intact; only G4 is (intentionally) gone.
7. **Scope clean.** Only files in `.claude/gate.scope` changed; no DDL change snuck into 0001/0002/0003.
8. **CI green** on `quality` + `security` (+ integration via testcontainers).

---

## 5. Top risk — the migration-drift tripwire (#1)

`tests/integration/test_migrations.py` (ADR 0030) asserts the ORM models, the Alembic head, and an
adopted pre-runway DB all produce an IDENTICAL introspected schema, and runs `alembic check`. If
`models.py` (column/index/unique removal) and `0004_drop_tenant_id.py` disagree by a single index
name, nullability, or unique-constraint column set, `_snapshot()` inequality or
`AutogenerateDiffsDetected` turns CI red. **Mitigation:** edit `models.py` and write 0004 as ONE
atomic change in slice-1; run `uv run alembic check` (or the local CI mirror) before pushing; the
two redefined uniques (`uq_er_queue_dedup`, `uq_resolver_judgement_pair`) and the 8 dropped indexes
are the most likely mismatch points.

---

## 6. Test changes (the only legitimate removals/splits)

- **DELETE wholesale:** `tests/integration/test_tenant_isolation.py` — it is a pure two-tenant / G4
  proof; the property it proves no longer exists.
- **DELETE** `tests/integration/test_graph_writer.py::test_writer_requires_tenant` (`:185-192`) — it
  asserts `write_entities(..., tenant_id="")` raises `WriterError`; the guard is being removed. *(This
  one is not in orient's list; it is the writer-guard's negative test and must go with the guard.)*
- **SURGICAL SPLIT** `tests/integration/test_signoff.py::test_negative_judgement_is_consumed_and_tenant_scoped`
  (`:103-148`): KEEP the consumption arm (`:118-132` — A's `negative` judgement prevents the merge);
  DELETE the "tenant B unaffected" arm (`:134-148`); rename the test to drop `_and_tenant_scoped`
  (e.g. `test_negative_judgement_is_consumed`).
- **REFRAME (do NOT delete)** `tests/unit/test_resolution.py::test_resolver_is_isolated_per_batch_no_cross_tenant_leak`
  (`:73`): rename to drop the cross-tenant framing (e.g. `test_resolver_is_isolated_per_batch`) and
  rewrite its docstring from a G4 cross-tenant-leak claim to the ADR-0028 per-batch ephemeral-resolver
  property. The membership assertion already passes zero tenant_id — it stands unchanged; only the
  framing changes.
- **CALL-SITE UPDATES (behaviour kept):** every keep-green test that calls `write_entities(...,
  tenant_id=t)`, `resolve_pending(..., tenant_id=t)`, `run_ingest(..., tenant_id=t)`,
  `get_entity(..., tenant_id=t)`, the sign-off functions, or constructs ORM rows with `tenant_id=...`
  drops that argument/field. The asserted counts/ids/edges/provenance/flags are IDENTICAL.
  `test_api_health.py` (the `/me` shape) and `test_ingest_driver.py` (the `run_resolution` return /
  per-tenant loop) update their *expectations* to the new behaviour-equivalent shape — these are
  call-shape updates, not weakenings, and the judge must read them as such.

---

## 7. Slice breakdown

**Recommendation: the 2-slice split (slice-1 functional, slice-2 contract), land slice-1 first.**
Reasoning below.

### SETUP (orchestrator, before spawning the fleet — see §8)
De-G4 the 4 agent specs. Inert to the scope-guard hook; committed within the gate.

### slice-1 — Functional teardown (individually mergeable, CI-green)
All `src/` deletion-surface files (§3.1, §3.3, §3.4) + `0004_drop_tenant_id.py` + the constraint/unique
redefinitions + the §6 test changes/deletions/split. Mergeable when quality + security + integration
are green, the grep gate (§4.1) is clean, and the migration drift guard (§5) is green. **This slice is
internally ATOMIC** — you cannot half-remove `tenant_id` and keep green: the `write_entities`
signature change ripples to every caller, the migration must match `models.py`, and the grep gate only
passes once the LAST live `tenant_id` is gone. A builder works it as one coherent change; the slice
boundary is the merge unit, not a within-slice checkpoint.

### slice-2 — Contract amendment + drift guard (individually mergeable, CI-green)
`CLAUDE.md` / `AGENTS.md` / `.clinerules` (the line-22 "tenant-scoped from day one (`tenant_id`
everywhere)" -> single-tenant, verbatim-together per the mirror rule) + the docs (§8.3) + ADR
supersede/notes (§8.4) + the NEW `tests/test_contract_consistency.py` (§8.2). Mergeable when quality +
security are green.

### Why 2 slices and not 1 atomic gate, and why this order
The two slices have **disjoint file sets** (slice-1 = `src/` + tests; slice-2 = `*.md` +
`tests/test_contract_consistency.py`) and each is independently reviewable. But they are **ordered, not
independent**: slice-2's `claim↔code` assertion ("if the contract says single-tenant, no live
`tenant_id` in `src/`") goes RED until slice-1 has removed `src/` `tenant_id`. So:
- slice-1 lands first (makes the *code* single-tenant; the grep gate proves it),
- slice-2 lands second (makes the *ground truth* say so + installs the guard that keeps them
  consistent forever).
Splitting keeps each PR small and lets the judge reason about the functional teardown (the risky part)
without the prose churn. A single atomic gate is defensible too, but the functional change is large
enough that isolating it from doc churn materially helps adversarial review — so the split is
recommended. The 4 agent specs are de-G4'd in SETUP regardless of slicing (the judge/test-author must
not be holding G4 when they run on this gate).

---

## 8. Contract / ground-truth amendment (so no merged commit is self-contradictory)

### 8.1 The three mirrors (verbatim-together)
`CLAUDE.md:22`, `AGENTS.md:22`, `.clinerules:22` are identical:
`- **API: Python 3.12+ / FastAPI**, stateless. **Auth: Zitadel (OIDC), tenant-scoped from day one** (`tenant_id` everywhere).`
Change all three to single-tenant verbatim-together (the mirror rule). Suggested replacement:
`- **API: Python 3.12+ / FastAPI**, stateless. **Auth: Zitadel (OIDC); single-tenant (D1, ADR 0042)** — no `tenant_id` scoping.`
The non-negotiable-invariants list and any other `tenant_id` mention in the three files must also be
reconciled. The mirror-sync drift-guard (§8.2) enforces byte-identity, so the existing 6-line CI-gate
footer in `CLAUDE.md` (absent from the mirrors) must be reconciled too — either mirror it into all
three or move it out of the byte-compared region (builder's call; see §8.2).

### 8.2 The drift guard — `tests/test_contract_consistency.py` (NEW, `quality` job)
Two assertions, designed to PASS only once slice-1 + slice-2 land together:
1. **mirror-sync:** `CLAUDE.md` ≡ `AGENTS.md` ≡ `.clinerules` byte-identical. **This already FAILS
   today** (`CLAUDE.md` has a 6-line CI-gate footer the mirrors lack — confirmed by `diff`), so the
   guard is non-vacuous and earns its keep. Slice-2 makes the three byte-identical.
2. **claim↔code (bidirectional):** if the contract asserts single-tenant (a sentinel string the
   amendment introduces), then `grep -rn 'tenant_id' src/worldmonitor/ --include='*.py'` excluding
   `db/migrations/versions/` returns nothing; and conversely if any live `tenant_id` exists in `src/`
   the contract must NOT claim single-tenant. (Migrations are excepted: history references the dropped
   column name.)
Place it at `tests/test_contract_consistency.py` (repo-root tests dir) so it runs in `quality`, not
behind the testcontainers `integration` gate (it touches only files, no DB/Neo4j).

### 8.3 Docs
- `docs/GATE_LEDGER.md`: rows `:20` (G4), `:40` (G4 isolation), `:43` (D1/G4 resolver leak). G4
  isolation is **deleted, not CLOSED** — re-mark these as `SUPERSEDED (D1 / ADR 0042)`, pointing at
  the teardown; the ephemeral-resolver row (`:43`) stays CLOSED for its B-1/0026 role but loses its G4
  framing. Add a Gate 0 row.
- `docs/60_API_AND_MCP.md:10,51`: "everything is tenant-scoped" / "every call logged with ... tenant"
  -> single-tenant wording.
- `docs/ARCHITECTURE_REVIEW.md`: the G4 invariant rows (`:266-268`), the per-table tenant-scoping
  table (`:220-232`), the resolution-flow tenant lines (`:95-130`), and the graph-isolation section
  (`:244-254`) — amend to single-tenant; X2/X3 per-tenant forks (`:291`) noted moot.
- `docs/10_ARCHITECTURE.md:134,140-143` and `docs/00_VISION_AND_SCOPE.md:70` ("multi-tenant SaaS from
  day one", "scope every row/node/edge by `tenant_id`", graph-isolation) — reconcile to D1 single-
  tenant, noting the multi-tenant path is a deferred cloud-tier decision (ADR 0042 leaves the door
  open per ADR 0017's original re-evaluation clause).

### 8.4 ADRs
- **0017 — SUPERSEDE.** The new ADR is **0042**; set 0017 status to `SUPERSEDED by 0042`.
- **0028 — NOTE only (do NOT supersede).** Its G4 motivation is moot, but the ephemeral resolver is
  KEPT for its B-1 crash-recovery / ADR-0026 batch-purity role. Add a dated note.
- **0029 — NOTE only.** Per-tenant routing (`_resolve_tenant`, distinct-tenant select) removed; the
  X2 (HA lease) / X3 (single-writer-per-tenant) forks are moot under single-tenant. Add a note.
- **0031 — NOTE only.** Judgement tenant-scoping dropped; the return-to-block state machine is
  unchanged. Add a note.

---

## 9. Adversarial target (the deletion-gate's job)

The judge (and a council cross-check if uncertain) must actively try to find a **kept caller whose
correctness depended on `tenant_id` for something OTHER than tenant isolation.** Orient found none;
candidate hiding spots to probe:
- **`_canonical_id`** — confirm `tenant_id` is not in the hash input (it is not — `:47-57`). A merge
  in two notional tenants would have produced the same id today; under single-tenant this is moot.
- **The two composite uniques** — confirm shrinking them does not let a previously-rejected duplicate
  through. With one tenant the leading column is constant, so the 2-col unique is exactly as
  selective. Probe: any code path that inserted the SAME `(source_record, entity_id)` under DIFFERENT
  tenant_ids relying on the constraint to keep them distinct? (Single-tenant: cannot happen.)
- **The Neo4j MERGE key** — confirm removing `tenant_id` from the node key does not fuse nodes that a
  single tenant intends distinct. Node id is the FtM/canonical id; with one tenant the `{id}` key is
  exactly as distinguishing as `{id, tenant_id}`.
- **The landing-zone key prefix** (`ingest.py:151-161`) — confirm dropping the `tenant_id` segment
  cannot collide two distinct records' S3 keys. With one tenant the prefix was a constant; the
  remaining `connector_id/dataset/{safe_key}.json` is unchanged in distinguishing power.
- **`run_resolution`'s distinct-tenant loop** — confirm collapsing to one pass does not skip a
  backlog. With one tenant the distinct select returned exactly one id; one pass is equivalent.
If any probe finds a real dependency, that is a DENY (a behaviour would change) — escalate to the
human, since it would contradict the result-neutrality result this whole gate rests on.

---

## 10. The DROP-vs-keep-dead decision (recorded in ADR 0042)

**DROP the column** (new 0004 migration). Keep-dead was rejected because the 8 columns are NOT NULL:
keeping them dead still forces every INSERT site to be rewritten (you cannot insert without supplying
the NOT NULL column, so the code change is identical), AND leaves a dead column + dead Neo4j property
forever. The two composite uniques + the Neo4j constraint must be redefined regardless of drop-vs-keep.
So keep-dead is strictly worse: same code churn, plus permanent dead schema. Orient found no
disproportionate-rewrite cliff — the change is broad but mechanical. DROP it.

---

## 11. Locked invariants this gate holds

- **G1 — provenance on every node AND edge:** PRESERVED. The writer keeps projecting `prov_*` onto
  every node and relationship (`_inject_tenant`'s `props`/`node_props_by_id` branches, `edge_props`);
  only the `tenant_id` stamp is removed. `test_edge_provenance` / `test_graph_writer` keep-green.
- **append-only / no un-merge:** PRESERVED. No delete path is added; 0004 removes a column, not graph
  history.
- **canonical-canonical only via the guard:** PRESERVED. The merge guard + return-to-block sign-off
  state machine are behaviourally untouched.
- **ADR-0036 deterministic canonical id:** PRESERVED & the basis of result-neutrality.
- **ADR-0028 ephemeral resolver:** PRESERVED (lifecycle frozen; §3.3).
- **G4 — tenant isolation:** DELETED, by the authority of D1 / ADR 0042. This is the sole invariant
  this gate removes; every other invariant above must survive untouched (the judge verifies).
