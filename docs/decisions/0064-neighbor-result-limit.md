# 0064 — Result-count bound on `get_neighbors` (read-surface hardening)

- **Status:** ACCEPTED (2026-06-28)
- **Date:** 2026-06-28
- **Gate:** `gate/neighbor-result-limit` (off `master`). A focused hardening follow-up to Phase-2 Stage-2
  (the API/MCP read surface — ADR 0062 REST, ADR 0063 MCP).
- **human_fork:** false (reversible — a tunable cap + a defense-in-depth clamp; default + reversal-cost +
  revisit-trigger recorded below).

## Context

`graph/queries.py::get_neighbors` returns every entity within `hops` of a node. It has **no result-count
`LIMIT`**, and it does **not** clamp `hops` itself — `depth = max(1, int(hops))` — relying on each caller
to clamp first. Both read surfaces do clamp the hop count (`api/graph.py::read_neighbors` and
`mcp/server.py::tool_get_neighbors` both call `read_guards.clamp_hops`), so depth is bounded in practice.
But the **result count is unbounded**: for a high-degree real entity (a heavily-connected OFAC /
OpenSanctions node, an ISO-3166 country) `get_neighbors(hops=4)` returns its **entire** 4-hop neighbourhood
— an unbounded payload over the wire and an unbounded Neo4j expansion.

This was flagged twice: slice 2a's clamp-bounds checker recommended "mirror `find_paths`' internal clamp +
a `LIMIT` into `get_neighbors` before slice 2b calls it directly," and slice 2b's completeness critic
re-surfaced it as the one concrete real-data stressor — now reachable by an **agent caller** through the
MCP surface (Hermes). It is **inherited from 2a** (REST has the same exposure), so it was not a 2b
regression and did not block 2b; this gate closes it for **both** surfaces.

`find_paths` already models the fix: it self-clamps depth to `read_guards.HOP_CAP` and carries a result
`LIMIT` (`_PATH_RESULT_LIMIT = 50`).

## Decision

Harden `get_neighbors` to the same shape as `find_paths`, and centralize the result caps in `read_guards`
(the single source of truth for read-access guards, established in ADR 0063):

1. **Internal hop clamp (defense-in-depth).** `get_neighbors` clamps its own depth:
   `depth = max(1, min(int(hops), read_guards.HOP_CAP))` — so a *direct* caller (not just the two surfaces)
   can never request unbounded traversal depth. Behaviour-preserving for existing callers (both already
   pass a clamped `hops ≤ HOP_CAP`).
2. **Result-count `LIMIT`.** The `get_neighbors` query gains `LIMIT {read_guards.NEIGHBOR_RESULT_LIMIT}`
   (read fully-qualified at call time, so a test can lower it). Default **`NEIGHBOR_RESULT_LIMIT = 500`** —
   generous enough not to truncate ordinary queries, bounded enough to cap the pathological hub case.
3. **Centralize the result caps in `read_guards`.** Add `NEIGHBOR_RESULT_LIMIT` and move the existing
   `_PATH_RESULT_LIMIT` (50) into `read_guards` as `PATH_RESULT_LIMIT`; `find_paths` repoints to it. After
   this, every read-access cap (`HOP_CAP`, `NEIGHBOR_RESULT_LIMIT`, `PATH_RESULT_LIMIT`) lives in one place —
   consistent with the `HOP_CAP` centralization, no orphaned literal.

The `LIMIT` has **no `ORDER BY`**: it returns an arbitrary bounded subset. For a v1 *safety* bound that is
acceptable (the goal is to cap payload + expansion, not to guarantee which neighbours); deterministic
ordering / pagination is a noted future enhancement, not v1 scope.

## Alternatives considered
- **Leave it (relying on callers to clamp depth).** Depth is bounded, but the *count* is not — a single
  hub node still returns thousands of rows to an agent caller. Rejected: the agent surface (2b) makes this
  exploitable; a finite cap removes the unbounded payload/expansion.
- **A settings-configurable limit (`graph_neighbor_limit`).** More flexible but more surface for a v1
  safety bound; a constant is simpler and reversible to a setting later. Deferred behind the revisit trigger.
- **`ORDER BY ... LIMIT` for deterministic truncation.** Adds a sort cost on large sets for a marginal v1
  benefit; the safety goal is met by `LIMIT` alone. Deferred.
- **Keep `_PATH_RESULT_LIMIT` in `queries.py`, add the neighbour cap there too.** Leaves the caps split
  across modules; centralizing in `read_guards` matches the ADR-0063 single-source-of-truth principle.

## Consequences
- Both read surfaces (REST + MCP) now return a **bounded** neighbour set; no unbounded payload or Neo4j
  expansion from a single call. `get_neighbors` is self-defending (clamp + `LIMIT`), parity with `find_paths`.
- A genuinely high-degree node returns at most `NEIGHBOR_RESULT_LIMIT` neighbours (an arbitrary subset).
  Callers needing the full set will need pagination (future).
- **Behaviour-preserving** for every in-repo caller and every existing test (fixtures are far below the cap;
  existing `hops` are ≤ `HOP_CAP`). **Not person-affecting. No migration. No new datastore. Single-tenant.**

## Reversibility
Reversible. Reversal cost: low — raise/lower `NEIGHBOR_RESULT_LIMIT`, or drop the `LIMIT` / internal clamp.
Revisit triggers: legitimate queries truncate at 500 → raise the cap or add `ORDER BY` + pagination; a need
to tune per-deployment → promote the constant to a setting.

## Invariant gate note
A read-surface bound — not an ER/canonical-id/merge-guard/provenance invariant, so no `@given` is mandatory.
Failing-test-first: (a) `get_neighbors` query carries `LIMIT` sourced from `read_guards.NEIGHBOR_RESULT_LIMIT`
(RED today — no `LIMIT`); (b) `get_neighbors(hops=99)` clamps its own depth to `HOP_CAP` (RED today — depth=99);
(c) end-to-end truncation: with `NEIGHBOR_RESULT_LIMIT` monkeypatched low over a testcontainer, a node with
more neighbours than the cap returns exactly the cap. 2a + 2b suites stay green.
