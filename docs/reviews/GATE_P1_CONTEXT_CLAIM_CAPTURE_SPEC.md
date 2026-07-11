# GATE P1 — context-claim capture lane — BUILD SPEC

- **Owning decision:** ADR 0106 (`docs/decisions/0106-context-claim-capture-lane.md`), status PROPOSED.
- **Source:** the Fable log-capture consult (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md` §4 + §8
  repo defects) and the pre-cutover gate sequence (`docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md`).
  Anchors verified first-hand against `origin/master` (`0efb9d3`) / local `db3b46b` (`resolution/**`,
  `graph/**`, `ontology/**` identical between them; `db/models.py` + migrations differ — read via
  `git show origin/master:<path>`).
- **Governance:** `human_fork: false`, `person_affecting: false`, `human_cosign: PENDING`. Additive
  capture of evidence the existing merge/sign-off path already produced (0099/0100 precedent): no ER
  threshold, no merge/park/score/erasure/gold change, no live graph write. Because the diff edits
  `resolution/**`, ADR 0097 §4/§5 requires the explicit cosign — the main loop asks the user **after**
  the verify+fix round and stamps the dated line (3a-ii-B pattern). See ADR 0106 §human_cosign.
- **Branch (do NOT create it here — planner writes files only):** `gate/p1-context-claim-capture`, cut
  from `origin/master`. **Next migration = `0012`** (0011 = `llm_egress` on origin only).

The test-author writes RED tests first; the builder makes them GREEN without weakening any FROZEN
invariant.

---

## 1. Verified current state (do not re-derive; confirm if editing)

| Fact | Location |
|---|---|
| Anchors live in FtM **context** (`wm_anchor_*` keys); fusion iterates `member.properties` only ⇒ no anchor can enter the statement lane (structural) | `ontology/anchors.py:33,41`, `resolution/merge.py:296` |
| On the node, anchors materialise as **bare** keys (`wikidata_id`/`geonames_id`/`lei`/`opencorporates_id`) via `get_anchors` (strips the prefix) | `graph/writer.py:191`, `ontology/anchors.py:99-123`, `CANONICAL_ID_FIELDS` `:31` |
| `divergence.py:85` `prop.startswith("wm_anchor_")` is **DEAD** — never matches a bare node key; the guard compares anchors today while the fold cannot reconstruct them | `resolution/divergence.py:82-88`, `resolution/projector.py:110` |
| `enrich` at `pipeline.py:437` runs **before** `record_statements` at `:483` (ADR 0100's parenthetical is backwards); capture point is a separate call in the same promote block | `resolution/pipeline.py:437,483` |
| `record_statements`/`record_decision` = pure `session.add`, caller-commits, INSERT-only; the exact idiom to mirror | `resolution/statements.py` |
| `get_anchors` **omits** a conflicting field (never picks `[0]`); `anchor_conflicts_across` is the guard's per-source-member park view | `ontology/anchors.py:99-123,79-96` |
| `signoff.approve()` builds `member_rows` + `[make_entity(r.raw_entity) for r in member_rows]`; writes graph BEFORE `session.commit()`; writes **no** statement/decision row (P3's gap) | `resolution/signoff.py:279-298` |
| The fold re-reads each **touched** survivor's FULL statement history + owns its watermark (F3, ADR 0101 A1) | `resolution/projector.py:329-354` |
| `ProjectionCheckpoint` has `last_statement_seq` + `last_decision_seq`; `project()` reads/upserts under `checkpoint_id` | `db/models.py`, `resolution/projector.py:296-382` |
| **Known trap:** a new `seq` IDENTITY column needs the dialect-guarded `_assign_sqlite_seq` `before_insert` listener (Postgres IDENTITY is a no-op on SQLite) — REUSE the function, add one `event.listen` | `db/models.py` `_assign_sqlite_seq` block |
| Drift guard: `alembic check` + per-table `create_all`-vs-alembic-head snapshot equality; a new table + additive column are exercised automatically | `tests/integration/test_migrations.py` |
| `graph_signature(client, exclude_node_props, exclude_edge_props)` — the byte-comparable fingerprint IT-PROJ/P-FOLD share; captures anchors by default | `tests/integration/test_projector.py`, `tests/property/test_prop_fold_engine.py` |
| The ingest runner stamps every mapped entity ⇒ an anchored production member is always provenance-stamped | `runner/ingest.py` |

---

## 2. The gate — three slices

### Slice P1-0 — docs-only errata + planning docs (independent, no cosign, ship FIRST as its OWN PR)

Autonomously mergeable while the main loop is parked at the P1 cosign pause. Touches no code. Ships as a
separate docs-only PR from its own branch, **before** the P1 code PR; the P1 branch then re-cuts/rebases
onto the merged master. **This slice also commits the planning corpus**: this spec,
`81_PRECUTOVER_GATE_SEQUENCE.md`, and ADRs **0106 + 0107 + 0108 as PROPOSED** (the P2/P3 drafts are
explicitly awaiting-cosign-before-build), plus the **`gen_adr_index.py` regen** so `README.md` carries
their three rows. Rationale (adversarial-verify finding, HIGH ×3): the index generator scans the
**filesystem**, so untracked draft ADRs make `--check` fail locally (the baseline
`test_real_repo_acceptance_guard` failure reproduced this) and would poison the P1 builder's regen with
rows for files the P1 PR doesn't commit — local-green/CI-red. Committing PROPOSED drafts is the repo's
normal pattern (0101/0102 precedent); the accept-flip still happens per-gate.

- **ADR 0100 erratum note** (`docs/decisions/0100-fold-engine-outbox-projector.md`): append an
  `**Erratum (2026-07-05, Gate P1):**` note correcting the E2 ordering parenthetical (`enrich` at
  `pipeline.py:437` runs **before** `record_statements` at `:483`, not after; the structural
  context-vs-properties fact is the real bar) and noting D3's "Anchors — not reconstructed (E2)" is
  **retired** by Gate P1. Append-only note — do not rewrite the original body (ADR-0097 immutable-body
  discipline).
- **ADR 0102 erratum note** (`docs/decisions/0102-projection-rebuild-diff-guard.md`): append an
  `**Erratum (2026-07-05, Gate P1):**` note correcting D6's `wm_anchor_*` key-shape text (nodes carry
  **bare** `CANONICAL_ID_FIELDS` keys; the `startswith("wm_anchor_")` exclusion was dead code, fixed in
  P1 to exclude the bare keys under pick-semantics).
- **Roadmap ★ truth-up** (`docs/40_ROADMAP.md`): the `## Next — Gate 0 … ★ CURRENT` marker (line ~56) is
  **stale** (Gate 0 shipped; the fleet is at the pre-cutover P-gate sequence). Move the `★ CURRENT`
  marker to reflect **Gate P1 = current**, referencing `81_PRECUTOVER_GATE_SEQUENCE.md`. Do not rewrite
  the historical done-items; add/repoint the current-milestone marker only.

The errata notes change no ADR headers, but the **newly committed 0106/0107/0108 files require the
`gen_adr_index.py` regen in this slice** (three new PROPOSED rows in `README.md`).

### Slice P1-a — capture write-lane (additive; carries the cosign)

The write side. The lane is written; the fold does not read it yet (dormant read-side, safe to merge
alone). Files: `db/models.py`, migration `0012`, `ontology/anchors.py` (additive helper),
`resolution/statements.py`, `resolution/pipeline.py`, `resolution/signoff.py`, tests.

**2.a.1 `ContextClaimRecord` (`db/models.py`)** — per ADR 0106 §1. Additive model. Add `Text` is
already imported; confirm imports. Register the SQLite fallback with **one** line after the existing two:
`event.listen(ContextClaimRecord, "before_insert", _assign_sqlite_seq)` — the `_assign_sqlite_seq`
function body stays **byte-unchanged** (the named ADR-0100 trap avoidance). Add the additive
`ProjectionCheckpoint.last_context_claim_seq: Mapped[int] = mapped_column(BigInteger,
server_default=text("0"), nullable=False)` column — the **only** change to an existing model.

**2.a.2 Migration `0012_context_claim_lane.py`** — `revision = "0012_context_claim_lane"`,
`down_revision = "0011_llm_egress_audit"`. `upgrade()` creates `context_claim` (+ indexes on `seq`,
`canonical_id`, `entity_id`) **and** `op.add_column("projection_checkpoint", ...last_context_claim_seq
BigInteger server_default '0' nullable=False...)`. `downgrade()` drops the column then the table + its
indexes. Byte-agree with the model (drift guard). Do NOT edit `0001`–`0011`.

**2.a.3 `anchors.set_anchor_claims` (`ontology/anchors.py`, additive)**:
```
def set_anchor_claims(entity: FtmEntity, field: str, values: Iterable[str]) -> None:
    """Set the raw multi-value anchor context for a field (fold reinstatement, Gate P1).
    Mirrors the merge_context union shape so get_anchors applies the identical omit-on-conflict."""
    if field not in CANONICAL_ID_FIELDS:
        raise ValueError(f"unknown anchor field: {field!r}")
    vals = sorted({v for v in values if isinstance(v, str) and v})
    if vals:
        entity.context[f"{_CONTEXT_PREFIX}{field}"] = vals
```
Every existing function (`set_anchor`, `get_anchors`, `_anchor_values`, `get_anchor_conflicts`,
`anchor_conflicts_across`) stays **byte-unchanged**.

**2.a.4 Writers (`resolution/statements.py`, additive)** — mirror `fuse_statement_rows` /
`record_statements`:
```
def fuse_context_claim_rows(canonical_id: str, members: Iterable[FtmEntity]) -> list[ContextClaimRecord]:
    # per member: prov = get_provenance(member); if prov is None or not prov.retrieved_at: skip+log
    #   dataset = prov.source_id or member.id;  retrieved_at = prov.retrieved_at
    #   for field, value in get_anchors(member).items():
    #       row(id=uuid4, canonical_id, entity_id=member.id or canonical_id, key=field, value=value,
    #           dataset=dataset, method="connector:map", retrieved_at=retrieved_at, scope="default")

def record_context_claims(session, canonical_id: str, members: Iterable[FtmEntity]) -> None:
    for row in fuse_context_claim_rows(canonical_id, members): session.add(row)
```
INSERT-only; no UPDATE/DELETE. Docstring mirrors `statements.py`'s append-only contract.

**2.a.5 Pipeline hook (`resolution/pipeline.py`, additive)** — after `record_statements(session,
cluster, by_id)` at `:483`:
```
record_context_claims(session, cluster.canonical_id, [by_id[m] for m in cluster.member_ids if m in by_id])
```
Module-level import so an integration monkeypatch works (as `record_statements` is). Every promoted
cluster (singleton + merge). Parked path unchanged (writes nothing).

**2.a.6 Sign-off hook (`resolution/signoff.py`, additive)** — in `approve()`, **before**
`session.commit()` (after `write_entities`), reusing the already-built member entities:
```
record_context_claims(session, canonical_id, [make_entity(r.raw_entity) for r in member_rows])
```
**Additive evidence banking only** — do NOT add statement/decision rows, do NOT change the approve/reject
decision or the graph write (that is Gate P3). `reject()` is **unchanged** in P1 (rejected members are
written under their own ids; their anchor capture is P3's member-write-equivalent concern).

### Slice P1-b — fold reinstatement + guard exclusion fix (depends on P1-a's table)

The read side. Files: `resolution/projector.py`, `resolution/divergence.py`, tests. Mergeable after P1-a.

**2.b.1 `reconstruct_entities` (`projector.py`)** — additive `context_claim_rows: list[ContextClaimRecord]
= ()` param (default empty ⇒ existing callers byte-behaviour-identical). After building each survivor
entity: group context rows by `survivor_of(row.canonical_id)`, and for each `key` set
`anchors.set_anchor_claims(entity, key, {distinct values})`. The docstring's "ANCHORS: NOT reconstructed"
note changes to "ANCHORS: reconstructed from the context_claim lane (Gate P1) — omit-on-conflict via
get_anchors". No change to schema/props/witness/provenance logic.

**2.b.2 `project()` (`projector.py`)** — read `context_claim` rows (full + incremental); the incremental
touched set = `{survivor_of(r.canonical_id) for r in statement_delta} | {survivor_of(r.canonical_id) for
r in context_claim_delta}`; re-read, **over that UNION touched set**, each touched survivor's FULL
**statement** history AND its FULL **context-claim** history (both preimages over the alias map, same
recipe as statements — a survivor touched ONLY by a context delta still needs its statement history
re-read, else there is no entity to hang the anchors on); pass `context_claim_rows` into
`reconstruct_entities`; read/advance `last_context_claim_seq` from/into the checkpoint (default = existing
watermark so it never goes backward). `ProjectionResult` gains an additive `context_claims_read: int = 0`
field for observability (default preserves existing constructions). **Context-only survivor with ZERO
statement rows** (production-real in P1: a `signoff.approve()` writes context claims but no statement rows
until P3): `reconstruct_entities` groups by statement rows, so such a survivor yields **no entity and no
anchors — a graceful no-op, never a crash**; its claims are correct-but-dormant until P3 (ADR 0106 §3).
A dedicated test pins the no-op (§4).

**2.b.3 `_excluded` (`divergence.py:82-88`)** — DELETE `prop.startswith("wm_anchor_")`; add
`or prop in CANONICAL_ID_FIELDS` (import `from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS`
— confirm no Neo4j/`worldmonitor.db` import lands; `anchors.py` has none (its `ontology.ftm` import
transitively loads SQLAlchemy via `followthemoney` — a pure library import, no live connection — so
`divergence.py` stays Docker-free / no-live-DB). Update the `_excluded` docstring (bare keys, pick-semantics, ADR 0106 §Sub-fork A).

**2.b.4 Existing-test truth-up (REQUIRED — the guard change breaks them RED otherwise; adversarial-verify
CRITICAL):** `tests/unit/test_projection_divergence.py::test_wm_anchor_prop_added_to_live_is_excluded`
(`:93-109`) asserts the OLD `wm_anchor_qid` exclusion and **fails under the new predicate** (the extra
live prop is no longer excluded ⇒ `total == 1`). **REPLACE** (not augment) its `wm_anchor_qid` extra with
a bare key (`"wikidata_id": frozenset({"Q123"})`) and update the docstring/assert message to the bare-key
E2 exclusion. Same REPLACE semantics for the property suite's synthetic key (§3 P-DIV update) — leaving
the old `wm_anchor_qid` extra anywhere would false-alarm under the new exclusion. Before finalising,
**grep the whole tree for `wm_anchor_` used as a NODE-property / divergence-exclusion assertion** (context
-key usage via `set_anchor` is fine and unaffected) — as of planning, the only two are the unit + property
divergence suites.

---

## 3. Property invariants (@given — RED-first)

NAME · STATEMENT · GENERATOR · ORACLE · NON-VACUITY. Container-backed property examples MUST wrap any
per-example engine in `try/finally` dispose (memory: given-red-tests-leak-connections). New file
`tests/property/test_prop_context_claim_capture.py` unless noted.

**P-CTX-1 — lossless anchor capture (Slice P1-a; real-Postgres round-trip).**
- *Statement:* for a promoted cluster / a sign-off member set, the persisted `context_claim` rows equal —
  none invented, none dropped — the independently-derived per-member anchor projection: for each member
  `m`, each `(field, value) ∈ get_anchors(m)`, the tuple `(canonical_id, entity_id=m.id, key=field, value,
  dataset=source_of(m), method="connector:map", retrieved_at=m.Provenance.retrieved_at)`.
- *Generator:* `@given` over member entities with 0..k anchors + stamped provenance.
- *Oracle:* an independent oracle re-deriving the tuple set from members; assert row-set equality
  (excluding `id`/`created_at`).
- *Non-vacuity (MANDATORY generator coverage — adversarial-verify HIGH: without these, capture-from-the-
  merged-entity passes):* the generator MUST produce (i) multi-member clusters whose members carry
  **DISTINCT `dataset`s** (kills merged-entity capture's dataset loss — a single shared source would mask
  it), and (ii) a cluster with a **cross-member CONFLICTING anchor pair** (same key, different values):
  per-member capture writes BOTH rows; merged-entity capture writes ZERO (its `merge_context` union makes
  `get_anchors` omit the key). Assert ≥1 anchored row overall; a dropped conflicting-member row fails.

**P-CTX-2 — non-mutation fence (Slice P1-a).**
- *Statement:* `fuse_context_claim_rows`/`record_context_claims` leave every `member.to_dict()` and
  `cluster.entity.to_dict()` byte-identical to a pre-snapshot, and `session.add` receives only
  `ContextClaimRecord` (never a `StatementRecord`/`DecisionRecord`/`MergeAudit`/FtM entity).
- *Non-vacuity:* an impl that `set_anchor`s onto a member (mutating context) fails; one that also emits a
  statement row fails.

**P-CTX-3 — provenance-complete · append-only · parked/no-anchor writes nothing (Slice P1-a).**
- *Statement:* every written row has **non-NULL** `method` AND `retrieved_at`; the writer issues only
  `session.add` (a spy session records no UPDATE/DELETE/`session.delete`); a member with no stamped
  provenance / no `retrieved_at` yields **zero** rows for its anchors (skip-and-log); a no-anchor member
  yields zero rows.
- *Non-vacuity:* an impl writing a naked (NULL-retrieved_at) row fails; an in-place UPDATE fails.

**P-CTX-4 — anchor round-trip fidelity / fold reinstatement (Slice P1-b; pure).**
- *Statement:* for a single-batch anchored corpus with **no** key conflict, `reconstruct_entities` +
  `get_anchors` on the fold entity yields the SAME `{field: value}` as `get_anchors` on the directly-merged
  entity.
- *Generator:* `@given` over survivors with 1 distinct value per anchor key.
- *Oracle:* `get_anchors(fold_entity) == get_anchors(direct_entity)`.
- *Non-vacuity:* a fold that never sets the context (today) fails; a corpus with ≥1 anchor.

**P-CTX-5 — omit-on-conflict parity (Slice P1-b; pure).**
- *Statement:* for a survivor whose context rows hold **>1** distinct value for a key, the fold entity's
  `get_anchors` OMITS that key — identical to `get_anchors` on the merged entity whose `merge_context`
  unions the conflicting values.
- *Non-vacuity:* a fold that picks an arbitrary `[0]` winner fails; a single-value key must still be
  present (guards against omit-everything).

**P-CTX-6 — incremental == full-rebuild WITH anchors (Slice P1-b; extends P-FOLD-2, real DB or the
P-FOLD direct-append recipe).**
- *Statement:* folding a multi-batch anchored log incrementally (INCLUDING a context-claim-only delta with
  no new statement rows for an already-folded survivor) produces node anchors byte-identical to a single
  `full_rebuild`.
- *Generator:* `@given` over a batch plan mixing statement deltas + context-only deltas for shared
  survivors, composed at the **writer level** (direct `StatementRecord`/`ContextClaimRecord` appends, the
  P-FOLD direct-append recipe — the log is the interface; the production pipeline writes both lanes
  atomically, so a context-only delta for a statement-bearing survivor is exercised here by construction,
  with the stated precondition that every anchored survivor in the plan carries ≥1 statement row).
- *Oracle:* `graph_signature` (anchors included) after incremental == after full_rebuild.
- *Non-vacuity:* an incremental fold whose touched set ignores the context-claim delta (drops the
  context-only anchor) fails; a thin-delta re-read that clobbers fails.

**P-DIV update (edit `tests/property/test_prop_projection_divergence.py`).** **REPLACE** the synthetic
`live_props["wm_anchor_qid"]` E-legit extra (`:128`) with a **bare** `CANONICAL_ID_FIELDS` key (e.g.
`live_props["wikidata_id"] = frozenset({f"Q-{surv}"})`) so P-DIV-1 proves the REAL bare-key exclusion —
REPLACE, not augment: a leftover `wm_anchor_qid` extra is NOT excluded by the new predicate and would
false-alarm. P-DIV-2 unaffected (its rot injection uses reserved tokens on compared props). The
same-shaped **unit** test is updated per §2.b.4 (RED-first evidence: both fail against today's predicate
once the bare key is in, and against the new predicate with the old key left in — the pair is the
regression witness in both directions).

---

## 4. Unit + integration tests

- **`tests/unit/test_anchors.py` (extend):** `set_anchor_claims` — single value sets `[v]`; multiple sets
  the deduped list; empty is a no-op; unknown field raises; `get_anchors` after a multi-value
  `set_anchor_claims` OMITS the key (round-trips the omit).
- **`tests/unit/test_statements.py` (new or extend):** `fuse_context_claim_rows` column mapping
  (key/value/dataset/method/retrieved_at/entity_id); skip-and-log for an unstamped member; no-anchor
  member ⇒ `[]`.
- **`tests/integration/test_context_claim_lane.py` (NEW, `pytest.mark.integration`, real Postgres — Docker
  IS available locally, run it):** apply migrations to a fresh DB; drive a real `resolve_pending` on an
  anchored corpus + a real `signoff.approve()`; SELECT `context_claim` and assert (a) lossless per-member
  rows with NOT-NULL `method`/`retrieved_at`; (b) the `projection_checkpoint.last_context_claim_seq`
  column exists and advances after `project()`; (c) append-only at the DB level (no UPDATE/DELETE issued);
  (d) `test_migrations.py` drift guard stays green **unchanged** (exercises the new table + column
  automatically once model + migration agree); (e) **server_default pin (adversarial-verify MEDIUM —
  neither `alembic check` nor the snapshot guard compares server_default):** run the `0012` upgrade
  against a DB whose `projection_checkpoint` was **pre-seeded with a row** at the prior head — a missing
  `server_default='0'` on the NOT-NULL add-column fails exactly here (the 0008 precedent), and/or
  introspect the column default; (f) **context-only-survivor no-op**: after `signoff.approve()` (context
  claims, zero statement rows), `project(full_rebuild=True)` completes without error and produces NO node
  for that survivor (dormant-until-P3 pinned).
- **SQLite `seq` listener runtime pin (unit; extend `tests/unit/test_statements.py` or the models suite):**
  inserting `ContextClaimRecord`s in an SQLite session assigns monotonic non-NULL `seq` (the reused
  `_assign_sqlite_seq` listener actually registered — no test today pins any lane's listener at runtime;
  this is the first).
- **`tests/integration/test_projector.py` (extend):** a new `_anchored_candidates()` fixture (single
  batch, single source `src:statement-spine-test`, entities carrying `geonames_id`/`opencorporates_id`
  anchors, **no** conflict) → **IT-PROJ-2-class anchor parity**: the fold's `graph_signature` (anchors
  **included**, NOT excluded — the equivalence signature compares them) EQUALS the direct writer's
  EXACTLY. Non-vacuous: assert ≥1 bare anchor key present on both sides.

---

## 5. Builder task list (ordered)

**Slice P1-0 (docs, ship first, OWN PR from its own branch):** ADR 0100 + 0102 errata notes; roadmap ★
move; commit this spec + `81_PRECUTOVER_GATE_SEQUENCE.md` + ADRs 0106/0107/0108 (PROPOSED); regen the ADR
index (three new rows). Open PR; merge on green (no cosign — docs-only, no code). Then re-cut/rebase the
P1 branch onto the merged master.

**Slice P1-a:** 1) `db/models.py` — `ContextClaimRecord` + the `event.listen` line +
`ProjectionCheckpoint.last_context_claim_seq` (`server_default=text("0")`, the §4(e) pin). 2) migration
`0012`. 3) `anchors.set_anchor_claims`. 4) `statements.py` writers. 5) pipeline + signoff hooks. 6) make
P-CTX-1/2/3 + unit (incl. the SQLite `seq` pin) + `test_context_claim_lane.py` GREEN.

**Slice P1-b:** 1) `projector.py` read-side + reinstatement (UNION touched set; context-only no-op). 2)
`divergence.py` exclusion fix. 3) the §2.b.4 existing-test truth-up (unit + property divergence suites,
REPLACE semantics). 4) make P-CTX-4/5/6 + the P-DIV update + the IT-PROJ anchored-corpus test GREEN.

**Both slices = ONE code PR** on `gate/p1-context-claim-capture`. At the accept flip (post-cosign): stamp
ADR 0106's `human_cosign` dated line, flip PROPOSED → ACCEPTED, re-run `gen_adr_index.py` (the 0106 row
status flips; 0107/0108 rows already exist from P1-0), `--check` passes.

Cosign gate: the main loop asks the user after the verify+fix round, discloses the fixed findings, then
stamps ADR 0106's `human_cosign` dated line and flips PROPOSED → ACCEPTED at merge.

---

## 6. Acceptance criteria (all measurable)

- **FULL** `uv run pytest -m "not integration"` GREEN repo-wide (the `quality` job runs exactly this).
- **Integration GREEN locally** (`uv run pytest -m integration`): `test_context_claim_lane.py`, the
  extended `test_projector.py`, and the unchanged `test_migrations.py` drift guard pass.
- All new `@given` properties GREEN: P-CTX-1..6; P-DIV-1/2 green with the bare-key update.
- **FROZEN-adjacent suites stay green:** the existing `test_prop_statement_spine.py` (P-STMT-1/2/3),
  `test_prop_fold_engine.py` P-FOLD-1/3/4/5 (P-FOLD-2 extended, not broken), and every IT-PROJ-1/2/3/4
  pass — the additive `context_claim_rows` default empty preserves them.
- `ruff format --check .` (REPO-WIDE) clean; `ruff check .` clean; `uv run pyright` clean.
- `uv run python scripts/gen_adr_index.py --check` passes on the P1 branch AND on a simulated clean
  checkout of the PR's file set (the 0106/0107/0108 rows exist from P1-0; the P1 PR only flips 0106's
  status at accept time).
- **Anchored fold-vs-direct parity is claimed over provenance-STAMPED corpora** (production connectors
  always stamp — `runner/ingest.py`); an anchored-but-unstamped member is the documented skip-and-log
  residual (ADR 0106 sub-fork), pinned by P-CTX-3, and excluded from parity corpora by construction.
- `quality` + `security` (+ `adr-index`) CI green before merge; `gh pr checks <N> --watch` before any
  merge.
- ADR 0106 `human_cosign` stamped (dated) before/at the accept flip; the checker + judge reproduce the
  `person_affecting: false` self-tag against the diff and DENY on a person-affecting-untagged or
  un-cosigned diff (ADR 0097 §5).

---

## 7. Invariants the checker MUST reproduce

- **INV-CTX-LOSSLESS** — captured rows == the independent per-member anchor projection; none invented,
  none dropped. (P-CTX-1)
- **INV-CTX-PROV** — every captured row has non-NULL `method` AND `retrieved_at`; `dataset` = the
  contributing member's `source_id`; an unprovenanceable anchor is skipped, never written naked. (P-CTX-3)
- **INV-CTX-APPENDONLY** — the writers issue only INSERTs (`session.add`); never UPDATE, DELETE, or
  `session.delete`, across any call sequence. (P-CTX-3)
- **INV-CTX-NONMUTATION** — capture mutates no FtM entity and no statement/decision/merge_audit row; the
  entity handed to `write_entities` is byte-identical with capture on vs off. (P-CTX-2)
- **INV-CTX-PARKED-NOTHING** — a block-mode parked cluster and a no-anchor member write zero context rows;
  `reject()` writes none in P1. (P-CTX-3)
- **INV-FOLD-ANCHOR-PARITY** — on a single-batch anchored corpus (no conflict), the fold node's anchors ==
  the direct write's, byte-for-byte; the equivalence signature COMPARES anchors. (P-CTX-4 / IT-PROJ
  anchored)
- **INV-FOLD-OMIT-CONFLICT** — the fold reproduces `get_anchors` omit-on-conflict: >1 distinct claim value
  for a key ⇒ no bare key projected. (P-CTX-5)
- **INV-FOLD-INCR-ANCHOR** — incremental fold (incl. a context-claim-only delta) == full_rebuild on node
  anchors; `last_context_claim_seq` advances; touched survivors re-read full context history. (P-CTX-6)
- **INV-DIV-ANCHOR-EXCLUSION** — `divergence._excluded` excludes the BARE `CANONICAL_ID_FIELDS` keys (the
  dead `wm_anchor_` prefix branch is gone); the guard no longer false-alarms on anchored live nodes;
  `divergence.py` stays Neo4j/DB-import-free. (P-DIV update)
- **INV-MODEL-ADDITIVE** — the `db/models.py` diff = new `ContextClaimRecord` + one additive
  `ProjectionCheckpoint.last_context_claim_seq` column + one `event.listen(ContextClaimRecord, ...)` line;
  every OTHER existing model **and** the `_assign_sqlite_seq` FUNCTION body are byte-unchanged; the
  migration drift guard proves the existing tables' schemas are unchanged (it does NOT compare
  `server_default` — see INV-CKPT-DEFAULT).
- **INV-CKPT-DEFAULT** — the additive column carries `server_default '0'` and the `0012` upgrade succeeds
  against a `projection_checkpoint` that already holds a row (the §4(e) pre-seeded pin; neither
  `alembic check` nor the snapshot guard verifies defaults).
- **INV-SEQ-SQLITE** — `ContextClaimRecord.seq` uses Postgres IDENTITY + the REUSED `_assign_sqlite_seq`
  listener (the ADR-0100 trap avoided — no new listener, no bricked SQLite insert); pinned at runtime by
  the §4 SQLite insert test (monotonic non-NULL `seq`).
- **INV-FROZEN** — every FROZEN file (§8) is byte-unchanged.

---

## 8. FROZEN (byte-unchanged — the checker verifies `git diff` touches none of these)

- **`resolution/merge.py`** (fusion + value-set), the **merge guard** (`resolution/guard.py` /
  sensitivity), **`resolution/canonical.py`**, **`resolution/referents.py`**, **`resolution/eval.py`**,
  **`resolution/gold.py`**, **`resolution/silver.py`** — no threshold/score/merge/park change.
- **`graph/writer.py`** and **`graph/ftmg_fork.py`** — the fold sets entity context; `write_entities` /
  `get_anchors` project it **unchanged** (no writer edit).
- **`ontology/anchors.py` existing functions** — `set_anchor`, `get_anchors`, `_anchor_values`,
  `get_anchor_conflicts`, `anchor_conflicts_across`, `CANONICAL_ID_FIELDS`, `_CONTEXT_PREFIX`
  byte-unchanged; the ONLY addition is `set_anchor_claims`.
- **`llm/**`, `mcp/**`, `authz/**`, `api/**`, `runner/**`** — P1 wires no driver enricher; the pipeline's
  `enrich` handling is unchanged.
- **Existing migrations `0001`–`0011`** — history is immutable; the delta lives only in `0012`.
- **Every EXISTING `db/models.py` model EXCEPT `ProjectionCheckpoint`** (which gains exactly one additive
  column) — `ConnectorInstance`, `ErQueueItem`, `MergeAudit`, `IngestDeadLetter`, `MergeAlert`, `TaskRun`,
  `ResolverJudgement`, `SignOff`, `CanonicalIdLedger`, `ErGoldPair`, `StatementRecord`, `DecisionRecord`,
  `LlmEgressRecord` — and the `_assign_sqlite_seq` **function body**.
- **`resolution/signoff.py` beyond the one additive `record_context_claims` call in `approve()`** — the
  approve/reject decision, orphan guards, judgement/audit writes, the graph write, and `reject()` are
  byte-unchanged. **P1 does NOT close the sign-off statement/decision gap (that is Gate P3).**
- **`tests/property/test_prop_statement_spine.py`** — stays green unchanged.

---

## 9. OUT OF SCOPE (do NOT build here — see `81_PRECUTOVER_GATE_SEQUENCE.md`)

- **Sign-off statement/decision spine routing** (`decided_by=<operator>`, ledger `record_durable_id`) —
  **Gate P3**. P1 adds only the additive context capture at `approve()`.
- **Erasure reaching the SoR** (three-lane scrub, live-removal mechanism, granularity reconciliation,
  stock scrub, P-FOLD-2 deletion bound) — **Gate P2**.
- **Zero-property promoted-entity disposition** (§7-3) — a post-P1 write-path-integrity slice (ADR 0106
  §Sub-fork H).
- **Enricher wiring** into the driver / capturing the post-enrich entity's anchors — a later gate (P1
  ships the writer + `method` interface only).
- **Set-valued anchor projection + guard comparison** (Sub-fork A other arm) — deferred with a revisit
  trigger.
- **Gate 2b backfill**, the E4 §5 `origin_datasets` rider, `statement.dataset` stamped-ness — Gate 2b.
- Any change to ER/thresholds/merge/guard/gold/scores/erasure, migrations `0001`–`0011`, or the live
  graph write path.
