# 0040 — ER anchor-conflict + identifier-override negative evidence

- **Status:** ACCEPTED — anchor-conflict fork **RESOLVED → (C) HYBRID** by the human on 2026-06-24.
- **Date:** 2026-06-24
- **Gate:** B-5 (`docs/reviews/GATE_B5_SPEC.md`)
- **Touches (intended):** `resolution/splink_model.py`, `resolution/review.py`, `ontology/anchors.py`
  (the last is contract-preserving for `graph/writer.py` — see §Blast radius). Tests in `tests/unit/`.
- **Extends:** ADR 0039 (B-3 distinguishing evidence), ADR 0035 (multi-script fingerprint),
  ADR 0016 (expert-set m/u weights), ADR 0020 (merge-guard thresholds). **Does NOT relitigate** the
  0.92 merge threshold, the name-fingerprint approach, the schema-compat gate, the existing guard
  triggers, the B-3 distinguishing-id behavior, or the `score_pairs` signature.
- **Source:** three independently-reproduced ER over-merge findings against HEAD `0ffc1a6` (post-B-3):
  one NEW HIGH (conflicting canonical anchors), H-5 (shared anchor overrides name disagreement), and a
  Judge MEDIUM B-3 follow-up (shared anchor overrides a clashing distinguishing id).

> **This ADR contained ONE OPEN decision** (the anchor-conflict POLICY, §Decision part 3) — a genuine
> ER-policy fork affecting real persons; per CLAUDE.md it was resolved **with the human**, not by the
> planner. **RESOLVED 2026-06-24 → (C) HYBRID** (hard-block in Splink scoring AND park residual/transitive
> conflicts in `needs_review`). Slice 1 is now UNBLOCKED. The two scoring fixes (parts 1 and 2) have a
> locked direction and are recorded here for record; only their m/u numbers are the builder's calibrated
> choice.

## Context

ADR 0039 (B-3) added a distinguishing-id negative-evidence level so a clashing `registrationNumber` drops
a same-trade-name pair below 0.92. It left two stronger over-merge paths intact, and a third hole — the
silent dropping of a conflicting canonical anchor — was never addressed. All three are reproduced (Bayes
factors below use the model's current m/u and prior `0.001`).

1. **NEW HIGH — conflicting canonical anchors are not negative evidence; the loser is silently dropped.**
   `resolution/review.py:34-50` (`needs_review`) flags only on cluster size (`> 10`) or `is_sensitive`
   topics; it never inspects whether members carry CONFLICTING single-valued canonical anchors.
   `ontology/anchors.py:29-39` (`get_anchors`) returns `value[0]` of a multi-valued anchor context,
   silently dropping the rest. Merging `a(wikidata_id=Q1)` + `b(wikidata_id=Q2)` (non-sensitive, size 2)
   auto-promotes; FtM `merge_context` unions to `wm_anchor_wikidata_id=['Q1','Q2']`, but `get_anchors`
   returns only `{'wikidata_id':'Q1'}`. **Two distinct authoritative Q-numbers are, by definition, two
   different real-world entities** — fusing them is the catastrophic merge the guard exists to prevent,
   and the conflict is then erased from the node. The per-tenant Wikidata uniqueness constraint
   (`graph/constraints.py`, fed from `get_anchors` via `graph/writer.py:165`) never sees `Q2`. Anchors in
   scope (`CANONICAL_ID_FIELDS`): `wikidata_id`, `lei`, `geonames_id`, `opencorporates_id`.

2. **H-5 — a shared `wikidata_id` exact level overrides total name disagreement.**
   `splink_model.py:318` `_exact_comparison('wikidata_id', m=0.999, u=0.000005)` → BF 199 800. A shared
   `wikidata_id` ALONE → posterior **0.995**; shared `wikidata_id` + same country + name at the `else`
   level (full disagreement, BF 0.0421) → posterior **0.9795**. Both clear 0.92. The name `else` level
   cannot veto the anchor. No name-corroboration requirement exists.

3. **Judge MEDIUM (B-3 follow-up) — a shared `wikidataId` overrides a CLASHING distinguishing id.**
   Two Companies, same name + country, **clashing `registrationNumber`** (B-3 clash BF ≈ 0.001667) but
   the **same `wikidataId`**: name exact (9900) × country exact (5.667) × wikidata exact (199 800) ×
   reg-id clash (0.001667) → posterior **0.999947**. The wikidata exact BF swamps the B-3 clash —
   negative-evidence precedence is inverted.

These corrupt the resolved graph that *is* the product, and none trips the catastrophic-merge guard on a
non-sensitive pair. FtM-contract note: `wikidataId`, `registrationNumber`, `taxNumber` are real FtM
properties; the canonical anchors live in `entity.context["wm_anchor_<field>"]` (ADR-0018 / `anchors.py`),
not in FtM properties, so the anchor-clash logic reads the context, not `_flatten`'s `wikidata_id`
column.

## Decision

Three parts. Parts 1 and 2 are **scoring fixes with a locked direction** (only the m/u numbers are the
builder's calibrated, recorded choice). Part 3 is the **OPEN fork** the human must resolve.

### Part 1 (LOCKED direction) — a shared anchor cannot ALONE clear 0.92 against an active name disagreement

Recalibrate the `wikidata_id` exact level in `score_pairs` so its Bayes factor no longer swamps name
evidence. Outcome required: shared anchor + name `else` + same country `< 0.92`; shared anchor alone
`< 0.92`; shared anchor WITH name corroboration (exact / jw>=.92) and no clash still `>= 0.92`. Two
acceptable encodings — the builder picks one and records the final numbers + measured worst-case scores
in the Builder record below:
- **(a)** set `u` to a realistic id-collision / mis-enrichment rate (orders of magnitude above
  `0.000005`) so the BF stops swamping name evidence (keep the level structure); or
- **(b)** make the strong anchor weight conditional on the name not being at the `else` level (a derived
  name-corroboration level), expressing "require name corroboration" directly.

### Part 2 (LOCKED direction) — a clashing B-3 distinguishing id must not be overridden by a shared anchor

After Part 1 relaxes the wikidata BF, ensure the worst case **name exact × country exact × shared
wikidata × reg-id CLASH** lands **below 0.92** (the B-3 clash wins). If Part 1's relax already achieves
this, no further change is needed (record the measurement); otherwise re-tune within the negative-evidence
shape ADR 0039 locked. MUST NOT regress B-3's INV-1/INV-3 (frozen test file passes unchanged).

### Part 3 (RESOLVED 2026-06-24 → (C) HYBRID) — anchor-conflict policy

> **When cluster members carry CONFLICTING single-valued canonical anchors (>1 distinct value for the
> same anchor field — `wikidata_id`, `lei`, `geonames_id`, or `opencorporates_id`), should the system:**
>
> - **(A) HARD-BLOCK the merge** — an anchor-clash comparison level in Splink (`m << u`, symmetric with
>   B-3) drops the pair below 0.92 so the two entities never cluster; **or**
> - **(B) ROUTE to the catastrophic-merge review queue** — `needs_review` PARKS any cluster whose members
>   hold conflicting anchors, for human sign-off; **or**
> - **(C) HYBRID** — hard-block in Splink scoring AND park any residual anchor-conflict cluster in
>   `needs_review` as defense-in-depth.

Regardless of the answer, `get_anchors` is changed so a conflicting field is **surfaced, not silently
collapsed to `[0]`** (omit the conflicting field from the returned dict + log/expose the conflict),
because the silent-winner behavior is the NEW HIGH finding itself; this is contract-preserving for the
clean single-value case (the writer is unaffected — see §Blast radius).

#### Trade-offs

- **(A) Hard-block.** Strongest precision; symmetric with B-3 (clash = negative evidence in the model).
  But it is **invisible**: a blocked pair leaves no reviewable lead, so if an anchor is itself wrong (a
  mis-enriched QID), a true duplicate is silently kept apart with no surfaced signal. It also **cannot
  catch a transitive conflict** (A~B and B~C each clean, A~C anchor-clash only assembled in
  `cluster_and_merge`), because Splink scores pairs, not assembled clusters.
- **(B) Route to review.** Produces the human-reviewable PARK the platform is built around ("leads, not
  verdicts"; "multiple independent agreements before merging"; "never auto-merge a sensitive entity") and
  **catches the transitive case** (it inspects the assembled cluster's members). But, alone, it lets the
  high-confidence pairwise *score* stand and relies entirely on the guard; a non-sensitive anchor-clash
  pair is still a *scored* high-confidence merge, and a future regression in the park path would re-open
  the hole.
- **(C) Hybrid.** Gets both: the model refuses to score the pairwise anchor-clash above threshold (A's
  precision) AND the guard parks any residual/transitive anchor-conflict cluster (B's reviewable lead +
  transitive coverage). Cost: the rule lives in two places (scoring + guard) and must be kept consistent
  — the gate spec pins both with named tests (INV-1 scoring + INV-1 transitive-park) so drift is caught.

#### Recommendation (NOT a decision — the human chooses)

The planner recommends **(C)**. Rationale: it is the only option that is both **precision-favoring at the
pairwise level** (A) and **surfaces a reviewable lead, including for transitive clusters** (B), which is
the CLAUDE.md posture for ER decisions affecting real persons ("leads, not verdicts" + catastrophic-merge
guard as defense-in-depth). The two-places-one-rule cost is bounded and tested. (B) alone is the
acceptable fallback if the team wants the minimum scoring change; (A) alone is **not** recommended because
it is silent and misses transitive conflicts.

**RESOLVED 2026-06-24 → (C) HYBRID.** Slice 1 of gate B-5 is UNBLOCKED. The scoring fixes (Parts 1, 2 /
Slices 2, 3) are independent of the fork and proceed regardless.

## Alternatives considered (anchor-conflict policy)

- **(A) / (B) / (C)** — above; (C) recommended, OPEN.
- **Do nothing (status quo).** Rejected: the silent fusion of two distinct authoritative-id entities is a
  catastrophic merge that the product cannot tolerate; it is the NEW HIGH finding.
- **Keep the silent `[0]` winner but add a uniqueness-constraint violation at write time.** Rejected: the
  per-tenant constraint never sees the dropped `Q2`, so it cannot fire; and failing at write time after a
  silent fuse is later and lossier than refusing/parking at resolution time.

## Consequences / trade-off

Every change is **precision-favoring**: it can only *lower* a score or *park* a cluster — never create a
merge that did not happen before. Consistent with "leads not verdicts" and the catastrophic-merge
invariant. The accepted cost is a possible false-negative (two true duplicates that legitimately share a
QID but were assigned conflicting anchors by upstream enrichment error would be kept apart or parked) —
the safe side of the trade-off for a graph where fusing two distinct real persons/orgs is the worse error,
and, under (B)/(C), surfaced for human review rather than silently lost.

**Locked-invariant impact.** G1 (provenance) untouched and reinforced — no node/edge added/removed; the
`get_anchors` change only removes a wrong anchor value and surfaces the conflict (strengthening the audit
log Finding 1 was erasing). G4 (tenant isolation) untouched — `score_pairs` keeps its per-batch
`Sequence[FtmEntity]` contract; `needs_review` works on one cluster within one batch. Append-only /
no-un-merge untouched and reinforced (fewer/parked merges to ever un-merge). Catastrophic-merge guard /
merge-guard default-block / ADR-0024 return-to-block untouched — Part 3(B)/(C) ADDS a guard trigger and
leaves the approved-group short-circuit (`pipeline.py`) and the sign-off path intact.

**Human sign-off (CLAUDE.md "ER thresholds affecting a real person").** This ADR does NOT change the 0.92
threshold; the scoring changes are precision-favoring. The anchor-conflict POLICY is the genuine
person-affecting decision and is resolved WITH the human (Part 3) before Slice 1 builds. The gate process
also satisfies sign-off: the fresh-context Opus judge approves and the human performs the final
`--ff-only` merge (STEP 4). No individual-affecting score change is auto-promoted.

## Blast radius — `anchors.py` ↔ `graph/writer.py` (in-scope but bounded)

`get_anchors` is consumed by `graph/writer.py:165` (`{**get_anchors(entity), ...}`) to project anchors
onto graph nodes, enforced by `graph/constraints.py`'s per-tenant uniqueness constraints. The Part 3
`get_anchors` change is **contract-preserving**: it still returns `dict[str, str]` and only ever *removes*
a conflicting field (never adds a key, never returns a non-str), so `writer.py` and `constraints.py` are
**unaffected and out of scope**. If any `get_anchors` change would force a writer edit, that is the wrong
design — HARD STOP, escalate to the human; do not widen scope.

## Out of scope (explicit)

`merge.py` / `pipeline.py` / `signoff.py` / `graph/writer.py` / `graph/constraints.py` / API / MCP / any
connector or enricher; changing the 0.92 threshold or the `score_pairs` signature; the two frozen test
files; the name-fingerprint approach, schema-compat gate, and existing guard size/sensitivity triggers;
Splink EM training (ADR 0016); nomenklatura `LogicV2` re-scorer (ADR 0035 deferred); abjad-script handling
(ADR 0035 KNOWN GAP); audit items B-4, H-2, H-3, H-6.

## Builder record (completed on implementation)

> Filled in on implementation, mirroring ADR 0039's builder record. All scores measured against the
> live `score_pairs` + `cluster_and_merge` (+ `needs_review`) path on the gate fixtures, prior `0.001`,
> merge threshold `0.92`. Two non-gate `tests/unit/test_settings.py` failures observed LOCALLY are
> caused by a pre-existing untracked `.env` shadowing `settings.py` defaults (batch-size / cadence) —
> unrelated to this gate; CI has no `.env`.

- **Human's answer to Part 3 (OPEN fork):** **(C) HYBRID** — chosen 2026-06-24 (block in Splink scoring +
  park residual/transitive anchor-conflict clusters in `needs_review`).
- **Final `wikidata_id` recalibration (Part 1):** encoding **(a) realistic `u`**. Kept the level
  structure and `m = 0.999`; raised `u` from `0.000005` to **`u = 0.02`** (a ~1-in-50 QID
  mis-enrichment / collision rate, four orders of magnitude above the old value). Bayes factor drops
  from 199 800 to **49.95** — a strong corroborator (comparable to a shared `registrationNumber`'s BF
  95) that can no longer alone clear the threshold against an active name disagreement, nor swamp a
  clashing distinguishing id. Localized to the single `_exact_comparison("wikidata_id", ...)` call in
  `score_pairs`.
- **Anchor-clash level (Part 3, fork (A)/(C)):** `_anchor_clash_comparison()` in `splink_model.py`,
  symmetric in shape with B-3's `_distinguishing_id_comparison`. Projection: one `anchor_<field>`
  VARCHAR column per `CANONICAL_ID_FIELDS`, read from the `wm_anchor_<field>` CONTEXT via
  `_anchor_value` / `_flatten` (NOT the `wikidataId` FtM property — independent signals), empty-string
  `''` sentinel for a missing anchor (VARCHAR type stability + neutral null). Three levels: null
  (no field present on both sides — neutral, evaluated first so a single-sided anchor never penalises),
  **clash** (any field present on both sides with DISTINCT values) `m = 0.0005 / u = 0.30` (BF
  ≈ 0.001667, the B-3 clash shape), ELSE neutral `m = u = 0.5` (BF 1 — a shared anchor *context* is not
  double-counted; the FtM property's exact level scores a shared wikidata). Iterates ALL fields, not
  hard-coded to wikidata (INV-1b). Measured worst case **name exact × country exact × anchor clash =
  0.085584 `< 0.92`** (and `0.823789 < 0.92` even if an additional shared `wikidataId` property is also
  present).
- **`needs_review` anchor-conflict trigger (Part 3, fork (B)/(C)):** third trigger after size and
  sensitivity. Reason format: `"members carry conflicting canonical anchors -> <field>: <v1>, <v2>"`
  (names the field and BOTH sorted distinct values; contains "anchor" and "conflict"). Computed over the
  cluster's SOURCE members via `by_id` (helper `anchor_conflicts_across` in `anchors.py`), **not** the
  merged `cluster.entity` — so it catches the transitive A~M~Z case pairwise scoring misses. No
  `needs_review` signature change.
- **`get_anchors` conflict representation:** **detect-and-omit** (option (a)) — a field with `> 1`
  distinct value is OMITTED from the returned `dict[str, str]` and logged; a sibling
  `get_anchor_conflicts(entity) -> dict[str, list[str]]` accessor surfaces the conflict. The clean
  single-value path is byte-equivalent to before (`{field: value}`), so `graph/writer.py:165` and the
  per-tenant uniqueness constraints are unaffected (contract preserved; `writer.py` UNCHANGED).
- **Measured scores:** INV-2 (shared anchor + name `else` + country) = **0.011789** `< 0.92`; shared
  anchor alone (different name, no country) = **0.002101** `< 0.92`; INV-2b (shared anchor + matching
  name + country) = **0.999644** `>= 0.92`; INV-3 (clash not overridden by shared wikidata) =
  **0.823789** `< 0.92`. INV-1 / INV-1b (anchor clash, name+country) = **0.085584** `< 0.92` for every
  `CANONICAL_ID_FIELDS` field, no cluster forms; the transitive A~M~Z cluster forms via clean bridges
  and is parked by `needs_review`.
- **B-3 (ADR 0039) regressions unchanged:** `tests/unit/test_resolution_distinguishing_evidence.py`
  passes with ZERO edits (confirmed: no diff vs `HEAD`).
- **ADR-0035 regressions unchanged:** `tests/unit/test_resolution_multiscript.py` passes with ZERO
  edits (confirmed: no diff vs `HEAD`).
