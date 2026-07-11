# 0106 — Context-claim capture lane (Gate P1): bank anchor/enricher evidence into the SoR spine as provenance-stamped claims, and make the fold reproduce anchors

- **Status:** PROPOSED (2026-07-05)
- **Date:** 2026-07-05
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** PENDING — Gate P1 user cosign (person_affecting:false waiver on the `resolution/**`
  diff, per ADR 0097 §4/§5). P1 is the **additive capture of evidence the existing, unchanged merge /
  sign-off path already produced** (the 0099/0100 precedent): it adds one append-only `context_claim`
  table + INSERT-only writers + a fold read-side + a dead-code guard fix. It changes **no** ER
  threshold, **no** clustering/merge/park outcome, **no** individual-affecting score, **no** erasure
  path, **no** gold label, and **no** live graph write. Because the diff nonetheless edits
  `resolution/**` (`statements.py`, `pipeline.py`, `signoff.py`, `projector.py`, `divergence.py`) while
  self-tagging non-sensitive, ADR 0097 requires the explicit human co-sign carried here. The main loop
  asks the user **after** the adversarial verify+fix round (with the fixed findings disclosed) and then
  stamps the dated line — the 3a-ii-B pattern (a completed, dated cosign, never a promissory one).
- **Realises:** the **E2 capture** blocking-3b-prerequisite of the Fable log-capture consult
  (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md` §4, §7-1) and pre-cutover gate **P1** of the sequenced
  roadmap (`docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md`). **Builds on:** ADR 0099 (the append-only
  statement/decision **spine idiom** — INSERT-only `session.add` writers, caller-commits, model +
  migration byte-agreement per ADR 0030, reserved `scope` column), ADR 0100 (the fold engine + the `seq`
  IDENTITY / dialect-guarded `_assign_sqlite_seq` SQLite listener **trap** this ADR deliberately reuses,
  and D2 global-fold-is-truth), ADR 0101 (F3 incremental touched-survivor full-history re-read, extended
  here to the new lane), ADR 0102 (the divergence guard whose dead `wm_anchor_` exclusion this ADR
  fixes), ADR 0040/0044 (anchor conflict = catastrophic-merge park trigger; anchors feed the durable
  id), ADR 0018 (anchors stored as flat FtM-context keys, projected to bare node properties). **Supersedes:**
  nothing. It **retires** ADR 0100 D3's "Anchors — not reconstructed (E2)" note (see §Consequences).

## Context

ADR 0095 sets the target: the **Postgres statement + decision log = system of record**; **Neo4j = a
derived, rebuildable projection**. Gate 2a (ADR 0099) banked the statement + decision lanes; Gate 3a
(ADR 0100/0101/0102) built the dormant fold engine + the rebuild-and-diff guard. The committed Fable
consult (`80_LOG_CAPTURE_CONSULT.md`) asked, at 3b planning: *does the log capture enough that the
projection is faithful at the irreversible cutover?* Its answer for **E2 (anchors + enricher output)**
is unambiguous: **capture it — a blocking prerequisite**, because anchors are **evidence**, not
decoration:

- **Anchors are structurally absent from the statement lane.** Anchor values live in FtM entity
  **context** (`wm_anchor_*` keys, `ontology/anchors.py:33`), and the statement fusion iterates
  `member.properties` only (`merge.py:296`). No anchor can enter the statement log regardless of call
  ordering — a structural bar, not an ordering accident. (The ADR-0100 E2 parenthetical claiming
  `enrich` runs *after* the dual-write is **backwards** — `enrich` at `pipeline.py:437` runs *before*
  `record_statements` at `:483`; corrected by the Gate-P1 docs-errata slice. Immaterial to the
  conclusion: the context-vs-properties fact is what bars capture.)
- **Anchors are load-bearing.** They materialise as **bare** node properties (`wikidata_id`,
  `geonames_id`, `lei`, `opencorporates_id` = `anchors.CANONICAL_ID_FIELDS`, `graph/writer.py:191` via
  `get_anchors`, which strips the `wm_anchor_` context prefix). They back Neo4j **uniqueness
  constraints**, feed the durable-id tiering, are a catastrophic-merge-guard **park trigger** whose
  values surface in the review UI, and are implicit members of every API/MCP entity response. A rebuild
  that silently drops them amputates the product surface.
- **The divergence guard already compares anchors against a fold that cannot produce them.**
  `divergence.py:85` excludes `prop.startswith("wm_anchor_")` — a key shape that **never occurs** on a
  node (nodes carry the bare keys). The exclusion is **dead code**; on any anchored corpus every
  anchored live node counts UNEXPLAINED forever. The guard therefore **cannot go green on an anchored
  corpus until capture lands**. This makes capture the thing that makes the pre-cutover verification
  instrument usable at all.
- **Provenance-naked anchors.** Enricher output carries no per-anchor method / retrieved-at / source
  stamp; a (future) SPARQL-derived anchor would be indistinguishable from a source-asserted one. Capture
  closes this in the same stroke.

The consult's lock-in warning is the sequencing driver: **once enrichers are wired, un-captured
enricher output is unreconstructable-forever** (re-running a network-backed enricher inside the fold is
rejected — it makes rebuild non-deterministic, ADR 0095's one named risk). Map-time anchors (the only
production anchors today — no enricher is wired: `runner/driver.py` calls `resolve_pending` without
`enrich`) are re-map-recoverable, so their capture is motivated by guard strength and the product
surface; but the **writer + interface must exist before any enricher is wired**. P1 lands that writer.

## Decision

Add a **second append-only lane in the same SoR spine** — a `context_claim` table — written at **both**
live promote points, and make the fold reproduce anchors from it. Purely additive; the live graph write
and every merge/guard/ER decision are byte-unchanged.

### 1. The `context_claim` table (`ContextClaimRecord`) — append-only, INSERT-only

One row per **(survivor `canonical_id`, contributing member, anchor `key`, `value`)** claim. Mirrors the
ADR-0099 statement lane's idiom (surrogate PK, `seq` IDENTITY watermark, `scope` reserved column, model
+ migration `0012` byte-agreement per ADR 0030).

| column | type | null | value at write time |
|--------|------|------|---------------------|
| `id` | `String(64)` PK | no | fresh `uuid4()` per row |
| `seq` | `BigInteger`, `Identity()`, indexed | no | server-assigned Postgres IDENTITY — the fold's outbox watermark (ADR 0100 D1); **never** set by app code |
| `canonical_id` | `String(255)`, indexed | no | the cluster's / sign-off's durable survivor id (subject) |
| `entity_id` | `String(255)`, indexed | no | the contributing source member id (per-member attribution) |
| `key` | `String(64)` | no | the anchor field — one of `anchors.CANONICAL_ID_FIELDS` in P1 (forward-compat for enricher keys; **deliberately not an FtM schema property** → not FtM-validated) |
| `value` | `Text` | no | the anchor value (e.g. the QID / LEI); unbounded TEXT — hostile-data rule, no cast |
| `dataset` | `String(255)` | no | the **contributing member's** `Provenance.source_id` (G1 source) — so the P2 erasure scrub reaches anchor claims by dataset |
| `method` | `String(64)` | **no** | `"connector:map"` for map-time anchors; the enricher interface (§4) uses `"enricher:<name>@<version>"`. **NOT NULL — mandatory provenance** |
| `retrieved_at` | `String(64)` | **no** | the member's `Provenance.retrieved_at` (ISO-8601 **string**, no cast). **NOT NULL — mandatory provenance** |
| `scope` | `String(64)`, `server_default "default"` | yes | reserved forward-compat (ADR 0099 Decision A) — UNENFORCED; single-tenant D1 unchanged |
| `created_at` | `DateTime(timezone=True)`, `server_default now()` | no | insert time |

- **`method` + `retrieved_at` are NOT NULL** — the one place P1 departs from the statement lane (which
  makes them nullable). Rationale: an anchor claim with no provenance is exactly the "provenance-naked
  anchor" gap the consult flags; a claim we cannot fully provenance is **not captured** rather than
  written naked (honesty over completeness). An anchored-but-unstamped member's anchor is **skipped and
  logged**, never written — see §3. (This should not occur for production connectors: the ingest runner
  stamps every mapped entity.)
- **Per-member grain (not merged-entity grain).** Capturing from the merged `cluster.entity` would lose
  the per-member `dataset` attribution the P2 scrub needs, and would fold the conflict prematurely. The
  lane holds **one row per member per anchor value**; the merge/conflict is reconstructed at fold time
  from the multiple rows (§2) — this reproduces `anchor_conflicts_across` (the guard's view over source
  members, `anchors.py:79`) exactly.
- **Pseudo-prop rows in `statement` are REJECTED** as the mechanism (consult §4): FtM's `get_prop_type`
  raises for a non-schema prop at `Statement` construction; anchors are deliberately not schema
  properties. The separate lane is the FtM-pure home. Its booked cost (its own watermark, the F3-style
  touched-survivor re-read, future 2b-backfill coverage, P2 erasure-scrub coverage) is paid here.
- **`seq` needs the ADR-0100 SQLite treatment.** Postgres IDENTITY is a no-op on SQLite (fast unit
  tests). The fix is to **reuse the existing `_assign_sqlite_seq` `before_insert` listener verbatim** and
  add exactly one line — `event.listen(ContextClaimRecord, "before_insert", _assign_sqlite_seq)`. The
  function body stays byte-unchanged; this is the named avoidance of the 0100 regression (do **not**
  write a new listener).

### 2. The fold reproduces anchors (`resolution/projector.py`) — deterministic omit-on-conflict

`reconstruct_entities` gains an **additive** `context_claim_rows` parameter (default empty → every
existing caller byte-behaviour-identical, the ADR-0102 `checkpoint_id` additive-param idiom). Per fold
survivor group, per `key`, it collects the **set of distinct claim values** and sets the entity's
context union `entity.context["wm_anchor_<key>"] = sorted(values)` via a new additive
`anchors.set_anchor_claims` helper. `write_entities` → `get_anchors` then applies the **identical**
omit-on-conflict rule the live path uses:

- exactly **one** distinct value for a key ⇒ the bare key is projected onto the fold node (matches the
  direct write);
- **more than one** distinct value ⇒ `get_anchors` **omits** the key (refuses to pick a winner, Gate
  B-5 / ADR 0040 Finding 1) — the fold node carries no bare key, matching the live merged node whose
  `merge_context` union `get_anchors` likewise omits.

`project()` reads the lane both **full** (`full_rebuild`) and **incremental**: the incremental touched
set is the union of survivors from the **statement delta AND the context-claim delta**, and for each
touched survivor the fold re-reads its **complete** context-claim history (ADR 0101 A1 applied to the
new lane — a thin delta would clobber the additive `SET n += props`). The `context_claim` watermark
lives in an **additive `ProjectionCheckpoint.last_context_claim_seq` column** (`BigInteger`,
`server_default 0`, `nullable=False`) — the minimal, consistent choice mirroring
`last_statement_seq`/`last_decision_seq`. Migration `0012` both creates `context_claim` and adds that
one column.

### 3. Capture at BOTH promote points (`pipeline.py` + `signoff.py`) — additive only

A new authoritative projection + writer in `resolution/statements.py`, mirroring `fuse_statement_rows` /
`record_statements`:

- `fuse_context_claim_rows(canonical_id, members) -> list[ContextClaimRecord]` — per member, read its
  stamped `Provenance` (dataset = `source_id`, retrieved_at) and its `get_anchors(member)` (single-valued
  per field for a source member), emitting one row per `(key, value)` with `method="connector:map"`. A
  member with **no** stamped provenance / no `retrieved_at` has its anchor **skipped and logged** (never
  written naked). Consumed by both the persist path and the P-CTX-1 oracle.
- `record_context_claims(session, canonical_id, members) -> None` — `session.add` each row; caller
  commits.

Two additive call sites, each co-committing atomically in the existing transaction:

- **Pipeline promote block** (`pipeline.py`, alongside `record_statements` at `:483`, after the
  `validate_or_raise` quarantine guard): `record_context_claims(session, cluster.canonical_id, [by_id[m]
  for m in cluster.member_ids if m in by_id])` — for **every promoted cluster** (singleton + merge; a
  singleton geonames/opencorporates entity carries an anchor). A parked (block-mode) cluster takes the
  existing `continue` and writes **nothing** (parked-writes-nothing preserved).
- **`signoff.approve()`** (before `session.commit()`): `record_context_claims(session, canonical_id,
  [make_entity(r.raw_entity) for r in member_rows])`. This is **additive evidence banking only** — P1
  does **not** add statement/decision rows to the sign-off lane (that is Gate P3) and does **not** change
  the approve/reject decision or the graph write. It exists so that (a) the anchor writer is wired at
  both promote points from day one (a capture hooked only at the pipeline point structurally misses
  sign-off-promoted entities, consult §6b), and (b) P3 only has to add the statement/decision routing
  beside an already-present anchor capture. In P1 a sign-off-approved survivor has no statement rows
  anyway (the P3 gap), so its context claims are correct-but-dormant until P3.

### 4. The enricher interface (specified, NOT wired)

An enricher, when a later gate wires it at the promote point, MUST be **log-first**: `enrich → record
the enricher's anchors as `context_claim` rows (`method="enricher:<name>@<version>"`,
`retrieved_at=<enrichment time>`) → write_entities`. P1 provides the writer + the `method` contract but
**wires no production enricher** (`runner/driver.py` stays out of scope; the pipeline's `enrich`
handling is unchanged). Note P1's pipeline capture reads **member** anchors (`by_id[m]`), not the
post-enrich `entity`, so the enricher-wiring gate must additionally capture the post-enrich entity's
anchors — additive future work the P1 writer already supports. Re-running an enricher inside the fold is
**rejected** (non-deterministic rebuild, ADR 0095's one named risk).

### 5. Divergence guard: delete the dead `wm_anchor_` branch (`divergence.py:85`)

Replace `prop.startswith("wm_anchor_")` in `_excluded` with an exclusion of the **bare**
`CANONICAL_ID_FIELDS` keys (`wikidata_id`, `geonames_id`, `lei`, `opencorporates_id`), imported from
`ontology.anchors` (single source of truth; `anchors.py` imports no Neo4j / `worldmonitor.db` module —
its `ontology.ftm` import does transitively load SQLAlchemy via `followthemoney` as a pure library
import needing no live connection — so `divergence.py`'s Docker-free / no-live-DB property is preserved). **The bare anchor keys stay guard-excluded**
under the pick-semantics decision (§Sub-fork A): the fold's whole-history omit-on-conflict is a
deterministic PICK over claims that can legitimately differ from the live node's last-write-wins under
cross-batch anchor drift — exactly the caption case (ADR 0102 D6-iii). The **equivalence signature**
(IT-PROJ / P-FOLD) is a *different* instrument and DOES compare anchors (it runs in the single-batch
null-divergence regime where no conflict/drift exists); it must keep comparing them.

## Sub-forks, classified by reversibility (ADR 0097 discipline)

- **A — anchor conflict / comparison semantics.** DECIDED: **deterministic omit-on-conflict** fold
  projection (mirror `get_anchors`) + **bare-key guard exclusion** (pick-semantics arm) + the
  **equivalence suites compare anchors**. **Reversible.** Reversal cost: none (a projection + comparison
  rule). **Revisit trigger:** multi-valued anchors are ever wanted, or a real incident where a
  mis-anchored live node would have been the *only* rot signal ⇒ switch to **set-valued** projection and
  add the bare keys to the divergence comparison (the other coherent arm).
- **B — watermark home.** DECIDED: additive `ProjectionCheckpoint.last_context_claim_seq` column.
  **Reversible.** Reversal cost: drop one nullable-default column. **Revisit trigger:** a second lane
  wants a differently-shaped checkpoint.
- **E — sign-off capture point.** DECIDED: hook `approve()` additively for context claims only (not
  statement/decision — Gate P3). **Reversible.** Reversal cost: remove the call. **Revisit trigger:**
  P3 lands (the call co-exists; P3 adds statement/decision beside it).
- **F — reinstatement mechanism.** DECIDED: fold sets the context union via a new additive
  `anchors.set_anchor_claims`; `write_entities`/`get_anchors` project + omit **unchanged**.
  **Reversible.**
- **G — incremental touched-set.** DECIDED: union the statement AND context-claim deltas; re-read each
  touched survivor's full context history. This is a **correctness requirement** (a lagging incremental
  fold would drop a context-only delta's anchor); "reversal" = drop the lane.
- **method value.** DECIDED: `"connector:map"` (map-time) / `"enricher:<name>@<version>"` (interface).
  **Reversible** (a string). **Revisit trigger:** the connector id/version is wanted in the method tag.
- **unstamped-anchored member.** DECIDED: skip-and-log (NOT-NULL provenance; no invented data).
  **Reversible.** **Revisit trigger:** the skip ever fires in production (a connector bug) ⇒ dead-letter
  it instead of logging.
- **zero-property promoted entity (§7-3).** DECIDED: **NOT a P1 rider** — deferred to a post-P1
  write-path-integrity slice (`81_PRECUTOVER_GATE_SEQUENCE.md`). Justification: it is a different
  evidence class (entity *existence*, not an anchor), its mechanism (an existence claim or a
  promote-time reject) touches the promote *decision* (blast-radius into person-affecting territory), and
  it is re-mappable from the landing zone (not on the lock-in-critical edge).

## Reversibility

**Reversible** (additive; the fold stays dormant/isolated; the live graph write is unchanged). Reversal
cost: drop the `context_claim` table + the `last_context_claim_seq` column (`downgrade()`), delete the
context writers, revert the ~2-line `pipeline.py`/`signoff.py` hooks and the projector read-side, revert
the `divergence.py` line and the `set_anchor_claims` helper. No live graph state migrates; no behaviour
unwinds (the projection never depended on the new lane). **Revisit trigger:** anchor volume or a
context-shaped payload that does not fit the `key·value` lane (would force a JSONB column, not a
redesign); or the Sub-fork A set-valued switch.

## Consequences

- **Anchors become first-class evidence in the SoR spine**, log-first, provenance-stamped
  (`method`/`retrieved_at` NOT NULL) — closing the provenance-naked-anchor gap and the lock-in-critical
  edge before any enricher is wired.
- **The fold reproduces anchors.** ADR 0100 D3's "Anchors — not reconstructed (E2)" note **retires**
  (the projector docstring changes with the code; the ADR-0100 markdown gets an erratum note in the
  Gate-P1 docs slice).
- **The divergence guard stops false-alarming on anchor properties.** The dead `wm_anchor_` exclusion is
  replaced by the real bare-key exclusion. (Scope honesty: a **zero-FtM-property anchored** entity — a
  live node with anchors but no statement rows — remains unexplained until its disposition lands (WPI-1,
  §7-3), and sign-off-promoted nodes remain unexplained until P3; "guard green on an anchored corpus" is
  claimed for statement-bearing, pipeline-promoted corpora.)
- **Additive, behaviour-preserving.** Neo4j stays the live SoR; every merge/guard/audit/write assertion
  holds unchanged. The new rows commit atomically with the existing spine writes and roll back together.
- **The P2 (erasure) scrub can reach anchor claims by dataset** (per-member `dataset` attribution), and
  the P3 (sign-off spine) gate finds the anchor capture already wired at `approve()`.

## Alternatives rejected

- **Pseudo-prop `wm_anchor_*` rows in `statement`.** Rejected (consult §4): FtM validation raises for a
  non-schema prop; anchors are deliberately not schema properties. The separate lane is FtM-pure.
- **Re-run the enricher at projection time.** Rejected: network-backed (SPARQL) ⇒ non-deterministic
  rebuild (ADR 0095's one named risk); couples DR to an external service.
- **Capture from the merged `cluster.entity`.** Rejected: loses per-member `dataset` attribution (P2
  needs it) and folds the conflict prematurely (the guard's `anchor_conflicts_across` view is per-member).
- **Set-valued anchor projection + guard comparison (Sub-fork A other arm).** Deferred, not rejected —
  recorded as the revisit path if multi-valued anchors or a mis-anchor incident appear.
- **A separate checkpoint mechanism for the lane.** Rejected: one additive column on the existing
  `ProjectionCheckpoint` is minimal and inherits the exact incremental machinery.

## ADR-index coupling

This ADR — together with the P2 (`0107`) and P3 (`0108`) PROPOSED drafts — is **committed by the P1-0
docs-only slice**, whose builder re-runs `uv run python scripts/gen_adr_index.py` so
`docs/decisions/README.md` gains all three rows (this one reads `PROPOSED | 2026-07-05 | false | false`).
Rationale (adversarial-verify finding): the index generator scans the **filesystem**, so leaving the
drafts as untracked working-tree files makes `--check` fail locally and would poison the P1 code PR's
regen with rows for uncommitted files (local-green / CI-red). Committing PROPOSED drafts is the repo's
normal pattern (0101/0102 precedent). The P1 **code** PR's only index action is the accept-time flip:
stamp the `human_cosign` dated line, PROPOSED → ACCEPTED, regen (the 0106 row's status changes; 0107/0108
rows are already present and unchanged). The accept flip **must not occur on a PENDING cosign line**.
