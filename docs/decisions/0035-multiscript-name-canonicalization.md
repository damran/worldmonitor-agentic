# 0035 — Multi-script name canonicalization (fingerprint name projection)

- **Status:** accepted
- **Date:** 2026-06-22
- **Touches:** `resolution/splink_model.py` (ER name matching). Code-orthogonal to G3
  (graph-write entity-links); the only coupling is that it changes *which* entities merge,
  so it lands **before** the G3 gate so G3 operates on the corrected canonical set.

## Context

`_flatten` projected the Splink comparison name as `entity.first("name").lower()`. FtM
`first()` returns the **sort-first** of the multi-valued `name`, whose value **flips
alphabet** for bilingual/cross-alphabet records — so two records of ONE entity that store
their names in a different script order projected **different** names, scored low, and
never merged. `block_on("substr(name, 1, 4)")` compounded it (cross-alphabet records never
co-block on the name prefix).

Proven on a **real** failing case: the `us_ofac_sdn` "Legion Komplekt" pair
(`NK-6mNvkSFuS8huYiAimBGH4X` / `NK-8WbtGpC3EtBaT4K59w89mQ`) — two **distinct sanctioned `ru`
orgs** that each carry both a Cyrillic and a Latin name. `first("name")` gave one the
Cyrillic, the other the Latin → `jaro_winkler = 0.378` → no merge. For a sanctions/CTI
platform this is a **core-ER correctness hole on exactly the multi-feed entities we most
need to resolve** (Russian/Arabic/Chinese sources).

The fork (how to canonicalize multi-script names) was surfaced for sign-off. Evaluated
against real multi-script data: **caption-based** (fixes the both-scripts-present case but
not the no-shared-variant case), **all-name-variants** (rejected — MAX aggregator + token
blocking materially raise over-merge), **transliteration alone** (rejected — full-name ICU
translit scores the Legion pair `0.0`, worse, because the Cyrillic spells the Russian
legal-form phonetically while the Latin is the English *translation*; also lossy for
abjad), and **name fingerprints** (chosen).

## Decision

Project a script-stable **name fingerprint** in `_name_fingerprint`:
`fingerprints.remove_types(fingerprints.generate(entity.caption))`, guarded on a real
`name` value (a no-name `Sanction` whose caption falls back to a programme code stays
`None`). `fingerprints` (already a locked dependency — the OpenSanctions/nomenklatura ER
stack) transliterates to Latin, sorts tokens, and strips legal-form words, so both
`"ООО Легион Комплект"` and `"LIMITED LIABILITY COMPANY LEGION KOMPLEKT"` →
`"komplekt legion"`. The fingerprint is the `name_fp` column used for both the
`jaro_winkler` comparison and `block_on("substr(name_fp, 1, 4)")`. The **0.92 merge
threshold and the catastrophic-merge guard are unchanged.**

Verified on real data: Legion merges (and, being sensitive, **parks**); the cross-script
**no-shared-variant** case (one record only-Cyrillic, the other only-Latin) also merges;
`my_aob_sanctions` "Sathiea Seelean" still merges (no regression). **Over-merge proven low
with tests, not spot-checks:** `us_dod_chinese_milcorps` (735 entities sharing "Co., Ltd.")
→ **0** merges; `my_aob_sanctions` (148) → exactly the **1** real pair; distinct orgs
sharing a legal-form token or a single brand token stay below 0.92
(`tests/unit/test_resolution_multiscript.py`).

## Consequence — the parking conclusion changes (real sensitive park now exists)

The earlier finding "OpenSanctions pre-dedups every sanctioned entity, so a sensitive 2+
cluster cannot form on real data → `parked_merges=0` is correct" was itself an **artifact of
this bug**: the buggy `first("name")` projection (and the dataset scan built on it) *missed*
cross-script sensitive duplicates. With the fix, the **Legion pair is a genuine sensitive
2-cluster that block mode PARKS** — so the sensitive→block→park path **is** reproducible on
real `us_ofac_sdn` data, no injected sensitivity required. The smoke runbook's parking
section is updated accordingly under the follow-up validation gate.

## Deferred (KNOWN GAP — recorded, documented at the projection site)

`fingerprints` renders **abjad scripts (Arabic/Persian)** as lossy consonant skeletons
(dropped short vowels), so it is **not a reliable sole key** for those — and must not become
a narrower undocumented version of the same script-class assumption this fix removed. The
robust follow-up is nomenklatura **`LogicV2`** (the OpenSanctions production matcher) as a
**post-blocking re-scoring step** — it is a row-wise Python matcher that does not vectorise
in DuckDB/Splink, so it is a larger change owed **its own ADR**, not this gate. Noted at
`_name_fingerprint` and in `0032`'s deferred list.
