# 0036 — Deterministic canonical id + cross-store commit ordering (audit B-1)

- **Status:** Part 1 **accepted** (implemented); Part 2 **accepted — Option A** (implemented 2026-06-23)
- **Date:** 2026-06-23
- **Addresses:** `docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md` finding **B-1**
- **Touches:** `resolution/merge.py` (canonical-id minting, Part 1); `resolution/signoff.py` + `review.py` (idempotent/recoverable sign-off, Part 2). Builds on [0026](0026-batch-first-resolution.md) (batch-first), [0028](0028-per-batch-resolver-isolation.md) (ephemeral resolver / G4), [0031](0031-return-to-block-signoff.md) (sign-off).
- **Relates to (does NOT build):** the deferred H1/H2 cross-store dual-write surface.

## Context

Audit finding **B-1**: a real merge minted a **random** canonical id and the graph was written
**before** the Postgres commit, so a crash in that window silently corrupted the resolved graph.

- The id came from nomenklatura: `merge.py` `resolver.get_canonical(entity_id)` → `Identifier.make()`
  → a fresh `NK-<shortuuid>` every invocation, and the per-batch resolver is ephemeral (0028), so the
  chosen id was never persisted.
- Ordering: `pipeline.py` `write_entities(...)` commits Neo4j **inside** `_resolve_batch`, while the
  Postgres commit (queue status + `merge_audit`) runs **after** the batch returns. Sign-off
  (`signoff.approve`/`reject`) has the same write-then-commit ordering.

So a crash after the graph write but before the Postgres commit left the queue rows `pending` and
no audit row, and the **next run re-resolved the same rows and minted a *different* id** → a duplicate
canonical node the `MERGE` (keyed on `id`) could not collapse, plus an orphan with no audit. The graph
is the product; this is silent corruption with no trail.

B-1 has two separable parts, handled separately here.

## Part 1 — Deterministic canonical id (ACCEPTED, implemented)

**Decision.** A **merged** cluster's canonical id is content-addressed: `wmc-` + the first 40 hex chars
of `sha256("\x00".join(sorted(member_ids)))` (`merge.py:_canonical_id`). nomenklatura still computes the
**clustering** (transitive positive judgements); only the *final* id is derived from membership instead
of a random mint. A **singleton keeps its own id** (so its node id and inbound edges are unchanged).

**Why this fixes the corruption.** The id is now a pure function of the cluster's membership, and the
member ids are the stable `er_queue` row ids. A crash+retry re-loads the same `pending` rows, re-clusters
the same membership, and re-derives the **same** id — so the graph `MERGE` converges on the one node
instead of creating a second. The duplicate/orphan failure mode is eliminated.

**Properties verified:**
- *Idempotent under retry* — same membership → same id (`tests/unit/test_resolution_canonical_id.py`).
- *No false collisions* — distinct member sets → distinct ids (SHA-256; an accidental collision between
  genuinely-distinct clusters is infeasible). Order-independent (members sorted before hashing).
- *Referent-rewriting intact* — the canonical id is computed before `build_referent_map`, and the merged
  entity is written under the same id, so every member edge rewrites onto the node that is actually
  written (unit test asserts `merged.entity.id == canonical_id` and the referent map agrees; the
  integration test asserts both `OWNS` edges land on the canonical node after recovery).
- *G4 preserved* — the id is intentionally **not** globally unique. Every node and row is keyed by
  `(tenant_id, id)` (the writer's composite MERGE key; all Postgres reads filter `tenant_id`), so an
  identical member set in two tenants still yields two distinct nodes. (The existing
  `test_resolver_is_isolated_per_batch` still passes: it compares *different* member sets, which get
  different ids.)
- *Sign-off inherits it for free* — a parked cluster's id (now deterministic) is stored in `merge_audit`;
  `approve` re-merges under that same id, so promotion is idempotent too.

**Verification (the audit's lesson — fixtures hid every prior bug).** The regression test
`tests/integration/test_b1_crash_recovery.py` **simulates the crash window**: it lets `write_entities`
commit the graph, then fails the next Postgres `session.commit()` (a crash-hook raise, the pattern from
the Gate A driver test), asserts the cross-store gap is real (graph node present, rows still `pending`,
no audit), then **restarts** by re-running `resolve_pending` and asserts the retry converges on the
**same** canonical node (no duplicate/orphan) with Postgres now consistent. Pre-fix (random id) this test
would see two canonical nodes and fail.

**Consequences.**
- ✅ The permanent duplicate/orphan corruption is eliminated; a crash-in-window is now self-healing for
  the pipeline (the still-`pending` rows are re-resolved automatically on the next pass).
- ✅ Canonical ids are stable across re-resolution of the same batch — a foundation Gate B (cross-batch
  dedup) can build on, though cross-batch stability across *re-ingests* (which mint fresh member ids)
  remains Gate B and is out of scope here.
- ⚠️ Id format changes from `NK-<shortuuid>` to `wmc-<hash>` for minted merges. This is a dev system with
  resettable data; no migration of existing ids is required. (A future refinement could prefer a shared
  anchor — e.g. a common Wikidata QID — as the canonical id when present; content-hash is the robust
  default and is what this ADR adopts.)

## Part 2 — Cross-store commit ordering (ACCEPTED — Option A, implemented)

Part 1 removes the permanent corruption but leaves a **transient** residual: between a crash and the
retry, the graph holds a canonical node whose `merge_audit`/status row is not yet committed (graph ahead
of audit). For the **pipeline** this self-heals automatically (rows stay `pending` → re-resolved). For
**sign-off** the recovery is *operator-driven* (the merge stays `pending_review` with no judgement until
someone re-runs `approve`/`reject`), and a re-run is not yet guarded for idempotency.

Three options (the fork the audit named):

| Option | What | Robustness | Work | H1/H2 overlap |
|---|---|---|---|---|
| **A — lean on Part 1's idempotency** | Keep the ordering. Pipeline self-heals via automatic retry of `pending` rows (now idempotent). | Closes the *corruption*; leaves a transient graph-ahead-of-audit window; sign-off recovery is manual. | Tiny (done, modulo a sign-off idempotency guard). | None. |
| **B — reorder + explicit reconcile** | Persist intent first (e.g. a `resolving` status / `canonical_id` + `graph_written` marker committed before the graph write), and add a startup/periodic reconcile that re-runs the (idempotent) graph write for any marked-but-unwritten row. | Bounded, explicit recovery not reliant on rows staying `pending`; handles sign-off symmetrically. | Medium — a schema column + a driver reconcile step. | Partial — introduces a reconcile loop akin to a lightweight outbox. |
| **C — transactional outbox / saga** | Write a graph-write outbox row in the *same* Postgres txn as status/audit; a worker drains it to Neo4j at-least-once with idempotency (Part 1). | Strongest; correct under HA/multi-writer. | Large — new table, worker, delivery semantics. | **This *is* the deferred H1/H2 dual-write-consistency surface.** |

**Decision: Option A** — lean on Part 1's idempotency, hardened with a sign-off idempotency guard +
surfacing. **Option C is explicitly DEFERRED** (it *is* the H1/H2 dual-write surface). Rationale: Part 1
already converts B-1 from permanent corruption into a self-healing transient for the pipeline (the
dominant path), so the general machinery of B/C is not justified by this gate; the only genuine residual
is the operator-driven **sign-off** path, and that is what Option A closes at the right size.

**What was built (`resolution/signoff.py`, `review.py`):**
- **Idempotent re-run.** `approve`/`reject` fetch the merge_audit regardless of decision and branch on a
  small state machine: a re-run from the *completed* state is a no-op (`SignOffResult.already_applied`),
  the opposite decision is refused. The graph write is already idempotent (Part 1: a merge re-MERGEs the
  same deterministic canonical id; a reject re-writes members under their own ids). Judgements insert
  `ON CONFLICT DO NOTHING` on `uq_resolver_judgement_pair`; the audit row is *mutated*, never duplicated;
  so a re-run produces **no duplicate canonical node, no orphan, no duplicate judgement/sign-off row.**
- **Orphan-canonical guard.** `reject` refuses if a canonical node already exists in the graph (the
  signature of a crashed *approve*) — completing a reject would strand that node, and there is no delete
  path (append-only). The operator is directed to complete the approve instead.
- **Surfacing.** `list_parked(session, tenant, neo4j)` annotates each parked merge with `graph_written`
  (a node for the canonical id *or* any member already exists). Since block mode never writes a parked
  cluster, a present node means a sign-off's graph write committed but its Postgres audit did not — the
  half-committed crash window. The `review` CLI flags it `[GRAPH-WRITTEN: … re-run … to recover]`.
- **No migration.** Reuses the existing `uq_resolver_judgement_pair` constraint and the audit state
  machine; no schema change (avoids the online-migration risk the audit flagged as M-5).

**Verification.** `tests/integration/test_b1_signoff_idempotency.py` simulates the crash window on the
sign-off path (graph write commits, the next Postgres commit raises) for **both** approve and reject,
asserts the state is surfaced (`graph_written`), then re-runs the SAME operation and asserts convergence
(one canonical node / two members, audit terminal, exactly one judgement + one sign-off row), and that a
further re-run is a clean no-op.

**Known limitation (out of scope — Gate B).** If the *same* canonical pair is parked twice
(a re-ingest mints new queue rows and re-parks before the first park is signed off), there are
two `pending_review` audit rows for one `canonical_id`. Sign-off flips only the most-recent
(`_require_audit` orders by `created_at, id` desc), leaving the older row surfacing as parked.
This is the deferred cross-batch / re-ingest surface (Gate B), not the crash window; it is
strictly safer than before (no `MultipleResultsFound`). Reconciling all rows for a
`(tenant, canonical)` is a Gate B follow-up.

**Relationship to H-1 (next gate).** Part 2 keeps judgement-write *semantics* unchanged (reject still
persists a negative judgement; the consumption path in `cluster_and_merge` is untouched). `ON CONFLICT DO
NOTHING` only dedups an identical pair; it does not alter which judgement wins. So the H-1 transitive-
bridge fix (teaching `cluster_and_merge` to respect a negative judgement transitively) is unaffected and
not pre-empted.

## Hard-stop note

This ADR fixes **B-1 only**. It does not touch Gate B/C/S4, does not start G3, and does not address
H-1/B-2/B-3. Option C (outbox) is identified as the deferred H1/H2 surface and is left unbuilt.
