# Phase 1 Audit — WorldMonitor

> Read-only audit of Phase 1 (PRs #9–#15) against the four CLAUDE.md questions.
> Scope: `src/worldmonitor/`, `tests/`, `docs/`. **No code was changed.**
> Date: 2026-06-21 · Branch: `claude/phase-1-audit-review-gzcii9`.

Phase 1 shipped: the L2 ontology contract (FtM + validation + anchors), the tenant-aware
FtM→Neo4j writer, the plugin framework v0 (connector ABC + registry), the OpenSanctions connector
→ landing zone → ER queue, entity-resolution v0 (Splink scoring + nomenklatura merge + catastrophic
guard), reference anchors (Wikidata + GeoNames enrichers), and graph queries + GDS degree centrality,
all gated by a full-pipeline acceptance test.

**Verdict in one line:** the invariants are *enforced by construction* and largely *proven by test*,
with **one genuine invariant violation (provenance is not written on edges)** and a cluster of
*enforced-but-untested* cases. The L2 ontology + plugin **interface** are clean for Phase 2; the
**pipeline implementations** (`run_ingest`, `resolve_pending`) bake in batch/bulk assumptions that the
first `StreamConnector` will break.

Severity legend: **`blocker`** (fix before building anything else on top) ·
**`pay-down-before-phase-N`** (safe for now, must be paid before phase N) · **`nice-to-have`**.

---

## Q1 — Does it honor the invariants?

| Invariant | Status | Enforced at | Proven by |
|---|---|---|---|
| `tenant_id` on every **node** | **PROVEN** | `graph/writer.py:44-59` (MERGE key tenant-scoped) + `graph/constraints.py:25-34` (composite `(tenant_id, anchor)` uniqueness) | `test_writer_stamps_tenant_id_on_nodes_and_edges` — `tests/integration/test_graph_writer.py:27-57` (asserts `n.tenant_id` on every node, lines 49-53) |
| `tenant_id` on every **edge** | **PROVEN** | `graph/writer.py:129-143` (Pass 2 tenant-scopes every relationship key) | same test, `tests/integration/test_graph_writer.py:55-57` (asserts `r.tenant_id` on every relationship) |
| Two tenants, **same canonical ID → separate nodes** | **ENFORCED, UNPROVEN** | `tenant_id` is in the MERGE node key (`writer.py:45-48`) **and** the uniqueness constraint is composite `(tenant_id, lei/qid/…)` (`constraints.py:29-30`), so `(A, X)` and `(B, X)` are distinct keys — correct *by construction* | **No test names this case.** Nearest is `tests/integration/test_graph_queries.py:71-72`, which only checks a *different* tenant sees `None` for a *different* entity — not that the *same* LEI/Q under two tenants yields two nodes |
| **Provenance on every node**, traceable to `s3://` | **PROVEN** | stamped into FtM context (`provenance/model.py:51-55`), projected to `prov_*` node props (`graph/writer.py:112-117` → `provenance/model.py:74-79`); the pointer is the real landing URI (`runner/ingest.py:54,57-62`) | `tests/integration/test_graph_queries.py:60-63` (asserts `prov_source_id` and `prov_source_record` on the node) |
| **Provenance on every edge** | **VIOLATED** | `graph/writer.py:142` — Pass 2 calls `_tenantize(batch, tenant_id)` **without** `node_props_by_id`, so relationships carry `tenant_id` but **no `prov_*`** | No test exists (none could pass) |
| Resolution is **central**; connectors emit candidates only | **PROVEN** | OpenSanctions `collect()` yields `RawRecord`, `map()` yields `FtmEntity` (`plugins/ftm_bulk.py:23-27`); `run_ingest` enqueues `ErQueueItem` to Postgres (`runner/ingest.py:63-74`), never Neo4j; only `resolve_pending` writes the graph (`resolution/pipeline.py:84-85`) | `test_collect_land_queue` — `tests/integration/test_opensanctions_ingest.py:23-62` |
| Catastrophic-merge guard — **negative test** | **PROVEN** | `resolution/merge.py` clustering | `test_clearly_different_records_do_not_merge` — `tests/unit/test_resolution.py:44-49` |
| Catastrophic-merge guard — **PEP/sanctioned → human review** | **PROVEN** | `needs_review` + `is_sensitive` (`resolution/review.py:25-50`); flagged clusters parked `pending_review`, never written (`resolution/pipeline.py:69-85`) | `test_sensitive_merge_goes_to_review` — `tests/unit/test_resolution.py:61-70`; integration `test_resolve_pending_pipeline` asserts the sanctioned person is **never written** to Neo4j (`tests/integration/test_resolution_pipeline.py:99-106`) |
| Catastrophic-merge guard — **size threshold (>10)** | **ENFORCED, UNTESTED** | `resolution/review.py:17,41-45` | no test forces an 11-member cluster |

### The two that matter most (ultrathink)

**1. Provenance on edges — a real invariant violation (`blocker`).**
CLAUDE.md: *"Provenance on every node **and edge** … Doubles as the GDPR/audit log."* The writer's
own docstring (`graph/writer.py:101-107`) only ever promises that *every node and edge carries
`tenant_id`* — it silently narrows the contract. In Pass 1 the node batches are stamped with anchors +
provenance via `node_props_by_id` (`writer.py:126`); in Pass 2 the relationship batches are stamped
with **only `tenant_id`** (`writer.py:142`). The consequence is that first-class **edge-schema
assertions** — `Ownership`, `Sanction`, `Directorship`, `Payment` — land in the graph with no
`source_id`, `retrieved_at`, `reliability`, or `s3://` pointer. An edge cannot be traced to its raw
record, so the GDPR/audit-log guarantee does not hold for relationships. This is the precise place
where "looks correct" (a passing tenant-stamps-nodes-and-edges test) diverges from "is correct"
(the test never checks edge provenance, and the invariant is broader than the test). **Fix before the
API/MCP surface exposes the graph in Phase 2.**

**2. Two-tenant / same-canonical-ID isolation — correct but unproven (`pay-down-before-phase-2`).**
The design is sound: `tenant_id` participates in both the MERGE node key and the composite uniqueness
constraint, so two tenants holding the same LEI/Q-number get two nodes and neither can collide. But the
headline isolation property the API will rely on has **no dedicated test**. The closest assertion proves
a weaker thing (a different tenant sees nothing for a different id). Before Phase 2 multi-tenant reads go
live, add a test that writes the *same* canonical ID under tenant A and tenant B and asserts two
distinct nodes plus mutual read-isolation.

---

## Q2 — Where are the gaps?

| # | Gap | Evidence | Risk if Phase 2+ builds on it | Severity |
|---|---|---|---|---|
| G1 | **Provenance not written on edges** | `graph/writer.py:142` | Edge-schema assertions (Ownership/Sanction/…) have no audit trail; GDPR/audit-log invariant broken for relationships | **blocker** |
| G2 | **Edge referent-rewriting for merged-away ids not done** | noted at `tests/integration/test_phase1_acceptance.py:14-16`; merge path (`resolution/pipeline.py:69-85`) writes only canonical nodes, never rewrites edges | After ER merges B→A, edges to/from B still point at B's dead id → orphaned edges; `get_neighbors` misses them. Neighbour linking is only asserted on **non-merged singletons** today | **pay-down-before-phase-2** (API exposes neighbours) |
| G3 | **Abstract `Thing`-range entity-links not materialised** | `graph/writer.py:136-138` (ftmg `generate_entity_links`); caveat at `tests/integration/test_phase1_acceptance.py:86-89` | Links whose range is the abstract `Thing` schema (`Sanction.entity`, `CourtCase.entity`, future `wm:Indicator.target`) are dropped; only concrete edges (Ownership owner/asset) are proven. Sanction→entity is not traversable | **pay-down-before-phase-4** (CTI/enrichers); flag now |
| G4 | **No two-tenant same-canonical-ID test** | see Q1 | Headline tenant-isolation invariant is unproven before multi-tenant reads | **pay-down-before-phase-2** |
| G5 | **Size-threshold guard untested** | `resolution/review.py:41`; no 11-member test | A regression in the `>10` guard would silently auto-merge oversized clusters | **nice-to-have** |
| G6 | **Sensitive-topic guard is OpenSanctions-specific** | `resolution/review.py:20-31`; `is_sensitive` reads a hardcoded `SENSITIVE_TOPICS` frozenset and the `topics` property | A future enricher using a different topic vocabulary (CTI, crypto) bypasses the catastrophic-merge guard → a sensitive entity auto-merges | **pay-down-before-phase-4** |
| G7 | **Expert-set Splink weights / fixed thresholds** | `resolution/splink_model.py:44-149` (hand-set m/u); `merge.py` `DEFAULT_MERGE_THRESHOLD=0.92`; `review.py:17` `MAX_AUTO_MERGE_SIZE=10` | Uncalibrated against real data; accuracy unknown when a second source arrives. Acceptable, transparent v0 | **pay-down-before-phase-3** (calibration) — see ADR 0016 |
| G8 | **Batch-bound ingest** | `runner/ingest.py:51-76` — iterates `collect()` to exhaustion, single terminal `commit()`; a raising `map()` aborts the whole run, no dead-letter | A `STREAM` connector's `collect()` never returns → nothing commits, unbounded memory; one bad record loses a batch with no audit | **pay-down-before-phase-2** (stream connectors) |
| G9 | **Whole-queue batch ER** | `resolution/pipeline.py:50-65` — loads **all** pending for the tenant and all-pairs scores them | A stream of small candidate batches forces O(n²) re-resolution per tick or incremental clustering that isn't built | **pay-down-before-phase-2** (stream connectors) — see ADR 0019 |
| G10 | **Enricher output not re-validated** | `resolution/pipeline.py:80` — `enrich(cluster.entity)` result is written without `validate_or_raise` | A buggy/third-party enricher can write invalid FtM to the graph | **pay-down-before-phase-3** (external enrichers) |
| G11 | **Landing `ensure_bucket` swallows `ClientError`** | `storage/landing.py` (`ensure_bucket`) treats all `ClientError` as "exists" | A permission/network misconfig is hidden until a later, less informative `put()` failure | **nice-to-have** |
| G12 | **Settings ship empty placeholders** | `settings.py:24-41` (empty OIDC/encryption keys, localhost URIs) | Boots `/health` without a stack by design; must be set before any authed/data op (fails loudly) | **nice-to-have** (Phase 0 acceptance) |

### The two known follow-ups (called out explicitly)

- **Edge referent-rewriting (G2).** When ER merges entity B into canonical A, the existing edges that
  referenced B are *not* rewritten to A. Today this is sidestepped by only asserting neighbour links on
  singletons (`test_phase1_acceptance.py:14-16`). The resolved graph is the product, and "resolve to
  canonical IDs" is a non-negotiable — so once the API exposes traversal, orphaned edges are a
  correctness bug. **Must land before Phase 2's graph-read surface.** Recorded as accepted v0 debt in
  ADR 0023.

- **Abstract `Thing`-range entity-links vs concrete `Ownership` edges (G3).** ftmg materialises an
  entity-link only when the property's range is a concrete schema (`Ownership.owner` → `LegalEntity`,
  `Ownership.asset` → `Asset`), and skips links whose range is the abstract base `Thing`
  (`Sanction.entity`, `CourtCase.entity`). Phase 1 proves neighbour traversal **only** via a concrete
  Ownership edge (`test_phase1_acceptance.py:86-89`); the Sanction→entity link is acknowledged as a
  follow-up. Low impact for OpenSanctions today, **high impact for Phase 4 CTI** (`wm:` indicator/target
  links are `Thing`-ranged). Recorded in ADR 0023.

---

## Q3 — Is the contract clean for Phase 2?

Phase 2 adds the API/MCP surface, the Integrations page, and the first live/stream connectors. Splitting
"contract" into the **L2 ontology + plugin interface** vs the **pipeline implementations**:

### What holds (no rework needed)
- **Connector interface is stream-shaped already.** `Connector.collect()` returns
  `Iterator[RawRecord]` (`plugins/base.py:104-106`), not a materialised list — a streaming source fits
  the signature. `map()` is per-record (`base.py:108-110`).
- **Mode and capability are modeled, not just documented.** `Mode.{EXTERNAL_IMPORT,INTERNAL_ENRICHMENT,
  STREAM}` and `Capability.{PASSIVE,ACTIVE}` are real enums on `Manifest` (`plugins/base.py:39-75`), so
  the Integrations page can read them off the catalog.
- **Registry is connector-agnostic.** OpenSanctions is registered as a plugin, not hardwired
  (`plugins/registry.py`, `plugins/connectors/opensanctions/`). Adding GeoNames/Wikidata followed the
  same path, which is the proof the framework is genuinely open.
- **ER queue and landing keys are tenant/connector-scoped** (`runner/ingest.py:53`,
  `db/models.py` `ErQueueItem`), so multiple connector instances coexist.

### What will break (must change for the first Stream/RestApi connector)
1. **`run_ingest` is a one-shot bounded run** (`runner/ingest.py:51-76`): it drains `collect()` and
   commits once at the end. A `STREAM` connector whose `collect()` never returns will never commit and
   will grow memory without bound. Phase 2 needs a **windowed/incremental commit** and a **long-running
   driver/scheduler**, not the current call-once function.
2. **`resolve_pending` is whole-queue batch ER** (`resolution/pipeline.py:50-65`): it pulls *all* pending
   rows for a tenant and runs Splink `dedupe_only` over the whole set each call. Under a stream this is
   either O(n²) re-resolution every tick or it needs **incremental clustering against the
   already-resolved graph** — which is not built. This is the single biggest contract stress and is an
   **OPEN** decision (ADR 0019).
3. **No scheduler / trigger model.** Ingest is invoked imperatively (by the acceptance test). The
   Integrations page needs persisted per-instance config + a scheduler to run connectors on a cadence.
   (`config` is currently passed as an in-memory `Mapping`; `db/crypto.py` exists for encrypting stored
   config but there is no connector-instance config table wired into a runner.)
4. **`Capability.ACTIVE` is modeled but not enforced.** CLAUDE.md requires active plugins run only with
   an authorized-scope token per run, with separate logging, never agent-auto-run. Nothing in the runner
   checks capability or gates an active run today. **Must land before any active Phase 2 connector.**
5. **No dead-letter / partial-failure path** (G8) — fine for a bulk file, unacceptable for a
   continuously running stream.

**Bottom line:** the **L2 ontology contract holds** and the **plugin interface holds**; Phase 1 did not
bake single-connector assumptions into the *interface*. It did bake **bulk + batch + run-once**
assumptions into the *orchestration* (`run_ingest`) and the *resolution* (`resolve_pending`). Those two
functions — plus active-capability gating and a scheduler — are the concrete Phase 2 changes.

---

## Cross-references
- Undocumented decisions surfaced by this audit are recorded as ADRs **0016–0023** (`docs/decisions/`).
- The ranked "fix before Phase 2" shortlist is delivered in the review summary accompanying this audit.
