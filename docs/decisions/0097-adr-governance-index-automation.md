# 0097 — ADR governance: index automation + machine-checkable status & sensitivity metadata

- **Status:** ACCEPTED (2026-07-04)
- **Date:** 2026-07-04
- **human_fork:** false
- **person_affecting:** false
- **human_cosign:** Mithat (plan approval 2026-07-04)
- **Supersedes:** nothing — governs the index maintenance and header metadata of all ADRs going forward.

## Context

The ADR corpus drifted from its own index. As of master `fee3e9b`, `docs/decisions/README.md`'s
decision index **stops at #35**; ADRs **0036–0096 (61 files) are absent** from it. The index is
hand-maintained with no generator, so it silently falls behind every merge. Three secondary defects
compound the drift:

- **22 ADRs are merged but still marked `PROPOSED`** — a merged decision reading as a proposal is a
  trust hazard for any agent (or human) that treats the header as ground truth. (The Fable review
  bundle's own digest was misled by exactly this kind of doc drift — see
  `docs/fable-review/70_EXECUTION_HANDOFF.md`.)
- **0031 has stale internal links** to pre-rename filenames (`0024-merge-guard-alert-mode.md` and
  `0028-ephemeral-per-batch-resolver.md`).
- **0024 lacks its back-annotation** — it is superseded for the production posture by 0031 but its
  header does not say so.

The headers themselves are inconsistent: status lives on line 3 in **two dialects** (blockquote
`> Status: **X**` and list `- **Status:** X`), and three overall header styles coexist. Reversibility
is recorded ad hoc via a `human_fork:` line on some ADRs; there is no machine-checkable field for
whether a decision affects a real person, even though CLAUDE.md's non-negotiable invariants turn on
exactly that distinction (ER thresholds, merge decisions, individual-affecting scores, erasure,
tagging-of-a-person → always human sign-off).

This ADR (**ADR-G**) is introduced by **Gate 0, slice 0a** to close that drift with a generator +
CI guard and to standardize the header metadata going forward. It is **extended by slice 0d**, which
adds the gate-fleet enforcement mandate (see the stub subsection at the end). It **dogfoods** every
convention it defines — its own header above already carries the canonical status line plus the new
`human_fork` / `person_affecting` / `human_cosign` fields, and it is the first ADR the generator
folds into the index.

## Decision

### 1. Index automation convention

The `#16+` region of `docs/decisions/README.md` (files `0016-*` onward) is **machine-generated** by
`scripts/gen_adr_index.py` from each ADR file's header:

- **number** — from the filename (`00NN-*.md`);
- **title** — from the H1 (`# 00NN — …`);
- **status**, **date**, **human_fork**, **person_affecting** — from the header fields.

The generated region is delimited by HTML-comment sentinels so it can be rewritten in place without
disturbing hand-authored prose:

```
<!-- BEGIN GENERATED ADR INDEX (scripts/gen_adr_index.py) -->
… generated table …
<!-- END GENERATED ADR INDEX -->
```

The **foundational #1–15 table stays hand-maintained** (those decisions have no `00NN-*.md` file);
the generator never touches anything outside the sentinels. A missing `person_affecting` field renders
as `—` (existing ADRs may omit it — see §3).

The generator MUST parse **both** historical status dialects — blockquote `> Status: **X**` and list
`- **Status:** X` — so it can read the whole corpus without a mass rewrite.

A CI job, `.github/workflows/adr-index.yml`, runs `python scripts/gen_adr_index.py --check` and
**fails on drift**: if the committed index does not match what the generator would produce from the
current ADR headers, the build is red. This `--check` mode is pure Python (no external binary) and is
this gate's guard test — it plays the role the FtM schema-diff gate plays for slice 0c.

### 2. Canonical status field going forward

New and updated ADRs SHOULD use the **list-style** line with an **uppercase token** and a date:

```
- **Status:** <TOKEN> (YYYY-MM-DD)
```

Tokens: **PROPOSED / ACCEPTED / SUPERSEDED / LOCKED**. Historical ADRs keep their existing dialect
(the generator reads both — see §1); they are not rewritten wholesale. The one hard rule: a **merged
ADR must not remain `PROPOSED`**. Slice 0a flips the 22 merged-but-`PROPOSED` ADRs (0040, 0041, 0042,
0043, 0044, 0046, 0048, 0051, 0052, 0053, 0054, 0055, 0056, 0057, 0058, 0059, 0060, 0061, 0086, 0087,
0089, 0090) to `ACCEPTED`, normalizing them onto this canonical form.

### 3. Machine-checkable sensitivity metadata

Two header fields are standardized going forward, both boolean and both parsed into the index:

- `- **human_fork:** true|false` — whether the decision required a **human fork** (an irreversible
  choice or a genuine architectural fork the agent must not make alone, per the build-discipline
  reversibility rule). Already present on recent ADRs; now formalized and indexed.
- `- **person_affecting:** true|false` — whether the change **affects a real person**: ER thresholds,
  merge decisions, individual-affecting scores, erasure, or tagging-of-a-person. This is a **NEW
  field**. Existing ADRs may omit it; the index shows `—` for a missing value. New/updated ADRs in a
  person-affecting area MUST set it explicitly.

These fields are machine-checkable precisely so slice 0d's gate-fleet mandate (below) can verify a
claimed classification against the actual diff.

### 4. Human co-sign convention

When an ADR self-tags `person_affecting: false` for a change in a **person-affecting area**, OR waives
a `human_fork` (self-classifies `human_fork: false` where a fork might be argued), it MUST carry an
explicit sign-off line:

```
- **human_cosign:** <name> <date>
```

This makes the human's endorsement of the low-sensitivity classification auditable rather than
implicit. **This ADR dogfoods it** — its header carries `human_cosign: Mithat (plan approval
2026-07-04)`, covering both its `person_affecting: false` and `human_fork: false` self-tags for a
governance change.

### Extended by slice 0d

> **STUB — to be filled by Gate 0, slice 0d (`[extends ADR-G]`).** Slice 0d extends this ADR with the
> **gate-fleet enforcement mandate**: the fleet **checker** and **judge** must reproduce the ADR's
> `human_fork` / `person_affecting` classification against the actual diff and **FAIL / DENY** on a
> mismatch, or on an un-cosigned waiver (a `person_affecting: false` in a person-affecting area, or a
> waived `human_fork`, without a `human_cosign` line). Insertion points are already scouted
> (`.claude/agents/checker.md`, `.claude/agents/judge.md`). 0d appends the concrete mandate text here;
> nothing above this subsection changes.

## Consequences

- **The index can no longer silently drift.** `adr-index` CI turns index staleness from an invisible
  trust hazard into a red build; the generator makes updating the index a one-command, deterministic
  operation instead of hand-editing a growing table.
- **Every ADR's status is honest.** No merged decision reads as a proposal; a reader (human or agent)
  can trust the header. The 0031 link fixes and 0024 back-annotation restore the supersession chain.
- **Sensitivity is first-class and checkable.** `human_fork` + `person_affecting` are structured header
  fields, indexed and (via 0d) enforced — the substrate for the "always human sign-off for
  person-affecting changes" invariant, machine-verified rather than trusted.
- **Non-sensitive, reversible.** This is docs + tooling governance: no runtime invariant, no datastore,
  no person-affecting behavior. `human_fork: false`, `person_affecting: false`. **Reversal cost:** low —
  drop the CI job and stop regenerating; the headers remain readable prose. **Revisit trigger:** the
  header schema needs a new machine-checkable field, or the corpus adopts a full front-matter format
  (e.g. YAML) that would replace the ad-hoc list fields.
- Historical ADR bodies keep their original dialect as immutable record; only headers of the enumerated
  files are touched, and only for status / link / back-annotation correctness.
