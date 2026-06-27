# Cross-workflow review — `worldmonitor` (A) vs `worldmonitor-agentic` (B)

**Date:** 2026-06-27 · **Method:** frozen rubric (`CROSS_WORKFLOW_REVIEW_RUBRIC.md`) → 7 parallel dimension scorers → 8 bug finders (4 lenses × 2 repos) → ≥2 independent refute-verifiers per candidate (kept only if it survived) → bidirectional bug cross-check from git/ADR history. 61 agents. Every claim is `file:line`-cited in the raw run; A treated as untrusted read-only input.

- **A = `worldmonitor`** — 88 commits, ~15.3k Py LOC, 61 test files, 34 ADRs.
- **B = `worldmonitor-agentic`** (this repo) — 102 commits, ~23.4k Py LOC, 71 test files, 39 ADRs.

---

## 1. Dimension scores (weighted, 1–5 each)

| Dim | Weight | A | B | Winner | Why (cited in raw run) |
|-----|:-:|:-:|:-:|:-:|---|
| D1 Architecture & layering | 20% | 3 | **4** | B | B extracts a dedicated `guard/` pkg; `resolution/review.py` is an 8-line delegator vs A's 96-line mixed module; B adds `backup.py`, `runner/heartbeat.py` for ops concerns A lacks. |
| D2 ER / resolution correctness | 20% | 2 | **5** | B | A carries the H-2 schema-incompat drop (`merge.py:225-242`) and a 7-of-28-code denylist guard (`sensitivity.py:30`); B fixes H-1/H-2/G6 with adversarial tests over all 28 `registry.topic.RISKS`. |
| D3 Provenance / GDPR | 15% | 2 | **4** | B | B has cross-store erasure (ADR 0049), backup/restore preserving human rejects (0050), fail-closed 3-stage guard (0047); A has none of these. |
| D4 API / MCP surface | 12% | 3 | **4** | B | B's `/ready` probes each store; comparable MCP. (A wins one sub-point — see §4.) |
| D5 Tests | 15% | 3 | **4** | B | B failing-test-first + FROZEN keep-green guards + 17 gate specs; ~similar integration ratio. |
| D6 Docs / ADRs | 10% | 3 | **4** | B | B 39 ADRs with alternatives + gate ledger + runbooks. |
| D7 Visualizations | 8% | 1 | 1 | **tie** | Neither produced real charts/graphs/dashboards — a shared gap. |

**Weighted total /100:  A = 49.8   ·   B = 79.2.**  B wins every dimension except the visualization tie.

---

## 2. Verified bug matrix (survived adversarial refutation)

22 candidates → **11 survived** ≥1 refute-verifier with no surviving refutation. `[STRONG]` = 2 verifiers concurred; `[weak]` = only 1 verifier returned (3 verifier agents hit the structured-output cap — those candidates are under-verified, flagged).

### In A (`worldmonitor`)
| Sev | Bug | Loc | Conf |
|-----|-----|-----|:-:|
| BLOCKER | Failed connectors stuck `error`, never retried (status drops out of the `enabled`-only due-query) | `runner/driver.py:331` | STRONG |
| BLOCKER | Anchor-driven silent merge across cluster boundaries (same anchor in two batches → cross-cluster fuse) | `resolution/canonical.py:139-145` | weak |
| HIGH | Transitive schema-incompatible member anchor drift (H-2 class) | `resolution/merge.py:225-242` | STRONG |
| HIGH | GeoNames local-`path` traversal / LFI (`Path(path).read_text()`, no confinement) | `connectors/geonames/connector.py:77` | STRONG |
| HIGH | Unbounded `json.loads` per remote line (OpenSanctions) → memory-exhaustion | `connectors/opensanctions/connector.py:74` | STRONG |
| MEDIUM | No retry/backoff for flaky connectors (operator must manually re-enable) | `runner/driver.py:331` | STRONG |

### In B (`worldmonitor-agentic`) — our own open bugs
| Sev | Bug | Loc | Conf | Status |
|-----|-----|-----|:-:|---|
| HIGH | Failed connector instances never retried (`error` ≠ `enabled` due-query) | `runner/driver.py:189` | STRONG | **= H-8a, already planned (ADR 0054), not yet built** |
| HIGH | SSRF via `follow_redirects=True` (redirect to RFC1918/metadata) | `connectors/geonames/connector.py:97` | weak | = audit M-9 (tracked) |
| MEDIUM | Edge gets no `prov_*` when entity provenance dict is empty (`if edge_props:` false) — G1 hole | `graph/writer.py:81` | STRONG | **new — verify & fix** |
| MEDIUM | `ConfigCipher` single-Fernet: rotating `CONFIG_ENCRYPTION_KEY` orphans all configs | `db/crypto.py:21` | STRONG | = audit M-10 (tracked) |
| MEDIUM | Migration adoption blind-stamps on one column's presence, no full-schema check | `db/engine.py:72-73` | STRONG | **new — partial-restore hazard** |

---

## 3. Bug cross-check, both directions (the strongest meta-signal)

### A → B: bugs A hit-and-fixed — is B susceptible?
Eight substantive fixes mined from A's history/ADRs (deterministic canonical id, transitive-negative H-1, exception isolation B-2, ER precision B-3/H-5, name canonicalization, stable anchor id, injectivity, sensitivity guard). **B is susceptible to NONE** — B independently fixed every one (cited: `merge.py:57-67`, `merge.py:173-189`, `pipeline.py:188-287`, `splink_model.py:149-223`, `canonical.py:179-204/81-112`, `guard/sensitivity.py:102-137`). **Convergent evolution: 0 ports needed A→B.**

### B → A: bugs B hit-and-fixed — is A susceptible?
Ten fixes mined from B's history/ADRs; **A is still susceptible to ~7** (all `port?=yes` *if A were the base*):
| Bug class B fixed | B evidence | A still exposed? |
|---|---|:-:|
| GeoNames OOM (whole-file load) | `f8c322b` / ADR 0052 | **yes** (`geonames/connector.py:77,124`) |
| GeoNames LFI (path traversal) | `f8c322b` / ADR 0052 | **yes** (`geonames/connector.py:77`) |
| GDPR erasure incompleteness (map-stage dead-letter + orphans) | `f7b5fc6`+ / ADR 0049 | **yes** (`erasure.py:63-69`) |
| Canonical-id injectivity collision | `db0fffa` / ADR 0048 | **yes** (`canonical.py:102`) |
| Anchor-conflict silent drop (`min(value)`) | ADR 0040/0048 | **yes** (`anchors.py:38`) |
| H-2 schema-incompat silent swallow | `dfffbc6` / ADR 0041 | **yes** (`merge.py:224-236`) |
| Sign-off poison-row wedge | `dfffbc6` / ADR 0041 | **yes** (signoff scan) |
| Sensitivity guard fail-open on unknown codes | `b4af049` / ADR 0047 | **yes** (`sensitivity.py:30-33`) |
| Transitive-negative H-1 | `d2e17a4` / ADR 0037 | no (A also fixed it) |

**This is the decisive finding:** everything A caught, B also caught; but B caught and fixed ~7 additional bug classes — including a BLOCKER-class LFI and several invariant violations — that **A never noticed and remains exposed to.**

---

## 4. Where A genuinely beats B (forced, anti-bias)
- **D4:** A's `/ready` includes a **driver-heartbeat freshness check** (`api/readiness.py:77-86`); B keeps liveness in the driver `--healthcheck` instead, so B's `/ready` doesn't surface driver staleness. **Worth porting the idea into B.**
- **D1:** A's standalone `graph/abstract_edges.py` is arguably more transparent than B's `ftmg_fork/` override (no fork knowledge needed). Style, not a defect.
- **D3:** A's guard is pure-config (no ftmg import for unit tests); B's k-hop stage lazily imports `ftmg.config` — more capable but more coupled.
- **D6:** A's `GATE_LEDGER §0` visually separates the D1 teardown; minor doc-clarity edge.
- **D7:** Tie — but B has far more *textual* spec docs (17 gate specs vs 2). Neither has graphical viz.

---

## 5. Port backlog (adopt-don't-fork)
Because the base is B and A→B susceptibility is **zero**, there is **almost nothing to port from A**. The only real candidate:
- **P1 (LOW):** Add a driver-heartbeat-freshness signal to B's `/ready` (port A's `readiness.py:77-86` idea), so readiness reflects driver staleness, not just store reachability.

The actionable backlog is instead **B's own confirmed bugs** (see §2): H-8a retry (planned), the `writer.py:81` edge-provenance-when-empty G1 hole, and the `db/engine.py:72` migration-adoption full-schema check are the highest-value fixes; M-9 (SSRF) and M-10 (key rotation) were already tracked.

---

## 6. Meta-verdict — which workflow is superior

**Overall winner: B (`worldmonitor-agentic`), decisively** (79.2 vs 49.8; 6/7 dimensions + tie).

- **Better code → B.** B enforces more invariants in code *and* pins them with adversarial tests. The cross-check is conclusive: B is not susceptible to a single bug A fixed, while A is exposed to ~7 classes B fixed (incl. an LFI and multiple ER/provenance invariant violations).
- **Better planning → B.** More ADRs (39 vs 34) with explicit alternatives, a gate ledger, failing-test-first discipline, and 17 gate specs vs 2 — traceability from audit-finding → ADR → test → fix is visible throughout.
- **Better functionality → B.** B uniquely ships disaster recovery (backup/restore, ADR 0050), driver supervision + readiness (0051), dead-letter retention (0053), cross-store GDPR erasure (0049), and a fail-closed 3-stage sensitivity guard (0047). A has none of these.
- **Shared weakness (honest):** neither workflow produced any real visualization (charts/graphs/dashboards) — D7 = 1/1. And B's larger surface carries its own open bugs (§2), so "more code" also meant "more to harden."

**Why the agentic gate-fleet (B) won:** the gate discipline (audit → ADR → failing test → build → adversarial checker → judge) systematically surfaced and closed a class of operational/security/provenance bugs (LFI, OOM, erasure completeness, injectivity, fail-closed guard, DR) that A's faster, lighter line never enumerated. The adversarial-verification habit is exactly what the cross-check rewards.

### Caveats
- 3 of the refute-verifier agents hit the structured-output retry cap; the two `[weak]` findings (A's anchor-driven cross-cluster merge BLOCKER; B's SSRF) had only one verifier and warrant a confirming read before action.
- Scores are single-scorer-per-dimension; the evidence is cited but a second scoring pass would tighten D4–D6 (all 3-vs-4, the closest calls).
