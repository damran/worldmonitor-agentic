# 0098 — FtM version pinning + schema vendoring + schema-diff CI gate

- **Status:** ACCEPTED (2026-07-04)
- **Date:** 2026-07-04
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** Mithat (plan approval 2026-07-04)
- **Supersedes:** nothing — governs FtM dependency management going forward.

## Context

FollowTheMoney (FtM) 4.x is our **L2 contract**. CLAUDE.md locks it three ways: *"Ontology =
FollowTheMoney 4.x"*, *"L2 (the ontology) is the contract"*, and *"validate every object against the
FtM schema; never invent a parallel model."* The whole architecture rests on that schema being stable:
below L2 connectors *produce* FtM/STIX entities-with-provenance; above L2 everything *consumes* the
resolved graph. If the schema changes, the contract changes underneath every layer at once.

Today that contract is left unguarded in two ways:

1. **Floors, not pins.** `pyproject.toml` declares the FtM family with `>=` floors — `followthemoney>=4.9.2`
   (line 13), `followthemoney-graph>=0.1.0` (line 14), `nomenklatura>=4.10` (line 24). `uv.lock`
   currently resolves these to exact versions (`followthemoney==4.9.2`, `followthemoney-graph==0.1.0`,
   `nomenklatura==4.10.0`) with sha256 hashes, but a transitive re-resolve or a manual `uv lock` bump
   can pull a **new FtM whose schema silently changes L2** — entity types, properties, or the range of
   `Thing`-valued edges — with **no gate** and no diff to review.
2. **The schema is not vendored.** The 69 FtM schema YAMLs (e.g. `Article.yaml`, `Address.yaml`) live
   only inside the installed package at `<venv>/lib/python3.12/site-packages/followthemoney/schema/*.yaml`.
   There is no in-repo copy, so **there is nothing to diff against** — we cannot detect that L2 changed,
   whether the change rode in on a version bump or (in principle) an in-place package change at the same
   version.

This is exactly the "L2 is the contract" invariant left without a guard. This ADR (**ADR-F**) is
introduced by **Gate 0, slice 0c** to close that gap: pin the FtM family exactly, vendor the schema as
reviewable data, and add a CI gate that fails when the installed schema diverges from the vendored copy.

## Decision

1. **Pin exactly.** In `pyproject.toml`, change the three FtM-family dependencies from `>=` floors to
   exact `==` pins that match the `uv.lock` resolution:
   - `followthemoney==4.9.2`
   - `followthemoney-graph==0.1.0`
   - `nomenklatura==4.10.0`

   `uv.lock`'s **resolved versions and sha256 hashes are unchanged** — it already pinned these exact
   versions. The only lock delta is its `requires-dist` mirror updating the three specifiers from `>=`
   to `==` to match the manifest, so `uv lock --check` stays consistent (a stale lock would make CI's
   `uv sync` attempt a re-resolve). The `==` pins make the intent explicit in the human-edited manifest
   and stop a floor-driven drift at resolve time.

2. **Vendor the schema as data.** Copy the 69 FtM schema YAMLs from the installed package into a new
   repo directory `ontology/vendor/ftm/` and commit them as **data** (not code — no import path, no
   runtime dependency on the copy). Alongside them, add a **provenance note** recording the upstream
   package name, the exact vendored version (`followthemoney 4.9.2`), and the retrieval date
   (`2026-07-04`). To keep the provenance note from tripping its own diff gate, the note is **excluded
   from the compared set**: the gate compares only the `*.yaml` schema files by name and bytes; any
   `PROVENANCE`/README note is a sibling that is not part of that set (an implementation constraint for
   the builder — see the scope note below).

3. **Add a schema-diff CI gate.** Add a **new, own workflow file** `.github/workflows/ftm-schema.yml`
   (following the repo convention that a new check gets its own file, like `adr-index.yml`, rather than
   editing `quality.yml`). The gate is modeled on the existing `alert-rules` job's shape — *pin a tool
   exactly → verify it → run a check that fails on divergence* — except here the "tool" is the Python
   package itself, already installed by `uv sync` from the locked hashes. The job:
   - syncs dependencies (`uv sync`),
   - **asserts the installed FtM version equals the pinned/vendored version** (`4.9.2`), and
   - **diffs the installed schema directory against `ontology/vendor/ftm/`, failing on any difference**
     (a missing file, an extra file, or any byte difference in a shared file).

   The check itself lives in a small stdlib-only script (proposed `scripts/check_ftm_schema.py`) that
   both the workflow and an in-repo test (proposed `tests/test_ftm_schema_vendored.py`) call, so the
   guard runs identically in CI and in `pytest`.

4. **Upgrade cadence.** An FtM (or `followthemoney-graph` / `nomenklatura`) upgrade becomes a
   **deliberate PR** that (a) re-pins the `==` version, (b) re-runs `uv lock`, (c) re-vendors the schema
   YAMLs into `ontology/vendor/ftm/`, (d) refreshes the provenance note, and (e) makes the resulting
   schema diff a **reviewed artifact** of the PR. A green schema-diff gate is the precondition for merge;
   a red one means L2 changed and must be understood before it lands.

## Reversibility & revisit trigger

**Reversible.** Per CLAUDE.md build-discipline (classify every ADR by reversibility; for a reversible
decision pick the sensible default and record the reversal cost + a revisit trigger, no human fork):

- **`human_fork: false`** — this is dependency governance, not a product/architecture fork.
- **Reversal cost: low.** Relax the `==` pins back to `>=`, delete `.github/workflows/ftm-schema.yml`,
  the check script, the test, and the `ontology/vendor/ftm/` directory. No data migration, no schema
  change, no runtime code path depends on the vendored copy.
- **Revisit trigger:** an FtM **major** version bump (e.g. `5.x`), or a `nomenklatura` /
  `followthemoney-graph` API break. At that point the vendored schema and the pin cadence should be
  re-evaluated together (a major bump is precisely the deliberate, reviewed upgrade this ADR is designed
  to force through review rather than let slip in silently).

## Consequences

- **L2 changes become visible and reviewed.** A failing schema-diff is the signal that the contract
  moved; it forces an intentional re-vendor + diff review instead of a silent drift.
- **Reproducible resolves.** The `==` pins make the manifest state the exact contract version, matching
  the already-hashed `uv.lock`.
- **A small maintenance tax on FtM upgrades** — each bump must re-pin, re-lock, re-vendor, and pass the
  diff gate. This is acceptable and intended: for a *contract* dependency, upgrades **should** be
  deliberate.
- **New CI job** `ftm-schema`, added as its own workflow file so it never conflicts with `quality.yml`
  or `adr-index.yml`. Like `adr-index`, it is **not** initially a branch-protection *required* check
  (admin TODO to promote), but it runs on every push/PR.

## Alternatives rejected

- **Floors-only (status quo).** Leaves L2 free to drift silently under a transitive or manual resolve —
  the exact gap this ADR closes.
- **Pin without vendoring.** Exact `==` pins stop version drift, but with no vendored schema there is
  nothing to diff against, so an in-place schema change at the same version is undetectable and the
  contract has no reviewable in-repo representation.
- **Vendor without a CI gate.** A committed schema copy documents L2 but, absent an automated diff, drift
  between the installed package and the vendored copy goes unnoticed until a human happens to look.

## Scope note

Pure **dependency-governance + CI + data-vendoring**. No runtime code path changes; the vendored YAMLs
are inert data, and the check script/test only read (they do not alter loading behaviour). This touches
**no runtime invariant** in the enumerated sensitive set (ER thresholds / merge / individual-affecting
scores / provenance / canonical-ID / erasure / tagging-of-a-person) — hence **`person_affecting: false`**
and **no mandatory `@given` property test**: the schema-diff gate is itself the guard, and the in-repo
test asserting `vendored == installed` is its example test.

One implementation constraint for the builder: the diff must be scoped to the **`*.yaml` schema set** so
the provenance note (and any README in the vendored dir) does not self-trip the gate — compare the set of
schema YAMLs by filename and bytes, treating a missing/extra YAML or any byte difference as a failure.

**ADR-index coupling:** adding this file means the builder MUST re-run `python scripts/gen_adr_index.py`
so the generated region of `docs/decisions/README.md` gains the `0098` row; otherwise the `adr-index` CI
check goes red. This header uses the canonical format the generator parses (status/date/`human_fork`/
`person_affecting` on lines 3–6), so the regenerated row will read `ACCEPTED | 2026-07-04 | false | false`.
