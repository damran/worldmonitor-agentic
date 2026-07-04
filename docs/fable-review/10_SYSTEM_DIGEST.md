# 10 — System Digest

> A faithful, self-contained tour of **what WorldMonitor is and how it is built**, so you can review
> at altitude without spelunking. Where it helps, file paths and ADR numbers are cited so you can go
> to primary evidence. Maturity ("built / partial / deferred") is flagged throughout — the platform
> is real and largely operational through Phase 2, with the agent layer partially deployed and the
> analysis/consumption layers still ahead of it.

---

## 1. Thesis and shape

WorldMonitor turns **many heterogeneous OSINT sources into one resolved, provenance-tracked entity
graph**, then runs analysis on top and exposes it through an API/MCP surface driven by a
self-improving agent. The organising belief: *OSINT is a graph-traversal problem — value is in how
entities connect across sources.* So the resolved graph is the centre of gravity; every component
either **produces** entities into it or **consumes** the resolved result.

Two maxims govern every design choice: **"de-dupe before you count; calibrate before you conclude"**
and **"leads, not verdicts."**

---

## 2. The layered model and the one rule

The system is a stack of layers. The **load-bearing rule** is that **L2 (the ontology) is the
contract**: everything below *produces* ontology objects with provenance; everything above
*consumes* the resolved graph. A new source or method is a new L1 plugin emitting/working on L2
objects — no layer above it changes. This is what keeps a multi-domain platform from becoming a
Frankenstein.

```
L9  AGENT LAYER (Hermes) + UI     assistant (Telegram/CLI), scheduled reports, autonomous
                                  investigation, gated self-improvement.  [partial]
L8  API + MCP SURFACE             FastAPI REST + FastMCP. The only read/act boundary.  [built]
L7  Fusion & forecast (plugins)   weighted / Bayesian / D-S · calibration.            [deferred]
L6  Anomaly & signals (plugins)   point / time-series / coordinated · insider signals.[deferred]
L5  Domain enrichers (plugins)    news/NLP · social · crypto · CTI · geo/imagery.     [deferred]
L4  Graph store & analytics       Neo4j + GDS = property-graph SYSTEM OF RECORD.      [built]
L3  Entity resolution             Splink(DuckDB)+nomenklatura · canonical-ID registry ·
                                  catastrophic-merge guard.                            [built]
L2  Normalization & ONTOLOGY      raw → FtM/STIX entities+relations, canonical IDs,
                                  provenance (schema-validated).      ← THE CONTRACT   [built]
L1  PLUGIN FRAMEWORK              connectors/mappers/resolvers/enrichers/rules/scorers/
                                  notifiers/tools · registry · manifest+config-schema. [built]
L0  Substrate                    Docker Compose · 12-factor · CI/CD · vault.          [built]

Cross-cutting: Provenance & audit · Auth (Zitadel, single-tenant) · Security (passive/active
gating, hostile-input, GDPR) · Observability (Prometheus) · LLM gateways · Self-improvement (gated).
```

### End-to-end data flow
```
connector (L1) → raw + source metadata → mapper → FtM/STIX candidates w/ provenance (L2)
  → entity resolution (L3): resolve to canonical IDs, dedupe, cluster (merge audit)
  → upsert into the graph (L4)
  → enrichers/anomaly/fusion (L5–L7) attach derived edges/attrs + scores        [ahead]
  → API + MCP (L8) expose query/action
  → Hermes + UI (L9) investigate, report, decide
  → self-improvement loop feeds outcomes back (gated) to params/rules/models    [ahead]
```
Invariants that thread the whole way: **provenance is never dropped; resolution is central at L3,
never inside a connector; self-modification is gated.**

---

## 3. The ontology contract (L2)

WorldMonitor does **not** invent an entity model. It adopts:

- **FollowTheMoney (FtM) 4.x** as the core ontology — the de-facto OSINT/fincrime schema already
  spoken by OpenSanctions and Aleph; MIT-licensed; ships a Neo4j bridge (`followthemoney-graph`) and
  an ER framework (`nomenklatura`). FtM's design that **"relationships are entities too"** (an
  `Ownership` is a node carrying dates, percentage, and *source*, not a bare edge) is used as a
  feature for provenance.
- **STIX 2.1** as the vocabulary for the CTI domain — **CTI is just one plugin domain, not special.**
  OpenCTI, if used, is an upstream STIX *source*, never the system of record.
- **`wm:` extensions** only where FtM cannot reach (news/events, social, geospatial, crypto,
  markets) — each additive, each with an ADR. Adding a type = adding a schema file (data, not code).

**Canonical IDs are the anchor (non-negotiable):** Wikidata Q, GeoNames, LEI, OpenCorporates,
VIAF/ISNI, ISO-3166. The same real-world entity from a news article and a reference source is meant
to land on one node keyed by these IDs. Connector output is **strictly FtM-validated and fails loud**
(ADR 0022) — bad data fails at the source, never corrupts the graph silently.

**Provenance model** (ADR 0018/0045/0060), stamped on every node and edge:
```
{ source_id, source_record (pointer to raw bytes in MinIO), retrieved_at, reliability, assertion }
```
A node merged from N sources carries N entries → this powers *both* catastrophic-merge protection
*and* the GDPR/audit log. **Current implementation caveat:** multi-source provenance is stored as
flat `prov_*` node properties (a JSON witness map for multi-source), because FtM's `merge_context`
cannot union nested dicts. Statement-level ("Tier-2") provenance — where each *claim* is a first-
class queryable subgraph — is designed but **deferred**. Provenance is enforced *fail-closed* for
single-source nodes; for merged nodes the graph-queryable projection is thinner than the audit
tables behind it (a known tension — see §12).

---

## 4. Entity resolution & the catastrophic-merge guard (L3 — the intellectual core)

This is where the platform's quality claim lives. It is a strictly **central** gate: connectors emit
**candidates** to an `er_queue`; **they never deduplicate themselves** (per-connector dedup fragments
the entity model). `resolve_pending` (`resolution/pipeline.py`) drains the queue in bounded batches
and runs:

1. **Splink (DuckDB)** — unsupervised Fellegi–Sunter probabilistic blocking + pairwise scoring
   (~1M rec/min on a laptop). Multi-script name canonicalisation via `fingerprints` (ADR 0035) and
   an abjad/Arabic-Persian normalisation step (ADR 0073) so cross-alphabet duplicates converge.
2. **nomenklatura** — FtM-native transitive clustering, seeded per-batch from durable human
   sign-off judgements; run on a **private in-memory resolver per batch** (ADR 0028) for crash
   idempotency and batch purity.
3. **Anchor-preferred durable canonical IDs** (ADR 0044/0048) — an entity that carries a canonical
   anchor (QID/LEI/…) gets a stable, injective, FtM-valid durable ID; a content-addressed
   deterministic hash otherwise (ADR 0036). An append-only **canonical-ID ledger** in Postgres does
   alias-on-read so merged-away/ superseded IDs resolve transparently to the surviving node.
4. **The catastrophic-merge guard** — the dominant failure mode is one wrong high-confidence link
   fusing two unrelated people. Mitigations: require multiple independent agreements before merging;
   an **anchor-conflict guard** (conflicting same-type IDs ⇒ negative evidence, never fuse); a
   **fail-closed sensitivity guard** (deny-by-default over FtM topics + a k-hop graph probe + a Chow
   abstain band — ADR 0047); and **queue any high size/value/sensitivity merge for human review —
   never auto-merge a sensitive entity.**
5. **Referent rewriting** (ADR 0025) + **Neo4j upsert** with per-stage quarantine isolation
   (ADR 0038) so one poison record never wedges the drain (it dead-letters at its own granularity).

**Human sign-off** (`resolution/signoff.py`) is an append-only, idempotent, crash-recoverable state
machine; a CLI (`worldmonitor.review`) lists/approves/rejects parked merges.

**The measurement / calibration harness** (`resolution/eval.py` + `gold.py`, ADR 0043) computes
**B³ / CEAFe / over_merge_rate** over a *gold partition* (not just Splink's candidate set — it
catches blocking-conditional over-merges that pairwise PR analysis misses). To break the circularity
of "labels derived from the model's own score," a **non-circular label on-ramp** exists:
**canonical-anchor silver labels** (ADR 0079/0085 — pairs sharing ≥2-source canonical anchors are
positives; conflicting same-type anchors are negatives) and an **external-benchmark floor**
(ADR 0080 — OpenSanctions OS-Pairs + Febrl, with a contamination guard).

> **Two things the reviewer should know:** (a) the merge guard currently defaults to **"alert" mode**
> (ADR 0024) — it flags and audits sensitive merges but *still writes them*; flipping to "block" is
> required before production and has not happened. (b) The 0.92 merge threshold and the 10:1 FP:FN
> cost prior are **expert-set and have never been calibrated against a real corpus** — the harness is
> a ruler with no measurement yet taken. Threshold promotion (G7) stays human-sign-off-gated because
> it is person-affecting.

---

## 5. Graph, storage, and data lifecycle (L4 + substrate)

A deliberate **three-store topology, no parallel datastore**:

- **Neo4j 2026.x Community + GDS** — the **exclusive system of record** for the resolved graph.
  Cypher for queries; GDS Community for analytics (currently a single degree-centrality projection —
  `graph/gds.py`). Writes go **only** through the `ftmg_fork`-patched writer, which raises
  `NodeProvenanceError` / `EdgeProvenanceError` at write time — **it is structurally impossible to
  write an unprovenanced node or edge** (G1 enforced in code).
- **PostgreSQL 16 (pgvector image)** — all relational state: connector registry, ER queue, merge
  audit, human-decision tables (sign-off, judgements), the canonical-ID ledger, task-run audit, gold
  pairs, dead-letter. Schema evolves via **Alembic** (ADR 0030). *Note:* the pgvector extension is
  **deployed but unused** — a latent capability with no ADR (flagged as a tension).
- **MinIO (S3-compatible)** — the immutable **landing zone**: every raw collected byte is stored
  verbatim *before* mapping (ADR 0021), giving a concrete `s3://` provenance pointer and replayable
  re-mapping. Reference-based orphan GC with a grace window (ADR 0083/0086).
- **Redis** — session/cache and driver heartbeat only. **DuckDB** — ephemeral Splink scratch, never
  persists.

**GDPR right-to-erasure** (`erasure.py`, ADR 0049) spans all stores atomically via `erase_source`:
source-scoped, idempotent, audit-trailed, with an over-delete guard; it deliberately preserves the
canonical-ID ledger (a no-un-merge sub-invariant). **Backup/restore/DR** (`backup.py`, ADR 0050) is a
Python-native cross-store logical dump with bidirectional count verification — but it is *not* point-
in-time-consistent under concurrent writes, has no scheduling/rotation/offsite, and RTO on a large
graph could be hours. (Neo4j Community has no online hot backup or HA — an accepted v0 constraint,
now revisitable.)

---

## 6. The plugin framework (L1)

**Everything that does work is a plugin** behind a typed interface, discovered by a registry, self-
describing, and independently enable/disable-able (`plugins/base.py`, `registry.py`; ADRs in the
0065–0072 range). A plugin ships: a **manifest** (id, kind, capability, status), a **`config.schema.
json`** (JSON Schema that *renders the UI form* and validates input — zero per-plugin frontend code),
an **implementation**, and **tests**.

**Kinds:** Connector · Mapper · Resolver · Enricher · Rule · Scorer/Algorithm · Notifier · Tool.
Research drops in as a Scorer/Enricher (+ optional `wm:` extension); removing it is unregistering.

**Connectors declare a mode** (`EXTERNAL_IMPORT` / `INTERNAL_ENRICHMENT` / `STREAM`, from OpenCTI's
taxonomy) **and a capability** (`passive` / `active`). They `collect()` raw → landing zone and
`map()` → FtM candidates → ER queue; **they never write the graph and never resolve.** Base classes
carry the machinery: `RestApiConnector`, `CliToolConnector` (containerised, sandboxed, egress-
constrained), `StreamConnector` (WebSocket firehose + cursor/resume), `FeedConnector` (RSS/Atom),
`FtmBulkConnector`.

**Active plugins are gated:** an `active` capability requires an **authorised-scope token per run**,
separate logging, and is **never agent-auto-run** — enforced in depth (the cadence driver refuses
unconditionally; the operator-run path requires the token + a sandbox check; the sandbox sidecar
re-validates auth and argv).

**Shipped connectors/plugins:** OpenSanctions & FtM-bulk, OpenCorporates (REST), Bluesky Jetstream
(stream), RSS/Atom feeds, GeoNames + a Wikidata slice (reference/enrichment), Wikidata enricher, and
the active CLI tools whois/dig/nmap (nmap execution-gated behind the sandbox). A `TelegramNotifier`
sends deterministic system alerts. An **Integrations page** (HTMX + Jinja2, ADR 0069) renders the
catalog and schema-driven config forms so a source is addable from the UI by filling a form.

---

## 7. API + MCP surface (L8) and the LLM gateway

**One contract, two front doors** — `docs/60`, `api/*`, `mcp/*`:

- **FastAPI REST** — bounded, parameterised graph reads: `/entities`, `/entities/{id}/neighbors`,
  `/provenance`, `/paths`; every read is **hop-capped and result-LIMITed** (ADR 0064); provenance is
  returned in responses. Raw Cypher / GraphQL is deferred to trusted/admin (an **open** decision).
- **FastMCP server** — the *same* bounded helpers exposed as **exactly four read-only tools**:
  `get_entity`, `get_neighbors`, `get_provenance`, `find_paths` (`mcp/server.py`). Transport is
  authenticated **streamable-HTTP with a Zitadel bearer** (ADR 0090) so a remote Hermes can connect.
  All active/write MCP tools are **out of scope until Phase 6**.
- **Auth:** **Zitadel OIDC**, **single-tenant** (ADR 0042 — no tenant scoping anywhere). Roles:
  read / run-passive / run-active / admin. Browser session auth via a dual-path middleware
  (ADR 0068).

**LLM gateway (service-side):** **LiteLLM** is the single auditable egress choke point for
WorldMonitor's *own* LLM use, with a **three-mode confidential selector** (ADR 0091): **Local/Ollama
(default, zero egress)**, **Claude-headless**, **OpenRouter**. An OpenAI-compatible `/v1/chat/
completions` shim (ADR 0092) routes Hermes' model traffic through the same choke point so the
sovereignty posture is enforced structurally. (Data-sovereignty principle: WorldMonitor's data never
leaves the perimeter unless the operator explicitly opts in.)

---

## 8. The agent layer (Hermes) and gated self-improvement (L9)

**Decision: adopt Hermes, don't build a runtime** (ADR 0089). **Hermes Agent** (NousResearch, MIT,
v0.17.0) runs as its **own external container**, connects to WorldMonitor's MCP as a Zitadel service
principal (read + run-passive), and drives the four read tools. It ships the things WorldMonitor
would otherwise build: a **skills/memory self-improvement loop**, **any-LLM** switching, a
**Telegram/cron** gateway, and an **MCP client**. Division of labour: **WorldMonitor owns the data,
ontology, graph, and tools; Hermes owns the agentic experience.** Two reporting paths — rich agentic
Telegram briefings (Hermes cron) and deterministic system alerts (the `TelegramNotifier`, which
survives agent downtime).

**The gated self-improvement loop** (`docs/50`) is the platform's differentiator *and* its riskiest
subsystem. Nothing self-modifies silently; every change flows
**propose → evaluate (held-out metrics) → gate → promote (versioned, rollback) → audit.** Three
mechanisms, increasing risk: (4a) Hermes' own skills/memory (lowest risk, always on, touches no
platform data); (4b) trajectory fine-tuning of the tool-calling model (batch, GPU path, deferred);
(4c) agents proposing changes to **scoring weights, ER thresholds, and rules** (highest stakes —
person-affecting changes *always* require human sign-off; bounded auto-tune only within pre-declared
safe ranges).

> **Maturity flag:** Hermes deployment is **partial** — the MCP-auth transport, the LiteLLM gateway,
> the `/v1` shim, and the compose services are built and merged (Phase-3 S1–S3b), but Hermes itself
> has **never been runtime-validated here** (the dev box can't build images), the first Telegram
> brief (S4) is blocked on an operator deploy, and the operator-console UI (S5) is design-paused.
> Crucially, **§4b and §4c are unbuilt** — the self-improvement loop is at present a *design contract*,
> and the only evaluation harness that exists measures ER, not scoring/rule parameters.

---

## 9. Runner, driver, and operations (L0)

The platform's own pipeline is driven by a **single-process asyncio loop** (`runner/driver.py`,
ADR 0029) backed by a **PostgreSQL task table — no external scheduler**. It cadence-runs batch
connectors, resolves on an independent cadence (serialised behind a `threading.Lock`), records
`task_run` audit rows, refuses ACTIVE connectors visibly, and recovers stale tasks at startup.
Resilience: exponential backoff (ADR 0054), auto-hard-disable after N failures (ADR 0074), periodic
maintenance + a resolve wall-clock timeout (ADR 0075). Outbound connector HTTP passes an **SSRF
guard** (ADR 0057/0087 — RFC1918/loopback/link-local blocking, redirect-chain validation, sensitive-
header stripping on cross-host redirects). Heavy active tools run in a **sandbox-runner sidecar**
(ADR 0077) on an isolated Docker network (egress isolation), non-root, read-only, resource-bounded,
with a per-tool default-deny argv allowlist.

**Observability:** an on-scrape **Prometheus** custom collector on the driver process (ADR 0076) +
seven alert rules tested by promtool in CI (ADR 0078/0088). OTel/Loki are deferred.

**Packaging:** Docker Compose (core services + opt-in profiles for agent/monitoring). **CI:** GitHub
Actions `quality` + `security` (Trivy, CodeQL) are branch-protection-required; a `compose-boot` job
boots the real stack; an `alert-rules` job runs promtool (not yet a required check).

---

## 10. How it's built — the multi-agent gate fleet (distinctive; relevant to the comms review)

WorldMonitor is built by a **supervised multi-agent adversarial gate fleet**: a daemon
(`orchestrator/run_fleet.py`) drives Claude Code headless through a fixed pipeline —
**orient → plan → test-author → builder(s) → checker → judge** — one gate at a time, parks at a human
boundary, and **self-merges on green CI**. It is codified in `CLAUDE.md` (always-loaded ground truth,
mirrored to `AGENTS.md`/`.clinerules`), backed by **93 recorded decisions** with explicit
**reversibility classification**, audited gate-by-gate in `docs/GATE_LEDGER.md`, and augmented by a
cost-gated **cross-vendor council** for decorrelated second opinions. **Property/metamorphic tests
are mandatory** for any gate touching an invariant (ER/merge/provenance/canonical-ID). This method is
arguably the most novel thing in the project and is part of what the communication review should
weigh — including its risks (self-classified reversibility, `bypassPermissions` build agents with CI
as the only containment, a solo human gate).

---

## 11. Technology stack at a glance (role → credible alternatives)

| Concern | Current choice | Role | Credible alternatives / when they'd win |
|---|---|---|---|
| Graph SoR | **Neo4j 2026.x Community + GDS** | Sole system of record; Cypher; in-DB analytics | Memgraph (lower mem); **RDF-star triple store** (native ontology + statement provenance, loses FtM tooling); Neo4j Enterprise/Aura (needed for HA/multi-tenant) |
| Ontology | **FollowTheMoney 4.x + STIX 2.1** | The L2 contract, ER vocabulary, provenance substrate | Custom OWL/RDF (rejected — no ER tooling); FtM as *interchange* format + own internal model (decouples provenance/versioning) |
| ER | **Splink (DuckDB) + nomenklatura** | Central probabilistic resolution + FtM-native clustering | Dedupe.io; recordlinkage; embedding/ANN blocking for multilingual names; managed ER (Senzing); LLM-assisted boundary matching |
| Agent runtime | **Hermes v0.17.0 (MIT), external** | Skills/memory loop, any-LLM, Telegram/cron, MCP client | LangGraph/AutoGen/CrewAI/OpenAI Agents SDK; a thin custom loop over the MCP contract (all swappable via the MCP seam) |
| Service LLM | **LiteLLM (in-process) + 3-mode selector** | Single auditable egress; local-default | Standalone LiteLLM proxy container (deferred; enables streaming/scaling); vLLM/TGI for stronger local |
| Auth | **Zitadel (self-hosted OIDC)** | One auth model across REST/MCP/browser/service | Keycloak; managed IdP (WorkOS/Auth0 — SSO + orgs, relevant if multi-tenant returns) |
| Relational | **PostgreSQL 16 (+pgvector, dormant)** | All relational + human-decision state | Wire pgvector for semantic ER/search, or drop it; Temporal's store to replace the task table |
| Object store | **MinIO (S3-compatible)** | Immutable raw landing zone; replay; erase-by-prefix | S3/GCS/R2 directly (portable now cloud is allowed) |
| Tasks | **asyncio + Postgres task table** | The single driver clock; startup crash recovery | Temporal (durable, HA); Celery/arq/Dramatiq; Prefect/Dagster (lineage/backfill); Kafka/Redpanda for streaming |

---

## 12. Tensions the operator already sees (so you don't just rediscover them)

The project is candid about its own seams. These are **known** — the review is more valuable pushing
*past* them (are they the right calls? how would you resolve them?) than re-listing them:

1. **Batch-only resolution violates "de-dupe before you count" as a standing condition** — cross-batch
   duplicates persist until records happen to co-occur in a batch; the normal case for stream/
   re-ingested sources. Incremental ER is the real fix and is deferred with no wired trigger.
2. **The merge guard defaults to "alert," not "block"** — "never auto-merge a sensitive entity" is
   currently an audit log, not an enforced control.
3. **The threshold is uncalibrated** — 0.92 and the 10:1 cost prior have never met a real corpus.
4. **Single-node by design** — one asyncio loop, a `threading.Lock` (no distributed lease), Neo4j-
   before-Postgres writes with idempotency instead of a saga/outbox, a hung resolve thread abandoned
   (not killed). Safe as one process; unsafe the moment it scales.
5. **Provenance is thinnest exactly on merged nodes** (single-source projection collapse, some edge
   drops, Tier-2 deferred) — the most analytically valuable case.
6. **L2's foundation (FtM) is an unpinned external dependency** the whole architecture declares the
   contract, with no stated pin/vendoring/migration policy.
7. **The consumption surface is thin** — four read tools + a config page; no analyst UX, graph
   explorer, alerting/triage, or scoring output yet. For a product whose thesis is relationships, the
   place a human extracts value is the least-built part.
8. **The differentiator (gated self-improvement) is aspirational** — §4b/§4c unbuilt; no evaluation
   substrate for non-ER parameters; the hardest questions (safe auto-tune ranges, reward signal,
   scoring a person-affecting proposal) untouched.
9. **Human-in-the-loop is the safety model *and* an unscaled bottleneck** — one reviewer, no review-
   queue UI, no sampling/tiering; the sign-off queue becomes the graph's growth rate-limiter.

These, plus the full decision register and the freedoms map, are your raw material.
