# Gate PEP-subcode — RISKS-parented sub-code sensitivity (BUG-FIX)

> **BUG-FIX gate** (failing-test-first). Fixes a confirmed catastrophic-merge **fail-open** found by
> the cross-line audit against Workflow A. A granular FtM risk *sub-code* (`role.pep.natl`,
> `crime.cyber`, …) is sensitive but the slice-1 guard misses it → the cluster **auto-merges
> UNFLAGGED**, violating CLAUDE.md *"never auto-merge a sensitive entity."*
> ADR: **amends** `docs/decisions/0047-fail-closed-sensitivity-guard.md` ("Post-merge fix
> (PEP/sub-code coverage)"; status stays **ACCEPTED**). No new ADR.
> Branch: `gate/pep-subcode-sensitivity` off `master@db0fffa` (clean cut).
> Person-affecting posture: **NEUTRAL (fail-closed) — no human sign-off required** (§9).

---

## 1. The bug (reproduced)

`src/worldmonitor/guard/sensitivity.py::is_sensitive` decides topic sensitivity by **EXACT set
membership**:

```python
if topic_codes & registry.topic.RISKS:      # (a) exact membership
    return True
return any(code not in registry.topic.names for code in topic_codes)   # (c) unknown-hinge
```

`registry.topic.RISKS` (FtM 4.9.2 — **28 codes**, verified against the installed package) contains the
**PARENT** risk codes (`role.pep`, `crime`, `crime.traffick`, …) but **NOT their
sub-classifications**. Those sub-classifications **ARE** in `registry.topic.names` (the full
73-code vocabulary), so clause (c) (unknown ⇒ sensitive) misses them too — they are *known*, just not
*risk-tagged at the parent granularity*.

Net effect: a cluster member whose **only** risk signal is a RISKS-parented sub-code is
`is_sensitive == False` → `needs_review` returns `(False, "")` → the merge **auto-promotes with no
human review**. Reproduced on `master@db0fffa`:

```
is_sensitive(Person topics=['role.pep.natl']) == False     # a national PEP — auto-merges UNFLAGGED
```

**Root cause (cross-line).** Workflow A kept the legacy `role.pep*` / `sanction*` **prefix** rule
(`startswith`), which catches sub-codes by prefix. Workflow B, inverting to deny-by-default
(ADR 0047), replaced the prefix rule with exact `& registry.topic.RISKS` membership and **dropped the
sub-code coverage** — and ADR 0047 Decision 1 wrongly asserted *"every code they matched is in
`RISKS`."* The `role.pep*` prefix matched `role.pep.natl/intl/frmr`, which are **NOT** in `RISKS`. (B
re-derives the corrected rule below; it does not copy A's prefix list.)

---

## 2. Scope — the 7 missed sub-codes (enumerated against the installed FtM)

Verified programmatically (`registry.topic.RISKS` / `registry.topic.names` on the installed FtM):
each of these is **in `names`**, **not in `RISKS`**, and has a **dot-ancestor in `RISKS`**:

| sub-code | RISKS dot-ancestor | in `RISKS`? | in `names`? |
|---|---|---|---|
| `role.pep.natl` | `role.pep` | no | yes |
| `role.pep.intl` | `role.pep` | no | yes |
| `role.pep.frmr` | `role.pep` | no | yes |
| `crime.cyber` | `crime` | no | yes |
| `crime.env` | `crime` | no | yes |
| `crime.traffick.drug` | `crime` / `crime.traffick` | no | yes |
| `crime.traffick.human` | `crime` / `crime.traffick` | no | yes |

**Zero-over-flag property (verified).** The full set of *known* codes (`∈ names`) that are **not in
`RISKS`** yet have a **RISKS dot-ancestor** is **exactly these 7** — no other known code has a risk
ancestor. So the fix below newly-flags precisely these 7 and introduces **no false positive** among
the 73 known codes. (`gov.head`, `corp.public`, `fin.bank`, `forced.labor`, … have no RISKS ancestor
and stay benign.)

---

## 3. The fix (precise — builder writes the code)

In `is_sensitive`, treat a topic `code` as sensitive iff **ANY** of:

- **(a)** `code ∈ registry.topic.RISKS` — *unchanged*;
- **(b)** a **DOT-ANCESTOR** of `code` ∈ `RISKS` — a sub-classification inherits its parent's risk
  (`role.pep.natl` → ancestor `role.pep` ∈ RISKS). Equivalent, branch-free form:
  ```python
  any(code == r or code.startswith(r + ".") for r in registry.topic.RISKS)
  ```
  (`r + "."` — the trailing dot makes it a true ancestor test, never a bare string-prefix; `crime`
  matches `crime.cyber` but not a hypothetical `crimes`);
- **(c)** `code ∉ registry.topic.names` — the unknown-hinge, *unchanged*.

The empty-topics early return (`if not topic_codes: return False`) is unchanged. The risk source stays
**programmatic** (`registry.topic.RISKS`), so a future RISKS-parented sub-code is covered with **no
code change** — the fix tracks the FtM pin exactly as the parent rule does.

**Out of scope (hard stops — DENY if touched):**
- `_legacy_is_sensitive` — **MUST NOT change** (§4). It models the *historical legacy guard*, not the
  new decision.
- `_risk_within_khop` / `_risk_labels` / Stage-2 k-hop, the Chow band, `needs_review`'s ordering,
  `has_nonexemptible_sensitivity` — untouched.
- `DEFAULT_MERGE_THRESHOLD`, any Splink weight/blocking, `cluster_and_merge`, scores — untouched.
- No config field, no new datastore/table/status/sink, no API/MCP change.

---

## 4. Exemption-interaction analysis (PIN it; do NOT change it)

The Decision-5 fence keys non-exemptibility on
`is_newly_broadened_sensitive = is_sensitive(e) AND NOT _legacy_is_sensitive(e)`.
`_legacy_is_sensitive` already prefix-matches `role.pep*` / `sanction*`. After the fix the
interaction is **consistent and intended** (verified):

- **`role.pep.*` sub-codes** → `is_sensitive` **True** (clause b) AND `_legacy_is_sensitive` **True**
  (the `role.pep` prefix) → `is_newly_broadened_sensitive` **False** → **stays EXEMPTIBLE**. Correct:
  the legacy guard *did* see these, so a prior approval could have considered them — not a stale
  exemption.
- **`crime.cyber` / `crime.env` / `crime.traffick.*`** → `is_sensitive` **True** (clause b) AND
  `_legacy_is_sensitive` **False** (legacy had **no** `crime*` prefix) → `is_newly_broadened_sensitive`
  **True** → **NON-exemptible** (re-parks past a stale approval). Correct: the legacy guard never
  caught them.

**Therefore `_legacy_is_sensitive` is left exactly as-is** — altering it would corrupt the
legacy-visibility model and either re-park a knowingly-approved `role.pep.*` merge (over-strict) or
let a `crime.*` sub-code slip an exemption (the fail-open). **DENY** if `_legacy_is_sensitive` is
modified.

---

## 5. Failing-first test plan (`tests/unit/test_sensitivity_subcode.py`, new file)

Written FROM this spec, independent of the implementation; pins **outcomes** at the guard's public
boolean entry points (never "no exception"). RED on `master@db0fffa`, GREEN post-fix.

**T-PEP1 — every missed sub-code is sensitive (the failing-first oracle).** Parametrise over all 7
(`role.pep.natl/intl/frmr`, `crime.cyber`, `crime.env`, `crime.traffick.drug/human`):
`is_sensitive(Person topics=[code]) is True`. *RED pre-fix* (`False` → auto-merge), GREEN post-fix.

**T-PEP2 — snapshot guardrail (non-vacuous, FtM-pin-tracking).** Assert, computed live from the
installed registry: each of the 7 is `∈ names`, `∉ RISKS`, and has a RISKS dot-ancestor; **AND** the
set `{c ∈ names : c ∉ RISKS ∧ some dot-ancestor of c ∈ RISKS}` **== exactly those 7**. Fails loudly
if a FtM bump shifts the set (re-verify), and proves the no-over-flag claim of §2.

**T-PEP3 — no over-flag regression.** A **known** code with **no** RISKS dot-ancestor (e.g.
`corp.public`; assert it is `∈ names` and has no RISKS ancestor so the test is non-vacuous) →
`is_sensitive(Person topics=[code]) is False`. Pins that deny-by-default does not degenerate into
flagging every known sub-code.

**T-PEP4 — exemption interaction (pure unit pins, §4).**
- `role.pep.*` (parametrise the 3): `is_sensitive is True` AND `is_newly_broadened_sensitive is
  False` (legacy-caught → exemptible).
- `crime.cyber` / `crime.env` / `crime.traffick.drug` / `crime.traffick.human` (parametrise):
  `is_sensitive is True` AND `is_newly_broadened_sensitive is True` (newly-broadened →
  non-exemptible).

**T-PEP5 — through `needs_review`.** A real 2-member merge (built via the production
`score_pairs` + `cluster_and_merge` path, or a directly-built `ResolvedCluster` with
`is_merge is True`) whose one member carries `topics=['role.pep.natl']`:
`needs_review(merge, by_id)[0] is True` and the reason mentions "sensitive". *RED pre-fix*
(auto-merges), GREEN post-fix.

**No-regression (frozen — must stay green, NOT edited; §6):** the existing 28-RISKS parametrised
oracle `test_sensitivity_guard.py::test_t2_every_risks_code_parks` (the fix only ADDS True results;
the 28 exact codes still hit clause (a)), `test_t6_off_ontology_topic_is_sensitive`,
`test_non_sensitive_cluster_still_auto_merges`, the slice-3 `test_exemption_fence.py` masking probe,
and the integration T4 `legacy-caught sanction stays-exemptible` discriminator.

---

## 6. Files in scope / frozen

**In scope (the only files this gate may change — see `.claude/gate.scope`):**
- `src/worldmonitor/guard/sensitivity.py` — clause (b) in `is_sensitive` + its docstring (correct
  the "every code they matched is in RISKS" claim). **Nothing else in this file.**
- `tests/unit/test_sensitivity_subcode.py` — the new failing-first suite (§5).
- `docs/reviews/GATE_PEP_SUBCODE_SPEC.md` — this spec.
- `docs/decisions/0047-fail-closed-sensitivity-guard.md` — the amendment.
- `.claude/gate.scope`.

**FROZEN (must pass byte-for-byte; a removed assert / added skip|xfail / loosened tolerance is a
judge DENY):**
- `tests/unit/test_sensitivity_guard.py` (esp. `test_t2_every_risks_code_parks` — the 28-RISKS
  oracle — and T1/T3/T6 + the no-over-park fences)
- `tests/unit/test_sensitivity_guard_chow.py`
- `tests/unit/test_exemption_fence.py` (the slice-3 structured-probe / masking suite)
- `tests/unit/test_settings_sensitivity.py`
- `tests/integration/test_sensitivity_guard.py` (T4 stale-exemption + the legacy-caught discriminator)
- `tests/integration/test_sensitivity_guard_khop.py`
- `tests/integration/test_exemption_fence_masking.py`

---

## 7. Locked invariants (must hold across the gate)

- **G1 provenance on every node AND edge** — untouched. No `prov_*` / provenance read or write is
  added, removed, or loosened. DENY if any G1 / edge-provenance test is weakened.
- **Append-only / no un-merge** — untouched. No clustering / merge / threshold / ledger change; the
  catastrophic-merge guard only ADDS parks.
- **Canonical-canonical only via the guard** — preserved. `DEFAULT_MERGE_THRESHOLD = 0.92`, Splink
  weights/blocking, `cluster_and_merge` membership, and `needs_review`'s axis ordering are unchanged;
  the fix only widens *which members are sensitive*, never *who merges with whom*.
- **Deny-by-default is not configurable open** (ADR 0047 §6) — the risk SET stays programmatic; no
  config field can remove a RISKS code or a RISKS-parented sub-code.
- **Legacy-visibility model intact** — `_legacy_is_sensitive` is byte-for-byte unchanged (§4).

---

## 8. APPROVE / DENY

**APPROVE** iff all hold:
1. All 7 RISKS-parented sub-codes are `is_sensitive == True` and park through `needs_review`
   (T-PEP1, T-PEP5 green).
2. The snapshot guardrail (T-PEP2) confirms the dot-ancestor set is exactly the 7 (no drift).
3. No over-flag: a known non-RISKS-descendant code stays `is_sensitive == False` (T-PEP3 green).
4. The exemption interaction is pinned and correct: `role.pep.*` exemptible, `crime.*`
   non-exemptible (T-PEP4 green).
5. `_legacy_is_sensitive` is unchanged; the full FROZEN suite (§6) stays green.
6. CI `quality` + `security` checks green.

**DENY** if **any** of:
- **D-SUBCODE** — any RISKS-parented sub-code still `is_sensitive == False` / still auto-merges.
- **D-OVERFLAG** — any known code with **no** RISKS dot-ancestor is newly flagged sensitive.
- **D-LEGACY** — `_legacy_is_sensitive` is altered in any way.
- **D-SCOPE** — any production edit outside `src/worldmonitor/guard/sensitivity.py`, or a touch of
  `DEFAULT_MERGE_THRESHOLD` / Splink / scores / k-hop / Chow / `needs_review` ordering.
- **D-FROZEN** — any FROZEN test (§6) regressed, skipped, xfailed, or loosened.

---

## 9. Person-affecting assessment + sign-off

**Person-affecting: NO. No human sign-off required.** Like Gate E (ADR 0047, person-NEUTRAL /
fail-closed):
- The change is **strictly stricter** — it can only move MORE clusters to human review (adds parks);
  it **auto-promotes nothing** and **un-flags nothing**.
- It does **not** touch `DEFAULT_MERGE_THRESHOLD`, any Splink weight/blocking/score, the k-hop or Chow
  stages, or any individual-affecting threshold. It widens the *sensitivity recall* of an
  already-fail-closed guard.
- The CLAUDE.md self-improvement rule requires sign-off for changes that *promote* / *loosen* / *alter
  a person-affecting score or ER threshold*. This gate does the opposite (adds review, never removes
  it), so it falls under the same NEUTRAL posture ADR 0047 already carries.

---

## 10. Slice breakdown

**ONE slice** (person-NEUTRAL; no product/architecture fork; `human_fork: false`). The change is a
single OR-clause in one function plus a docstring correction and one new test file; splitting it would
add ceremony without independence.

- **Slice 1 (only) — dot-ancestor sub-code coverage.** Add clause (b) to `is_sensitive` + correct its
  docstring; add `tests/unit/test_sensitivity_subcode.py` (T-PEP1…T-PEP5, failing-first). Amend
  ADR 0047 + this spec + `.claude/gate.scope`. Individually mergeable; CI-green required; FROZEN suite
  (§6) stays green.

(If, contrary to §2, the builder finds the dot-ancestor rule would over-flag a *known* non-risky
code — i.e. T-PEP2 fails on the installed FtM — **STOP and flag the human**: that is a genuine
ontology question, not a guess.)
