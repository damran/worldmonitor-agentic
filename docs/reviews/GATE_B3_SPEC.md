# Gate B-3 — ER distinguishing evidence (registration-number discriminator + generic-token guard)

- **Gate id:** `B-3`
- **Status:** SPEC (planner output — STEP 1 of a reviewer-gated build)
- **Branch:** `gate/b3-er-distinguishing-evidence`
- **ADR:** `docs/decisions/0039-er-distinguishing-evidence.md` (PROPOSED)
- **Extends (does NOT relitigate):** ADR 0035 (multi-script name fingerprint)
- **Source finding:** `docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md` B-3 (lines 87–99); must-fix order #3 (line 266)
- **Locked direction:** the audit's "Recommended fix" (line 97). This spec defines the HOW only.

---

## 1. Problem statement

ADR 0035 made the name fingerprint script-stable and added a schema-compatibility gate. That fix is
correct but, by making *more* name matches reach the comparison, it exposed two pre-existing holes
that **silently fuse distinct real legal entities** into one canonical node. Both are REPRODUCED
end-to-end against real corporate/sanctions data, and neither trips the catastrophic-merge guard
(`review.py`) because that guard fires only on *sensitivity* or cluster *size > 10* — a same-trade-name
pair of two **non-sensitive** companies is auto-merged with no review, no alert, and no audit beyond a
`decision="merged"` row. This corrupts the resolved graph that *is* the product.

### Defect 1 — generic-token fingerprint collapse

`_name_fingerprint` (`splink_model.py:132–133`) returns
`fingerprints.remove_types(fingerprints.generate(entity.caption))`. `remove_types` strips generic
descriptors, not just legal forms. REPRODUCED (verified this gate, `uv run python`):

```
'International Trading Co Ltd'   gen='co intl ltd trading'      remove_types='trading'   ntok=1
'Import Export Trading Co Ltd'   gen='co exp imp ltd trading'   remove_types='trading'   ntok=1
```

Both distinct orgs reduce to the single token `"trading"` and hit the **exact** name level
(`splink_model.py:59–63`, m=0.99 / u=0.0001). With same `country` they score well above the 0.92
threshold and merge. The same collapse happens for any name whose only non-legal-form token is itself
generic (`group`, `holdings`, `general`, `global`, `trading`, …).

> Note (no-regression anchor): the ADR-0035 Legion pair fingerprints to `"komplekt legion"` (**2 tokens**,
> neither generic) and is therefore NOT affected by the single-generic-token rule. Verified this gate.

### Defect 2 — registration-number blindness

`_flatten` (`splink_model.py:136–149`) projects only `name_fp / country / birth_date / wikidata_id`.
The comparison set (`splink_model.py:182–189`) has **no `registrationNumber` / `taxNumber`** column.
REPRODUCED (and corroborated by ADR 0035 §"Identifier-aware discrimination", and by the real Legion
pair carrying different registration numbers 1177746464378 / 5177746188219): two `Company` rows with the
**same name + country** but **different `registrationNumber`** score ~0.9825 and merge. A present,
*clashing*, government-issued identifier is the single strongest signal that two records are **distinct**
legal persons, and the model currently ignores it entirely.

FtM property check (verified this gate, `followthemoney` model): `registrationNumber` and `taxNumber`
exist on `Company`, `Organization`, `LegalEntity`, and `Person`; both are of FtM type `identifier`. They
are the correct ontology-contract properties to use (no `wm:` extension needed).

---

## 2. Locked direction (from the audit — spec the HOW, do not relitigate)

1. Add `registrationNumber`/`taxNumber` as a **distinguishing / negative-evidence** comparison level: a
   *present-but-clashing* id must drop the pair **below** the merge threshold.
2. Require a **minimum distinguishing-token count** before a `remove_types` fingerprint is usable as an
   *exact* key. An over-stripped single generic token must NOT be usable as the sole exact match key.

This gate does **not** change: the name fingerprint approach, the schema-compatibility gate, the 0.92
merge threshold, the catastrophic-merge guard, or the `score_pairs` public signature. (ADR 0035 invariants.)

---

## 3. Design — the precise HOW the builder must implement

### 3.1 Distinguishing-id comparison level (Fellegi–Sunter, negative evidence)

Splink scores are multiplicative Bayes factors: each comparison contributes match weight
`log2(m/u)`. A level only **lowers** the posterior when `m/u < 1`. So the id comparison needs THREE
levels with the following shape (this is the locked level structure; exact m/u numbers below):

| Level | SQL condition (over the projected `reg_id` column) | role | requirement |
|---|---|---|---|
| (a) both-present-and-equal | `reg_id_l = reg_id_r` (and both non-null) | corroborating | `m > u` (Bayes factor > 1) |
| (b) both-present-and-clash | both non-null AND `reg_id_l <> reg_id_r` | **penalizing** | `m << u` (Bayes factor < 1) — this is the new negative-evidence level |
| (c) at-least-one-null | `reg_id_l IS NULL OR reg_id_r IS NULL` | neutral / null level | `is_null_level: True` (no contribution) |

The order matters: the null level must be evaluated such that a missing id on *either* side is the
neutral path (never the clash path). Builder must confirm the null condition cannot be reached by the
clash branch.

**Projected column.** `_flatten` must add a single `reg_id` column. To be robust to the same id being
stored as `registrationNumber` on one record and `taxNumber` on the other (and to multi-valued FtM
identifiers), project the entity's identifiers via the FtM `identifier` type. The chosen projection is a
**deterministic set comparison**, not `first()` — see 3.1.1.

#### 3.1.1 Multi-valued identifiers (this is load-bearing — get it right)

FtM properties are multi-valued. `entity.get("registrationNumber")` returns a `list`. A naive
`first()` projection would make level (b) fire spuriously when two records of the SAME entity list their
ids in different order, or when one record carries two ids and the other one of them. The clash level
must therefore mean **"the two records share NO identifier AND both have at least one"**, not "their
first ids differ". The builder implements one of:

- **(Recommended)** Project the *set* of normalized identifiers and compare in SQL/Python as: equal-level
  = sets intersect; clash-level = both sets non-empty AND disjoint; null-level = either set empty. If the
  set comparison cannot be expressed cleanly as a single Splink scalar column, compute a derived boolean
  pairwise is not possible inside Splink's column model — so instead project a **canonicalized scalar
  key** that is order-independent (e.g. the sorted, normalized id list joined) for the *equal* test, and
  carry a separate presence flag. The ADR records the exact representation the builder lands on.

The intent the tests pin (INV-1 / INV-3) is what is locked; the column representation is the builder's
call as long as the multi-valued / cross-field (`registrationNumber` vs `taxNumber`) cases behave
correctly. Identifier normalization should reuse the FtM `identifier` type's clean/normalize (consistent
with how the codebase already uses `registry` types), so `"12345"` and `" 12345 "` are equal.

#### 3.1.2 m/u calibration (must be checked, not guessed)

The builder must choose m/u for level (b) such that the **worst case for over-merge** — a pair that hits
the **exact name** level (m=0.99/u=0.0001) AND **exact country** (m=0.85/u=0.15) AND has a **clashing id**
— lands **below** `predict_threshold` (0.92 in the merge sense; note `score_pairs` is called with
`predict_threshold=0.5` in tests via `cluster_and_merge`, and the 0.92 gate is applied in
`merge.cluster_and_merge`; the test-author asserts against the 0.92 merge boundary, see §5). Concretely,
the clash level must contribute a Bayes factor small enough to overcome the combined name+country weight.
With name exact (`m/u = 9900`) × country exact (`m/u ≈ 5.67`) the pre-clash odds are large; the clash
level's `u/m` must exceed that product so the posterior falls under the threshold. A starting point to
calibrate from: equal `m=0.95/u=0.01`; **clash `m=0.0005/u=0.30`** (Bayes factor ≈ 0.0017). The builder
MUST verify the chosen numbers satisfy INV-1 and do NOT break INV-3 by running the named tests, and record
the final numbers + the resulting worst-case score in the ADR.

**G1 / append-only / merge-guard note.** This level only *lowers* scores (precision-favoring). It can
never *cause* a merge that did not happen before, so it cannot weaken provenance (G1), append-only, or the
merge-guard. It strictly removes false-positive merges.

### 3.2 Generic-token guard on the exact-name key

The recommended option (justified in the ADR) is **option (i): demote over-stripped single-generic-token
fingerprints so they never serve as the SOLE exact-name key.** Implementation in `_name_fingerprint`:

- After `fingerprints.remove_types(...)`, split the result into tokens.
- If the result is a **single token** AND that token is in a small, explicit **generic stoplist**
  (`{"trading", "group", "holdings", "general", "global", "company", "international", "services",
  "industries", "enterprise", "enterprises", ...}` — the builder curates a tight list from the
  over-stripped residue observed on real names; keep it small and documented), then **fall back to the
  un-`remove_types` fingerprint** (`fingerprints.generate(...)`, which still strips legal forms via
  `fingerprints` but retains `intl` / `imp` / `exp` distinguishing tokens) instead of the over-stripped
  single token. This makes `"International Trading Co Ltd"` → `"co intl ltd trading"` and
  `"Import Export Trading Co Ltd"` → `"co exp imp ltd trading"` — **different** keys, jaro_winkler below
  the exact level → no merge on name alone (INV-2).

> Rationale for option (i) over the alternatives (full justification in ADR 0039):
> - (ii) "keep a richer fingerprint everywhere" would change the key for ALL names and risk regressing the
>   ADR-0035 cross-script cases (the legal-form stripping is exactly what bridges `ООО`/`LLC`). Rejected.
> - (iii) "gate the exact-name level on token count in SQL" pushes name-key policy into the Splink settings
>   and splits the rule across two places. The fingerprint *is* the key; the demotion belongs at the key.
> - (i) is the smallest change, localized to `_name_fingerprint`, and only affects the pathological
>   single-generic-token case. It is **precision-favoring** (a slightly richer key can only make two names
>   *less* likely to collide), consistent with "leads not verdicts" + the catastrophic-merge invariant:
>   B-3 is explicitly about stopping false-positive over-merges. A possible false-negative (two records of
>   ONE org both named e.g. only "Trading Ltd" would no longer merge on name alone) is the acceptable side
>   of the trade-off and is, anyway, an extremely weak merge signal that should never have fired.

The guard must NOT return `None` for these (returning `None` would suppress the `country` / id / wikidata
signals for that entity too). It returns the **richer fingerprint**, so the entity still participates in
blocking and comparison — it just won't hit the exact level on a generic token alone.

---

## 4. Acceptance criteria — explicit, testable invariants

All invariants are asserted against the `score_pairs` + `cluster_and_merge` path on hand-built
`make_entity` fixtures (no live stores). "Merge / no-merge" means a real merge cluster
(`c.is_merge`) at the 0.92 boundary, matching the ADR-0035 test convention.

- **INV-1 (registration clash blocks merge).** Two **non-sensitive** `Company` entities with the **same
  name + same country** but **different `registrationNumber`** do NOT merge: no merge cluster, and the top
  pair probability is **below 0.92**. (Defect 2.)
- **INV-1b (taxNumber clash, cross-field).** Same as INV-1 but one record carries the clashing id as
  `taxNumber` and the other as `registrationNumber` — still a clash, still no merge. (Pins 3.1.1.)
- **INV-2 (generic-token collapse blocked).** Two distinct orgs whose names both over-strip to the same
  single generic token (`"International Trading Co Ltd"` vs `"Import Export Trading Co Ltd"`, both →
  `"trading"` pre-fix) do NOT merge **on name alone** (same country, no ids): no merge cluster, top
  probability below 0.92. (Defect 1.)
- **INV-3 (no recall regression — matching id).** A genuine duplicate pair with the **same name + country
  + MATCHING `registrationNumber`** still merges (≥ 0.92). (Guards against over-correction.)
- **INV-3b (no recall regression — id absent on one/both sides).** A genuine duplicate pair with same name
  + country and the id **null on one or both sides** still merges exactly as before (the id comparison is
  neutral, not penalizing, when absent). This is the null-level guarantee from 3.1.
- **INV-4 (ADR-0035 Legion still merges + parks).** The bilingual Legion pair (sensitive) still merges and
  the catastrophic-merge guard still **parks** it. NB the Legion pair has *different* registration numbers —
  INV-4 confirms the clash level does NOT block it from merging at the score level **only because** it is
  sensitive and parks; the spec requires the test to assert the EXISTING ADR-0035 behavior is unchanged.
  > Decision recorded for the judge: ADR 0035 deliberately lets the sensitive Legion pair reach the guard
  > and PARK (human rejects it). If the id-clash level now drops Legion below 0.92, it would no longer reach
  > the guard — changing ADR-0035's validated outcome. To AVOID relitigating 0035, the test fixtures for
  > INV-4 use the ADR-0035 Legion fixture **as-is (no registrationNumber set)**, preserving the documented
  > merge+park. INV-1 uses a *separate* non-sensitive fixture WITH clashing ids. The two are independent.
- **INV-5 (ADR-0035 us_dod_chinese_milcorps).** The three real us_dod company names (735× "Co Ltd" family,
  represented by the ADR-0035 sample) still produce **0** merges (`test_distinct_orgs_sharing_legal_form_do_not_merge`).
- **INV-6 (ADR-0035 my_aob single real pair).** The `my_aob_sanctions` "Sathiea Seelean" same-script
  duplicate still merges (≥ 0.92) — exactly **1** real pair, no regression.
- **INV-7 (ADR-0035 multi-script no-shared-variant).** The cross-script no-shared-variant Legion case still
  merges (≥ 0.92).
- **INV-8 (schema gate intact).** The Org↔Person and Org↔Vessel namesakes are still dropped; compatible
  Org↔Company still merges. (`score_pairs` schema-compat behavior unchanged.)

INV-4..INV-8 are satisfied by the **existing** `tests/unit/test_resolution_multiscript.py` suite passing
unchanged. The builder MUST NOT modify those tests; they are the ADR-0035 regression guards. If any of
them goes red, the change is wrong, not the test.

---

## 5. Named tests (the test-author writes these; builder does NOT)

New file: **`tests/unit/test_resolution_distinguishing_evidence.py`**

| Test function | Invariant | Asserts |
|---|---|---|
| `test_clashing_registration_number_blocks_nonsensitive_merge` | INV-1 | two non-sensitive Companies, same name+country, different `registrationNumber` → no merge cluster; `_top_probability < 0.92` |
| `test_clashing_id_crossfield_tax_vs_registration_blocks_merge` | INV-1b | clash detected when one side `taxNumber`, other `registrationNumber` → no merge |
| `test_matching_registration_number_still_merges` | INV-3 | same name+country+matching `registrationNumber` → merges, `>= 0.92` |
| `test_missing_id_one_side_still_merges` | INV-3b | same name+country, id present on one side only → merges, `>= 0.92` |
| `test_missing_id_both_sides_still_merges` | INV-3b | same name+country, no id either side → merges as before, `>= 0.92` |
| `test_multivalued_id_overlap_is_not_a_clash` | 3.1.1 | record A ids `{X, Y}`, record B ids `{Y}` → overlap, NOT a clash → still merges |
| `test_generic_single_token_names_do_not_merge_on_name_alone` | INV-2 | `"International Trading Co Ltd"` vs `"Import Export Trading Co Ltd"`, same country, no ids → no merge; `_top_probability < 0.92` |
| `test_name_fingerprint_demotes_single_generic_token` | INV-2 (unit) | `_name_fingerprint(_org("International Trading Co Ltd"))` is NOT the bare `"trading"` (asserts the demotion to a richer key; distinct from the Import-Export variant) |
| `test_legion_pair_fingerprint_unaffected_two_real_tokens` | INV-4 anchor | `_name_fingerprint` of the Legion record is still `"komplekt legion"` (the multi-token, non-generic case is untouched) |

Existing file (RUN UNCHANGED as regression guards): **`tests/unit/test_resolution_multiscript.py`**
covers INV-4..INV-8. The CI gate is: the new file passes AND the existing file passes with zero edits.

Test conventions to follow (match the existing suite): use `make_entity`, the local `_org` /
`_top_probability` helpers (copy the pattern), `cluster_and_merge` + `score_pairs` for merge assertions,
`needs_review` for the park assertion. Non-sensitive fixtures must NOT set `topics`/`sanction`.

---

## 6. Slice breakdown (builder slices — each individually mergeable, each with its own green tests)

The gate is small (one production file). It is broken into **two independent slices** that can land in
either order; a third optional slice is a no-op consolidation only if needed.

- **Slice 1 — generic-token guard in `_name_fingerprint`.**
  - File: `src/worldmonitor/resolution/splink_model.py` (`_name_fingerprint` only).
  - Implements §3.2 (demote single-generic-token fingerprints to the richer key; curated stoplist).
  - Proves: INV-2, plus the two unit fingerprint tests, plus INV-4..INV-8 stay green.
  - Independent of Slice 2 (touches only the name key; no new column).

- **Slice 2 — distinguishing-id comparison level.**
  - File: `src/worldmonitor/resolution/splink_model.py` (`_flatten` + a new `_distinguishing_id_comparison`
    helper + wiring into the `comparisons=[...]` list in `score_pairs`; multi-valued identifier projection
    per §3.1.1).
  - Implements §3.1 (three-level negative-evidence comparison; m/u calibrated so INV-1 holds and INV-3
    does not regress).
  - Proves: INV-1, INV-1b, INV-3, INV-3b, the multivalued-overlap test, plus INV-4..INV-8 stay green.
  - Independent of Slice 1 (adds a column + comparison; does not touch the name key).

- **Slice 3 (only if the two interact) — calibration reconciliation.**
  - If, when both slices are present, the combined behavior needs a single m/u re-tune to keep ALL
    invariants green (e.g. the richer fingerprint changes a borderline score), do it here as a small,
    test-driven adjustment with the final numbers recorded in ADR 0039. If no interaction surfaces, this
    slice is dropped. **No new behavior** beyond satisfying the already-named invariants.

The builder may land Slice 1 and Slice 2 as two PRs or one; the checker reproduces every INV on each. No
slice may modify `test_resolution_multiscript.py` or weaken any invariant.

---

## 7. Out of scope (do NOT touch under this gate)

- **B-4** (backup/restore, driver supervision, compose, `/ready`) — separate gate.
- **H-2** (schema-skip must drop the member), **H-3** (multi-source provenance), **H-6** (stream GeoNames).
- Any file other than `src/worldmonitor/resolution/splink_model.py` (production) and the test files named
  in §5. Specifically NOT: `merge.py`, `review.py`, `pipeline.py`, `signoff.py`, the ontology, the API.
- The **nomenklatura `LogicV2` post-blocking re-scorer** (ADR 0035 deferred) — its own future ADR; not B-3.
- Changing the 0.92 threshold, the name-fingerprint *approach*, the schema-compat gate, the
  catastrophic-merge guard, or the `score_pairs` signature (ADR-0035 locked).
- Splink **EM training** (the model stays expert-set m/u per the module docstring).
- Abjad-script handling (ADR-0035 KNOWN GAP).

---

## 8. Invariant compliance (the locked fleet invariants — how this gate holds them)

- **G1 provenance on every node AND edge.** Untouched. This gate only changes *which pairs score above the
  merge threshold*; it adds no node/edge and removes none of the provenance the merge path attaches.
- **G4 tenant isolation.** Untouched. `score_pairs` operates on a single per-batch `Sequence[FtmEntity]`;
  tenant scoping is enforced upstream at the batch boundary. The signature and the per-batch contract do
  not change, so no key/filter crosses a tenant.
- **Append-only / no un-merge.** Untouched and *strengthened in spirit*: the change can only *prevent*
  false merges (it lowers scores, never raises them past a new threshold), so it never creates a merge that
  would later need un-merging.
- **Canonical-canonical only via the guard / merge-guard default block / ADR-0024 return-to-block.**
  Untouched — this gate does not touch the guard or the sign-off path. It reduces the population of pairs
  that reach the guard at all (fewer false positives), which is strictly safer.
- **CLAUDE.md: "ER thresholds affecting a real person need human sign-off."** This gate *adds* discriminating
  evidence and *demotes* an over-stripped key — it does not change the 0.92 threshold itself, and every
  change here is **precision-favoring** (it can only reduce merges). The human-sign-off invariant is
  satisfied by the **gate process**: the change merges only after the fresh-context Opus **judge** approves
  and the **human** performs the final `--ff-only` merge in STEP 4. Calling this out explicitly so the judge
  records it: B-3 does not auto-promote any individual-affecting score change; it lands through the gated
  propose→evaluate→gate→promote path.

---

## 9. Definition of done (judge checklist)

1. `tests/unit/test_resolution_distinguishing_evidence.py` exists and all functions in §5 pass.
2. `tests/unit/test_resolution_multiscript.py` passes **unchanged** (INV-4..INV-8).
3. Only `src/worldmonitor/resolution/splink_model.py` changed in production; scope honored
   (`.claude/gate.scope`).
4. The final m/u numbers and the chosen identifier-projection representation are recorded in ADR 0039,
   and ADR 0039 is moved from PROPOSED to ACCEPTED as part of the merge.
5. `scripts/dev/local_ci.sh` (quality + security mirror) green before approval; GitHub `quality` +
   `security` checks green before the human merges.
