# Gate B-5 — ER anchor-conflict + identifier-override negative evidence

- **Gate id:** `B-5`
- **Status:** SPEC (planner output — STEP 1 of a reviewer-gated build)
- **Branch:** `gate/b5-anchor-conflict-negative-evidence`
- **ADR:** `docs/decisions/0040-er-anchor-conflict-negative-evidence.md` (PROPOSED — contains ONE **OPEN** fork)
- **Extends (does NOT relitigate):** ADR 0039 (B-3 distinguishing evidence), ADR 0035 (multi-script
  fingerprint), ADR 0016 (expert-set m/u weights), ADR 0020 (merge-guard thresholds).
- **Source findings:** three independently-verified ER over-merge holes, all REPRODUCED against current
  code at HEAD `0ffc1a6` (post-B-3). File:line + Bayes-factor reproductions in §1.

> ONE decision in this gate is a genuine ER-policy fork that affects real persons (CLAUDE.md: resolve
> with the user). It is recorded **OPEN** in ADR 0040 §Decision and is restated verbatim in §3 below.
> **The builder MUST NOT pick it.** Slice 1 is BLOCKED until the human resolves it; Slices 2 and 3 are
> independent of the fork and may proceed.

---

## 1. Problem statement — the three holes (all reproduced)

B-3 (ADR 0039) added a *distinguishing-id* negative-evidence level so a clashing `registrationNumber`
drops a same-trade-name pair below 0.92. It did **not** touch two stronger over-merge paths that survive
it: (a) **conflicting canonical anchors** are not treated as negative evidence at all, and (b) a single
**shared anchor** (`wikidata_id`) is weighted strongly enough to **override** both total name
disagreement (H-5) and B-3's own clashing-id level (Judge MEDIUM). Each fuses two distinct real-world
entities into one canonical node — the catastrophic merge the guard exists to prevent — and the loser is
then silently hidden on the node.

### Finding 1 — NEW HIGH: conflicting canonical anchors are not a do-not-merge signal; the losing anchor is silently dropped

- `resolution/review.py:34-50` (`needs_review`) flags a cluster ONLY on size (`> MAX_AUTO_MERGE_SIZE`,
  10) or `is_sensitive` topics (PEP/sanction/crime). It NEVER inspects whether members carry CONFLICTING
  single-valued canonical anchors.
- `ontology/anchors.py:29-39` (`get_anchors`) takes `value[0]` of a multi-valued anchor context,
  **silently dropping the rest** — so after a merge fuses two anchors, only one survives onto the node.
- **Reproduced.** Merge member `a` (`wm_anchor_wikidata_id=['Q1']`) + member `b`
  (`wm_anchor_wikidata_id=['Q2']`). The cluster is non-sensitive and size 2, so `needs_review` returns
  `(False, "")` → **auto-promoted**. FtM `merge_context` unions the two context lists, so the merged
  entity carries `wm_anchor_wikidata_id=['Q1','Q2']`, but `get_anchors` returns only
  `{'wikidata_id': 'Q1'}`. Two entities with **distinct authoritative Q-numbers are, by definition,
  different real-world entities**; fusing them is a catastrophic merge, and the conflict is then erased
  from the node. The per-tenant Wikidata uniqueness constraint (`graph/constraints.py`, populated from
  `get_anchors` via `graph/writer.py:165`) never even sees `Q2`.
- **Anchors in scope** (`ontology/anchors.py` `CANONICAL_ID_FIELDS`): `wikidata_id`, `lei`,
  `geonames_id`, `opencorporates_id`. Each is single-valued and authoritative: a real entity has at most
  one. `>1` distinct value for the same field across a cluster's members ⇒ the cluster spans `>1` real
  entity.

### Finding 2 — H-5: a shared `wikidata_id` exact level overrides total name disagreement

- `resolution/splink_model.py:318`: `_exact_comparison('wikidata_id', m=0.999, u=0.000005)`. Bayes
  factor `m/u = 199 800`.
- **Reproduced (Bayes-factor arithmetic, prior 0.001).** A shared `wikidata_id` ALONE
  (everything else null/neutral) → posterior **0.995**. A shared `wikidata_id` + same country + **total
  name disagreement** (name `else` level, `m=0.04 / u=0.95`, BF 0.0421) → posterior **0.9795** — both
  clear the 0.92 merge threshold. The name `else` level's `m/u < 1` cannot veto the anchor: `199800 ×
  0.0421 = 8415`, still a huge positive odds. There is **no name-corroboration requirement**: one shared
  anchor alone clears the threshold against any name evidence.

### Finding 3 — Judge MEDIUM (B-3 follow-up): a shared `wikidataId` overrides a CLASHING distinguishing id

- Two `Company` records, same name + country, **clashing `registrationNumber`** (the exact case B-3's
  `_distinguishing_id_comparison` clash level, `m=0.0005 / u=0.30`, BF ≈ 0.001667, is built to block) but
  the **same `wikidataId`**.
- **Reproduced.** name exact (BF 9900) × country exact (BF 5.667) × wikidata exact (BF 199 800) × reg-id
  clash (BF 0.001667) → posterior **0.999947**. The wikidata exact BF **swamps** the B-3 clash BF, so the
  pair merges at ~0.9999 — exactly the negative-evidence-precedence inversion B-3 was supposed to fix. A
  present-but-clashing distinguishing id must **not** be overridden by a single shared anchor.

All three corrupt the resolved graph that *is* the product, and none trips the catastrophic-merge guard
on a non-sensitive pair.

---

## 2. Locked direction & the two scoring fixes (spec the HOW; the policy fork is OPEN, §3)

This gate is **precision-favoring**: every change can only **reduce or park** merges, never create one.
It does NOT change `DEFAULT_MERGE_THRESHOLD = 0.92` (`merge.py`) or the `score_pairs` signature.

Three changes, two of them with a locked direction and one OPEN:

1. **Scoring fix A — anchor-clash must not be overridden by a shared anchor / shared anchor must not
   alone clear 0.92 vs an active name disagreement** (Findings 2 + 3). Locked direction; HOW in §4.1.
2. **Scoring fix B — a clashing B-3 distinguishing id must not be overridden by a shared anchor**
   (Finding 3). Locked direction; HOW in §4.1.
3. **Policy — what to do with a cluster whose members carry CONFLICTING single-valued canonical
   anchors** (Finding 1). The (A)/(B)/(C) choice is an **OPEN ER-policy fork affecting real persons**
   (§3). The `needs_review` defense-in-depth path (§4.2) and the `get_anchors` conflict-surfacing
   (§4.3) are gated on the human's answer.

This gate does **not** change: the name-fingerprint approach, the schema-compatibility gate, the
catastrophic-merge guard's existing size/sensitivity triggers, the B-3 distinguishing-id behavior, the
0.92 threshold, the `score_pairs` signature, or any of ADR 0035's validated outcomes.

---

## 3. THE OPEN DECISION (for the human — do NOT pick it; recorded in ADR 0040)

> **When cluster members carry CONFLICTING single-valued canonical anchors (>1 distinct value for the
> same anchor field — `wikidata_id`, `lei`, `geonames_id`, or `opencorporates_id`), should the system:**
>
> - **(A) HARD-BLOCK the merge** — treat anchor disagreement as decisive negative evidence inside Splink
>   scoring (an anchor-clash comparison level with `m << u`, mirroring B-3), dropping the pair below 0.92
>   so the two entities never cluster; **or**
> - **(B) ROUTE to the catastrophic-merge review queue** — let the pair score as today but make
>   `needs_review` PARK any cluster whose members hold conflicting anchors, parking for human sign-off
>   (consistent with "leads not verdicts", "multiple independent agreements before merging", "never
>   auto-merge a sensitive entity"); **or**
> - **(C) HYBRID** — hard-block within Splink scoring AND have `needs_review` park any residual
>   anchor-conflict cluster as defense-in-depth.

**Trade-offs** (full version in ADR 0040; recommendation there is **(C)**, left OPEN):

- (A) is the strongest precision guarantee and is symmetric with B-3 (clash = negative evidence in the
  model). But it is invisible: a blocked pair leaves no human-reviewable artifact, and if the anchor
  data itself is wrong (a mis-enriched Wikidata QID on one record), a true duplicate is silently kept
  apart with no surfaced lead. It also cannot catch a clash that first appears via a *transitive* cluster
  (A~B and B~C each clean, A~C anchor-clash assembled in `merge.cluster_and_merge`), because Splink only
  scores pairs.
- (B) produces a human-reviewable park (the "lead, not verdict" the platform is built around) and catches
  the transitive case because it inspects the **assembled cluster**. But, taken alone, it lets the
  high-confidence merge *score* stand and relies entirely on the guard; a future change to the park path
  could regress it, and a non-sensitive anchor-clash pair would still be a *scored* high-confidence merge.
- (C) gets both: the model refuses to score the pairwise anchor-clash above threshold (A's precision) AND
  the guard parks any residual/transitive anchor-conflict cluster (B's reviewable lead + transitive
  coverage). Cost: the rule lives in two places (scoring + guard) and must be kept consistent; the spec
  pins both with named tests so drift is caught.

**Why this is OPEN, not a planner call:** anchor-clash policy decides whether two records *about real
persons/orgs* are kept apart silently or surfaced for human judgement — an ER-policy decision affecting
real persons. CLAUDE.md requires resolving that with the user and recording it as an ADR. The planner
recommends (C) but does not pick it.

**Builder gating by answer:**

| Human answer | Slice 1 builds | Slice 1 tests assert |
|---|---|---|
| (A) | §4.1 anchor-clash Splink level only | INV-1 (no-merge); §4.2/§4.3 NOT built |
| (B) | §4.2 `needs_review` anchor-conflict park + §4.3 `get_anchors` conflict-surface only | INV-1 (park, not no-merge); scoring level NOT built |
| (C) | §4.1 anchor-clash level + §4.2 park + §4.3 conflict-surface | INV-1 (no-merge AND park-on-residual/transitive) |

Scoring fixes A/B (§4.1 wikidata m/u recalibration + clash-overrides-anchor ordering, Slices 2 & 3) are
**independent of the fork** and proceed regardless.

---

## 4. Design — the precise HOW per change

### 4.1 Scoring: shared anchor cannot alone clear 0.92, and cannot override a clash (Slices 2 & 3, and the (A)/(C) part of Slice 1)

Two sub-changes in `resolution/splink_model.py`, both purely in the `comparisons` list / its m/u, with
the `score_pairs` signature unchanged:

**(i) Relax the `wikidata_id` exact level so one shared anchor cannot alone clear 0.92 against an active
name disagreement** (Findings 2 + 3). The current `m=0.999, u=0.000005` gives BF 199 800 — far larger
than any real shared-id corroboration warrants given QID-collision/mis-enrichment rates. The builder
recalibrates so that:

- a shared `wikidata_id` with the name at the `else` level (`m=0.04/u=0.95`) **AND** same country lands
  **below 0.92** (kills Finding 2's 0.9795), AND
- a shared `wikidata_id` ALONE (everything else null) lands **below 0.92** (kills the 0.995 alone-case),
  AND
- a shared `wikidata_id` WITH name corroboration (name exact or jw>=.92) and no clash still merges
  (`>= 0.92`) — a genuine duplicate must not regress.

Two acceptable encodings (the builder picks one, records it + the final numbers + measured worst-case
scores in ADR 0040, mirroring the B-3 builder record):

- **(a) Realistic `u` (collision rate).** Set `u` to a realistic id-collision/mis-enrichment rate
  (orders of magnitude larger than `0.000005`) so the BF no longer swamps name evidence. Keep the
  level structure. Simplest; localized to the one `_exact_comparison` call.
- **(b) Require name corroboration.** Make the shared-anchor corroboration *conditional* on the name not
  being at the `else` (active-disagreement) level — e.g. a derived comparison level that only awards the
  strong anchor weight when name is at least jw>=.82, and a weaker/neutral weight otherwise. Larger but
  expresses the intent directly. The ADR records which was chosen and why.

The pin is the OUTCOME (INV-2, INV-3 below), not the encoding.

**(ii) A present-but-clashing B-3 distinguishing id must not be overridden by a shared anchor**
(Finding 3). After (i) relaxes the wikidata BF, the builder verifies — and if necessary re-tunes the B-3
clash level's m/u (within ADR 0039's negative-evidence shape) OR the relaxed wikidata level — so that the
worst case **name exact × country exact × shared wikidata × reg-id CLASH** lands **below 0.92**. The B-3
clash must win against a single shared anchor. The builder MUST confirm this does not regress B-3's own
INV-1/INV-3 (`tests/unit/test_resolution_distinguishing_evidence.py` passes UNCHANGED) and records the
final numbers + measured worst-case in ADR 0040.

> **If the human chooses (A) or (C):** add an **anchor-clash comparison level** in `score_pairs`,
> symmetric with B-3's `_distinguishing_id_comparison`: project the per-field canonical anchors into a
> comparable column (the anchors live in `entity.context["wm_anchor_<field>"]`, NOT in FtM properties, so
> the projection reads the context — see §4.4) and add a three-level comparison whose **clash** level
> (both sides present, distinct value for the same anchor field) has `m << u` so an anchor clash drops the
> pair below 0.92. Calibrate per the same worst-case discipline (name exact × country exact × anchor
> clash → `< 0.92`). The null level (either side absent) is neutral. This is the (A) part of Slice 1.
> **If the human chooses (B):** this scoring level is NOT built — the park path (§4.2) carries the policy.

**Precision-favoring / locked-invariant note.** (i) and (ii) only *lower* scores; the anchor-clash level
only *lowers* scores. None can create a merge that did not happen before, so none can weaken G1
provenance, append-only, or the merge-guard (ADR 0024). They strictly remove false-positive merges.

### 4.2 Guard: park a cluster with conflicting anchors (Slice 1, only if human picks (B) or (C))

In `resolution/review.py` `needs_review`, add a third trigger alongside size and sensitivity: a cluster
is flagged (parked, `mode="block"`) when its members carry **conflicting single-valued canonical
anchors** — i.e. for some field in `CANONICAL_ID_FIELDS`, the union of that field's values across all
`member_ids` (read from each member's `entity.context["wm_anchor_<field>"]`) contains `> 1` distinct
non-empty value. The reason string names the field and the conflicting values (e.g.
`"members carry conflicting wikidata_id anchors: Q1, Q2"`).

- `needs_review` already receives `by_id: Mapping[str, FtmEntity]`, so the per-member contexts are
  reachable with NO signature change. (Confirm: the conflict is computed over the cluster's **source
  members**, not the merged `cluster.entity`, because the merged context unions the values and
  `get_anchors` would mask the conflict — that masking is Finding 1.)
- This is **defense-in-depth** for (C) and the **sole** policy mechanism for (B). It catches the
  **transitive** anchor-clash that pairwise scoring (§4.1 (A)) cannot: a cluster assembled in
  `cluster_and_merge` from individually-clean pairs but spanning two anchors is parked here.
- The existing approved-group short-circuit in `pipeline.py:284-298` (`members <= group` ⇒ promote, never
  re-park) is **untouched**: a human who explicitly approved an anchor-conflict cluster still gets it
  promoted, exactly as for the size/sensitivity triggers. Do NOT alter pipeline.py.

### 4.3 `get_anchors`: surface/record a conflict instead of silently taking `[0]` (Slice 1, only if (B)/(C); see §4.4 for the coupling)

`ontology/anchors.py` `get_anchors` currently returns `value[0]` for a multi-valued anchor context,
silently dropping a conflicting `Q2`. Change it so a conflicting anchor is **surfaced, not hidden**. The
builder picks ONE of (record the choice in ADR 0040):

- **(a) Detect-and-omit:** when a field's context list holds `> 1` distinct non-empty value, **omit that
  field from the returned anchors dict** (do not project an arbitrary winner onto the node) AND log a
  warning naming the field + values. The node then carries NO anchor for that field rather than a silently
  wrong one — and the conflict is the guard's job to park (§4.2). This is the minimal, writer-safe option.
- **(b) Expose a conflict accessor:** keep `get_anchors` returning a single value for the clean case but
  add a sibling `get_anchor_conflicts(entity) -> dict[str, list[str]]` that `needs_review` (§4.2) uses,
  and have `get_anchors` omit (per (a)) any conflicting field so the writer never projects a wrong value.

**Hard constraint (§4.4):** whatever is chosen, `get_anchors` MUST keep returning `dict[str, str]` for
the clean (single-value) case so `graph/writer.py:165` (`{**get_anchors(entity), ...}`) is unaffected.
The ONLY behavior change is for a *conflicting* field, which today silently picks a wrong winner.

### 4.4 Blast-radius confirmation: `anchors.py` ↔ `graph/writer.py` coupling (IN-SCOPE but bounded)

`get_anchors` is consumed by `graph/writer.py:165` to project anchor properties onto graph nodes (which
the per-tenant uniqueness constraints in `graph/constraints.py` enforce). Therefore:

- `anchors.py` IS in scope (the §4.3 change). It is included in the gate.scope allow-list.
- `graph/writer.py` is **NOT** in scope and MUST NOT be edited. The §4.3 change is contract-preserving:
  `get_anchors` still returns `dict[str, str]` and only ever *removes* a conflicting field (never adds a
  new key, never returns a non-str), so `writer.py:165`'s `{**get_anchors(entity), ...}` spread and the
  constraint projection are unaffected. If the builder finds any change to `get_anchors` that would force
  a writer edit, that is the wrong design — **HARD STOP, escalate to the human** (do not widen scope).
- `graph/constraints.py` consumes `CANONICAL_ID_FIELDS` (unchanged) — untouched, not in scope.

---

## 5. Acceptance criteria — explicit, testable invariants

All invariants are asserted against the `score_pairs` + `cluster_and_merge` (+ `needs_review`) path on
hand-built `make_entity` fixtures (no live stores), mirroring the ADR-0035 / B-3 test convention
(`make_entity`, local `_company` / `_org` / `_top_probability` / `_merges` helpers, the 0.92 boundary).
Anchors are set via `set_anchor(entity, field, value)` (or the same `wm_anchor_<field>` context the merge
path produces). Non-sensitive fixtures MUST NOT set `topics`/`sanction` (that is load-bearing: the guard
fires on sensitivity, which would mask the auto-merge hole — see B-3 spec §note).

**INV-1 — conflicting canonical anchors do not silently auto-promote.** Two **non-sensitive** entities,
same name + country, with **conflicting** `wikidata_id` (`Q1` vs `Q2`), do not silently fuse. The exact
assertion depends on the human's fork answer:
- (A): `score_pairs` yields top probability `< 0.92` and `cluster_and_merge` yields no merge cluster.
- (B): the pair may still score `>= 0.92`, but `needs_review` returns `(True, <reason naming the
  conflicting anchors>)` so `mode="block"` parks it — `_merges_auto_promoted == []`.
- (C): BOTH — pairwise anchor-clash scores `< 0.92` AND a **transitively** assembled anchor-conflict
  cluster (`Q1`~middle~`Q2` via clean bridges) is parked by `needs_review`.
- Named tests (the exact set is fork-dependent — the test-author writes only the ones the answer
  selects): `test_conflicting_wikidata_anchor_blocks_merge` (A/C),
  `test_conflicting_anchor_cluster_parks` (B/C),
  `test_transitive_conflicting_anchor_cluster_parks` (C).

**INV-1b — anchor-clash holds for every anchor field.** The same outcome as INV-1 for a `lei` clash and a
`geonames_id` clash (proves the rule is over `CANONICAL_ID_FIELDS`, not hard-coded to wikidata).
- Named test: `test_conflicting_anchor_blocks_or_parks_per_field` (parametrized over fields per answer).

**INV-1c — `get_anchors` does not silently pick a wrong winner on a conflict.** Given an entity whose
context holds `wm_anchor_wikidata_id=['Q1','Q2']`, `get_anchors` does NOT return `{'wikidata_id': 'Q1'}`
(the silent-winner behavior of Finding 1). Per §4.3 it omits the conflicting field (and the conflict is
observable via the logged warning or `get_anchor_conflicts`). A single-value context (`['Q1']`) still
returns `{'wikidata_id': 'Q1'}` (clean case unchanged — guards `writer.py`).
- Named tests: `test_get_anchors_omits_conflicting_field`, `test_get_anchors_clean_single_value_unchanged`.

**INV-2 — a shared anchor cannot ALONE clear 0.92 against an active name disagreement** (Finding 2). Two
entities sharing a `wikidata_id`:
- with same country but the name at the `else` level (total disagreement, no shared name tokens) → top
  probability `< 0.92`, no merge. (Kills the 0.9795 case.)
- with NO other corroboration at all (different name, no country) → top probability `< 0.92`. (Kills the
  0.995 alone-case.)
- Named tests: `test_shared_wikidata_with_name_disagreement_does_not_merge`,
  `test_shared_wikidata_alone_does_not_merge`.

**INV-2b — a shared anchor WITH name corroboration still merges** (no recall regression from the m/u
relax). Two entities, same `wikidata_id` + same name (exact fingerprint) + same country, no clash → still
merges (`>= 0.92`). Genuine duplicates that legitimately share a QID are not lost.
- Named test: `test_shared_wikidata_with_matching_name_still_merges`.

**INV-3 — a clashing B-3 distinguishing id is NOT overridden by a shared anchor** (Finding 3). Two
Companies, same name + country, **same `wikidataId`** but **clashing `registrationNumber`** → top
probability `< 0.92`, no merge. The negative evidence wins.
- Named test: `test_clashing_reg_id_not_overridden_by_shared_wikidata`.

**INV-4 — B-3 regressions hold UNCHANGED.** `tests/unit/test_resolution_distinguishing_evidence.py`
passes with **zero edits**. In particular: INV-1 (reg clash blocks), INV-1b (cross-field clash), INV-3
(matching id merges), INV-3b (null id neutral), the multivalued-overlap case, and the generic-token guard
all still hold. The m/u relax in §4.1 MUST NOT change any B-3 outcome.

**INV-5 — ADR-0035 regressions hold UNCHANGED.** `tests/unit/test_resolution_multiscript.py` passes with
**zero edits**: the bilingual Legion pair still merges + parks (INV-4 of B-3 lineage); the
`us_dod_chinese_milcorps` "Co Ltd" family → 0 merges; the `my_aob` same-script duplicate still merges; the
cross-script no-shared-variant case still merges; the schema gate (Org↔Person / Org↔Vessel dropped,
Org↔Company merges) is intact.

INV-4 and INV-5 are proven by the **existing** B-3 and ADR-0035 suites passing unchanged. The builder
MUST NOT modify either file; they are the regression guards. If one goes red, the change is wrong, not the
test.

---

## 6. Named tests (the test-author writes these; the builder does NOT)

New file: **`tests/unit/test_resolution_anchor_conflict.py`**

| Test function | Invariant | Asserts | Fork |
|---|---|---|---|
| `test_conflicting_wikidata_anchor_blocks_merge` | INV-1 | same name+country, `wikidata_id` Q1 vs Q2 → top prob `< 0.92`, no merge | (A)/(C) |
| `test_conflicting_anchor_cluster_parks` | INV-1 | anchor-conflict cluster → `needs_review` True (reason names the field+values); not auto-promoted | (B)/(C) |
| `test_transitive_conflicting_anchor_cluster_parks` | INV-1 (C) | Q1~M~Q2 via clean bridges → assembled cluster parked by `needs_review` | (C) |
| `test_conflicting_anchor_blocks_or_parks_per_field` | INV-1b | parametrized over `lei` / `geonames_id` (and `opencorporates_id`) → same no-merge/park | per answer |
| `test_get_anchors_omits_conflicting_field` | INV-1c | context `['Q1','Q2']` → `get_anchors` does NOT return `{'wikidata_id':'Q1'}` (field omitted; conflict surfaced) | all |
| `test_get_anchors_clean_single_value_unchanged` | INV-1c | context `['Q1']` → `get_anchors` returns `{'wikidata_id':'Q1'}` (writer contract preserved) | all |
| `test_shared_wikidata_with_name_disagreement_does_not_merge` | INV-2 | shared `wikidata_id` + same country + name `else` level → top prob `< 0.92`, no merge | all |
| `test_shared_wikidata_alone_does_not_merge` | INV-2 | shared `wikidata_id`, different name, no country → top prob `< 0.92` | all |
| `test_shared_wikidata_with_matching_name_still_merges` | INV-2b | shared `wikidata_id` + same name + country, no clash → merges `>= 0.92` | all |
| `test_clashing_reg_id_not_overridden_by_shared_wikidata` | INV-3 | same name+country, same `wikidataId`, clashing `registrationNumber` → top prob `< 0.92`, no merge | all |

Existing files (RUN UNCHANGED as regression guards): **`tests/unit/test_resolution_distinguishing_evidence.py`**
(INV-4) and **`tests/unit/test_resolution_multiscript.py`** (INV-5). The CI gate is: the new file passes
AND both existing files pass with zero edits.

Test conventions: copy the `make_entity` / `_company` / `_org` / `_top_probability` / `_merges` patterns
from the B-3 and multiscript suites. Set anchors via `set_anchor` (or the `wm_anchor_<field>` context the
merge path emits). Note `score_pairs` reads anchors from `entity.context` for the (A)/(C) scoring level,
NOT from FtM properties — fixtures must set the context, not a `wikidataId` property, for the anchor-clash
*scoring* test (the `wikidataId` FtM property is a separate, already-projected `_flatten` column used by
the existing wikidata comparison; the anchor-clash level is over the `wm_anchor_*` context). The
test-author confirms this distinction with the builder via the ADR.

---

## 7. Slice breakdown (each individually mergeable, each with its own green tests)

Three slices. **Slices 2 and 3 are independent of the OPEN fork and may land first.** Slice 1 is
**BLOCKED** until the human resolves §3.

- **Slice 2 — relax the `wikidata_id` exact level (Findings 2; no fork dependency).**
  - File: `src/worldmonitor/resolution/splink_model.py` (the `wikidata_id` `_exact_comparison` call /
    its m/u, or the derived name-corroboration level per §4.1 (i)).
  - Implements §4.1 (i): a shared anchor cannot alone clear 0.92 against an active name disagreement.
  - Proves: INV-2, INV-2b; INV-4 (B-3) + INV-5 (0035) stay green.
  - Independent of Slices 1 and 3.

- **Slice 3 — clash-not-overridden-by-anchor reconciliation (Finding 3; no fork dependency).**
  - File: `src/worldmonitor/resolution/splink_model.py` (verify/retune so the B-3 clash beats a shared
    anchor; §4.1 (ii)). May be a no-op if Slice 2's relax already makes INV-3 hold — in that case Slice 3
    folds into Slice 2 and is dropped, with the measurement recorded in ADR 0040.
  - Proves: INV-3; INV-4 + INV-5 stay green.
  - Builds on Slice 2's numbers (land Slice 2 first or together).

- **Slice 1 — anchor-conflict policy (BLOCKED on the OPEN fork §3).**
  - Files (fork-dependent): `src/worldmonitor/resolution/splink_model.py` (anchor-clash scoring level, if
    (A)/(C)); `src/worldmonitor/resolution/review.py` (`needs_review` anchor-conflict park, if (B)/(C));
    `src/worldmonitor/ontology/anchors.py` (`get_anchors` conflict-surfacing, §4.3, if (B)/(C) — and for
    INV-1c regardless, since the silent-winner is Finding 1).
  - Implements §4.2 / §4.3 / the (A) part of §4.1 per the human's answer.
  - Proves: INV-1, INV-1b, INV-1c; INV-4 + INV-5 stay green. `graph/writer.py` UNCHANGED (§4.4).
  - The test-author writes only the fork-selected INV-1 tests (§3 table); INV-1c tests are written
    regardless.

The builder may land Slice 2+3 as one PR or two; the checker reproduces every applicable INV on each. No
slice may modify the two frozen test files or weaken any invariant.

---

## 8. Out of scope (hard stops)

- Any file other than the four production files (`resolution/splink_model.py`, `resolution/review.py`,
  `ontology/anchors.py`) and the new test file — specifically NOT `merge.py`, `pipeline.py`, `signoff.py`,
  `graph/writer.py`, `graph/constraints.py`, the API/MCP, any connector/enricher.
- Changing `DEFAULT_MERGE_THRESHOLD = 0.92` (`merge.py`) or the `score_pairs` signature.
- The two frozen test files (`test_resolution_distinguishing_evidence.py`,
  `test_resolution_multiscript.py`) — pass UNCHANGED.
- The B-3 distinguishing-id behavior, the name-fingerprint approach, the schema-compat gate, the existing
  size/sensitivity guard triggers — all preserved.
- Splink EM training (ADR 0016 — model stays expert-set); nomenklatura `LogicV2` re-scorer (ADR 0035
  deferred); abjad-script handling (ADR 0035 KNOWN GAP).
- B-4, H-2, H-3, H-6, and any audit item not in §1.
- Picking the OPEN fork (§3) — that is the human's call.

---

## 9. Invariant compliance (locked fleet invariants — how this gate holds them)

- **G1 provenance on every node AND edge.** Untouched and reinforced. No node/edge added or removed. The
  §4.3 change can only *remove a wrong anchor value* from a node's projected properties (it never adds a
  key, never returns a non-str), so provenance projection (`writer.py`) is unaffected; surfacing a
  conflict strengthens the audit log Finding 1 was erasing.
- **G4 tenant isolation.** Untouched. `score_pairs` keeps its per-batch `Sequence[FtmEntity]` contract;
  `needs_review` operates on a single cluster's members within one batch; nothing crosses a tenant.
- **Append-only / no un-merge.** Untouched and reinforced: every change only *prevents or parks* a merge
  (lowers scores / flags clusters); it never creates a merge that would later need un-merging.
- **Canonical-canonical only via the guard / merge-guard default block / ADR-0024 return-to-block.**
  Untouched. The §4.2 change ADDS a guard trigger (more parks, never fewer); the existing approved-group
  short-circuit (`pipeline.py`) is left intact; the sign-off / return-to-block path is not touched.
- **CLAUDE.md "ER thresholds affecting a real person need human sign-off."** This gate does NOT change
  0.92; every scoring change is precision-favoring (can only reduce/park merges). The anchor-conflict
  POLICY itself is the genuine person-affecting fork — it is resolved WITH the human (§3) and recorded as
  ADR 0040 before Slice 1 builds. The required sign-off is also satisfied by the gate process: the
  fresh-context Opus judge approves, then the human performs the final `--ff-only` merge (STEP 4). No
  individual-affecting score change is auto-promoted.

---

## 10. Pre-existing, out-of-gate working-tree state (ESCALATED — do not absorb)

At session start the working tree carried uncommitted/untracked **fleet-setup** files unrelated to B-5:
`.claude/agents/*`, `.claude/hooks/*`, `.claude/settings.json`, `.claude/council-broker.log`,
`.claude/fleet.*`, `orchestrator/`, `scripts/council/`, `scripts/dev/fleet_*.sh`, `scripts/smoke/`,
`scripts/dev/{local_ci,orient}.sh`. The scope-guard hook strips `.claude/` paths, so those are inert to
it; the rest are NOT added to the gate.scope glob allow-list (adding them would silently widen the B-5
blast radius and defeat the guard — exactly as the B-3 gate.scope handled the same class of files). They
must NOT be touched, staged, or committed on the `gate/b5` branch. Resolving them touches pre-existing
state the planner does not own → HARD STOP, escalated to the human.

---

## 11. Definition of done (judge checklist)

1. The human has resolved the OPEN fork (§3); ADR 0040 records the chosen (A)/(B)/(C), is moved from
   PROPOSED to ACCEPTED, and its status/Decision reflect the answer.
2. `tests/unit/test_resolution_anchor_conflict.py` exists and the fork-selected functions (§6) pass.
3. `tests/unit/test_resolution_distinguishing_evidence.py` (INV-4) and
   `tests/unit/test_resolution_multiscript.py` (INV-5) pass **unchanged**.
4. Only the in-scope production files changed (`splink_model.py`, `review.py`, `anchors.py` per the
   answer); `graph/writer.py` UNCHANGED; scope honored (`.claude/gate.scope`).
5. The final wikidata m/u (and any B-3 clash retune), the chosen `get_anchors` conflict representation,
   and the measured worst-case scores are recorded in ADR 0040.
6. `scripts/dev/local_ci.sh` (quality + security mirror) green before approval; GitHub `quality` +
   `security` checks green before the human merges.
