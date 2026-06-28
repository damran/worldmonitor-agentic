# 0073 — Abjad (Arabic/Persian) name normalization in the ER fingerprint

- **Status:** accepted
- **Date:** 2026-06-28
- **Touches:** `resolution/splink_model.py` (`_name_fingerprint`, the ER name-matching key).
  **Invariant-touching** (it changes *which* entities reach a high name score), so it carries
  mandatory `@given` property tests (`tests/property/`). The **0.92 merge threshold, the
  catastrophic-merge guard, and sensitive-entity parking are UNCHANGED** — this is a *recall*
  normalization, exactly the class of fix as ADR [0035](0035-multiscript-name-canonicalization.md),
  not a threshold or guard change.

## Context

ADR 0035 projected a script-stable `name_fp` via `fingerprints.generate(entity.caption)` and recorded
a **deferred KNOWN GAP**: `fingerprints` renders **abjad scripts (Arabic/Persian)** as lossy consonant
skeletons, so it is "not a reliable *sole* key for those." H-4 (roadmap "Next" / `phase-2-complete-
stage-4-next`) is the slice that pays that gap down at the projection site.

Probing the **actual** `fingerprints` behaviour on real abjad input (not assumptions) isolated the
**dominant, concrete failure** — it is the *same* bug class ADR 0035 fixed for Cyrillic/Latin (two
records of ONE entity projecting DIFFERENT keys), but abjad-specific:

| Input (same name) | `fingerprints.generate` | |
|---|---|---|
| `محمد` (Muhammad, bare) | `'mhmd'` | |
| `مُحَمَّد` (Muhammad, **with tashkeel**) | `'muhamad'` | **≠ — never matches** |
| `علي` (Ali, bare) | `'ly'` | |
| `عَلِيّ` (Ali, **with tashkeel**) | `'ali'` | **≠** |
| `حسين` (Hussein, bare) | `'hsyn'` | |
| `حُسَيْن` (Hussein, **with tashkeel**) | `'husayn'` | **≠** |

Arabic short vowels (**harakat / tashkeel**: fatḥa, kasra, ḍamma, shadda, sukūn, tanwīn, …) are
pronunciation aids normally *unwritten*. When a source *does* include them, `fingerprints`
transliterates the marked vowels into Latin vowels, so the **same name written with vs. without
tashkeel produces different keys** and two records of one sanctioned individual never co-block / never
score the exact level. For a sanctions/CTI graph fed by Arabic/Persian sources this is a core-ER
recall hole on exactly the entities we most need to resolve.

What `fingerprints` **already** handles correctly (verified, therefore **out of scope** — re-doing it
would only add regression risk): alef variants (`أ/إ/آ/ا → 'ahmd'`), Arabic vs Persian yeh
(`ي/ی`) and kaf (`ك/ک`), tatweel/kashida (`U+0640`), and Arabic-Indic vs Western digits
(`٢٠٢٤/2024`) all converge unchanged.

## Decision

Pre-normalize the caption by **stripping Arabic combining marks (harakat/tashkeel) and tatweel before
`fingerprints.generate`**, in `_name_fingerprint`:

```
fingerprint = fingerprints.generate(_strip_arabic_marks(entity.caption))
```

`_strip_arabic_marks` **deletes** every codepoint in these ranges and **nothing else**:

- `U+0610–U+061A` (Arabic signs), `U+064B–U+065F` (harakat + extended marks), `U+0670` (superscript
  alef / dagger alif), `U+06D6–U+06DC`, `U+06DF–U+06E4`, `U+06E7–U+06E8`, `U+06EA–U+06ED` (Qur'anic
  annotation marks), and `U+0640` (tatweel).

It is a **pure deletion**: the output is the input with exactly those codepoints removed — a
subsequence of the input. Two consequences fall out *by construction*:

1. **No over-merge is introduced.** Two names that differ in any *base* (non-mark) character still
   differ after stripping, so the fix cannot newly collide two distinct consonant skeletons. It only
   removes the *spurious divergence* tashkeel caused. (Verified: `حسن/حسين/سعد/سعيد` stay distinct.)
2. **Strict no-op on non-abjad text.** Latin, Cyrillic, CJK, and even Latin diacritics
   (`Société Générale → 'generale societe'`) contain none of these codepoints, so their fingerprints
   are byte-for-byte unchanged. The ADR-0035 Legion Latin/Cyrillic case is untouched.

With the fix, `مُحَمَّد`, `محمد`, and `محـمد` all → `'mhmd'`; `عَلِيّ`/`علي` → `'ly'`;
`حُسَيْن`/`حسين` → `'hsyn'`.

**Person-affecting safety (the load-bearing argument).** Abjad names are frequently *people*
(sanctioned individuals). This change *raises recall* for them, so it is reviewed as person-affecting.
It is nonetheless safe to ship autonomously because it **does not touch the human-gated path**:

- the **0.92 threshold is unchanged**; the other comparison signals (country, birthDate, wikidataId,
  registrationNumber/taxNumber) are unchanged, so they still discriminate same-name-different-person;
- the **catastrophic-merge guard is unchanged** — any cluster containing a sensitive entity
  (Person / PEP / sanctioned) still **PARKS for human sign-off** (ADR [0031](0031-return-to-block-signoff.md))
  and is **never auto-fused**. No person is auto-merged by this change.

**Why this and not nomenklatura `LogicV2` (the robust path ADR 0035 named).** `LogicV2` is the general
abjad-aware matcher, but it is a **row-wise Python re-scorer that does not vectorise in DuckDB/Splink**
— a larger pipeline change owed **its own ADR**, and it **remains deferred**. This gate closes the
*dominant, demonstrable* sub-case (tashkeel inconsistency) with a per-row, vectorisable normalization,
**zero new dependencies**, and **no pipeline change** — strictly within the existing projection.

## Consequences

- **Reversibility — REVERSIBLE** (recorded per the build discipline). Reversal cost: revert one helper
  + one call site (`splink_model.py`). **Revisit triggers:** (a) the abjad over-merge rate on the
  golden/fixture set exceeds tolerance, or (b) `LogicV2` is built — at which point re-evaluate whether
  this prenorm is still needed or is subsumed. Because it is reversible, threshold-neutral, and leaves
  person-affecting merges human-gated, it was decided autonomously (no human fork manufactured).
- **Gate tests (`tests/property/` + `tests/unit/`):** (1) a `@given` **recall-metamorphic** property —
  decorating a generated abjad name with arbitrary tashkeel yields the **identical** `_name_fingerprint`;
  (2) a `@given` **structural** property — `_strip_arabic_marks` removes *only* the listed mark
  codepoints (output is the marks-filtered subsequence), independently pinned, which is what guarantees
  no-over-merge + non-abjad no-op; (3) example fixtures on real Arabic/Persian names (converge) and
  distinct names (stay distinct); (4) regression guards — a sensitive abjad duplicate still **parks**,
  and the ADR-0035 Latin/Cyrillic keys are unchanged.

## Deferred (unchanged from ADR 0035 / recorded, not built)

- **nomenklatura `LogicV2`** as a post-blocking re-scorer — the general abjad/multi-script matcher;
  its own future ADR (does not vectorise; larger change).
- The **ʿayn (`ع`) token-splitting** artifact (`سعد → 'd s'`) is a separate `fingerprints`
  transliteration quirk, **not** addressed here: it is *consistent* (it does not depend on
  tashkeel presence) and does **not** introduce a correctness hole (distinct names stay distinct), so
  it is left to the `LogicV2` follow-up rather than widened scope here.
