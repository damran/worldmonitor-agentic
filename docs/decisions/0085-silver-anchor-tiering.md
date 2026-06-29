# ADR 0085 — Silver anchor tiering: globally-unique vs. jurisdiction-scoped (G7 correctness fix)

- **Status:** accepted (reversible default — correctness fix to ADR 0079's silver label deriver)
- **Date:** 2026-06-29
- **Gate:** G7 label on-ramp, correctness fix to slice 2. Addresses two CONFIRMED review findings
  in `resolution/silver.py`.
- **Touches:** `resolution/silver.py` (two new exported constants, revised classification logic);
  `tests/unit/test_silver.py` (updated + extended, 45 tests total);
  `tests/property/test_prop_canonical_silver.py` (updated + extended, covers both findings).
  **No schema change, no migration, no live-ER change.** Measurement labels only — no
  merge/threshold/graph/live-ER modification. `human_fork: false` (non-person-affecting; silver
  is measurement-only; promotion stays human-sign-off-gated).

## Context — the two review findings

**Finding 1 — jurisdiction-scoped anchor false-positives (correctness).**
ADR 0079 treated `registrationNumber` as a globally-definitive anchor: a shared value across
two sources was immediately emitted as a `match` silver label.  But a company registration
number is unique **only within its register/jurisdiction** — two entirely different entities
registered in two different countries can carry the same number string (e.g. `"123456"` is a
valid UK Companies House number AND a plausible German Handelsregister number).  This produced
false-positive `match` labels wherever two records happened to share a registration number but
belonged to different national registers.

**Finding 2 — contradiction-drop bypass for same-source pairs (correctness).**
The original classification loop applied the ≥2-distinct-sources check **before** the
contradiction check.  For a same-source pair that carried both a shared anchor (positive-eligible
if sources were distinct) and a conflicting anchor (negative-eligible regardless of source):

- Old code: `is_positive = False` (same source) → `is_negative = True` (conflict) →
  emitted as `"non_match"`.
- Correct behaviour: the contradiction (`has_shared AND has_conflict`) must be evaluated
  FIRST and INDEPENDENTLY of source — such a pair is logically contradictory and must be
  DROPPED, not labelled.

The pre-fix code could never produce a same-source `"match"` (same-source positives were already
correctly suppressed), but it incorrectly produced `"non_match"` for same-source contradictions.

## Decision

### D1 — Anchor tiering (Finding 1)

Split the anchor set into two tiers, exported as new public constants alongside the unchanged
`ANCHOR_PROPERTIES` union:

**`GLOBALLY_UNIQUE`** (`wikidataId`, `leiCode`, `isin`, `permId`, `swiftBic`, `ogrnCode`,
`innCode`, `okpoCode`): a shared value alone is a definitive cross-source signal.

Rationale:
- `wikidataId` (QID), `leiCode`, `isin`, `swiftBic` — globally administered unique
  identifier registries; no two real-world entities in distinct registers can share a value.
- `ogrnCode`, `innCode`, `okpoCode` — Russian Federation schemes (OGRN 13 digits, INN 10/12,
  OKPO 8/10).  Each is unique within its scheme and the schemes are nationally administered;
  no entity outside the Russian registry can carry a valid OGRN — making these identifiers
  *globally distinct* despite being nationally issued.
- `permId` — Refinitiv/LSEG Permanent Identifier, globally unique across financial instruments.

**`JURISDICTION_SCOPED`** (`registrationNumber`): a shared value is a positive signal **only
when** the two entities' `jurisdiction` and/or `country` FtM property values **corroborate** —
both sides non-empty and sharing at least one value (case-folded).  Absent or disjoint
jurisdiction → the anchor **abstains** (no positive, no negative).  Symmetrically, a conflicting
`registrationNumber` is a negative signal only when jurisdiction corroborates (same register,
different number ⇒ different entity); across unknown or different jurisdictions a differing
`registrationNumber` carries no signal.

`ANCHOR_PROPERTIES` is kept as the union `GLOBALLY_UNIQUE + JURISDICTION_SCOPED` so the ADR 0080
`benchmark.identity_keys` import continues to work unchanged.

### D2 — New internal helpers for jurisdiction corroboration

```python
_JURISDICTION_PROPS: tuple[str, ...] = ("jurisdiction", "country")

def _jurisdiction_values(entity: FtmEntity) -> frozenset[str]:
    """Case-folded union of jurisdiction + country values for corroboration matching."""

def _jurisdictions_corroborate(a_jur: frozenset[str], b_jur: frozenset[str]) -> bool:
    """True iff both sides are non-empty and share at least one value."""
```

Case-folding (`.lower()`) normalises `"GB"` and `"gb"` to the same token regardless of how the
connector populated the field.

### D3 — Revised classification order (Finding 2)

The pair classification is restructured as:

1. **Compute `has_shared` and `has_conflict` independently of the source check:**
   - Globally-unique tier: shared value → `has_shared = True`; conflict → `has_conflict = True`.
   - Jurisdiction-scoped tier: corroboration gate runs first; if jurisdictions corroborate,
     shared → `has_shared`, conflict → `has_conflict`; otherwise → no signal.
2. `has_shared AND has_conflict` → **DROP** (contradiction) — regardless of source.
3. `has_shared AND distinct sources` → `"match"`.
4. `has_shared AND same source` → **ABSTAIN** (downgrade, never `"non_match"`).
5. `has_conflict` (only) → `"non_match"` (source-independent, ADR 0079 §Decision 4).
6. Otherwise → **ABSTAIN**.

The invariants N1/N2/N3 (ADR 0079) are preserved:
- N1: no score parameter, no scoring symbol referenced.
- N2: labels are a pure function of (anchors + jurisdiction/country, source_id); jurisdiction
  is an FtM property value, not a model output.
- N3: `clerical_score=None` write-boundary guard unchanged.

## Alternatives considered

- **Treat `registrationNumber` as globally-unique (status quo):** rejected — the false-positive
  is confirmed real-world behavior (e.g. UK vs. German company registers both use numeric ids
  in overlapping ranges). Keeping it globally-unique would pollute the silver label set with
  false matches, making the non-circular label signal worse than useless.
- **Drop `registrationNumber` from anchors entirely:** too conservative — when the register IS
  known (jurisdiction/country set), a shared `registrationNumber` is a strong positive.  Keeping
  it jurisdiction-gated preserves the recall that IS reliable.
- **Apply `registry.identifier.clean` normalisation before jurisdiction check:** deferred —
  FtM already cleans identifier values on `make_entity`; the extra normalisation risk
  (false-positive collapse) and the unknown interaction with jurisdiction corroboration make this
  a separate revisit trigger.
- **Require jurisdiction for ALL anchors:** rejected — LEI/BIC/QID are globally administered;
  requiring jurisdiction would silently drop a large fraction of valid silver labels.

## Consequences

- The silver label set is **more precise**: shared `registrationNumber` pairs without
  jurisdiction evidence no longer produce false `match` labels; the over-merge potential from
  cross-jurisdiction false-positives is eliminated.
- Same-source contradictions are now correctly dropped rather than mis-labelled `non_match`.
- **Recall trade-off:** entities with `registrationNumber` but no FtM `jurisdiction`/`country`
  property abstain instead of matching — an expected recall reduction in exchange for precision.
  The label-sufficiency report (G7 slice 4) will measure the net impact.
- `benchmark.identity_keys` (ADR 0080) is unchanged — it imports `ANCHOR_PROPERTIES` which
  remains the nine-element union.
- 45 tests (up from 28): 17 new/updated tests cover both findings + the tier behavior at all
  boundary conditions (same jurisdiction, different jurisdiction, absent jurisdiction, same-source
  contradiction).

## Reversibility

**Reversible.** This is a measurement-label correctness fix; the labels feed only the harness,
not the live ER path.

**Reversal cost: low** — revert `silver.py` to the pre-ADR 0085 state and `DELETE FROM
er_gold_pair WHERE source='canonical_silver'` (silver rows are self-identifying).

**Reversible defaults recorded:**
- The `JURISDICTION_SCOPED` set — if `registrationNumber` is confirmed globally-unique in
  practice (e.g. all ingested data already carries jurisdiction), move it to `GLOBALLY_UNIQUE`.
- The case-folding in `_jurisdiction_values` — could be tightened to require ISO-3166 codes only.
- The `_JURISDICTION_PROPS` tuple (`jurisdiction`, `country`) — extend to `registeredAddress`
  country extraction if needed.

**Revisit triggers:**
1. Silver recall too thin → if the jurisdiction-absent abstain rate is too high, consider
   falling back to a weaker positive signal (e.g. same source = abstain but different source +
   no jurisdiction = weak positive, if empirically justified).
2. A false-positive `match` observed for a `GLOBALLY_UNIQUE` anchor → drop it or reclassify.
3. Any promotion step derived from these labels → new ADR required (human-sign-off-gated).

## Tests added / updated

**Unit (`tests/unit/test_silver.py`):**
- `test_anchor_tier_constants_are_consistent` (new): structural proof of tier constants.
- `test_example_positive_shared_registration_number_with_jurisdiction` (new): same jurisdiction → match.
- `test_example_positive_shared_registration_number_absent_jurisdiction_abstains` (new): no jur → abstain.
- `test_example_positive_shared_registration_number_different_jurisdiction_abstains` (new): diff jur → abstain.
- `test_example_positive_shared_registration_number_country_corroboration` (new): `country` prop also suffices.
- `test_example_negative_conflicting_registration_number_same_jurisdiction` (new): same jur conflict → non_match.
- `test_example_negative_conflicting_registration_number_absent_jurisdiction_abstains` (new).
- `test_example_negative_conflicting_registration_number_different_jurisdiction_abstains` (new).
- `test_example_negative_same_source_conflicting_globally_unique_anchor` (new, replaces old regNo test).
- `test_example_contradiction_dropped_globally_unique` (updated: uses ogrnCode conflict instead of regNo).
- `test_example_no_contradiction_regnum_absent_jurisdiction` (new): documents the behavioural change.
- `test_example_contradiction_dropped_regnum_with_jurisdiction` (new): regNo conflict + same jur → dropped.
- `test_finding2_same_source_contradiction_dropped` (new): Finding-2 regression test.
- `test_finding2_same_source_clean_positive_abstains_not_non_match` (new).

**Property (`tests/property/test_prop_canonical_silver.py`):**
- `test_p_pos_shared_anchor_distinct_sources_yields_match`: narrowed to `GLOBALLY_UNIQUE`.
- `test_p_neg_conflicting_anchors_yield_non_match`: narrowed to `GLOBALLY_UNIQUE`.
- `test_p_contradiction_pos_and_neg_on_different_anchors_drops_pair`: uses `_GLOBALLY_UNIQUE_COMPANY_ANCHORS`; same-source skip removed (Finding 2).
- `test_p_mm_source_collapse_removes_positive`: narrowed to `GLOBALLY_UNIQUE`.
- `test_p_jur_shared_regnum_same_jurisdiction_yields_match` (new, @given).
- `test_p_jur_shared_regnum_absent_jurisdiction_abstains` (new, @given).
- `test_p_jur_shared_regnum_different_jurisdiction_abstains` (new, @given).
- `test_p_jur_conflicting_regnum_same_jurisdiction_yields_non_match` (new, @given).
- `test_p_jur_conflicting_regnum_absent_jurisdiction_abstains` (new, @given).
- `test_p_finding2_same_source_contradiction_is_dropped_not_non_match` (new, @given).
