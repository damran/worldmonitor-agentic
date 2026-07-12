# 0112 — Zero-prop-entity disposition (WPI-1): a promoted propertyless entity gets a reconstructable node

- **Status:** ACCEPTED (2026-07-12)
- **Date:** 2026-07-12
- **human_fork:** false — a reversible, additive write-path-integrity disposition (an existence-claim
  `StatementRecord` for a class that today logs nothing + two source-reachability guards + adopting an
  existing spine lock). Revert = drop the existence-claim branch, the guards, and the projector sentinel
  skip. **No schema change, no migration** (the sentinel uses the existing `prop`/`value` columns).
- **person_affecting:** false — the disposition changes nothing about *what* merges, parks, resolves, or is
  erased; it captures the *existence* of an already-promoted entity into the log so a rebuild reproduces the
  node the direct write already made. It makes erasure reach *more* (rider-2: the zero-prop member id becomes
  log-derivable) and never less. The person-affecting arm — *reject-at-promote* — is **excluded**, not
  chosen. Reversible + non-person-affecting → proceed-and-report (no cosign).

## Context

The fold (`resolution/projector.py::reconstruct_entities`) materialises **one node per survivor group**, and
it groups **by statement rows only**. A promoted entity with **zero FtM properties** produces **zero
statement rows**, so the fold materialises **no node** for it.

**Discovery during build (2026-07-12) — the mechanism is sharper than the plan assumed.** The plan's root
cause said "`fuse_statement_rows` returns `[]` for a zero-prop entity (its only non-`id` prop is skipped)."
In fact `fuse_statement_entity` (`merge.py`) builds each member's `StatementEntity` via
`StatementEntity.from_statements(dataset, statements)`, and `_member_statements` iterates `member.properties`
— which is **empty** for a zero-prop entity, yielding **zero** statements (not even an `id` pseudo-statement,
which FtM synthesises only when ≥1 real statement exists). `StatementEntity.from_statements(dataset, [])`
therefore **raises `InvalidData: No valid schema for entity: None`**. Consequences of that raise today:

> - **Pipeline:** the raise fires inside `_merge_entities` (which fuses to derive the witness map), so
>   `cluster_and_merge` throws and `_resolve_batch` **quarantines the whole batch** (`resolve-batch` stage) —
>   one zero-prop entity dead-letters *all* its batch-mates and none of them promote.
> - **Sign-off:** `approve()`/`reject()` fuse at their `record_statements` call **after** the Neo4j write
>   commits, so the raise leaves the graph written but Postgres rolled back (the B-1 cross-store window), and
>   every re-run re-raises — it never converges.

So a zero-prop entity is not "promoted then silently dropped at fold" — it is **crash-excluded** at promote
(pipeline) or **crash-half-committed** (sign-off). Either way it never becomes a durable, reconstructable
node, and the batch-poisoning + non-convergent-sign-off behaviours are live defects. The fix must (i) let the
zero-prop entity promote without crashing, and (ii) give it a foldable spine row so the rebuild reproduces
the node the direct write already makes.

This is an accreted, previously-uncaptured class:
- **ADR 0106 Sub-fork H** — a context-only (zero-prop-**with**-anchor) survivor materialises no node from
  the fold (it groups by statement rows; anchors are applied *inside* that per-group loop).
- **ADR 0108 edge (d)** — the sign-off lane inherits the same gap at its promote point.
- **ADR 0107** — a zero-prop-**zero**-anchor member whose id lands in `decision.member_ids` after erasure.
- **WPI-2 / ADR 0111** — makes this residual **fail loud** at `full_rebuild`
  (`IncompleteAliasedSurvivorError`) for any **aliased** (merge) survivor, precisely so it is not a silent
  corruption. WPI-1 gives the survivor a node so the rebuild passes. The two slices interlock exactly there.

`INV-ZEROPROP-DISPOSITION`: a promoted entity with zero FtM properties (and the sub-case zero-prop **and**
zero-anchor) has a **decided, tested disposition at BOTH promote points** (pipeline `_resolve_batch` +
sign-off `approve`/`reject`) — no longer a silently un-captured class the fold drops.

## Decision

**Disposition (a) — existence-claim `StatementRecord`. CHOSEN. Migration-free.**

**Two coordinated changes** deliver the disposition:

1. **Harden `fuse_statement_entity` (`merge.py`) — crash → the existing `None` sentinel.** Skip any member
   whose `_member_statements` is empty (never call `from_statements([])`); return `None` when *all* members
   are propertyless. Both existing callers already handle `None`: `_merge_entities` guards
   `if fused is not None: stamp_witness_map(...)` (a zero-prop entity has no witnesses anyway), and
   `fuse_statement_rows` returns `[]` on `None`. This is **byte-behaviour-preserving for every currently-
   fusing input** (any entity with ≥1 property still contributes every statement — the fused statement SET
   is the same union regardless of the skip/merge order) — it only converts the zero-statement **crash** into
   the sentinel the codebase already expects. It lets the zero-prop entity promote (pipeline no longer
   batch-quarantines; sign-off no longer half-commits).
2. **Emit the existence claim in `fuse_statement_rows`.** When the projection is empty (`fuse_statement_entity`
   returned `None`, i.e. every member is propertyless), emit **one existence-claim `StatementRecord` per
   member** carrying a **reserved sentinel prop** — the module constant `WM_EXISTS = "wm:exists"` — with an
   **empty `value`**, and the full G1 quad off that member's `Provenance`:

| field | value |
|---|---|
| `canonical_id` | `cluster.canonical_id` (the survivor id) |
| `entity_id` | `member.id or cluster.canonical_id` (**rider-2**: the zero-prop member id, log-derivable) |
| `schema` | `member.schema.name` |
| `prop` | `WM_EXISTS` |
| `value` | `""` |
| `dataset` | `member Provenance.source_id` (**rider-1**: non-empty, source-reachable) |
| `reliability` / `retrieved_at` / `raw_pointer` / `first_seen` | off the member's `Provenance` |
| `statement_id` | deterministic `sha256(canonical_id ␀ entity_id ␀ WM_EXISTS ␀ dataset)` (idempotent dedup) |

`reconstruct_entities` **skips `WM_EXISTS` for FtM-property assignment and for the witness map exactly as it
already skips `prop == "id"`**, but the survivor still materialises a **bare node** because the group now has
`≥1` row. A bare zero-prop node carries only `id` + schema labels + `prov_*` (+ `datasets`, excluded) — no
`prov_witnesses` (the witness map is empty, so `witness_node_properties` writes nothing, matching the direct
write's `stamp_witness_map({})` no-op).

Because both promote points call `record_statements` → `fuse_statement_rows`, the disposition is emitted at
**both** points from **one** change to `fuse_statement_rows` (the single authoritative projection consumed by
the persist path *and* the P-STMT-1 property oracle) — no divergent copy in `pipeline.py`/`signoff.py`.

**Acceptance = fold-vs-direct BYTE-EQUIVALENCE for a zero-prop entity** (IT-PROJ-2 style; `datasets` excluded
per the standing E4 carve-out, ADR 0100). For a zero-prop **singleton** and a single-source zero-prop
**merge** (E3-null, exactly the regime IT-PROJ-2 tests), the fold reproduces the direct node byte-for-byte:
one row → representative provenance = the member's provenance = the direct node's `prov_*`.

**Fork — options considered:**

- **(a) existence-claim sentinel `StatementRecord`. ← CHOSEN.** Migration-free, byte-equivalent,
  producer-agnostic (both promote points inherit it via `fuse_statement_rows`), forward-compatible with
  WPI-2's statement-lane-only coverage (a sentinel row *is* a statement row → the aliased survivor becomes
  covered and materialised, with **no** change to `spine_integrity.py`).
- **(b) fold-materialise a context-only survivor as a bare node.** Sub-case-only (covers zero-prop-**with**
  anchor, not zero-prop-**zero**-anchor); would require WPI-2's check to count context rows (re-touching a
  just-shipped module) and a deeper `reconstruct_entities` change. `(a)` subsumes it. Not chosen.
- **(c) documented, guard-excluded, re-mappable-from-landing exclusion** (acceptable per ADR 0106
  Sub-fork H). The plan pre-defined `(c)` as the fallback *iff* byte-equivalence would force editing a
  FROZEN/load-bearing path (`graph/writer.py`, the provenance model, or the merge fusion
  `fuse_statement_entity`). **The build discovery flips this analysis, and `(a)` is still chosen — here is
  the honest reasoning.** The disposition *does* require a minimal edit to `fuse_statement_entity` (the
  crash→`None` hardening above), which the plan named as a `(c)` trigger. But `(c)` **cannot satisfy this
  gate's own mandatory WPI-2 interlock requirement:** an *excluded* zero-prop **merge** survivor still has a
  `canonical_id_ledger` alias written (the merge promoted) but **no** statement row — exactly the
  aliased-survivor-without-content shape WPI-2 / ADR 0111 makes `full_rebuild` **raise** on. So `(c)` leaves a
  zero-prop merge failing loud at every rebuild, violating the plan's explicit "promote a zero-prop merge →
  `full_rebuild` no longer raises + produces a bare node" acceptance. `(a)` is therefore the only disposition
  that meets the requirement, and the `fuse_statement_entity` edit it needs is **defensive and byte-behaviour-
  preserving** (crash→existing sentinel, no change for any fusing input) rather than a logic change — so the
  spirit of the frozen-path guard (don't perturb a load-bearing path's real behaviour) is honoured. This is a
  reversible, non-person-affecting deviation from the plan's pre-decided frozen list, taken by the main loop
  and disclosed here (proceed-and-report).
- **(d) reject at promote** (refuse to promote a zero-prop entity). **EXCLUDED** — person-affecting (it
  changes *what is promoted*, dropping an entity that may reference a real person), needs cosign, and is not
  offered.

### Rider 1 — non-empty `source_id` guard (both lanes)

A statement/context-claim row whose `dataset` is a `member.id`-keyed fallback or empty is
**source-unreachable**: P2's erasure scrub reaches rows by `dataset == <erased source_id>`, and a
member.id-keyed dataset matches no real source. Tighten **both** projections in `statements.py` to write only
source-reachable claims:

- `fuse_statement_rows` (statement lane) and the existence-claim writer — require the contributing member's
  `Provenance.source_id` to be non-empty; on empty, **skip-and-log** (mirror the existing context-lane
  no-provenance skip) rather than write a row with `dataset = member.id or ""`. Because `_member_source`
  falls back to `member.id`/`""` only when `source_id` is empty, guarding on `source_id` makes every written
  `statement.dataset` equal a real `source_id`.
- `fuse_context_claim_rows` (context lane, `dataset = prov.source_id or member.id or ""`) — extend the skip
  condition to also skip when `not prov.source_id`, and simplify `dataset` to `prov.source_id`.

This is defence-in-depth: in production an unstamped member never reaches the writers (`write_entities` fails
closed on no provenance, ADR 0060), so no real corpus is affected — it only closes the source-unreachable
write path so **neither** lane can create a claim P2 cannot scrub.

### Rider 2 — erased-member-id derivability

Disposition (a) puts the zero-prop(-zero-anchor) member's id in the existence-claim row's `entity_id`, so the
id is **derivable from the log** and P2's `decision.member_ids` redaction path can reach it. For a zero-prop
**singleton** (which writes no `decision` row) this is the *only* place the member id enters the spine — so
without the existence claim it would be un-erasable-from-the-log. A test proves the id is derivable.

### Rider 3 — adopt the single-writer spine lock in sign-off (INV-SINGLE-WRITER)

WPI-3 / ADR 0110 disclosed that `signoff.approve()/reject()` is a **second, unguarded** SoR-spine writer.
Since this slice already edits `signoff.py`, adopt the lock: `acquire_spine_writer_lock(session)` at the top
of each promote transaction (after the idempotency early-returns, before the writes — one import + one call
each; sign-off is a single transaction, not batched). This closes the sign-off-vs-resolve `seq`-gap. **Distinct
invariant (INV-SINGLE-WRITER)**, verified separately from the zero-prop disposition; a no-op on SQLite and
xact-scoped (auto-releases at commit) on Postgres, exactly as `pipeline.py` uses it.

## Consequences

- A zero-prop promoted entity now leaves a spine row, so a `full_rebuild` reproduces it as a bare node
  (byte-equivalent to the direct write on the E3-null corpus) instead of dropping it. WPI-2's
  `IncompleteAliasedSurvivorError` no longer fires on a zero-prop **merge** survivor — the interlock closes.
- Erasure reaches strictly *more*: the zero-prop member id is now log-derivable (rider-2), and neither lane
  writes a source-unreachable claim (rider-1).
- `signoff.approve()/reject()` is now single-writer-guarded (rider-3), closing the last unguarded spine writer.
- **FROZEN / byte-unchanged:** `graph/writer.py`, the provenance model, `canonical.py`, `db/models.py` (no
  schema change), all migrations, every merge/park/ER/erasure *outcome*. `merge.py` is edited **only** for
  the defensive `fuse_statement_entity` crash→`None` hardening (no behaviour change for any fusing input; the
  witness map and every merge outcome are byte-identical). The only fold edit is the additive `WM_EXISTS` skip
  in `reconstruct_entities` (owned by this ADR).

## Reversibility

Fully reversible: drop the existence-claim branch in `fuse_statement_rows`, the `fuse_statement_entity`
hardening (reverting to the crash), the rider-1 guards, the `reconstruct_entities` sentinel skip, the rider-3
lock calls, and the tests. No data-shape lock-in — a `wm:exists` row is inert to every consumer except the
fold's group-membership count, and a superseded zero-prop entity's sentinel becomes a harmless skipped row
once a real survivor absorbs it. (Reverting the `fuse_statement_entity` hardening alone would restore the
zero-prop batch-poisoning / non-convergent-sign-off defects, so it should be reverted only together with the
disposition.)
**Revisit trigger:** if a genuinely multi-source zero-prop **merge** becomes reachable (E3 non-null), the
representative-provenance byte-equivalence caveat that already applies to every multi-source merge applies
here too — at that point extend the acceptance corpus to the multi-source case (the same open item IT-PROJ-2
carries). Second trigger: if the `wm:exists` sentinel ever needs to be queryable/filterable, promote it from a
reserved `prop` string to a modelled column (a migration at that point, not now).
