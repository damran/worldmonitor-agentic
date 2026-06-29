# 0079 — Canonical-anchor SILVER labels for the ER measurement harness

- **Status:** accepted (reversible default — the non-circular label on-ramp the user decided 2026-06-29)
- **Date:** 2026-06-29
- **Gate:** G7 label on-ramp, **slice 2 of 4**. Unblocks the long-stalled G7 (calibrated-threshold /
  EM-weight promotion) by giving the harness (ADR [0043](0043-er-measurement-harness-em-weights.md))
  a labelled set that is **NOT a function of the model's own score**.
- **Touches:** new `resolution/silver.py` (the deriver); `tests/property/test_prop_canonical_silver.py`
  (the mandatory `@given` invariant test); `tests/unit/test_silver.py` (example + idempotent-persist).
  **No schema change, no migration** — silver labels reuse the existing `er_gold_pair` table and the
  `GoldPair` dataclass + `persist_gold_pairs` writer; the only new thing is a new `source` tag value.
  **Not person-affecting** (`human_fork: false`) — writes *measurement labels only*, never a
  threshold / EM weight / merge / graph value.

## Context

**The G7 blocker is circularity.** Gate A (ADR 0043) built a sound measurement harness
(`resolution/eval.py`: B³ / CEAFe / `over_merge_rate` over the *full* gold partition;
`recommended_threshold` is **report-only**; promotion is the documented, human-gated slice-2). But the
only labels feeding it (`resolution/gold.py`) are a **stratified uncertainty sample over the live
Splink 0.5–0.95 score band** — each pair's provisional `match`/`non_match` label is assigned by
*"is the model's own probability ≥ the mid-band?"*. Calibrating or promoting a threshold against
labels **derived from the very score you are trying to calibrate** is circular: the model would be
graded against its own opinion. So G7 promotion stayed blocked (ledger G7: *MEASUREMENT CLOSED /
promotion OPEN*).

**The decided fix (user, 2026-06-29): build a non-circular label on-ramp.** Strategy = **canonical-
anchor SILVER labels** (this slice) + an **external-benchmark floor** (a later slice). The signal is
free and real: OpenSanctions aggregates the *same real-world entity* across **independent source
lists**, and a live OFAC SDN sample shows **38 %** of entities carry a canonical anchor
(`registrationNumber` / `wikidataId` / `leiCode`). So **a canonical id shared across ≥2 distinct
sources is a strong same-entity signal that owes nothing to any model output**, and two **conflicting**
authoritative ids of the same type is an equally score-independent distinctness signal (the
pair-level form of the ADR [0040](0040-er-anchor-conflict-negative-evidence.md) anchor-conflict guard).

**This ADR is slice 2 only:** the deriver + its property test. Slice 1 (running the real connectors on
the host to build a multi-source corpus — an ops run, not code), slice 3 (the external-benchmark
importer) and slice 4 (the label-sufficiency report) are **out of scope** (see §Out of scope).

## The non-circularity invariant (the load-bearing property)

A silver label is **NEVER a function of the Splink/match score or any model output.** Concretely:

- **N1 — the deriver does not take a score.** `build_silver_pairs(entities)` accepts *only* FtM
  entities. It has **no** `score` / `probability` / `match_probability` / `threshold` / `linker`
  parameter, and its source text references no scoring symbol. It never calls `score_pairs`.
- **N2 — labels are a pure function of (anchors, source provenance) alone.** Mutating any feature a
  model would score on but that the anchor rule does not read — most sharply the `name` property
  (Splink's dominant feature), or non-`source_id` provenance fields — leaves the emitted label set
  **byte-identical**. This is the metamorphic property the gate must prove (§Property-test plan, P-MM).
- **N3 — no score is ever written.** Every silver row carries `clerical_score = NULL`; the persist
  boundary asserts it.

These three are what make the silver set a *legitimate ruler* for G7 instead of a hall of mirrors.

## Decision

1. **New deriver `resolution/silver.py`.** Pure / DB-free derivation, mirroring `gold.py`'s split:

   ```python
   SILVER_SOURCE = "canonical_silver"           # the new er_gold_pair.source tag

   ANCHOR_PROPERTIES: tuple[str, ...] = (        # reversible default — see §Reversibility
       "wikidataId", "leiCode", "registrationNumber", "ogrnCode",
       "innCode", "swiftBic", "isin", "okpoCode", "permId",
   )

   def build_silver_pairs(entities: Sequence[FtmEntity]) -> list[GoldPair]: ...
   def persist_silver_pairs(session: Session, pairs: Sequence[GoldPair]) -> int: ...
   ```

   All nine anchors are confirmed FtM `identifier`-typed properties in this repo's FtM 4.x model.
   `build_silver_pairs` reuses the existing `gold.GoldPair` dataclass, emitting
   `source=SILVER_SOURCE`, `clerical_score=None`. It runs over **source-record entities**
   (pre-resolution — each carries exactly one `Provenance.source_id` via
   `provenance.model.get_provenance`), **not** merged entities.

2. **Anchor extraction.** For an entity `E` and anchor property `P`,
   `anchor_values(E, P)` = the set of non-empty values `E.get(P, quiet=True)` returns (the values FtM
   already cleaned for the `identifier` type on `make_entity`, so the *same* raw id from two sources
   yields the *same* FtM-clean value — compare clean-against-clean, per the `strategies.py` trap note).
   No second normalisation pass is applied (reversible — §Reversibility).

3. **POSITIVE rule (`label="match"`).** A pair `(a, b)` (distinct ids) is a `match` iff some anchor
   property `P` has `anchor_values(a,P) ∩ anchor_values(b,P) ≠ ∅` **and** `a`, `b` come from
   **≥2 distinct sources** (`get_provenance(a).source_id != get_provenance(b).source_id`). The
   provenance-distinctness is what makes it the *real, free, non-circular* OpenSanctions cross-list
   signal — and excludes trivial within-source duplicates.

4. **NEGATIVE rule (`label="non_match"`).** A pair `(a, b)` is a `non_match` iff some anchor property
   `P` has `anchor_values(a,P)` and `anchor_values(b,P)` **both non-empty and disjoint** (two
   *different* values of the *same* authoritative id type — e.g. two distinct `leiCode`s). This is the
   pair-level ADR-0040 anchor-conflict signal; it is **source-independent** (a conflict is a conflict
   whether or not the records share a source).

5. **ABSTAIN (no label).** A pair that neither shares an anchor value across distinct sources nor
   conflicts on any anchor gets **no silver label** — left for human gold / the external-benchmark
   floor / the uncertainty sample. **Contradiction also abstains:** a pair that qualifies as *both*
   positive (shared value on `P`) *and* negative (conflict on `Q`) is **dropped, never emitted as
   either** — the data contradicts itself, so a measurement label would be a guess. Concretely the set
   is `match = Pos \ Neg`, `non_match = Neg \ Pos`, `Pos ∩ Neg` dropped.

6. **Canonical ordering + de-dup.** Every emitted pair is canonically ordered `left_id <= right_id`
   and de-duplicated on `(left_id, right_id)`, exactly as `gold._canonical` / `build_gold_pairs` do;
   self-pairs (`left_id == right_id`) are skipped. The output is deterministic and order-independent
   in the input entity sequence.

7. **Write strategy — reuse, no migration.** `persist_silver_pairs` asserts every pair satisfies
   `source == SILVER_SOURCE and clerical_score is None` (the N3 write-boundary guard), then delegates
   to `gold.persist_gold_pairs`, which is `ON CONFLICT DO NOTHING` on `uq_er_gold_pair (left_id,
   right_id)`. Consequences of reuse, all intended:
   - **Distinguishable.** Silver rows carry `source="canonical_silver"`, set-apart from human gold,
     the `"uncertainty"` (circular) prior, and the `"os_pairs"` hard cases — the harness/promotion can
     select or exclude silver explicitly.
   - **Append-only + human-precedence.** The `(left_id, right_id)` unique means a pair already labelled
     by a human/curated row is **never overwritten** by silver (the conflict-do-nothing keeps the
     existing row) — the correct precedence (human > silver) and the append-only invariant.
   - `er_gold_pair.source` is a free-text `String(32)`; `"canonical_silver"` (16 chars) fits, so
     **no `db/models.py` change and no Alembic migration** are required.

8. **Out of the live path.** This gate writes *only* `er_gold_pair` rows. It does **not** touch
   `merge.py`, any threshold, any EM weight, the Splink model, or the graph. Promotion of any value
   derived from these labels remains the separate, **human-sign-off-gated** slice (person-affecting,
   CLAUDE.md self-improvement gate) — explicitly forbidden in this gate.

## Alternatives considered

- **Keep calibrating on the uncertainty-band labels (status quo):** rejected — that is exactly the
  circularity that blocked G7. Score-derived labels can never grade the score.
- **Hand-label a human gold set first:** not rejected, complementary — but slow and small; the
  canonical-anchor signal is free, large, and available *now* from the multi-source corpus. Human gold
  remains the highest-precedence source (§Decision 7) and the eventual arbiter for promotion.
- **Use the resolver's own clusters / `pick_anchor` durable ids as labels:** rejected — `pick_anchor`
  and the resolver consume the merge decision; deriving labels from them re-introduces circularity by
  the back door. Silver reads raw anchors off **source records**, never the resolved graph.
- **A new `er_silver_pair` table:** rejected — the harness already reads `er_gold_pair`; a parallel
  table is a schema migration + a second read path for no gain. A `source` tag separates the rows.
- **Apply `registry.identifier.clean` as a second normalisation pass:** deferred — FtM already cleans
  identifier values on `make_entity`; an extra pass risks collapsing two *different* ids onto one
  string (a false-positive `match`), the class `canonical.py` deliberately avoids for QID/LEI. Left as
  a revisit trigger (§Reversibility).
- **Require ≥2 distinct sources for the NEGATIVE rule too:** rejected — a conflicting authoritative id
  is a distinctness signal regardless of source (a single source legitimately holds two records with
  two LEIs that are two real entities). Coupling it to source distinctness would only *lose* true
  negatives.

## Consequences

- G7 gains a **non-circular** label source: a `canonical_silver` partition the harness can measure
  against and (later, human-gated) calibrate from — the circular `"uncertainty"` prior is no longer
  the only ruler.
- **Invariant-touching ⇒ a `@given` property test is mandatory** (CLAUDE.md build discipline). The
  load-bearing one is the metamorphic score-independence property (N2 / §Property-test plan, P-MM).
- **High precision by construction:** silver only labels pairs with a hard, score-free anchor signal;
  everything ambiguous (including self-contradicting pairs) abstains. Recall is intentionally partial —
  the external-benchmark floor (slice 3) and human gold fill the rest.
- **No new dependency, no schema change, no migration.** Reuses FtM, the provenance model, the
  `GoldPair` dataclass and the `er_gold_pair` writer already in the tree.
- **Coverage is corpus-bound:** with a *single*-source landing zone (today) the positive rule emits
  nothing (no cross-source agreement) — silver only becomes productive once slice 1 lands a real
  multi-source corpus. The label-sufficiency report (slice 4) measures whether the silver+benchmark
  recall is enough to trust a promotion; until then promotion stays `blocked-on-measurement-harness`.

## Out of scope (recorded)

- **Slice 1** — running the connectors on the host to build the real multi-source corpus (an ops run).
- **Slice 3** — the external-benchmark (gold-standard ER dataset) importer / floor.
- **Slice 4** — the label-sufficiency report deciding whether recall supports a promotion.
- **Any promotion** — threshold / EM-weight / `merge.py` change. Person-affecting, human-sign-off-gated
  (CLAUDE.md); **strictly forbidden in this gate.** The live ER decision path is untouched.
- Committing **real OpenSanctions data** — licensing + hermeticity. Tests use **synthetic** FtM
  entities (Hypothesis-generated, with/without shared/conflicting anchors + varied source provenance).

## Property-test plan (`tests/property/test_prop_canonical_silver.py`, all `@given`)

Synthetic FtM entities only; ids are distinct (each entity is a source record); provenance stamped via
the existing `strategies.source_tagged_entity` idiom; anchors set as FtM properties.

- **P-POS** — two distinct-id entities sharing the same value at a drawn anchor `P`, from two distinct
  sources, no conflict ⇒ exactly one pair `label="match"`, `source="canonical_silver"`,
  `clerical_score is None`, canonically ordered.
- **P-SAME-SOURCE** — same shared anchor value but a *single* source ⇒ **no** `match` (the
  ≥2-distinct-sources rule).
- **P-NEG** — two entities with conflicting (both non-empty, disjoint) values at `P`, no shared
  anchor ⇒ exactly one pair `label="non_match"` (holds for same *and* distinct source).
- **P-ABSTAIN** — entities sharing no anchor value and conflicting on none ⇒ **no** silver label.
- **P-CONTRADICTION** — a cross-source pair sharing a value at `P` **and** conflicting at `Q` ⇒ **no**
  label (dropped); never emitted as both.
- **P-MM (load-bearing, metamorphic — proves N2):** for a generated entity set, arbitrarily mutate
  each entity's `name` (and other non-anchor, non-`source_id` fields) with anchors + `source_id`
  held fixed ⇒ the emitted label set is **identical**. Reverse direction: collapsing two distinct
  sources to one *removes* the corresponding positive (proving source-distinctness is load-bearing,
  not vestigial).
- **P-ORDER** — `build_silver_pairs` output is invariant under input permutation; every pair has
  `left_id <= right_id`, no duplicate `(left_id, right_id)`, no self-pair.
- **P-SIGNATURE (proves N1)** — `inspect.signature(build_silver_pairs)` has no
  score/probability/threshold/linker parameter, and `inspect.getsource(silver)` references no scoring
  symbol (`score`, `probability`, `match_probability`, `score_pairs`).
- **Example / persist (`tests/unit/test_silver.py`)** — round-trips a built silver set through
  `persist_silver_pairs`: rows land with `source="canonical_silver"` / `clerical_score IS NULL`;
  re-persist is idempotent (`ON CONFLICT DO NOTHING`); an existing human/gold row on the same
  `(left_id, right_id)` is not overwritten; `persist_silver_pairs` rejects a pair carrying a score.

## Reversibility

**Reversible** (a measurement-label source, not a live-path or data-shape lock-in).
**Reversal cost: low** — stop calling `build_silver_pairs` and `DELETE FROM er_gold_pair WHERE
source='canonical_silver'` (silver rows are self-identifying; no human/curated row is ever
overwritten, so the delete is clean). The deriver module drops out with no live-path impact.

**Reversible defaults recorded:**
- the **anchor-ID set** (§Decision 1) — `taxNumber`, `vatCode`, `idNumber`, `imoNumber`, `permId`-like
  registry ids can be added/removed; widening only adds labels;
- the **≥2-distinct-sources** positive rule (§Decision 3) and the **conflict** negative rule
  (§Decision 4);
- the **contradiction-abstain** precedence (§Decision 5);
- using FtM-clean values **without** a second `registry.identifier.clean` pass (§Decision 2).

**Revisit triggers:**
1. Slice 4's label-sufficiency report shows silver+benchmark recall is **too thin** to support a G7
   promotion ⇒ widen `ANCHOR_PROPERTIES` and/or add the second-normalisation pass.
2. Cross-source format drift causes **missed** matches (same real id, different FtM-clean string) ⇒
   add the `registry.identifier.clean` normalisation pass (§Alternatives).
3. A false-positive `match` is observed from an anchor value that collides across two real entities ⇒
   drop that property from the set and/or add a shape validator (the `is_qid`/`_is_lei` idiom).
4. Any move to **promote** a value derived from these labels ⇒ that is the separate, human-sign-off
   slice — open a new ADR; this one does not authorise it.
