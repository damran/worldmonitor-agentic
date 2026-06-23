# 0039 — ER distinguishing evidence (registration-number discriminator + generic-token guard)

- **Status:** accepted
- **Date:** 2026-06-23
- **Gate:** B-3 (`docs/reviews/GATE_B3_SPEC.md`)
- **Touches:** `resolution/splink_model.py` ONLY (`_name_fingerprint`, `_flatten`, the `comparisons`
  list in `score_pairs`). Tests in `tests/unit/`.
- **Extends:** ADR 0035 (multi-script name canonicalization). **Does NOT relitigate** the name-fingerprint
  approach, the schema-compatibility gate, the 0.92 merge threshold, or the catastrophic-merge guard — all
  of those remain exactly as 0035 set them.
- **Source:** Production-readiness audit B-3 (`docs/reviews/PRODUCTION_READINESS_AUDIT_2026-06-23.md`,
  lines 87–99). ADR 0035 itself named this as the owed follow-up (§"Identifier-aware discrimination").

## Context

ADR 0035 made the ER name key script-stable. By making *more* name matches reach the Splink comparison,
it surfaced two pre-existing holes that **silently over-merge distinct legal entities** on ordinary
real corporate/sanctions data. Both are reproduced (verified this gate with `uv run python`):

1. **Generic-token fingerprint collapse.** `_name_fingerprint` returns
   `fingerprints.remove_types(fingerprints.generate(entity.caption))`. `remove_types` strips generic
   descriptors, not just legal forms, so `"International Trading Co Ltd"` and
   `"Import Export Trading Co Ltd"` **both reduce to the single token `"trading"`** and hit the exact-name
   level (m=0.99). Two distinct orgs fuse. Any name whose only non-legal-form token is itself generic
   (`group`, `holdings`, `general`, `global`, `trading`, …) is exposed.
2. **Registration-number blindness.** `_flatten` projects only `name_fp / country / birthDate / wikidataId`;
   the comparison set has **no `registrationNumber` / `taxNumber`**. Two `Company` rows with the same
   name + country but **different `registrationNumber`** score ~0.9825 and merge. The real Legion pair
   (ADR 0035) is itself two distinct OFAC listings with different registration numbers (1177746464378 /
   5177746188219).

Neither trips the catastrophic-merge guard (`review.py`), which fires only on *sensitivity* or cluster
*size > 10*. A same-trade-name pair of **non-sensitive** companies auto-merges with no review, no alert,
and no audit beyond a `decision="merged"` row — silently corrupting the resolved graph that *is* the
product.

This is **not a product/architecture fork.** The direction is locked by the audit's recommended fix and
was pre-named by ADR 0035. What remains is an implementation decision (how to encode the negative evidence
and how to demote the over-stripped key), recorded here.

FtM-contract check (verified this gate): `registrationNumber` and `taxNumber` exist on `Company`,
`Organization`, `LegalEntity`, `Person`; both are FtM type `identifier`. No `wm:` extension is needed —
these are the correct ontology properties.

## Decision

Add **discriminating / negative evidence** to the pairwise model, on top of ADR 0035, in two parts.

### 1. Distinguishing-id comparison level (Fellegi–Sunter negative evidence)

Project a single `reg_id`-style column in `_flatten` from the entity's FtM `identifier`-typed values
(`registrationNumber` + `taxNumber`, normalized via the FtM `identifier` type so trivial differences don't
clash and so the same id stored as `taxNumber` on one record and `registrationNumber` on the other is
treated as the same identifier). Add a comparison with **three levels**:

- **both-present-and-equal** (identifier sets intersect) → corroborating, `m > u`.
- **both-present-and-clash** (both non-empty AND disjoint) → **penalizing**, `m << u`. Because Splink is
  multiplicative on `m/u` Bayes factors, this level's match weight is `log2(m/u) < 0` and *actively lowers*
  the score. This is the new negative-evidence level the audit prescribes.
- **at-least-one-null** (either id set empty) → neutral null level (`is_null_level: True`), so a missing id
  never penalizes a genuine duplicate.

The clash level's m/u is calibrated so the worst case for over-merge — exact name (m=0.99/u=0.0001) AND
exact country (m=0.85/u=0.15) AND a clashing id — falls **below the 0.92 merge boundary**. The builder
verifies the numbers against INV-1 (must drop) and INV-3 (must NOT regress) by running the named tests,
and **records the final m/u and the resulting worst-case score here before this ADR is accepted**. (Spec
starting point to calibrate from: equal `m=0.95/u=0.01`; clash `m=0.0005/u=0.30`, Bayes factor ≈ 0.0017.)

Multi-valued safety (load-bearing): FtM ids are multi-valued. "Clash" means the two records share **no**
identifier while both have at least one — never "their first ids differ". The projection is order-
independent. The exact representation the builder lands on (sorted-normalized scalar key + presence flag,
or equivalent) is recorded here on implementation.

### 2. Generic-token guard on the exact-name key (chosen: demote, option (i))

In `_name_fingerprint`, after `remove_types`, if the result is a **single token** that is in a small,
explicit **generic stoplist** (`trading`, `group`, `holdings`, `general`, `global`, `company`,
`international`, `services`, `industries`, `enterprise(s)`, … — a tight, documented list curated from the
over-stripped residue seen on real names), **fall back to the un-`remove_types` fingerprint**
(`fingerprints.generate(...)`, which still strips legal forms but keeps distinguishing tokens like
`intl` / `imp` / `exp`). So `"International Trading Co Ltd"` → `"co intl ltd trading"` and
`"Import Export Trading Co Ltd"` → `"co exp imp ltd trading"` — **different** keys, jaro_winkler below the
exact level, no merge on name alone. The guard returns the **richer key** (never `None`), so the entity
still blocks and still contributes its country/id/wikidata signals.

The multi-token, non-generic case is untouched: the Legion record still fingerprints to `"komplekt legion"`
(2 real tokens) and every ADR-0035 case is preserved.

## Alternatives considered (for the generic-token guard)

- **(i) Demote over-stripped single-generic-token fingerprints to the richer key — CHOSEN.** Smallest
  change, localized to `_name_fingerprint`, affects only the pathological single-generic-token case, and is
  precision-favoring. The fingerprint *is* the key, so the demotion belongs at the key.
- **(ii) Keep a richer fingerprint everywhere (skip `remove_types`) + a generic stoplist.** Rejected: it
  changes the key for *all* names and risks regressing ADR-0035's cross-script bridging (the legal-form
  stripping is exactly what maps `ООО` ↔ `LLC`). Broad blast radius for a narrow defect.
- **(iii) Gate the exact-name comparison level on token count in the Splink SQL.** Rejected: pushes name-key
  policy into the Splink settings and splits the rule across two places (key + comparison). Harder to test
  and reason about than fixing the key.

## Consequences / trade-off

This change is **precision-favoring**: both parts can only *lower* scores or make two keys *less* likely to
collide — they can never *create* a merge that did not happen before. Consistent with "leads not verdicts"
and the catastrophic-merge invariant: B-3 is explicitly about stopping false-positive over-merges. The
accepted cost is a possible false-negative (e.g. two records of one org *both* named only "Trading Ltd"
would no longer merge on name alone) — an extremely weak signal that should never have merged unaided, and
the safe side of the trade-off for a graph where fusing two distinct sanctioned legal persons is the worse
error.

**Locked-invariant impact:** G1 (provenance) untouched — no node/edge added or removed. G4 (tenant
isolation) untouched — `score_pairs` keeps its per-batch `Sequence[FtmEntity]` contract; nothing crosses a
tenant. Append-only / no-un-merge untouched and reinforced (fewer false merges to ever un-merge).
Catastrophic-merge guard / merge-guard default-block / ADR-0024 return-to-block untouched — this gate does
not touch the guard or sign-off; it simply reduces the population of false-positive pairs reaching them.

**Human sign-off (CLAUDE.md "ER thresholds affecting a real person").** This ADR does NOT change the 0.92
threshold; it adds discriminating evidence and demotes an over-stripped key, all precision-favoring. The
required human sign-off is satisfied by the **gate process**: the change lands only after the fresh-context
Opus judge approves and the human performs the final `--ff-only` merge (STEP 4). No individual-affecting
score change is auto-promoted.

## Out of scope (explicit)

B-4, H-2, H-3, H-6; any file other than `splink_model.py`; the nomenklatura `LogicV2` post-blocking
re-scorer (ADR 0035 deferred — its own future ADR); Splink EM training; abjad-script handling (ADR-0035
KNOWN GAP); changing the 0.92 threshold, the name-fingerprint approach, the schema-compat gate, the guard,
or the `score_pairs` signature.

## Builder record (measured — this ADR is ACCEPTED on these numbers)

### Final `reg_id` projection representation

`_distinguishing_ids` returns a **non-null `str`** (never `None`): the entity's
`registrationNumber` + `taxNumber` values are each cleaned via the FtM `identifier` type, deduped
into a set, sorted for determinism, and **joined with the ASCII Unit Separator `0x1F`** into a
single VARCHAR. A record with no id projects the **empty string `""`** (the sentinel), not `None`.

- **Why a packed VARCHAR and not a list/`None`.** DuckDB infers the pandas column type from
  content. An all-`None` column (every record in a batch id-less — the common case) is inferred as
  **`INTEGER`**, which then fails to bind `len(reg_id_l)` / `string_split(reg_id_l, …)` in the
  comparison SQL (`BinderException: No function matches … 'len(INTEGER)'`) and crashed
  `score_pairs` for EVERY id-less batch (the confirmed regression; reproduced and fixed this gate).
  An all-empty-string column is always inferred as **`VARCHAR`**, so the SQL binds in all three
  cases: (a) some rows have ids, (b) no rows have ids, (c) mixed. Verified explicitly for case (b).
- **Set / overlap semantics preserved.** The multi-valued comparison runs entirely in SQL:
  `string_split("reg_id_l", chr(31))` reconstitutes each side's id SET, and
  `list_has_any(split_l, split_r)` is the shared-id (overlap) test. So ids `{X, Y}` vs `{Y}`
  OVERLAP (not a clash) and still merge — `test_multivalued_id_overlap_is_not_a_clash` passes. The
  clash (ELSE) level is reached ONLY when both sets are non-empty AND disjoint.
- **`0x1F` is a safe, collision-free delimiter.** The FtM `identifier` type's `clean` strips ALL
  C0 control characters (`"a\x1fb"` → `"ab"`, verified), so a cleaned id can never itself contain
  `0x1F`. No escaping is needed.
- **Cross-field clash (INV-1b).** `registrationNumber` and `taxNumber` feed the SAME column, so the
  same id stored under different fields is treated as the same identifier, and two distinct ids
  across the two fields are a clash.
- **Null-level order (load-bearing).** The null level (`reg_id IS NULL OR len(reg_id) = 0` on
  either side) is the FIRST level, so a missing id on either side is always the neutral path and can
  never fall through to the clash branch (INV-3b).

### Final m/u numbers (expert-set, verified against the named tests)

| Level | condition | m | u | Bayes factor m/u | role |
|---|---|---|---|---|---|
| null | either side empty / NULL | — | — | (`is_null_level`, no contribution) | neutral |
| shared-id | `list_has_any(split_l, split_r)` (sets overlap) | 0.95 | 0.01 | 95 | corroborating |
| clash | ELSE (both non-empty, disjoint) | 0.0005 | 0.30 | ≈ 0.001667 | **penalizing** |

These are the spec's starting-point numbers; they were verified (not re-tuned) to satisfy INV-1 and
not regress INV-3. The generic-token guard and the id level do not interact at the borderline, so
Slice 3 (calibration reconciliation) was a no-op and was dropped.

### Measured scores (DuckDB, this gate)

- **INV-1 worst case for over-merge** — exact name (`m=0.99/u=0.0001`) AND exact country
  (`m=0.85/u=0.15`) AND a clashing id: **top match probability = 0.0856** (measured at
  `predict_threshold=0.0`; at the default `0.5` threshold Splink does not even emit the pair). Far
  **below** the 0.92 merge boundary. INV-1b (cross-field tax-vs-registration clash) is identical:
  **0.0856**.
- **INV-3 (matching id, must still merge):** top probability **0.9998** ≥ 0.92. Merges.
- **INV-3b baseline (no id on either side, ADR-0035 behavior preserved):** top probability
  **0.9825** ≥ 0.92. Merges — the null level is neutral, exactly as before this gate.

### Final generic stoplist contents (`_GENERIC_NAME_TOKENS`)

`trading`, `group`, `holdings`, `general`, `global`, `company`, `international`, `services`,
`industries`, `enterprise`, `enterprises`, `import`, `export`. A tight, documented set of
descriptors that are (a) generic enough to recur across unrelated firms and (b) routinely the lone
non-legal-form token surviving `remove_types` on real corporate names. Only the SINGLE-generic-token
pathological case is demoted to the richer `fingerprints.generate` key; multi-token keys (e.g.
`"komplekt legion"`) and single non-generic tokens are untouched — so every ADR-0035 case is
preserved (INV-4..INV-8 pass unchanged).
