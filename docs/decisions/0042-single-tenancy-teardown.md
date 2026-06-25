# ADR 0042 — Single-tenancy teardown: remove `tenant_id` (supersedes ADR 0017)

> Status: **PROPOSED** · 2026-06-25 · `human_fork=false` (user-authorized 2026-06-25, locked
> decision **D1: single-tenant**). Supersedes **ADR 0017**. Notes **0028 / 0029 / 0031**.
> Gate: `docs/reviews/GATE_0_SPEC.md` (Gate 0, a DELETION gate).

## Context

ADR 0017 introduced application-layer tenant isolation: `tenant_id` injected into every Neo4j node
and relationship, every MERGE/MATCH key rewritten to `{id, tenant_id}`, composite `(tenant_id, anchor)`
graph constraints, a `tenant_id` column on all 8 relational tables (2 of them in composite unique
constraints), and a `tenant_id` predicate on every read. Its rationale ("tenant-scoped from day one";
two tenants holding the same real-world entity get two isolated nodes) assumed a multi-tenant SaaS
trajectory.

The user has set **locked decision D1: WorldMonitor is single-tenant.** With exactly one tenant,
`tenant_id` is a constant threaded through ~110-120 call sites, an 8-table NOT NULL column, a Neo4j
property + composite constraint, two relational uniques, and a guard in the graph writer. It is dead
weight on every future change and — more importantly — a standing contradiction between the code
(which scopes everything by tenant) and the ground truth (which says there is one tenant). This ADR
removes it.

## Result-neutrality (the load-bearing argument)

Removing `tenant_id` changes **no result anywhere** in a single-tenant deployment:

- **Canonical ids are unaffected.** `resolution/merge.py::_canonical_id` hashes ONLY
  `sorted(member_ids)`; `tenant_id` is not an input. Dropping it re-derives the same ids (ADR 0036
  holds).
- **The two composite uniques are exactly as selective.** `uq_er_queue_dedup (tenant_id,
  source_record, entity_id)` -> `(source_record, entity_id)` and `uq_resolver_judgement_pair
  (tenant_id, left_id, right_id)` -> `(left_id, right_id)`: with one tenant the leading column is
  constant, so the `ON CONFLICT DO NOTHING` enqueue and the idempotent judgement insert behave
  identically.
- **The Neo4j MERGE key** `{id, tenant_id}` -> `{id}` is exactly as distinguishing when there is one
  tenant; ftmg is single-tenant by design, so this returns to its native node-key.
- **The per-batch ephemeral resolver is provably independent of the column** — proven by
  `tests/unit/test_resolution.py:73`, which exercises the isolation property passing zero `tenant_id`.

The deletion gate proves this empirically: the kept-behaviour suites (B-1 crash recovery, B-2 poison
isolation, B-6 incompatible-member + sign-off poison, referent rewriting, end-to-end resolve, edge
provenance, GDS, ingest, migrations) pass before AND after the teardown, with identical asserted
counts/ids/edges/provenance/flags — only their `tenant_id` call sites change.

## Decision

1. **Remove `tenant_id` everywhere it is live in `src/`** — the relational columns + indexes, the two
   composite uniques (redefined without `tenant_id`), the Neo4j property + composite constraint + the
   `tenant_id` index, the graph-writer tenant-scoping machinery (`_KEY_REWRITES`, `_tenantize_query`,
   `_tenantize`, the `tenant_id` stamp in `_inject_tenant`, the `write_entities` guard + signature),
   the `tenant_id` predicates in reads/resolution/sign-off/audit/runner/ingest, the per-tenant
   resolution loop in the driver, `Principal.tenant_id` + `ORG_ID_CLAIM`, `/me`'s `tenant_id` field,
   and the `review.py` CLI `--tenant` arg. (Full surface: GATE_0_SPEC §3.)

2. **DROP the column via a new Alembic migration `0004_drop_tenant_id`** — do NOT keep it dead. (See
   the explicit decision below.) Existing migrations 0001/0002/0003 are NOT edited; the delta layers
   in 0004, exactly as 0002 layered the runway delta on the 0001 baseline.

3. **KEEP the per-batch ephemeral resolver** (`merge.py::_ephemeral_resolver`, ADR 0028) verbatim in
   behaviour. Its G4 motivation is now historical, but its in-memory `sqlite://` + `StaticPool`
   per-call lifecycle remains required for B-1 crash recovery and ADR-0026 batch purity. It must NOT
   be made persistent or shared during this teardown.

4. **Amend the ground truth in the same gate** so no merged commit is self-contradictory: the three
   mirrors (`CLAUDE.md`/`AGENTS.md`/`.clinerules` line 22 + invariant list), the docs (GATE_LEDGER,
   60_API_AND_MCP, ARCHITECTURE_REVIEW, 10_ARCHITECTURE, 00_VISION_AND_SCOPE), and the 4 fleet agent
   specs that list "G4 tenant isolation" as a locked invariant.

5. **Install a durable drift guard** (`tests/test_contract_consistency.py`, in `quality`): mirror-sync
   (the three contract files byte-identical — non-vacuous, it fails today) + claim↔code (single-tenant
   contract ⇒ no live `tenant_id` in `src/`, bidirectional). This is the durable fix for "a future
   session reloads a contradictory ground truth".

### Sub-decision — DROP, not keep-dead

The 8 `tenant_id` columns are `NOT NULL`. Keeping them dead would STILL require rewriting every INSERT
site (you cannot insert without supplying a NOT NULL column), so the code churn is identical to a full
drop — AND it leaves a dead column + dead Neo4j property in the schema forever. The two composite
uniques and the Neo4j constraint must be redefined regardless. Therefore keep-dead is strictly worse:
same churn, plus permanent dead schema. The teardown is broad but mechanical (no disproportionate
rewrite cliff). **DROP it.**

## Alternatives considered

- **Keep `tenant_id` dead (NULLable, unused).** Rejected — strictly worse than DROP (above):
  identical code churn for the NOT NULL inserts, plus permanent dead schema and a permanently-failing
  claim↔code drift guard.
- **Keep `tenant_id` live as a forward-compat hedge for future multi-tenancy.** Rejected under D1 —
  it perpetuates the code-vs-ground-truth contradiction and taxes every future change. The
  multi-tenant path is not closed: like ADR 0017's own re-evaluation clause, a future managed-cloud
  tier can reintroduce isolation (RLS / Neo4j Enterprise multi-db) as its own gate, with the benefit
  of a clean single-tenant baseline to branch from.
- **Do it as one atomic gate (no slice split).** Defensible, but the functional teardown is large
  enough that isolating it from the prose/ADR churn materially helps adversarial review. We recommend
  the 2-slice split (functional first, contract+guard second) — see GATE_0_SPEC §7.

## Notes on adjacent ADRs (NOT superseded)

- **ADR 0028 (per-batch resolver isolation):** its G4 motivation is moot, but the ephemeral resolver
  is KEPT for B-1 crash recovery / ADR-0026 batch purity. Note added, not superseded.
- **ADR 0029 (ingest driver, Gate A):** per-tenant resolution routing (`_resolve_tenant`, the
  distinct-tenant select) is removed; the X2 (HA lease) and X3 (single-writer-per-tenant) forks are
  moot under single-tenant. Note added.
- **ADR 0031 (return-to-block sign-off):** judgement tenant-scoping is dropped; the approve/reject
  state machine is unchanged. Note added.

## Consequences

- ✅ Code and ground truth agree: single-tenant in both, enforced by the drift guard forever.
- ✅ ~110-120 fewer call sites carry a constant; the graph writer returns to ftmg's native node-key;
  the schema loses a column + 8 indexes + the composite leading columns.
- ✅ Result-neutral: proven empirically by the kept-behaviour suites passing pre + post.
- ⚠️ Re-introducing multi-tenancy later is a fresh gate (not a revert) — acceptable; it would want a
  proper RLS / Enterprise-multi-db design anyway, not the app-layer scoping ADR 0017 settled for.
- ⚠️ Migration-drift is the #1 build risk: `models.py` and `0004_drop_tenant_id` must produce an
  identical introspected schema or `test_migrations.py` (ADR 0030) turns CI red. Mitigated by editing
  them as one atomic change and running `alembic check` pre-push.
