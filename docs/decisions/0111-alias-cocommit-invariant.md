# 0111 — Alias⇔co-commit invariant (WPI-2): every supersession alias has a reconstructable survivor

- **Status:** PROPOSED (2026-07-12)
- **Date:** 2026-07-12
- **human_fork:** false — a reversible, additive write-path-integrity guard (one new pure module + one
  call in `project()`); revert = drop the call. No data-shape lock-in, no schema/migration change, no
  change to `reconstruct_entities` node/edge construction, no change to any producer's write logic.
- **person_affecting:** false — the check changes nothing about *what* merges, parks, resolves, or is
  erased; it only asserts, at rebuild, that the log the fold reads is *self-consistent* (no aliased
  survivor left without content to reconstruct). No individual-affecting outcome changes. Reversible +
  non-person-affecting → proceed-and-report (no cosign).

## Context

Gate 3a-ii-A surfaced a HIGH backlog integrity gap: the fold groups statement rows by
`survivor_of(canonical_id)` and materialises **one node per survivor group** (`projector.py:151-159`).
A survivor node exists **iff** at least one foldable row folds into it. Separately, a promoted **merge**
writes a supersession **alias** into `canonical_id_ledger` (`record_durable_id`) so `survivor_of` rewrites
every collapsed-member id and every inbound edge endpoint onto the surviving id. These two facts couple:

> If the ledger holds an alias `prior → survivor` but **no** foldable content row folds into `survivor`,
> a rebuild-from-log yields an **aliased survivor with an empty/incomplete node** — inbound edges rewrite
> onto a node that carries no properties, no provenance, no schema. Post-3b (rebuild is the routine path)
> that is a silent structural corruption of the resolved graph.

**Today the invariant holds at both alias producers** — this gate **locks it against regression**, it does
not fix a live break:
- `pipeline.py::_resolve_batch` — `record_durable_id` (`:483`, inside `if cluster.is_merge:`) is followed
  by `record_statements` (`:497`, called **always**) in the **same per-batch transaction** (ADR 0026 commit
  boundary). A promoted merge co-commits its alias and its statements or rolls both back.
- `signoff.py::approve` — `record_durable_id` (`:323`) is followed by `record_statements` (`:326`) in the
  **same sign-off transaction** (`session.commit()` at `:329`). Gate P3 / ADR 0108 established this
  co-commit; this gate asserts it.
- `signoff.py::reject` writes **no alias** (each member keeps its own id as its own canonical/survivor), so
  it cannot produce an aliased-survivor-without-content and is not in scope.

The gap the invariant guards is a **future regression** (a new producer, or a refactor that reorders the
two writes across a transaction boundary) and one **extant extreme edge**: a zero-prop-zero-anchor **merge**
survivor whose only non-`id` statement is skipped by `fuse_statement_rows` (`statements.py:74`) → zero
foldable rows → an aliased survivor with an empty node. That edge is closed by **WPI-1** (ADR 0112,
existence-claim disposition); this gate's fold-side check is exactly what makes that residual **fail loud**
at rebuild instead of corrupting the graph silently.

## Decision

Enforce **`INV-ALIAS-COCOMMIT`** with a **pure, fold-side completeness check** as the primary mechanism,
plus a producer-side **example test** as a regression lock. **No runtime guard is added at the producers.**

**Fork — options considered, `(a)` chosen:**

- **(a) Pure fold-side completeness function, called at rebuild. ← CHOSEN.** A new
  `resolution/spine_integrity.py` exposes `IncompleteAliasedSurvivorError(RuntimeError)` and a pure
  `find_incomplete_aliased_survivors(alias_map, statement_rows, context_claim_rows, *, survivor_of=None)
  -> set[str]` returning the set of **aliased final survivors** with **no** foldable statement- **or**
  context-claim row folding into them. `project()` calls it **only on `full_rebuild=True`** (where the
  loaded rows are the complete log) after the row loads and `build_survivor_of`, **before** `write_entities`,
  and **raises** `IncompleteAliasedSurvivorError` on a non-empty set. It is **additive validation only** —
  `reconstruct_entities`'s node/edge construction is byte-frozen, so every fold-vs-direct byte-equivalence
  invariant (3a-i / 3a-ii, IT-PROJ-2/4) is preserved.
- **(b) A call-time guard inside `record_durable_id`** asserting "statements already exist for this
  survivor." **Rejected — ordering trap.** In *both* producers the alias is written **before** the
  statements (`:483`→`:497`, `:323`→`:326`), so a call-time check would false-fire on every legitimate
  merge. Co-commit means *same transaction*, verifiable only at the transaction boundary or at the fold —
  hence `(a)`.
- **(c) A DB `CHECK`/trigger constraint.** Rejected — cross-row/cross-table integrity over the append-only
  log is not expressible as a simple constraint without coupling the two lanes' write order at the schema
  level; it would also fire mid-transaction before the co-committed statements land.

**Why fold-side is both correct and sufficient.** The **producer co-commit** (asserted by the example test)
is the *real-time* guarantee. The **fold-side check** is the *rebuild-time* backstop — and rebuild-from-log
is precisely the path Gate 3b makes routine, so the backstop fires exactly where the risk concentrates.

**Target set = final survivors, not raw alias values.** The aliased-survivor set is
`{survivor_of(a) for a in alias_map}` — every alias key resolved **transitively** to its final survivor.
Using `set(alias_map.values())` directly would be **wrong** for a chain `a → b → c`: rows for `b` fold into
`c` (`survivor_of(b) == c`), so requiring the intermediate `b` to carry its own foldable rows would
false-fire. `survivor_of` is the projector's existing transitive resolver.

**Context-claims count as content.** A zero-prop-**with-anchor** survivor has `context_claim` rows but no
statements and **is** reconstructable-in-principle; it must not trip the check. `covered` therefore unions
`survivor_of(row.canonical_id)` over **both** the statement lane and the context-claim lane.

**`full_rebuild`-gated raise.** On `full_rebuild=True`, `project()` reads the entire statement and
context-claim log (no watermark `WHERE`), so the loaded rows are complete and the check is exact. In
**incremental** mode the loaded rows are only the delta, so a completeness check over them would false-fire
on any aliased survivor not touched this delta — the raise is therefore **scoped to `full_rebuild`**.
Incremental integrity is upheld in real time by the producer co-commit; a per-fold incremental check (two
`DISTINCT` canonical-id queries) is the recorded upgrade path, deferred to avoid per-fold cost and
false-fire risk.

## Consequences

- A rebuild over a log that violates co-commit now **fails loud** (`IncompleteAliasedSurvivorError`, before
  any Neo4j write) instead of silently materialising an empty aliased-survivor node. The single-node
  production path is unchanged: both producers already co-commit, so a full rebuild over a healthy log
  raises nothing.
- No schema change, no migration. `reconstruct_entities`, all producer write logic (`pipeline.py`,
  `signoff.py`, `statements.py`, `canonical.py`), and every merge/erasure path are **byte-frozen**. The
  only `project()` change is one import + one guarded call.
- The check makes the WPI-1 residual (zero-prop-zero-anchor merge survivor) **fail loud** at rebuild until
  ADR 0112 lands the existence-claim disposition — the two slices interlock as designed.

## Reversibility

Fully reversible: delete `spine_integrity.py`, its one call site in `project()`, and the tests; the
projector returns to its prior behaviour.
**Revisit trigger:** the first time incremental-fold integrity must be enforced in real time (e.g. a
multi-writer / HA topology where a full rebuild is too infrequent to be a timely backstop) — at that point
promote the check to run every incremental fold via the two `DISTINCT` canonical-id queries named above.
