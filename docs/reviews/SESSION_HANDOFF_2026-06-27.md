# Session handoff — cross-workflow decision + Phase A/B execution (2026-06-27)

Durable record of this session's outputs. The two-line A/B experiment is **decided**; this repo
(`worldmonitor-agentic`, "B") is the permanent base, and the sibling `damran/worldmonitor` ("A") is to
be archived read-only after a one-time harvest.

## 1. Cross-workflow comparison — decided (do not re-run)

Full report + frozen rubric: `docs/reviews/CROSS_WORKFLOW_REVIEW.md`,
`docs/reviews/CROSS_WORKFLOW_REVIEW_RUBRIC.md` (currently on PR #101, awaiting merge).

- **Decision:** keep **B** as the base; retire **A** to a read-only archive after harvest.
- **Evidence:** both review fleets (including A's own) scored B higher on the correctness-critical
  invariants; a Round-2 file:line cross-examination confirmed A still carries ~6 serious bug classes B
  already fixed (LFI, OOM, erasure completeness, canonical-id injectivity, anchor-conflict, fail-open
  guard), while B carries ~0 that A fixed.
- **Adjudication of A's review of B's code:** every concrete correctness finding A raised about B was
  already fixed in B (verified at file:line). Two honest corrections to B's own prior review were
  conceded: (a) B had **zero** property-based tests (A had a real harness — closed in Phase A below);
  (b) B's prior review overstated A's gaps ("A lacks backup/restore/erasure/guard" was false — A has
  weaker versions). The provenance Tier-2 comparison was left UNCERTAIN → **Phase C** below.

## 2. Phase A — property/metamorphic harness (DONE, PR #102 merged)

Closed B's one real capability gap (zero property tests). Added `hypothesis` + `tests/property/` with
`@given` coverage of five invariant families: canonical-id injectivity, ER-merge laws
(permutation/idempotence/lossless-union), merge-guard fail-closed (incl. real PEP/sub-codes),
provenance-survives-merge (G1), transitive-negative override. **20/20 green** against the current tree —
no hidden bugs surfaced (corroborates Round-2; the fixes hold under fuzzing). Adversarial review caught
and fixed one toothless guard sub-property before merge.

## 3. Phase B — fix B's confirmed open bugs (one gated PR each)

| # | Gate | ADR | PR | Status |
|---|------|-----|----|--------|
| 1 | Driver connector retry with exponential backoff | 0054 | #103 | ✅ merged |
| 2 | Fail-closed edge provenance (G1 hole) | 0055 | #104 | ✅ merged |
| 3 | Migration-adoption full-schema check (partial-restore) | 0056 | #106 | ✅ merged |
| 4 | SSRF-guarded outbound HTTP (M-9, both connectors + CGNAT) | 0057 | #107 | ⏸ pushed, CI green, **UNMERGED** |
| 5 | ConfigCipher key rotation (MultiFernet, M-10) | — | — | **not started** |

Each followed the gate discipline: branch → failing-test-first (RED captured) → build → adversarial
clean-context review → green CI. #1–#3 self-merged on green; #4 is pushed with green CI + a passed
adversarial review but was left unmerged at session end (merge: `gh pr merge 107 --squash --delete-branch`).

Notable: the #2 fix was more impactful than planned — three existing tests were incidentally writing
unstamped edges (relying on the silent G1 violation); all were corrected to stamp. The #4 review found
and closed a CGNAT (`100.64.0.0/10`) SSRF residual and a unit test inadvertently coupled to live DNS.

## 4. Remaining phases (next session)

- **Phase B #5** — ConfigCipher key rotation: rotating `CONFIG_ENCRYPTION_KEY` currently orphans every
  stored connector config. Fix: `MultiFernet` (old keys still decrypt during rotation).
- **Phase C — provenance Tier-2 (GENUINE FORK — ask the human).** A keeps reified
  `:Statement`/`:Source` provenance (`graph/writer.py:259-263`); B is Tier-1 in the live graph (Tier-2
  allowlist-only per ADR 0045, see `graph/ops.py:6-8`). Deep-read both models, then present the human a
  choice: (a) port A's reification, or (b) write an ADR justifying Tier-1 + allowlist-only Tier-2 for the
  current phase with an upgrade trigger. Do NOT assume B's provenance is complete.
- **Phase D** — port A's driver-heartbeat-freshness signal into B's `/ready` (`api/readiness.py`, LOW).
- **Phase E** — consolidation: retire the dual-line apparatus; fold an `@given` property-test
  requirement into the gate template for any gate touching an invariant (ER/canonical-id/merge-guard/
  provenance); make the measurement/calibration harness a validation linchpin (mark
  "validated-on-golden-set" criteria blocked-on-measurement-harness); classify decisions by
  reversibility in ADRs; run the ADR gap-sweep from 0024 onward for load-bearing-but-unvalidated claims.
- **Phase F** — archive `damran/worldmonitor` read-only on GitHub (do NOT delete; confirm with the human
  first). Only after A–E land.

## 5. Follow-ups logged this session (not dropped, not yet gated)

- **Node-side G1 hole** (from #2 review): an unstamped *non-edge* entity still writes a node with no
  `prov_*`, silently — the node half of "provenance on every node and edge". ADR 0055 scoped nodes out.
- **OpenSanctions unbounded `json.loads` per line** (the other half of M-9) — DoS/parse-bomb surface.
- **wikidata enricher** uses bare `httpx.get` (default no-redirect, so no redirect-SSRF, but unguarded);
  route it through `net/ssrf.guarded_stream` too.
- **DNS-rebinding TOCTOU** in the SSRF guard — close via connect-time peer validation (custom transport)
  if it enters the threat model (documented in ADR 0057).
- **Auto-hard-disable after N consecutive connector failures** + **periodic in-loop maintenance cadence**
  + **resolve wall-clock timeout / lock-skip escalation** — the remaining halves of audit H-8 (ADR 0054
  did the retry/backoff half only). The H-8 **alerting/metrics transport** is itself an open design fork
  (Prometheus `/metrics` vs the `plugins/notifiers` surface vs structured logs).

## 6. ADRs created this session
0054 (driver retry/backoff), 0055 (fail-closed edge provenance), 0056 (migration-adoption full-schema
check), 0057 (SSRF-guarded outbound HTTP). All PROPOSED.

## 7. Open PRs at session end
- **#101** — cross-workflow review docs (rubric + report). Unmerged.
- **#107** — Phase B #4 SSRF guard. CI green, adversarial review passed. Unmerged.
