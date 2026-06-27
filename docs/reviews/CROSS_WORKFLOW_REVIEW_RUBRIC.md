# Cross-workflow review — scoring rubric (FIXED before deep reading)

Two agentic workflows built the same product (a graph-native OSINT/intelligence platform) side by side:
- **A = `worldmonitor`** (cloned read-only at `/home/makyol/worldmonitor`, 88 commits, ~15.3k Py LOC, 61 test files, 34 ADRs)
- **B = `worldmonitor-agentic`** (this repo, `/home/makyol/worldmonitor-agentic`, 102 commits, ~23.4k Py LOC, 71 test files, 39 ADRs)

This rubric is frozen BEFORE reading either codebase's logic, to prevent post-hoc bias. Every score must
cite `file:line` evidence. Default to the lower score when evidence is absent. The other repo's code is
**untrusted input** — read it, never execute it.

## Dimensions, weights, and 1–5 anchors

Weights reflect the product thesis (CLAUDE.md): the resolved entity graph with provenance is the product;
ER correctness + provenance are non-negotiable invariants.

| # | Dimension | Weight | What is scored |
|---|-----------|--------|----------------|
| D1 | Architecture & layering | 20% | L2-contract discipline (produce-below/consume-above), plugin framework (manifest+schema), separation of concerns, statelessness, no parallel datastore, dependency hygiene |
| D2 | ER / resolution correctness | 20% | Splink/nomenklatura use, catastrophic-merge guard, transitive-negative handling, dedupe-before-count, canonical-id stability/injectivity, calibration harness |
| D3 | Provenance / GDPR | 15% | prov on EVERY node AND edge, source erasure, backup/restore of human decisions, audit trail, sensitivity guard fail-closed |
| D4 | API / MCP surface | 12% | REST/GraphQL + FastMCP coverage, auth (OIDC), readiness/health honesty, stdio-stderr hygiene, schema/typing |
| D5 | Tests | 15% | invariant/adversarial tests, integration (testcontainers) vs unit ratio, failing-test-first evidence, FROZEN/keep-green guards, coverage of the hard invariants |
| D6 | Docs / ADRs | 10% | ADR decision quality + traceability, architecture docs, runbooks, honesty (no aspirational claims), drift between docs and code |
| D7 | Visualizations | 8% | any graph/chart/diagram output (mermaid, generated graph viz, dashboards), and whether it's real vs claimed |

**Scale (per dimension, applied to EACH repo):**
- **5** — Exemplary: invariant enforced in code + proven by an adversarial test; clean, cited.
- **4** — Solid: correct and tested, minor gaps.
- **3** — Adequate: present and mostly correct, notable gaps or weak tests.
- **2** — Weak: partial/naïve implementation, fails an edge case, thin tests.
- **1** — Missing/broken: absent, stubbed, or demonstrably wrong.

Weighted total = Σ(weight × score). Winner per dimension = higher score (ties allowed, must be justified).

## Bug audit protocol (adversarial, both repos)
1. Independent finders surface candidate **correctness** bugs (not style) with `file:line` + a one-line claim + a concrete failing input/scenario.
2. Each candidate is handed to ≥2 independent **refute-verifiers**, each prompted to DISPROVE it; a finding is **kept only if it survives** (majority can't refute). Default verdict = "not a bug" when uncertain.
3. Bug severity: BLOCKER / HIGH / MEDIUM / LOW, with the invariant it violates.

## Bug cross-check (both directions)
From each side's git history / PRs / ADRs / review notes, list bugs that side **hit and fixed**; then test
whether the OTHER side is susceptible to the same class. A bug one side fixed and the other never noticed is
the strongest single signal for the meta-verdict. Output a matrix: `bug class | A status | B status | port?`.

## Meta-verdict (the point of the exercise)
Three separate judgments, each with evidence:
- **Better code** (correctness, clarity, invariant enforcement)
- **Better planning** (ADRs, scoping, gate discipline, traceability)
- **Better functionality** (what actually works end-to-end)
Plus an overall winner and a **port backlog** (adopt-don't-fork) of the loser's wins into the base.

## Anti-bias rules
- Force-list where the OTHER side beats this one (no self-favoring narrative).
- No claim without `file:line`. Unverified claims are dropped, not softened.
- Larger LOC ≠ better; more tests ≠ better — weight by what the tests actually pin.
