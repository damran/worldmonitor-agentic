# 40 — Roadmap

> `v0.4` · June 2026 · **Vertical slices, spine first, breadth/agent/UI later.** Each phase is
> end-to-end and testable. Don't start a phase before the prior one's acceptance criteria are green.
> Built by **Claude Code autonomously** (branch → PR → CI → merge), pausing only for questions / OPEN ADRs.

## Sequencing rationale
Binding order (Algorithms Sec 9.4): **ingestion+normalization → entity resolution → graph+analytics →
streaming anomaly → domain enrichers → fusion**. WorldMonitor is graph-native + ontology-first, so
Phase 1 proves the *spine* with one zero-risk source. The **API/MCP surface** comes next so the agent
layer has tools; **Hermes** connects after that; self-improvement is unlocked last.

---

## Phase 0 — Foundations
**Goal:** a clean, reproducible, secure, auth-gated skeleton.
- [ ] `uv` project + toolchain (Ruff, Pyright strict on `src/`, pytest+coverage, pre-commit incl. secret-scan).
- [ ] Repo layout (below); `.env.example`; `CLAUDE.md` (mirrored to `AGENTS.md`/`.clinerules`).
- [ ] **GitHub Actions** `quality` green + `security` (Trivy, CodeQL); **branch protection requires both** (enables safe autonomous merge).
- [ ] `deploy/compose.yaml` (core): **Neo4j+GDS, PostgreSQL(+pgvector), MinIO, Redis, Zitadel**; optional profiles for the rest.
- [ ] **Zitadel** configured: instance, org, admin user, OIDC apps for the API and for Hermes (service principal).
- [ ] FastAPI boots, **auth-gated (OIDC)**, `/health` returns.
- [ ] `runner/` runs an async subprocess with **timeout + error handling** (base for `CliToolConnector`).
- [ ] No hardcoded secrets; everything on a feature branch.

**Done when:** `docker compose up` → a logged-in, empty, single-tenant platform with green CI.

---

## Phase 1 — The spine: one source → ontology → ER → graph ✅ COMPLETE
**Goal:** prove `connector → ontology → resolution → graph → query` with **OpenSanctions** (FtM-native, free, zero-risk), with tests.
- [x] **Ontology bootstrap:** FtM installed + schema validation; `followthemoney-graph` writes FtM → Neo4j with unique constraints on canonical IDs.
- [x] **Plugin framework v0:** base interfaces + registry + `FtmBulkConnector` + provenance stamping + instance table (`30`).
- [x] **OpenSanctions connector:** manifest + schema + collect + (near-identity) map; raw → MinIO, candidates → ER queue.
- [x] **Entity resolution v0:** **Splink** (DuckDB) + **nomenklatura** → canonical entities; **merge audit trail**; a size/sensitivity **review-queue threshold**.
- [x] **Reference anchor:** load **GeoNames** + a Wikidata slice → canonical IDs on resolved entities.
- [x] **Graph queries:** Cypher returns resolved entities + relationships + provenance; one **GDS** run (centrality/community) over a projection.
- [x] **Tests:** unit (raw→FtM; ER merges right pairs, refuses bad — incl. a catastrophic-merge negative test); integration (queried-back graph = expected resolved, deduped, provenance-tagged).

**Done when:** "show this sanctioned entity, everyone linked to it, and where each fact came from" returns a correct, deduplicated, canonical-ID-anchored answer. **No second source until green.**

---

## Phase 2 — API/MCP surface + Integrations page + first live/stream connectors ✅ COMPLETE (2026-06-28)
**Goal:** expose the graph outward, and the flagship self-service surface. _16 gates, ADRs 0060–0072, PRs #114–#129; each failing-test-first → build → adversarial verify → green CI → self-merge._
- [x] **API + MCP (`60`):** auth-gated REST reads (`/entities`,`/entities/{id}/neighbors`,`/provenance`,`/paths`) + a **FastMCP stdio** server over the same bounded/parameterized helpers; provenance-in-responses; guarded reads (hop-cap + result LIMIT). GraphQL + raw `query_graph` deferred to trusted/admin. — ADR 0062/0063/0064 (#119/#120/#121).
- [x] **Integrations page (UI):** **HTMX+Jinja2** catalog from the registry + **schema-driven config forms** → save (vault-encrypted) → enable → status/health → **Run**. Browser auth = **Zitadel OIDC** session (dual-path middleware). — ADR 0068/0069 (#125/#126).
- [x] **`RestApiConnector`** base + **OpenCorporates** (0065, #122); **`StreamConnector`** = **Bluesky Jetstream** + the **G8 cursor/resume** protocol (0070, #127); **`FeedConnector`** RSS/Atom → FtM `Article` (0066, #123; full-text → a Phase-4 enricher).
- [x] **`TelegramNotifier`** + the Notifier plugin type (0067, #124).
- [x] Active-capability gating proven: scope token + operator-run + audit + `CliToolConnector` + **whois/dig** (run, subprocess) + **nmap** (execution-gated until a container sandbox) (0071/0072, #128/#129).

**Done when (✅ MET):** add a source from the UI by filling a form and watch it collect into the graph; external workflows can query via MCP/REST.

---

## Next — F1 storage inversion: the pre-cutover gate sequence (2026-07-12)
_The statement-log → derived-projection substrate is built and dormant (ADRs 0095/0099–0102; spine
dual-write #162, fold engine #163, incremental correctness + rebuild-diff guard #164/#165; durable
LLM-egress audit ADR 0105 #170). The log-capture consult (`docs/fable-review/80_LOG_CAPTURE_CONSULT.md`,
#169) named the binding pre-cutover prerequisites; the sequenced plan is
`docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md`._
- [x] **Gate P1 — context-claim capture lane** (ADR 0106, #174) — anchor/enricher evidence into the
  SoR spine as provenance-stamped claims at both promote points; the fold reproduces anchors; the
  divergence guard's dead `wm_anchor_` exclusion fixed to the bare keys.
- [x] **Gate P3 — sign-off spine durability** (ADR 0108, person-affecting, cosigned, #176):
  `approve()`/`reject()` co-commit statement + decision (`decided_by=<operator>`) + ledger rows — a
  rebuild must not drop human-approved merges.
- [x] **Gate P2 — right-to-forget reaches the SoR** (ADR 0107, person-affecting, cosigned, #178):
  three-lane log scrub (flow + accumulated stock), a defined live-removal mechanism, granularity
  reconciliation, both-surfaces round-trip property. Three rounds of adversarial verification
  closed a CRITICAL/HIGH/2 MEDIUM; one narrow, over-removal-only residual disclosed
  (self-heals at Gate 2b — `resolution/erasure_scrub.py`'s `KNOWN RESIDUAL`).
- [x] **WPI write-path-integrity slices** — single-writer ingest assert (ADR 0110, #180) ·
  alias⇔co-commit invariant + fold-side completeness check (ADR 0111, #181) · zero-prop-entity
  disposition (ADR 0112, #182): spine-write hardening so a `full_rebuild` cannot silently corrupt.
- [x] **Gate 2b — statement/context-claim log backfill** (ADR 0113, person-affecting, cosigned, #183):
  the pre-2a window of the spine backfilled byte-faithfully from `er_queue.raw_entity`, so a
  `full_rebuild` reconstructs the whole graph and the WPI-2 completeness obligation is discharged
  (`find_incomplete_aliased_survivors == ∅`). Mechanism merged + tested; **running** it over a
  real-seed corpus is operator-blocked (per-cohort fidelity spike, SF-4).
- [x] **3b-planning-proper ✅ (paused 2026-07-12 per ADR 0115; engineering finished 2026-07-18)** —
  exclusion-surface audit + one-time reconciliation instruments (#184/#185) · driver LOWs: single
  ledger read + handshake-refusal observability **DONE** (#204, including the operator reconcile
  CLI `python -m worldmonitor.resolution.reconcile_cli`); snapshot streaming stays spec+defer
  (doc 82 §6). Everything still open before the cutover is **operator-blocked** — run
  `docs/runbooks/OPERATOR_SESSION.md` §§4–7 — plus the ADR 0114 cosign. See
  `docs/fable-review/81_PRECUTOVER_GATE_SEQUENCE.md` §7 and `82_GATE_3B_CUTOVER_PLAN.md`.
- [x] **Consumption dashboard MVP ✅ COMPLETE (ADR 0115, 2026-07-13)** — an interactive, graph-native
  product at `/app`: a 3D globe of geo-located events + a live feed rail + a click-through entity
  relationship panel with provenance receipts + entity search + AI-synthesized briefs with citations.
  A **read-model** over the resolved graph (bounded Cypher; never writes it); Neo4j stays the live SoR.
  Five thin slices, light process (the product lane touches no person-affecting invariant): **A**
  seed + open-read carve-out (#186) · **C** dashboard read API (#187) · **D** 3D globe SPA (#188) · **B**
  news→event LLM extraction, default-OFF (#189, adversarially reviewed) · **E** AI briefs (#190). Run:
  `docker compose up` → open `/app`. News→event extraction + briefs need Ollama on the host +
  `EXTRACTION_ENABLED=true`.
- [ ] **CTI on-ramp (OG-harvest 91)** — a named-threat-actor substrate for the CTI persona. **S-3
  landed** (ADR 0117): the `mitre_gid` canonical-anchor namespace + the seeded `mitre_attack`
  connector (ATT&CK Enterprise intrusion-set catalog). **S-2** (IOC feeds → intrusion-set edges)
  and **F-2** are queued, F-2 scheduled post-S4.
- [ ] **Gate 3b cutover + retire the direct write** — human-gated, irreversible, LAST; the first
  sanctioned live `full_rebuild` consumes Gate 2b's output. Blocked on the operator preconditions
  (run the 2b backfill, enable the guard, green over N cycles) + the human cutover sign-off.

---

## Gate 0 (truth-up & governance) ✅ shipped 2026-07-04/05 (PRs #158–#161); Stage-4 hardening backlog; Phase-3 infra S1–S3b shipped
_Pay down the deferred hardening before/alongside Phase 3. (Full notes: the forward plan + the `phase-2-complete-stage-4-next` memory.)_
- [x] **H-4 Abjad/Arabic-Persian ER** ✅ (ADR 0073, PR #131) — strip harakat/tashkeel + tatweel before `fingerprints.generate` in `splink_model.py::_name_fingerprint`, so the same abjad name written with/without short-vowel marks projects the same `name_fp`. `@given` recall/precision/no-op properties + Arabic/Persian fixtures; threshold + merge-guard + sensitive-park unchanged. `LogicV2` re-scorer still deferred.
- [x] **H-8 remaining halves** (sliced; decided, ADR 0054) — [x] auto-hard-disable after N failures (ADR 0074, PR #132) · [x] periodic in-loop maintenance cadence (ADR 0075) · [x] resolve wall-clock timeout + lock-skip escalation (ADR 0075) · [x] Prometheus `/metrics` transport (ADR 0076) · [x] Prometheus scrape config + alert rules in-repo (ADR 0078, H-8c follow-up) — 7 alerts (2 critical/5 warning), INV-PARITY drift test, opt-in compose service; closes ADR 0075 revisit trigger.
- [x] **Container/egress sandbox** ✅ (ADR 0077, sandbox-runner sidecar) — flips `container_sandbox_enabled` (default-off; operator opts in); unlocks nmap execution (ADR 0072 follow-up). Slice 1 (app seam — settings + app-side `ContainerRunner` + `operator_run` refuse-or-route + the sidecar service code, behind the default-off flag) **landed**; Slice 2 **landed** (Dockerfile `sandbox-runner` stage with the tool binaries — api/driver image stays slim; isolated `sandbox-runner` compose service on `sandbox_net` ONLY — off the stores' network for egress isolation, non-root + read-only + mem/pids/cpus/ulimit bounds + no host port; per-tool DEFAULT-DENY argv allowlist in the sidecar validator). Egress = Docker **network isolation** (ADR 0077 §D4 refinement); nftables metadata/RFC1918 denial deferred.
- [ ] **MEDIUM/LOW sweep** — #105 (edge-prov skip+dead-letter), ~~M-5 (online-migration safety)~~ (**CLOSED** ADR 0084 — dialect-aware `lock_timeout` guard, `migration_lock_timeout_ms=3000` default, migrate-while-stopped runbook, `CONCURRENTLY`/`NOT VALID` patterns documented + `transaction_per_migration=True` deferred), ~~M-6 (landing GC)~~ (**CLOSED** ADR 0083 — reference-based orphan GC, report-only default + deletion opt-in, disk-growth gauges, deterministic-key invariant), ~~wikidata enricher via `guarded_stream`~~ (**CLOSED** ADR 0081), dig/nmap richer FtM map, ~~suffix-match allowlist~~ (**CLOSED** ADR 0082).
- [ ] **G7 threshold promotion** — promotion itself stays **human-sign-off-gated** (person-affecting; never promote off circular evidence; ADR 0043 harness exists). The original blocker — the only labels were a provisional clerical prior derived from the model's own score (circular) — is being paid down via a **non-circular label on-ramp** (decided 2026-06-29; validated against live OFAC data, 38% canonical-ID coverage):
  - [x] **Canonical-anchor silver labels** (ADR 0079) — `resolution/silver.py` derives `er_gold_pair` labels from shared canonical IDs across ≥2 distinct sources (positive) / conflicting same-type IDs (negative); non-circular by construction (no score input, N1/N2/N3); `@given` property test. Measurement labels only — no merge/threshold change.
  - [x] **Silver-correctness fixes** (ADR 0085) — two CONFIRMED review findings: (1) `registrationNumber` is jurisdiction-scoped (not globally-unique); split into `GLOBALLY_UNIQUE` + `JURISDICTION_SCOPED` tiers; shared/conflicting regNo requires `jurisdiction`/`country` corroboration; (2) contradiction-drop precedes source check — same-source contradictions now correctly dropped (not mis-labelled `non_match`). `ANCHOR_PROPERTIES` union preserved for ADR 0080 compat. 45 tests (↑17). Measurement-only — no live-ER change.
  - [x] **External-benchmark floor** (ADR 0080) — `resolution/benchmark.py`: OS-Pairs + Febrl importers; `evaluate_floor` (score_fn injected, INV-IMPORT-PURITY); contamination guard `drop_contaminated` (LOAD-BEARING: drop + count pairs overlapping our silver/gold partition, no silent truncation); `FloorMetrics`; `recordlinkage` optional/dev dep. Floor is returned in-memory, sanity-only — no promotion, no er_gold_pair write, no live-path change. Full 755k OS-Pairs scoring run is an ops step (not in tests). `@given` property + unit tests; 48 tests. Promotion still human-sign-off-gated.
  - [x] Label-sufficiency report (`eval.py`: labels by source + boundary coverage + metric CIs) (WP-1, `resolution/measure.py` CLI).
  - [ ] Real-seed corpus run (ops: run the sanctions connectors on the host to populate the candidate corpus).

---

## Phase 3 — Agent layer (Hermes) connected
**Goal:** the self-improving assistant on top of the surface.

**Shipped infrastructure (ADRs 0089–0093; PRs #149–#153):** S1 MCP-auth (Zitadel bearer, ADR 0089, #149) · S2 LiteLLM gateway + 3-mode confidential selector (ADR 0091, #150) · S3a HTTP /v1 shim (ADR 0092, #151) · S3b Hermes + MCP compose deploy (ADR 0093, #153). Infrastructure COMPLETE; operational deployment (below) is R1 and awaits operator verification.

- [ ] **Hermes deployed** and connected to WorldMonitor's **MCP** as a service principal (read + run-passive).
- [ ] **LLM pluggability** verified — Hermes on Ollama and on OpenRouter (`hermes model`); **LiteLLM** wired for any service-side LLM use.
- [ ] **Scheduled reports** (Hermes cron → Telegram): a daily brief + "what changed about entity X" queries.
- [ ] Hermes' **learning loop (skills/memory)** on (lowest-risk improvement) — active-tool/graph-write skills still gated.

**Done when:** you can ask WorldMonitor questions from Telegram and receive scheduled briefings, driven by Hermes over the MCP tools.

---

## Phase 4 — Domain enrichers (plugins, one at a time)
Each an `INTERNAL_ENRICHMENT`/`Scorer` plugin (Algorithms sections in parens), with tests, writing provenance edges:
news/NLP & multilingual fusion (Sec 6: GDELT + dedup → NER/linking to Q-numbers → topic/narrative via LaBSE → sentiment) ·
crypto/fund-flow (Sec 2: clustering + taint; USDT-on-Tron) · CTI/infra (Sec 7: passive-DNS/cert/JARM-JA3; ingest STIX from OpenCTI/MISP feeds) ·
financial/trading (prediction-market insider signals, options flow, macro/geo indices) · geospatial/imagery & media forensics (Sec 4–5; GPU; latest).

---

## Phase 5 — Anomaly, fusion & forecasting (plugins)
Anomaly (Sec 3: IsolationForest/LOF + CUSUM/EWMA/BOCPD + coordinated-behaviour; streaming+batch) ·
fusion/scoring (Sec 8: transparent weighted first, then Bayesian; **calibration** before any score is surfaced) ·
forecasting/early-warning (GBMs on ACLED/GDELT labels; prediction-market odds as a leading feature).
**Rule:** every score ships with calibration and is a *lead*, not a verdict.

---

## Phase 6 — Self-improvement (gated) & scale
- **Param/rule auto-tuning** (`50` §4c): agents propose → evaluate → gate → promote, versioned; sensitive changes (ER thresholds, individual-affecting scores) human-gated; bounded auto-tune ranges.
- **Trajectory fine-tuning** (`50` §4b): batch on a GPU path (serverless/local — OPEN); promote a new model only if it beats the incumbent on a benchmark; rollback retained.
- **Scale/cloud:** managed containers/K8s, S3, **Neo4j Enterprise/Aura** (multi-tenant RBAC), Qdrant, durable task engine, full observability — when load demands.
- **UI beyond integrations:** graph explorer (Neo4j Bloom first; custom React later, Flowsint as reference), dashboards.

---

## Repository layout (Phase 0 scaffolds this)
```
worldmonitor/
├── CLAUDE.md  AGENTS.md  .clinerules        # agent ground truth (mirror; < 200 lines)
├── pyproject.toml  uv.lock  .python-version  .env.example  .pre-commit-config.yaml
├── docs/                                    # THIS plan
├── src/worldmonitor/
│   ├── api/               # FastAPI REST/GraphQL (auth-gated)
│   ├── mcp/               # FastMCP server (the MCP tool surface)
│   ├── authz/             # Zitadel/OIDC, RBAC, capability gating
│   ├── ontology/          # FtM use, wm: extensions, STIX mapping, validation
│   ├── plugins/           # base interfaces + registry; connectors/ enrichers/ resolvers/ rules/ scorers/ notifiers/
│   ├── runner/            # async subprocess + timeout/sandbox; scheduler; stream consumers
│   ├── resolution/        # Splink + nomenklatura; merge audit; review queue
│   ├── graph/             # Neo4j + followthemoney-graph + GDS projections/queries
│   ├── provenance/        # the ledger (doubles as audit log)
│   ├── improvement/       # propose→evaluate→gate→promote; versioned artifacts; rollback
│   ├── llm/               # LiteLLM gateway for service-side LLM use
│   └── settings.py
├── tests/{unit,integration,contract,fixtures}/
├── deploy/{compose.yaml, compose.*.yaml, neo4j/, zitadel/}
├── scripts/{dev,seed_catalog.py,...}
└── vendor-repos/          # READ-ONLY reference clones, gitignored (ftm stack, hermes, flowsint, opencti, mcp lists)
```
*Hermes runs as its own process/container (or on a separate host), configured to reach the MCP server — it is not vendored into `src/`.*

---

## Decisions that gate the roadmap
Resolve the **OPEN** items in [`decisions/`](decisions/) *with the user* as each phase begins —
especially Phase-1 source (A), Integrations UI timing (E), where fine-tuning runs (new), and the
agents' safe auto-tune ranges (new).
