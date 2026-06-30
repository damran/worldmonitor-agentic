# Gate D — promtool-in-CI (alert-rule validation + unit tests)

- **Status:** SPEC (builder-ready)
- **ADR:** [0088](../decisions/0088-promtool-in-ci.md) (PROPOSED)
- **Source:** adversarial review 2026-06-29 of PRs #138–145 — cheap-high-value LOW
  ("promtool-in-CI"). ADR [0078](../decisions/0078-prometheus-scrape-and-alerts.md)
  §Consequences explicitly deferred this; the `.test.yml` fixture shipped but is
  **never executed in CI**, so a broken alert rule or an alert-logic regression ships silently.
- **Touches no runtime invariant** (ER/merge/canonical-id/provenance/sensitivity). CI-only,
  reversible. **No `@given` property test required** (no invariant class touched).

## Problem (verified against the tree)

- `deploy/prometheus/alerts/worldmonitor.rules.yml` — one group, 7 alerts (ADR 0078 D2).
- `deploy/prometheus/tests/worldmonitor.rules.test.yml` — a `promtool test rules` fixture
  (`rule_files` → `../alerts/worldmonitor.rules.yml`, `evaluation_interval: 1m`, `tests:` with
  `alert_rule_test`/`exp_alerts`, fire + no-fire cases for all 7 alerts). Header line 3 literally
  says *"Run manually (promtool not wired to CI …)"*.
- `.github/workflows/quality.yml` — single `quality` job, `ubuntu-latest`, Python/uv stack.
  Nothing invokes `promtool`.

The existing pytest suite (`tests/unit/test_prometheus_*.py`, ADR 0078 D5) checks **metric-name
parity and structure** by parsing YAML — it does **not** run the PromQL engine, so it cannot catch:
a syntactically invalid `expr`, a malformed `for:`/`labels`/`annotations` block that promtool
rejects, or an alert-logic regression where the rendered alert no longer matches the fixture's
`exp_alerts`. `promtool` is the only tool that actually evaluates the rules.

## Scope (exact)

**In scope — CI wiring only:**
- `.github/workflows/quality.yml` — add `promtool check rules` + `promtool test rules` execution.

**Allowed supporting edits (docs/ledger only):**
- `docs/decisions/0088-promtool-in-ci.md` (flip to `accepted` on merge).
- `docs/GATE_LEDGER.md` and/or `docs/40_ROADMAP.md` — record gate close.
- The two-line "Run manually (promtool not wired to CI …)" header note in
  `deploy/prometheus/tests/worldmonitor.rules.test.yml` MAY be corrected to "also enforced in CI"
  (comment-only; no rule/fixture logic change).

**Explicitly OUT of scope (do NOT touch):**
- The alert rules or the test fixture's *logic/assertions* — they are correct and ADR-0078-locked.
  CI must validate them as-is; if CI goes red against the current rules, the **CI wiring** is wrong,
  not the rules.
- `.claude/gate.scope` — **owned by Gate C**, do not edit. (This gate edits only the workflow + docs;
  no scope-glob change is needed for a CI-config gate.)
- `deploy/prometheus/prometheus.yml`, `deploy/compose.yaml`, any `src/` code, any pytest.

## Decision summary (full rationale in ADR 0088)

1. **What runs, both blocking:**
   - `promtool check rules deploy/prometheus/alerts/*.rules.yml` — static lint of every rule file
     (use the glob, not the single filename, so a future second rule file is covered automatically).
   - `promtool test rules deploy/prometheus/tests/*.rules.test.yml` — evaluate the alert logic
     against the fixture (the unit test).
2. **A new dedicated job `alert-rules`** (sibling of `quality`), NOT a step inside `quality`.
   - Justification: promtool is Python-less; bolting it onto the uv/Pyright/pytest job couples an
     ops concern to the Python toolchain setup and serializes ~a few-second download behind the full
     `uv sync --dev`. A separate `ubuntu-latest` job runs in parallel, needs only `actions/checkout`,
     and fails independently with a clear signal. Cost is one extra runner + one tarball download
     (~tens of MB, < ~10s).
   - **Branch protection:** the new job introduces a new status-check name. Branch protection
     currently requires `quality` + `security`. Adding `alert-rules` as a **required** check is an
     admin/settings change the builder cannot self-apply; the builder MUST (a) name the job
     `alert-rules`, (b) note in the PR description that an admin should add `alert-rules` to required
     checks, and (c) confirm the job runs and is green on the PR. Until it is added to required
     checks it still runs and is visible on every PR/push (the workflow `on:` triggers are
     inherited), so a red `alert-rules` is observable at review time even before it is enforced.
3. **promtool acquisition — pinned, checksum-verified release tarball:**
   - Download `prometheus-<VERSION>.linux-amd64.tar.gz` from the official GitHub release
     (`https://github.com/prometheus/prometheus/releases/download/v<VERSION>/…`).
   - **Pin an exact `<VERSION>`** (no `latest`). Builder picks the **current stable Prometheus
     release at build time** (3.x line) and records the exact number in the workflow and in ADR 0088
     §Decision. Verify the tag exists before pinning.
   - **Checksum-verify** the download against the value from that release's `sha256sums.txt`
     (pin the expected SHA-256 literal in the workflow and fail the job on mismatch — `sha256sum -c`
     or an explicit compare). External downloads are hostile (CLAUDE.md): no unverified binary runs.
   - Extract only `promtool` from the tarball; do not add it to the repo (no vendored binary).
   - Do NOT use a third-party `setup-promtool` action — pinned official-release + checksum is the
     supply-chain-clean path and avoids trusting an unaudited Marketplace action.
4. **Blocking:** both commands gate the build (the whole point). Non-zero exit fails `alert-rules`.

## Acceptance criteria

- **AC1 — green on current rules.** With `deploy/prometheus/` unchanged, the `alert-rules` job runs
  `promtool check rules` (glob) and `promtool test rules` (glob) and **passes**. All 7 current
  alerts and their fixture cases validate.
- **AC2 — fails on a broken rule.** A deliberately-broken rule (e.g. a malformed `expr` such as
  `up{job=="x"` with a syntax error, or an `exp_alerts` mismatch) makes the job **fail** with a
  non-zero exit. Demonstrated, not assumed (see §How the builder proves AC2).
- **AC3 — pinned + checksum-verified.** The workflow pins an exact Prometheus `vX.Y.Z` and verifies
  the tarball SHA-256 against a pinned literal; a checksum mismatch fails the job before promtool
  runs. No `latest`, no unverified binary, no third-party action.
- **AC4 — glob, not hardcoded filename.** Both commands target `…/alerts/*.rules.yml` and
  `…/tests/*.rules.test.yml` so a second rule/test file is covered without a workflow edit.
- **AC5 — blocking + parallel.** `alert-rules` is a required-intent status check (admin adds to
  required list), runs as its own job in parallel with `quality`, and a failure blocks merge intent.
- **AC6 — no logic drift.** The alert rules and fixture are byte-unchanged except the optional
  one-line CI header note in the `.test.yml` comment block (AC verified by `git diff` showing no
  change under `groups:`/`tests:`).

## Named proof (no new pytest — CI is the test)

This gate's "test" is the CI job itself plus a one-time local demonstration. There is no new
`tests/unit/*` file (adding one would re-implement promtool in Python, which the gate explicitly
rejects in favour of the real tool).

### How the builder proves AC2 (the negative test)

Before opening the PR, the builder runs locally (or in a scratch branch) the exact commands the
workflow will run, against a **temporarily** broken copy of the rules, and confirms a non-zero exit;
then reverts and confirms green:

```
# pinned promtool already extracted to ./promtool
./promtool check rules deploy/prometheus/alerts/*.rules.yml        # expect: SUCCESS on real rules
./promtool test  rules deploy/prometheus/tests/*.rules.test.yml    # expect: SUCCESS on real rules
# then break one expr in a throwaway copy and re-run -> expect non-zero exit, then discard the break
```

The PR description records the observed output of both (pass on real rules, fail on the broken copy).
The break is **never committed** — AC6 requires the rules/fixture unchanged.

## Slice breakdown

**Single slice** (this is a small, self-contained CI gate):

- **Slice 1 — `alert-rules` CI job.** Add the `alert-rules` job to `.github/workflows/quality.yml`
  (checkout → download pinned+checksum-verified Prometheus tarball → extract `promtool` →
  `check rules` glob → `test rules` glob, both blocking). Flip ADR 0088 to `accepted`, update
  `docs/GATE_LEDGER.md`/`docs/40_ROADMAP.md`, optionally correct the `.test.yml` header note.
  Proof of AC2 recorded in the PR description per §Named proof.

No second slice: there is nothing independently mergeable to split out. If branch-protection
enforcement is treated as separate, it is an admin settings action (not code), handled via the PR
note in §Decision-2.

## Invariants held

- No runtime invariant touched (CI-config only) — G1 provenance, append-only, and the
  canonical-canonical merge guard are all untouched and out of this gate's path.
- The ADR-0078 metric-name parity invariant (D5, enforced by `tests/unit/test_prometheus_*`) is
  unchanged and still runs in the `quality` job; this gate is strictly additive (PromQL-level
  validation on top of YAML-level parity).
