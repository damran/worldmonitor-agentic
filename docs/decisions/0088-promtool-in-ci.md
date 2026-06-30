# ADR 0088 — promtool-in-CI: validate + unit-test the alert rules in CI

- **Status:** ACCEPTED
- **Date:** 2026-06-30
- **Gate:** Gate D — promtool-in-CI (`docs/reviews/GATE_D_PROMTOOL_CI_SPEC.md`).
- **Addresses:** adversarial review 2026-06-29 of PRs #138–145 — the cheap-high-value LOW
  "promtool-in-CI". Closes the deferred follow-up named verbatim in ADR
  [0078](0078-prometheus-scrape-and-alerts.md) §Consequences and §D3
  ("wiring promtool into CI is out of scope … operator-run for now"). 0 critical / 0 high.
- **Touches:** `.github/workflows/quality.yml` (a new `alert-rules` job) + docs/ledger only. **No
  runtime path, no `src/`, no schema, no migration.** Not person-affecting (`human_fork: false`).
  Does **not** touch the ER/merge/canonical-id/provenance/sensitivity invariant class — no `@given`
  property test required.

## Context

ADR 0078 shipped the driver alert rules (`deploy/prometheus/alerts/worldmonitor.rules.yml`, 7
alerts) and a `promtool test rules` unit-test fixture
(`deploy/prometheus/tests/worldmonitor.rules.test.yml`, fire + no-fire cases for all 7). It
**deliberately** deferred running `promtool` in CI, relying instead on pytest that parses the YAML
for **metric-name parity and structure** (ADR 0078 D5).

That pytest is necessary but not sufficient: it never invokes the PromQL engine, so it cannot catch
a syntactically invalid `expr`, a `for:`/`labels`/`annotations` block promtool rejects, or an
alert-logic regression where the rendered alert diverges from the fixture's `exp_alerts`. The
fixture exists but is **never executed** — its own header says "Run manually". A broken alert rule
therefore ships green. The rules guard the operational signals from ADR 0074 (auto-hard-disable),
ADR 0075 (resolve wedge/timeout), and the catastrophic-merge guard (ADR 0024/0031) — a silently
broken rule means a silently un-pageable failure.

The fixture is the standard `promtool test rules` schema (`rule_files`, `evaluation_interval`,
`tests` with `alert_rule_test`/`exp_alerts`). It uses no version-specific features, so any recent
promtool (Prometheus 2.x or 3.x) evaluates it; we pin a current 3.x stable release.

## Decision

### D1 — Run promtool in CI, both invocations blocking

Add to `.github/workflows/quality.yml`:
- `promtool check rules deploy/prometheus/alerts/*.rules.yml` — static validation of every rule file.
- `promtool test rules deploy/prometheus/tests/*.rules.test.yml` — evaluate the alert logic.

Both are **blocking** (non-zero exit fails the build). A CI gate that does not fail on a broken rule
is pointless. Globs (not the single current filename) so a future second rule/test file is covered
without a workflow edit.

### D2 — A new dedicated `alert-rules` job, not a step in `quality`

promtool is Python-less. Putting it inside the `quality` job would couple an ops concern to the
uv/ICU/Pyright/pytest setup and serialize the download behind a full `uv sync --dev`. A separate
`ubuntu-latest` job needs only `actions/checkout`, runs **in parallel** with `quality`, and fails
with an isolated, legible signal. Cost: one extra runner + one tarball download (~tens of MB, well
under ~10s). The new status-check name is `alert-rules`.

**Branch protection:** branch protection currently requires `quality` + `security`. Adding
`alert-rules` to the required set is an admin settings change the builder cannot self-apply. The
builder names the job `alert-rules`, notes in the PR that an admin should add it to required checks,
and confirms it runs green on the PR. Until then it still runs and is visible on every PR/push (so a
red `alert-rules` is observable at review time), it just is not yet merge-blocking at the GitHub
level.

### D3 — promtool acquisition: pinned, checksum-verified official release tarball

Download `prometheus-<VERSION>.linux-amd64.tar.gz` from the official GitHub release, extract only
`promtool`, run it. Constraints (CLAUDE.md: treat external downloads as hostile):
- **Pin an exact `vX.Y.Z`** — no `latest`. The builder pins the **current stable Prometheus release
  at build time** (3.x line), verifies the tag exists, and records the exact version here in D3 on
  merge: `Prometheus v3.12.0` (SHA-256: `20da47f8e5303f74aecb78edd7f7e39041dac08ac4939dba75efd7a900ae8867`).
- **Verify SHA-256** of the tarball against the value in that release's `sha256sums.txt`, with the
  expected digest pinned as a literal in the workflow; **mismatch fails the job before promtool
  runs**. No unverified binary executes.
- **No third-party `setup-promtool` Marketplace action** — pinned official release + checksum is the
  supply-chain-clean path and avoids trusting an unaudited action.
- Do not vendor the binary into the repo.

### D4 — Rules and fixture are validated as-is

The alert rules and the fixture's logic/assertions are ADR-0078-locked and correct; this gate must
not edit them. If CI goes red against the current rules, the **CI wiring** is wrong, not the rules.
The only permitted content edit is correcting the fixture's "Run manually (promtool not wired to
CI…)" header comment to reflect that CI now enforces it (comment-only; no logic change).

## Reversibility

**Reversible.** This is CI configuration: a new job in one workflow file plus docs. **Reversal cost:
low** — delete the `alert-rules` job from `quality.yml` (and, if it was added, remove `alert-rules`
from required checks); nothing in the app, driver, or any test depends on it. Not a human fork (no
data shape, no schema, no public surface, no person impact) — per the CLAUDE.md reversible-decision
discipline the sensible default is picked and we proceed.

**Revisit triggers:**
1. If a managed/central Prometheus + CI pipeline becomes the system of record for alert validation,
   this job may move there and be dropped here.
2. If Prometheus changes the `test rules` fixture schema in a way the pinned version no longer
   handles, bump the pinned `vX.Y.Z` + checksum (a one-line change) and re-confirm green.
3. If a second alert-rule domain is added (e.g. API/connector rules), the glob already covers it;
   revisit only if rules need separate groups/jobs.

## Alternatives considered

- **A step inside the existing `quality` job.** Rejected: couples a Python-less ops check to the
  Python toolchain, serializes the download behind `uv sync`, and muddies the failure signal. A
  parallel job is cleaner and barely more expensive (D2).
- **Re-implement rule evaluation in pytest (extend the ADR 0078 D5 parser).** Rejected: that would
  reimplement the PromQL engine in Python — exactly what promtool is for, and it would drift from
  real Prometheus semantics. Run the real tool.
- **A third-party `setup-promtool` GitHub Action.** Rejected: adds an unaudited Marketplace
  dependency to the supply chain; pinned official release + SHA-256 is auditable and minimal (D3).
- **`apt-get install prometheus` / unpinned `latest`.** Rejected: distro packages lag and pin no
  digest; `latest` is non-reproducible and unverifiable. Pin + checksum (D3).
- **Non-blocking (warn-only) check.** Rejected: a non-blocking rule validator does not prevent a
  broken rule from shipping — it defeats the gate's purpose. Blocking (D1).
- **Leave it operator-run (status quo of ADR 0078).** Rejected: that is precisely the gap this gate
  closes; the fixture already exists and is cheap to execute — "operator-run" in practice means
  "never run".

## Tests

No new pytest (per D4 / the spec — adding one would reimplement promtool). The gate's enforcement is
the CI job itself; the negative case is proven once locally before the PR:
- Run the two pinned-promtool commands against the **current** rules → both pass (AC1).
- Break one `expr`/`exp_alerts` in a throwaway copy → non-zero exit (AC2); discard the break.
- Record both observations in the PR description. The rules/fixture stay byte-unchanged (AC6).
The ADR 0078 D5 parity/structure pytest is untouched and continues to run in `quality`.

## Slice plan

- **Slice 1 (only) — `alert-rules` CI job.** Add the job to `.github/workflows/quality.yml`
  (checkout → pinned+checksum-verified promtool → `check rules` glob → `test rules` glob, both
  blocking). Flip this ADR to `accepted`, update `docs/GATE_LEDGER.md` / `docs/40_ROADMAP.md`,
  optionally correct the fixture header comment. Proof of AC1+AC2 in the PR description.
