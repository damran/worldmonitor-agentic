# WorldMonitor — Gate & Audit-Gap Completion Ledger

One consolidated record of every gate and Phase-1 audit gap: **what it was → the ADR
that owns the decision → status → the tests that prove it.** Source material:
`docs/reviews/PHASE_1_AUDIT.md` (the gaps), `docs/decisions/` (ADRs 0001–0031), and the
test suite. Companion to `docs/ARCHITECTURE_REVIEW.md` (latent-issue hunt).

Status legend: **CLOSED** (built + proven) · **OPEN** (tracked debt, scheduled) ·
**DEFERRED** (a named later gate, locked decision) · **BY DESIGN** (intentional v0) ·
**SUPERSEDED** (a later locked decision tore it out — kept for the historical record).

---

## 1. Phase-1 audit gaps (`PHASE_1_AUDIT.md`, Q2)

| Gap | What | ADR | Status | Proof / tests |
|---|---|---|---|---|
| **G1** | Provenance not written on **edges** (GDPR/audit invariant broken for relationships) | 0018 | **CLOSED** | `graph/writer.py` stamps `prov_*` on every relationship; `tests/integration/test_edge_provenance.py`, `test_graph_writer.py` |
| **G2** | Edge **referent-rewriting** for merged-away ids not done (orphaned edges after a merge) | 0025 | **CLOSED** (batch) | `resolution/referents.py` rewrites entity-typed values to canonical before write; `tests/integration/test_referent_rewriting.py`, `tests/unit/test_referents.py` |
| **G3** | Abstract `Thing`-range entity-links not materialised (`Sanction.entity` etc. dropped) | 0023, **0046** | **CLOSED** (Gate D) | Thin `graph/ftmg_fork/` override re-keys both drop sites (`generate_entity_links`, `generate_edge_entity`) onto `prop.type == registry.entity` with an `ENTITY_LABEL` fallback; a never-ingested target is MERGEd + tagged `:Ghost`. `tests/test_abstract_edge.py` (7 cases incl. ghost/idempotency/G1/contraction), inverted `tests/integration/test_entity_link_materialization.py` |
| **G4** | No two-tenant same-canonical-ID test; resolver leaked across tenants (the **D1** regression) | 0017, **0028**; **0042** | **SUPERSEDED** (D1 / ADR 0042) | Was CLOSED via ephemeral per-batch resolver + `test_tenant_isolation.py`. **D1: single-tenant** (ADR 0042 supersedes 0017) tore out `tenant_id` entirely — tenant isolation is no longer a property to prove, so the two-tenant test was removed with the teardown. The ephemeral resolver is KEPT (see §2) for its B-1 / ADR-0026 role |
| **G5** | Size-threshold guard (`>10`) untested at the boundary | 0020 | **OPEN** (nice-to-have) | guard eval `resolution/review.py`; no 11-member boundary test yet |
| **G6** | Sensitive-topic guard is a hardcoded **denylist** (fails open for unmodelled topics) | 0020, **0047** | **CLOSED** (Gate E) | Denylist → **deny-by-default**: `guard/sensitivity.py` evaluates topics-first (`registry.topic.RISKS`, 28 codes programmatic + off-ontology⇒sensitive; legacy literal deleted) → Stage-2 k-hop graph sensitivity (Neo4j, Configuration-derived risk labels, `:Ghost`-excluded, `[*1..k]` inlined-validated-int) → Stage-3 Chow (1970) abstain band over `ResolvedCluster.score`; reason-scoped approved-group exemption fence (k-hop/Chow flags non-stale-exemptible). `tests/unit/test_sensitivity_guard*.py`, `tests/integration/test_sensitivity_guard*.py`. |
| **G7** | Expert-set Splink weights / fixed thresholds (uncalibrated) | 0016, **0043** | **MEASUREMENT CLOSED** (Gate A) / promotion OPEN | Gate A built the harness (`resolution/eval.py`: B³/CEAFe/`over_merge_rate`, gold set, EM *candidate*) — over-merge is now measurable. Promoting a calibrated threshold / EM weights into the live path is the person-affecting, human-sign-off slice-2 (still OPEN). |
| **G8** | Batch-bound ingest (`collect()` to exhaustion, one commit, no dead-letter) | 0027 | **CLOSED** | windowed commits + wall-clock/record bounds + `ingest_dead_letter`; `tests/integration/test_ingest_runner.py`, `tests/unit/test_settings.py` |
| **G9** | Whole-queue batch ER (loads all pending, all-pairs) | 0026 | **CLOSED** (batch-first) | bounded windows per `RESOLVE_BATCH_SIZE`; `tests/integration/test_resolution_batching.py` |
| **G10** | Enricher output not re-validated before write | — | **OPEN** (pre-phase-3) | `resolution/pipeline.py` enrich path; external enrichers not in scope yet |
| **G11** | Landing `ensure_bucket` swallows `ClientError` (hides misconfig) | — | **OPEN** (nice-to-have) | `storage/landing.py`; flagged in `ARCHITECTURE_REVIEW.md` |
| **G12** | Settings ship empty placeholders (boots `/health` without a stack, fails loud on use) | — | **BY DESIGN** | `settings.py`; `tests/unit/test_settings.py`, `tests/unit/test_api_health.py` |

---

## 2. The runway (build gates)

The vertical slice built one gate at a time, each green on quality + security +
integration with independent adversarial review.

| Gate | What | ADR | Status | Proof / tests |
|---|---|---|---|---|
| **G1 provenance** | `prov_*` on every node **and** edge | 0018 | **CLOSED** | `test_edge_provenance.py`, `test_graph_writer.py` |
| **G4 isolation** | App-layer composite `(id, tenant_id)` keys; two-tenant proof | 0017; **0042** | **SUPERSEDED** (D1 / ADR 0042) | Built + proven, then torn out: D1 made the system single-tenant, so the composite keys reverted to native `{id}` and `test_tenant_isolation.py` was removed with the teardown |
| **G2 referent rewriting** | Redirect merged-away ids to canonical before the write | 0025 | **CLOSED** | `test_referent_rewriting.py` |
| **resolve_pending (G9)** | Batch-first drain in bounded windows | 0026 | **CLOSED** | `test_resolution_batching.py` |
| **Ephemeral per-batch resolver** | Private in-memory nomenklatura resolver per `cluster_and_merge` call (no shared ledger) | 0026, 0028 | **CLOSED** | `test_resolution.py` — KEPT for B-1 crash recovery + ADR-0026 batch purity. (Its original G4 / D1 tenant-leak framing is moot under single-tenancy — ADR 0042 §3.) |
| **run_ingest (G8)** | Windowed commits + bounded collection + dead-letter | 0027 | **CLOSED** | `test_ingest_runner.py` |
| **ER-streaming Gate A** | Long-running asyncio driver; cadence; ACTIVE-refusal; idempotent enqueue | 0029 | **CLOSED** | `test_ingest_driver.py`, `test_connector_instance.py` |
| **Alembic migrations** | In-package baseline + delta; adopt pre-Alembic DBs; drift guard | 0030 | **CLOSED** | `test_migrations.py` (fresh ≡ create_all ≡ adopted; `alembic check`) |
| **Return-to-block + sign-off** | `block` default; durable judgements; approve/reject CLI | 0031; **0042** | **CLOSED** | `test_signoff.py` (consumption + approve/reject + accretion re-park), `test_settings.py`. (Judgement tenant-scoping dropped under D1 — ADR 0042 §"Notes on adjacent ADRs"; the approve/reject state machine is unchanged.) |
| **Smoke-run harness** | Driver launcher + read-only metrics + runbook (operator-run) | 0029 | **CLOSED** (build) | `test_driver_wiring.py`; `docs/runbooks/smoke-run.md` |
| **Gate B-front — stable canonical ids** | Anchor-preferred durable id (QID>LEI>regNo>taxNo) + append-only `canonical_id_ledger` (alias) + adopt/merge survivor; `wmc-` demoted to an idempotency fingerprint (durable id derives from it in no path) | **0044**; extends 0036/0039, depends 0037 | **CLOSED** (slice-1) | `test_stable_id.py`, `test_stable_id_graph.py`, `test_canonical.py`; ledger migration `0006`. Cross-batch singleton graph re-key + sensitive-canonical split-via-sign-off are slice-2 / ADR-0019-deferred. |
| **Gate C — value-level provenance** | `StatementEntity` per-claim fusion (3-source merge keeps 3 lineages, not `source[0]`) + two-tier witness model (Tier-1 `prop_sources` map always; Tier-2 reified `(:Statement)-[:FROM_SOURCE]->(:Source)` allowlist-only) + source-scoped `delete_source` | **0045**; deepens 0018 | **IN PROGRESS** (slice-1) | `test_provenance_merge.py` + witness/reification/delete-source suites. Value-set-invariance fence (lineage added, values unchanged); `delete_source` value-*retraction* is sign-off-gated, not shipped. |
| **Gate 2a — graph-read REST API** | Auth-gated, read-only, bounded REST over the resolved graph: `GET /entities/{id}`, `/neighbors` (hops clamped ≤4), `/provenance`, `/paths` (`find_paths` shortestPath, max_hops clamped ≤4, `LIMIT 50`); ids are bound params + shape-validated (422 pre-query); Neo4j client injectable into `create_app` (no eager connect). GraphQL / raw-Cypher / MCP (slice 2b) deferred. | **0062** | **CLOSED** (slice 2a) | `tests/unit/test_api_graph.py` (auth-gate, 404, hop-clamp, injection-reject), `tests/integration/test_api_graph_read.py` + `test_graph_queries.py::find_paths*` (testcontainer Neo4j; read-only/bound-param proof). Follow-ups: `get_neighbors` result-LIMIT; GraphQL/raw-Cypher/caching (ADR 0062 deferred). |

---

## 3. Deferred surfaces (locked, not built)

These are intentional later gates with their seams left visible in code (see
`ARCHITECTURE_REVIEW.md` §6). **Not to be built without an explicit go** (Gate B/C/S4
are gated on a named real-time consumer / explicit incremental-ER decision).

| Surface | What is deferred | ADR | Why now |
|---|---|---|---|
| **Gate B** (back half) | Incremental / cross-batch ER (cross-batch dedup). The **stable-canonical-ids front half is BUILT** (ADR 0044 / Gate B-front, §2). | 0019, **0044** | F0: no real-time consumer; batch cadence covers downstream |
| **Gate C-rewrite / cross-run referent surface** | Persisted cross-run referent rewriting / graph-mutation surface; inbound-edge restore on sign-off. (Renamed from "Gate C" to avoid collision with the §2 "Gate C — value-level provenance" / ADR 0045 — unrelated gate.) | 0023, 0025 | append-only locked; reconstructable from retained landing + queue |
| **S4** | First-class canonical-canonical merge routing | 0031 | routed *through* the guard for now (never auto-fuse two canonicals) |
| **X1** | STREAM cursor / checkpoint | (runway) | no STREAM connector in scope |
| **X2** | Driver lease / HA (replace single-node startup stale-reset) | 0029; **0042** | **MOOT under D1** (ADR 0042): a single-tenant single-node driver needs no HA lease; revisit only if a managed-cloud tier reintroduces concurrency |
| **X3** | ~~Single-writer-per-tenant~~ single-writer (advisory lock / `SKIP LOCKED`) | 0029; **0042** | **MOOT under D1** (ADR 0042): with one tenant the per-tenant resolution loop is gone and the single-node lock holds; surface only under future concurrency |

---

## 4. Summary

- **Closed:** G1, G2, G8, G9 (audit blockers + phase-2 pay-downs) and the full
  runway (referent rewriting → batch resolution → bounded ingest → driver → migrations →
  return-to-block sign-off), each ADR-backed and test-proven.
- **Superseded (D1 / ADR 0042):** G4 and the G4-isolation runway row — built + proven, then
  deliberately torn out when **D1: single-tenant** removed `tenant_id` everywhere (ADR 0042
  supersedes 0017). The ephemeral per-batch resolver survives the teardown for its B-1 / ADR-0026 role.
- **Open debt (scheduled):** G7 (promotion half), G10 (phase-3), G5, G11 (nice-to-have).
  (G3 CLOSED by Gate D / ADR 0046; G6 CLOSED by Gate E / ADR 0047 (topics-first → k-hop → Chow band);
  G1/G2/G8 + audit B-1/B-2/B-3/H-1/H-2/H-3 CLOSED; G4 superseded by single-tenancy.)
  Several are re-confirmed with fresh file:line evidence in `ARCHITECTURE_REVIEW.md` §7.
- **Deferred (locked):** Gate B / Gate C / S4 — none built; each gated on
  an explicit decision. (X2 / X3 were single-tenant-conditioned forks, now moot under D1 — ADR 0042.)
  (**X1 / STREAM cursor is now CLOSED** — see §5, ADR 0070.)

---

## 5. Phase-2 gates (ADRs 0060–0072) — **the API/MCP read surface + Integrations UI + live/stream/active connectors**

Phase 2 is **COMPLETE** (2026-06-28, master `ae50874`). Each gate: failing-test-first → build →
adversarial verification → green CI → self-merge. The adversarial fleet caught a real security bug
on every sensitive slice (token-in-URL log leak; source-visible-key auth bypass; PKCE-off-in-prod;
CSRF 500) — each fixed before merge.

| Gate | What | ADR | PR | Status |
|---|---|---|---|---|
| M-3 / M-1 / M-2 | Stage-0 safety: fail-closed `MERGE_GUARD_MODE`; node-provenance integrity; loopback-bind stores + placeholder-secret validator | —/0060/0061 | #114/#115/#116 | **CLOSED** |
| Stage-1 decisions | ADR 0019 (periodic re-batch) ACCEPTED · H-8 transport = Prometheus `/metrics` · G7 blocker recorded | 0019/0054 | #118 | **CLOSED** |
| 2a / 2b | graph-read **REST** + **FastMCP stdio** over a shared bounded/parameterized guard layer | 0062/0063 | #119/#120 | **CLOSED** |
| 0064 | `get_neighbors` result LIMIT + self-clamp (read-surface hardening) | 0064 | #121 | **CLOSED** |
| 3a / 3b | **RestApiConnector** + **OpenCorporates**; **FeedConnector** (RSS/Atom → FtM `Article`) | 0065/0066 | #122/#123 | **CLOSED** |
| 3c | **Notifier** plugin type + **TelegramNotifier** | 0067 | #124 | **CLOSED** |
| 4a / 4b | Browser **session auth** (Zitadel OIDC + dual-path middleware); **Integrations UI** (HTMX/Jinja2 catalog + schema forms + save/enable/status + Run) | 0068/0069 | #125/#126 | **CLOSED** |
| 5 | **StreamConnector** (Bluesky Jetstream) + the **G8** cursor/resume protocol (closes audit **X1**) | 0070 | #127 | **CLOSED** |
| 6a / 6b | **Active-capability gating**: scope token + operator-run + audit + `CliToolConnector` + **whois/dig** (run) + **nmap** (execution-gated until a container sandbox) | 0071/0072 | #128/#129 | **CLOSED** |

**Open after Phase 2 (Stage-4 backlog; see `docs/40_ROADMAP.md` "Next"):** ~~H-4 Abjad ER~~ (**CLOSED** §6),
H-8 remaining halves + `/metrics`, the container/egress sandbox (unlocks nmap), the MEDIUM/LOW sweep
(#105, M-5, M-6, wikidata-via-`guarded_stream`, …), and **G7** promotion (**BLOCKED** on ground-truth labels).

---

## 6. Stage-4 hardening gates (ADRs 0073+) — **paying down the deferred hardening**

Same gate discipline as Phase 2: failing-test-first → build → adversarial verification → green CI →
self-merge. Invariant-touching gates carry a mandatory `@given` property test in `tests/property/`.

| Gate | What | ADR | PR | Status |
|---|---|---|---|---|
| H-4 | **Abjad (Arabic/Persian) name normalization** — strip harakat/tashkeel + tatweel before `fingerprints.generate` in `_name_fingerprint`, so the SAME abjad name with vs. without short-vowel marks projects the SAME `name_fp` ER key (closes the deferred ADR-0035 abjad sub-case). 0.92 threshold + catastrophic-merge guard + sensitive-park **UNCHANGED**; pure deletion ⇒ no over-merge + strict no-op on non-abjad. `LogicV2` re-scorer + ʿayn-splitting **still deferred**. | 0073 | #131 | **CLOSED** |
| H-8a | **Auto-hard-disable after N consecutive failures** — after `ingest_max_consecutive_failures` (default 10, `0`=off) consecutive ingest failures the driver flips the instance to a terminal `status="error"` instead of ADR-0054's retry-forever; failure stays visible, operator re-enables from the UI. Extends ADR 0054 (its named follow-up); streak reused from `task_run` (no schema change). Non-person-affecting. First of the H-8 remaining halves. | 0074 | #132 | **CLOSED** |
| H-8b | **Periodic maintenance cadence + resolve wall-clock timeout + lock-skip escalation** — the two retention prunes (`prune_task_runs` + `prune_dead_letters`) move from startup-only into a periodic in-loop `maintenance_cadence_seconds` gate (first tick fires ⇒ boot prune preserved; `recover_stale` stays startup-only, NOT wrapped); `resolve_pending` grows a cooperative between-batch wall-clock deadline (`resolve_timeout_seconds`, `<=0` off) reporting `ResolveStats.stopped_reason` (per-batch commit ⇒ no work lost, remainder resumes next tick); `run_resolution` escalates info→WARNING after `resolve_lock_skip_alert_threshold` consecutive lock-skips + an `asyncio.wait_for` loop-liveness backstop (abandon-not-kill). No schema change; non-person-affecting (scheduling/liveness only — same guard/threshold/sign-off on every merge). | 0075 | #133 | **CLOSED** |
| H-8c | **Prometheus `/metrics` exporter on the driver** — a read-only, on-scrape `prometheus_client` collector (served by `start_http_server(driver_metrics_port)` on a daemon thread started ONCE at the top of `run_forever`; `0` disables) makes the H-8a/H-8b signals scrapeable: `instances_in_error` (ADR 0074), `resolve_consecutive_lock_skips` (in-memory, ADR 0075 D3), `resolve_last_stopped_reason` (ADR 0075 D2), plus the reused `smoke_metrics` queue/dead-letter/parked/task/graph counts (shared `collect_snapshot` ⇒ no CLI↔/metrics drift). Counts/gauges only — no entity/person data; in-network only (no host publish); the Prometheus server + alert rules stay external/ops (ADR 0054). No schema change; non-person-affecting. Closes the last H-8 half. | 0076 | #134 | **CLOSED** |
| Sandbox sidecar (Slice 1+2) | **App seam + ContainerRunner + sidecar service behind the default-off flag** — settings `sandbox_runner_url` + `sandbox_runner_secret` (SecretStr; NOT in `validate_production_secrets`, ADR 0061 frozen); the app-side `make_container_runner` (a `Runner` that POSTs `{argv-LIST, timeout}` + the `X-Sandbox-Secret` header to the sidecar and maps the base64 JSON back to a `RunResult`, fail-loud on transport/HTTP/malformed); `operator_run` flips the heavy-tool gate from refuse-only to refuse-unless-enabled-AND-configured-else-route (INV-1 flag-off refuse UNCHANGED · INV-2 enabled-but-unconfigured STILL refuse · INV-3 route via the sidecar, replacing the host runner); the sidecar SERVICE (`create_sandbox_app`: `POST /run` + `/health`, constant-time secret, INDEPENDENT allowlist `{nmap,dig,whois}` + no-shell argv + bounded timeout, then `run_command`). No shell end-to-end; no schema change; flag default-off ⇒ no prod behaviour change. **Slice 2 (deploy + argv hardening):** a dedicated `FROM runtime AS sandbox-runner` Dockerfile stage apt-installing nmap/dnsutils/whois (api/driver image stays slim/apt-free, INV-3); an isolated `sandbox-runner` compose service on `sandbox_net` **only** (OFF the stores' `default` network ⇒ cannot reach postgres/neo4j/minio/redis/zitadel — egress isolation), non-root + `read_only` + tmpfs + mem/pids/cpus/ulimit bounds + a stdlib `/health` healthcheck + NO host port (api/driver join BOTH networks + carry `SANDBOX_RUNNER_URL`/`SANDBOX_RUNNER_SECRET`); and a per-tool DEFAULT-DENY EXACT argv template in the sidecar validator (`argv[:-1]` must equal `nmap(-oX,-,--)`/`dig(+short,--)`/`whois(--)`, last token a host/IP — closes `--script`/`-oN`/`-iR`/`-iL` AND option-with-argument recombinations like `nmap -oX -- <target>`). Egress = Docker **network isolation** (ADR 0077 §D4 refinement); nftables metadata/RFC1918 denial deferred. | 0077 | #135 + #136 | **CLOSED** (Slice 1 app seam + Slice 2 deploy/egress + argv hardening both landed) |
| H-8c follow-up — Prometheus scrape + alert rules | **In-repo scrape config + alert rules making the H-8a/H-8b signals pageable** — `deploy/prometheus/prometheus.yml` (global 30s scrape/eval, job `worldmonitor-driver` → `driver:9108` port-coupled to `driver_metrics_port`); `deploy/prometheus/alerts/worldmonitor.rules.yml` (7 alerts: 2 critical `DriverDown`/`ResolutionWedged`, 5 warning `ConnectorInstanceHardDisabled`/`ResolvePassTimingOut`/`ErQueueBacklogHigh`/`IngestDeadLettersPresent`/`MergesParkedForReview`); a `promtool test rules` fixture; an opt-in `prometheus` compose service (`profiles:[monitoring]`, default-off, loopback-only 9090, on `default` not `sandbox_net`); INV-PARITY test (metric names derived dynamically from `collector.py` — a rename/removal breaks the test immediately). No schema change; non-person-affecting; no new runtime dep. Closes ADR 0075's revisit trigger ("WARN can be paged"). | 0078 | #138 | **CLOSED** |
| G7 slice-2 — canonical-anchor silver labels | **Non-circular ER label source unblocking G7** — `resolution/silver.py::build_silver_pairs` derives `er_gold_pair` labels from canonical anchors ALONE: POSITIVE = same value of a same canonical-ID property ({wikidataId, leiCode, registrationNumber, ogrnCode, innCode, swiftBic, isin, okpoCode, permId}) shared across **≥2 DISTINCT sources** (`get_provenance().source_id`); NEGATIVE = conflicting same-type anchors; CONTRADICTION (positive on P + negative on Q) dropped; ABSTAIN otherwise. Tagged `source="canonical_silver"`, `clerical_score=None`. **N1 non-circularity BY CONSTRUCTION** (entities-only signature — no score/probability/threshold/linker param, never calls `score_pairs`); N2 pure fn of (anchors, source_id); N3 write-boundary guard in `persist_silver_pairs`. Append-only persist (`ON CONFLICT DO NOTHING` ⇒ never overwrites human gold). No schema change (`source` is `String(32)`). **Measurement labels ONLY — no merge/threshold/person-affecting change; promotion stays human-sign-off-gated (separate slice).** `@given` property test (P1 non-circularity / P2 positive / P3 negative / P4 abstain / P5 structural) + example test; 28 tests. | 0079 | #139 | **CLOSED** (slice 2) |
| G7 slice-3 — external-benchmark FLOOR | **Independent external sanity floor for the ER matcher** — `resolution/benchmark.py`: `BenchmarkPair` common shape; `load_os_pairs` (line-JSON OS-Pairs parse, local-path or in-memory iterable, judgement `"positive"→"match"/"negative"→"non_match"`, others skipped, FtM-native via `make_entity`); `fetch_os_pairs` (pull-only download-on-demand, CC BY-NC attributed, bulk file `.gitignored` + never committed); `load_febrl` (lazy `recordlinkage` import, clear error when absent, `_febrl_record_to_entity` mapper split out for hermetic testing); `identity_keys` (entity.id ∪ `silver.ANCHOR_PROPERTIES` values — single source of truth); `drop_contaminated` (LOAD-BEARING: drop + count every pair overlapping our silver/gold partition, log warning, `len(kept)+n_dropped==len(input)` invariant, no silent truncation); `evaluate_floor` (runs decontamination first, `score_fn` **injected** INV-IMPORT-PURITY, `precision/recall/f1` via `eval._harmonic_mean`, `over_merge_rate=FP/(TP+FP)`, returns `FloorMetrics`). `recordlinkage` in optional `benchmark` dep group (folded into `dev`; never a runtime dep). **No schema change, no migration, no er_gold_pair write, no threshold/EM-weight/merge.py/graph change. Floor is returned in-memory; sanity-only; promotion remains human-sign-off-gated.** `@given` property tests: P-GUARD-SOUND (3 sub-properties), P-GUARD-EMPTY, P-JUDGEMENT-TOTAL, P-FLOOR-MATH (oracle/inverted-oracle/general-OMR), P-CONTAM-IN-FLOOR (2 sub-properties), P-IMPORT-PURITY (2 sub-properties); + 28 unit tests. 48 tests total. | 0080 | — | **CLOSED** (slice 3) |

| MEDIUM/LOW sweep — Wikidata via `guarded_stream` | **Route `WikidataEnricher._lookup_qid` through the SSRF guard** — the only enricher/connector that called `httpx.get(...)` directly, bypassing ADR 0057. Extended `guarded_stream` with an optional backward-compatible `headers: Mapping[str, str] \| None = None` kwarg (threaded into both the injected-transport and production paths; SSRF host validation unchanged on every hop). `_lookup_qid` now builds a URL with query-string params baked in via `urllib.parse.urlencode` and calls `guarded_stream("GET", url, headers=_SPARQL_HEADERS, ...)` (Wikimedia UA policy complied with). Added `transport: httpx.BaseTransport \| None = None` ctor param to `WikidataEnricher` for `httpx.MockTransport` test injection (mirrors `RestApiConnector`/`FeedConnector`). `BlockedAddressError` added to the best-effort `except` clause. 11 new unit tests: 5 for the `guarded_stream` headers extension (including 2 SSRF-not-weakened proofs), 6 for the enricher (transport injection, no-`httpx.get`, UA header forwarded, empty-result, error-swallowed). No schema change; non-person-affecting. | 0081 | #141 | **CLOSED** |
| MEDIUM/LOW sweep — suffix-match allowlist | **Wildcard-subdomain entries in `allowed_targets`** — extends `CliToolConnector.collect()` (ADR 0072 §2) to support `*.<domain>` entries: a target T matches iff T ends with `"." + domain` (strict subdomain, dot-boundary anchored). The apex, siblings (no-dot), and suffix-spoofs are all refused. Exact-match entries unchanged. Malformed wildcards (`*.` empty domain, `*.*.x` nested star) match nothing — no catch-all bypass. Extracted into a pure `_target_allowed(target, allowed) -> bool` helper. No schema change (connector schemas use `{type: string}` only, no pattern constraint to loosen). 22 new unit tests in `tests/unit/test_cli_tool_allowlist_wildcard.py` (7 adversarial security-axis parametrized cases pin the dot-boundary invariant). No migration; non-person-affecting. | 0082 | #142 | **CLOSED** |
| MEDIUM/LOW sweep — M-6 landing-zone orphan GC | **Reference-based GC for landing-zone orphans + disk-growth signal** — `runner/gc.py::gc_landing_orphans` lists landing objects (`list_objects_with_metadata`), builds the referenced-URI set from `ErQueueItem.source_record` ∪ `IngestDeadLetter.source_record`, and treats an object as an orphan iff UNREFERENCED **and** older than the `landing_gc_min_age_seconds` grace window (race closure for put-before-commit; a no-`LastModified` object is treated as recent). NOT a TTL — landing bytes are provenance and must persist (reference-based, ADR 0083). **Report-only by default**: `landing_gc_enabled`=False (master, pass never runs) + `landing_gc_delete_enabled`=False (deletion opt-in); the disk-growth signal (orphan count + bytes) is ALWAYS computed and exposed via 3 new gauges (`worldmonitor_landing_objects`/`_orphans`/`_orphan_bytes`). Deletion (when enabled) batches ≤1000 via `delete_keys`, fail-loud on a partial `Errors` array (mirrors `delete_prefix`). Wired into `run_maintenance` (ADR 0075); URI built identically to `LandingStore.put` so a referenced object can't be misclassified. Deterministic-key invariant test = the real orphan PREVENTION. No schema change; non-person-affecting; default-off ⇒ no behaviour change. MinIO integration test (orphan deleted; referenced + recent survive) + unit tests. | 0083 | #143 | **CLOSED** |
| MEDIUM/LOW sweep — M-5 online-migration safety | **Dialect-aware `lock_timeout` guard + migrate-while-stopped runbook** — `db/_migration_guard.py::apply_migration_timeouts(connection)` issues `SET LOCAL lock_timeout = '<N>ms'` (and optionally `statement_timeout`) on Postgres BEFORE every migration batch in `env.py::_run`, inside the migration transaction. `SET LOCAL` scopes the GUC to the transaction only — reverts on commit/rollback, no bleed onto the shared app connection. **Dialect-aware**: silently skipped on non-Postgres dialects (SQLite etc.) where `SET LOCAL` is invalid syntax. Default `migration_lock_timeout_ms=3000` (3 s fail-fast); `0` = opt-out (Postgres default: no timeout). `migration_statement_timeout_ms=0` (off by default — lock_timeout is the key guard). Closes M-5: a migration that cannot acquire its `ACCESS EXCLUSIVE` DDL lock FAILS FAST instead of stalling the driver's enqueue path. Runbook `docs/runbooks/migrations.md`: migrate-while-stopped procedure, rollback procedure, online-safe patterns (`CREATE INDEX CONCURRENTLY`, `NOT VALID`+`VALIDATE`) with explicit call-out that `transaction_per_migration=True` is needed and is DEFERRED (ADR 0084 D1-DEFERRED). No schema change; non-person-affecting. 15 unit tests + 4 integration tests (real Postgres connection; `SHOW lock_timeout` assertion; SET LOCAL revert proof; opt-out proof; full `migrate_to_head` with guard active). | 0084 | — | **CLOSED** |

**Still open (Stage-4):** the ~~rest of H-8~~ (all three remaining halves — ~~periodic maintenance
cadence~~ · ~~resolve wall-clock timeout + lock-skip escalation~~ ADR 0075 · ~~Prometheus `/metrics`
H-8c~~ ADR 0076 — **all CLOSED** §6), ~~the container/egress sandbox~~
(unlocks nmap — **Slice 1 app seam + Slice 2 deploy/egress + argv hardening both landed, CLOSED**, ADR 0077), ~~Prometheus scrape + alert rules (ADR 0078, H-8c follow-up — CLOSED)~~, ~~wikidata enricher via `guarded_stream` (ADR 0081 — CLOSED)~~, the remaining MEDIUM/LOW sweep (#105, M-5, M-6, dig/nmap richer
FtM map, suffix-match allowlist), and **G7** threshold promotion — the **non-circular label on-ramp is
UNDER WAY** (~~slice-2 canonical-anchor silver labels, ADR 0079~~ **CLOSED**; remaining: external-benchmark
floor, label-sufficiency report, real-seed corpus run), but **promotion itself stays human-sign-off-gated**
(person-affecting — never promote off circular evidence).
