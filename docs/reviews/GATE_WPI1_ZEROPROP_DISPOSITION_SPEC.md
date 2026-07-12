# Gate WPI-1 — zero-prop-entity disposition (ADR 0112)

> Write-path-integrity slice 3 of 3 (F1 pre-cutover). Non-person-affecting, reversible, additive,
> migration-free. Consult item §7-3 (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md`). Owner-mapped in
> `docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md:128-132`. Fable-sharpened plan:
> `.claude/plans/merry-doodling-dolphin.md` §"Slice 3 — WPI-1".

## Why

The fold (`resolution/projector.py::reconstruct_entities`) materialises **one node per survivor group** and
groups **by statement rows only**. A promoted entity with **zero FtM properties** yields **zero statement
rows**, so the fold materialises no node. **Build discovery (2026-07-12):** the mechanism is sharper than the
plan assumed — `fuse_statement_entity` builds `StatementEntity.from_statements(dataset, [])` for a
propertyless member, which **RAISES `InvalidData: No valid schema for entity: None`** (a zero-prop entity has
no statements, not even an `id` pseudo-statement). So today a zero-prop entity is **crash-excluded**: the
pipeline **quarantines its whole batch** (the raise fires in `_merge_entities`), and sign-off's
`approve()`/`reject()` **raise after the Neo4j write commits** (half-committed, never converges). WPI-2 makes
the aliased-survivor residual **fail loud** at `full_rebuild`; WPI-1 makes the entity promote cleanly AND
gives it a foldable node so direct-vs-fold match and the interlock closes.

## Invariant

**`INV-ZEROPROP-DISPOSITION`** — a promoted entity with **zero FtM properties** (and the sub-case zero-prop
**and** zero-anchor) has a **decided, tested disposition at BOTH promote points** (pipeline `_resolve_batch`
+ sign-off `approve`/`reject`): it emits a foldable spine row so a `full_rebuild` reproduces its node
**byte-equivalently** to the direct write (`datasets` excluded, standing E4 carve-out). It is no longer a
silently un-captured class the fold drops.

**Distinct invariant carried in the same slice — `INV-SINGLE-WRITER`** (rider-3): `signoff.approve()/reject()`
holds the transaction-scoped SoR-spine advisory lock (ADR 0110) before its spine writes, so the sign-off lane
can no longer interleave its `seq`-assign/commit window with the resolve lane.

## Mechanism (ADR 0112 disposition (a) — existence-claim, migration-free)

### `src/worldmonitor/resolution/merge.py` — defensive fusion hardening (crash → `None`)

`fuse_statement_entity` must **skip any member whose `_member_statements` is empty** (never call
`StatementEntity.from_statements([])`) and **return `None` when every member is propertyless**. Both callers
already handle `None` (`_merge_entities` guards `if fused is not None: stamp_witness_map(...)`;
`fuse_statement_rows` returns `[]`). **Byte-behaviour-preserving for every currently-fusing input** (any
member with ≥1 property still contributes every statement; the fused statement SET is the same union) — it
only converts the zero-statement crash into the existing sentinel, letting the zero-prop entity promote
(pipeline no longer batch-quarantines; sign-off no longer half-commits). This is the ONE frozen-fusion edit
this gate makes; see the ADR §Decision (c) for why disposition (a) requires it and (c) cannot substitute.

### `src/worldmonitor/resolution/statements.py`

- **`WM_EXISTS = "wm:exists"`** — module constant, the reserved sentinel `prop`.
- **`fuse_statement_rows(cluster, by_id)`** — when the normal projection would return `[]` (every member is
  propertyless), emit **one existence-claim `StatementRecord` per member**:
  - `canonical_id = cluster.canonical_id`, `entity_id = member.id or cluster.canonical_id` (**rider-2**),
    `schema = member.schema.name`, `prop = WM_EXISTS`, `value = ""`.
  - `dataset = member Provenance.source_id` (**rider-1**, non-empty), plus `reliability`/`retrieved_at`/
    `raw_pointer`/`first_seen` off the member's `Provenance`.
  - `statement_id = sha256(f"{canonical_id}\x00{entity_id}\x00{WM_EXISTS}\x00{dataset}").hexdigest()` —
    deterministic so a re-observation dedups and each member's claim is distinct.
  - This is the **single authoritative projection** consumed by both the persist path *and* the P-STMT-1
    oracle, so persist and oracle never drift.
- **Rider-1 statement lane** — inside `fuse_statement_rows`, skip-and-log (mirror the context lane's
  no-provenance skip) any member whose `Provenance.source_id` is empty, for BOTH the normal rows and the
  existence-claim rows, rather than writing a `dataset = member.id or ""` (source-unreachable) row.
- **Rider-1 context lane** — `fuse_context_claim_rows`: extend the skip condition to also skip when
  `not prov.source_id`; simplify `dataset = prov.source_id or member.id or ""` → `dataset = prov.source_id`.

### `src/worldmonitor/resolution/projector.py`

- **`reconstruct_entities`** — skip `WM_EXISTS` in the per-row props/witness loop **exactly as** `prop == "id"`
  is skipped, so a sentinel-only group builds a **bare node** (empty props, empty witness map → no
  `prov_witnesses`) with representative-derived `prov_*`. Import `WM_EXISTS` from `statements.py`. No other
  fold change; `reconstruct_entities` construction otherwise byte-frozen.

### `src/worldmonitor/resolution/signoff.py` (rider-3)

- Import `acquire_spine_writer_lock` from `resolution/spine_lock.py`; call it in `approve()` and `reject()`
  after the idempotency early-returns (already-merged/-rejected) and **before** the spine writes. One import +
  one call per function. (The existence-claim disposition itself needs **no** signoff edit — it flows through
  the existing `record_statements` calls.)

## Acceptance criteria

1. **Primary INV test** — a zero-prop(-zero-anchor) entity promoted via the **pipeline** (singleton) and via
   **sign-off** (`approve`, merge) each leaves ≥1 `WM_EXISTS` spine row; neither promote point drops it.
2. **Byte-equivalence (IT-PROJ-2 style)** — a zero-prop singleton: direct `write_entities` vs
   `project(full_rebuild=True)` produce **byte-identical** `graph_signature` (excluding `datasets`). The bare
   node carries `id` + schema labels + `prov_*`, no `prov_witnesses`, no `wm_anchor_*`.
3. **WPI-2 interlock (integration)** — promote a zero-prop **merge** (aliased survivor); confirm
   `project(full_rebuild=True)` **no longer raises** `IncompleteAliasedSurvivorError` and materialises a bare
   node. `find_incomplete_aliased_survivors` counts the sentinel row as coverage with **no** change to
   `spine_integrity.py`.
4. **Rider-1 negative** — a member with empty `source_id` is skipped-and-logged in **both** lanes; no
   `dataset = member.id or ""` (source-unreachable) row is written.
5. **Rider-2** — the zero-prop(-zero-anchor) member's id is derivable from the log (present as an existence
   claim's `entity_id`), so P2's `decision.member_ids` redaction path can reach it.
6. **Rider-3 (INV-SINGLE-WRITER)** — `approve()`/`reject()` take the advisory lock before their spine writes
   (verified on Postgres; no-op on SQLite).

### Mandatory `@given` — `tests/property/test_prop_zeroprop_disposition.py`

Over synthetic zero-prop (and zero-prop-zero-anchor) promoted entities: dispositioned at **both** promote
points + the fold reproduces disposition (a); **rider-1 negative** (empty `source_id` → skipped, no
source-unreachable row); **rider-2** (erased zero-prop member id derivable). Heavy DB-backed examples:
`settings(deadline=None)`; wrap any per-example engine in `try/finally` + `dispose()`.

## FROZEN (byte-unchanged)

`graph/writer.py`, `provenance/model.py`, `resolution/canonical.py`, `db/models.py` (**no schema change**),
`db/migrations/**` (**no new migration**), `resolution/merge.py` clustering/merge OUTCOMES (only
`fuse_statement_entity`'s crash→`None` hardening is edited; every merge/witness-map outcome byte-identical),
`resolution/spine_integrity.py` (WPI-2, unchanged), `resolution/spine_lock.py` (WPI-3, unchanged), all
merge/park/ER/erasure **outcomes**, `graph/**` writer transform, `ontology/**`, `api/**`, `mcp/**`,
`settings.py`. `reconstruct_entities` construction is byte-frozen **except** the additive `WM_EXISTS` skip
(owned by ADR 0112). `pipeline.py`/`signoff.py` promote *logic* is unchanged except rider-3's lock calls in
`signoff.py`.

## Governance

`person_affecting: false` (captures existence of an already-promoted entity; erasure reaches more, never
less; reject-at-promote EXCLUDED) + reversible (drop the branch/guards/skip/lock) → **NO cosign, NO human
fork**. ADR 0112 written PROPOSED; flips PROPOSED→ACCEPTED + README index regen in the SAME PR at merge.
proceed-and-report. ONE PR (docs + test + src together). Branch `gate/wpi1-zeroprop-disposition`.
