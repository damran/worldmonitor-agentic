# 0036 — Deterministic canonical id + cross-store commit ordering (audit B-1)

- **Status:** Part 1 **accepted** (implemented); Part 2 **proposed** (awaiting maintainer decision — not implemented)
- **Date:** 2026-06-23
- **Addresses:** `docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md` finding **B-1**
- **Touches:** `resolution/merge.py` (canonical-id minting). Builds on [0026](0026-batch-first-resolution.md) (batch-first), [0028](0028-per-batch-resolver-isolation.md) (ephemeral resolver / G4), [0031](0031-return-to-block-signoff.md) (sign-off).
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

## Part 2 — Cross-store commit ordering (PROPOSED — awaiting decision, NOT implemented)

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

**Recommendation: Option A now, hardened with a *minimal* slice of B for sign-off — and explicitly DEFER C.**
Rationale: Part 1 already converts B-1 from permanent corruption into a self-healing transient for the
pipeline (the dominant path), so the high-cost general machinery is not justified by this gate. The only
genuine residual is the sign-off path, where the in-scope, low-risk hardening is: (1) make
`approve`/`reject` **idempotent** (safe to re-run against an already-graph-written merge — Part 1 makes
the node write idempotent; add the same guard to the judgement/sign-off rows), and (2) surface a
"graph-written-but-audit-pending" parked merge so an operator is told to re-run. A **full outbox/saga
(Option C) is the deferred H1/H2 surface** — building it in this gate would expand scope into deferred
territory, which is a stop-and-flag condition, so it is explicitly **not** recommended here.

**This section is a proposal only.** No Part-2 code is written pending the maintainer's choice of
A / lite-B / C (or another).

## Hard-stop note

Per the gate scope: this ADR fixes **B-1 only**. It does not touch Gate B/C/S4, does not start G3, and
does not address H-1/B-2/B-3. Option C (outbox) is identified as the deferred H1/H2 surface and is left
unbuilt.
