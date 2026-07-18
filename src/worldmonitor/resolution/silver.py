"""Canonical-anchor SILVER labels for the ER measurement harness (ADR 0079, G7 slice 2).

A **NON-CIRCULAR** label source that unblocks G7 (calibrated Splink ER threshold / EM-weight
promotion). The only existing labels (:mod:`worldmonitor.resolution.gold`) are a stratified
uncertainty sample over the live Splink 0.5–0.95 score band — each pair's provisional label is
set by *"is the model's own probability ≥ the mid-band?"*. Calibrating against them is circular:
the model grades itself. This module derives labels from **canonical anchors only**, a signal
that owes nothing to any model output:

* **POSITIVE** (``label="match"``): two records share the same value of the same canonical-ID
  property AND the two records come from ≥2 **distinct** sources
  (:func:`~worldmonitor.provenance.model.get_provenance` ``.source_id`` differs). Cross-source
  anchor agreement is the real, free, non-circular signal — the same signal OpenSanctions exploits
  across its independent source lists (ADR 0079 §Context).

* **NEGATIVE** (``label="non_match"``): two records have **conflicting** values of the same
  canonical-ID property (both non-empty and disjoint). Source-independent (a conflict is a
  conflict regardless of source). This is the pair-level form of the ADR-0040 anchor-conflict
  guard.

* **ABSTAIN** (no label): a pair that neither shares a cross-source anchor value nor conflicts
  gets no silver label — left for human gold / the external-benchmark floor / the uncertainty
  sample. A **contradicting** pair (positive on anchor P *and* negative on anchor Q) is **dropped,
  never emitted as either** — the data contradicts itself, so a measurement label would be a
  guess. Concretely: ``match = Pos \\ Neg``, ``non_match = Neg \\ Pos``, ``Pos ∩ Neg`` dropped.

**Anchor tiering (ADR 0085):** the anchor set is split into two tiers:

* :data:`GLOBALLY_UNIQUE` — a shared value alone is a definitive cross-source positive (BIC, LEI,
  ISIN, QID, and the nationally-unique-but-globally-distinct Russian id schemes OGRN/INN/OKPO).
* :data:`JURISDICTION_SCOPED` — a shared value is **only** a positive when the two entities'
  ``jurisdiction``/``country`` FtM properties corroborate (both non-empty and share a value).
  Currently contains ``registrationNumber``, which is unique only within a register/jurisdiction —
  two entities in different countries can legitimately share the same number.

:data:`ANCHOR_PROPERTIES` is kept as the union of both tiers so that the benchmark's
``identity_keys`` import (ADR 0080) sees the full set unchanged.

The three non-circularity invariants (ADR 0079 §"The non-circularity invariant"):

* **N1** — ``build_silver_pairs`` accepts *only* FtM entities (no score / probability /
  threshold / linker parameter); this module references no scoring symbol and never calls
  ``score_pairs``.
* **N2** — labels are a pure function of (anchors, ``source_id``) alone; mutating a name or
  other non-anchor, non-``source_id`` field with anchors + source_id fixed leaves the emitted
  label set byte-identical.
* **N3** — every silver row carries ``clerical_score=None``; :func:`persist_silver_pairs`
  asserts this at the write boundary.

**Candidate blocking (ADR 0085 follow-up, WP-1 D2 — ``docs/reviews/
GATE_WP1_MEASUREMENT_SPEC.md``):** :func:`build_silver_pairs` no longer evaluates the full O(n²)
cross-product of entities. Instead, for each anchor property in :data:`ANCHOR_PROPERTIES` it forms
``S_prop`` — the entities carrying >=1 non-empty value for that property — and generates candidate
pairs as the union, over every property, of the within-``S_prop`` pairs (canonical-keyed,
deduplicated); the per-pair classification itself is UNCHANGED. This is EXACT-equivalent, not an
approximation: a pair outside every ``S_prop × S_prop`` combination has, for every anchor property,
at least one side with no value at all — the same ``if not av or not bv: continue`` guard the
classifier already applies per-tier — so it was ALWAYS going to abstain in the unblocked loop too;
omitting it from the candidate set is therefore byte-identical output (proven by
``tests/property/test_prop_silver_blocking.py``, a `@given` equivalence test against a
self-contained copy of the pre-blocking double-loop semantics).

**Residual revisit trigger:** the labelled NEGATIVES this module produces are, by construction,
inherently pairwise WITHIN one anchor block (whichever tier's ``S_prop`` the conflicting pair
shares) — there is no cross-block negative sampling. If any single ``S_prop`` grows past roughly
10k members, the within-block O(|S_prop|²) pair count becomes the new bottleneck and negative
sampling would need its own ADR; this module does not attempt that today.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.provenance.model import get_provenance
from worldmonitor.resolution.gold import GoldPair, persist_gold_pairs

# ---------------------------------------------------------------------------
# Public constants (ADR 0079 + ADR 0085)
# ---------------------------------------------------------------------------

SILVER_SOURCE: str = "canonical_silver"
"""The ``er_gold_pair.source`` tag for canonical-anchor silver labels.

Distinguishable from human gold (``"uncertainty"`` / ``"os_pairs"``) so the evaluation harness
and future promotion logic can select or exclude the silver partition explicitly.
"""

GLOBALLY_UNIQUE: tuple[str, ...] = (
    "wikidataId",
    "leiCode",
    "isin",
    "permId",
    "swiftBic",
    "ogrnCode",
    "innCode",
    "okpoCode",
)
"""Globally-administered canonical IDs — a shared value alone is a definitive cross-source signal.

Rationale for each (ADR 0085 §Decision 1):

* ``wikidataId`` (QID), ``leiCode``, ``isin``, ``swiftBic`` — globally administered unique
  identifier registries; no two real-world entities in distinct registers can share a value.
* ``ogrnCode``, ``innCode``, ``okpoCode`` — Russian Federation identifier schemes (OGRN 13 digits,
  INN 10/12 digits, OKPO 8/10 digits). Each number is unique within the scheme and the schemes
  are nationally administered; no entity outside the Russian registry can carry a valid value, so
  the codes are *globally distinct* even though they are nationally issued.
* ``permId`` — Refinitiv/LSEG Permanent Identifier, globally unique across financial instruments.

**Reversible default (ADR 0085):** revisit if a false-positive ``match`` is observed for any of
these (drop the offender from this tuple); or add further globally-unique types as needed.
"""

JURISDICTION_SCOPED: tuple[str, ...] = ("registrationNumber",)
"""Canonical IDs that are unique ONLY within their jurisdiction/register (ADR 0085 §Decision 2).

A company registration number such as ``123456`` may be legitimately assigned to two entirely
different entities registered in two different countries.  A shared value is therefore a
**positive signal ONLY when the two entities' FtM** ``jurisdiction`` **and/or** ``country``
**properties corroborate** — both non-empty and sharing at least one value (case-folded).
Absent or disjoint jurisdiction/country → the shared value **abstains** (no positive emitted).

Symmetrically, a *conflicting* ``registrationNumber`` (both non-empty, disjoint values) is a
**negative signal ONLY when jurisdiction corroborates** (same register, different number ⇒
different entity).  Across different or absent jurisdictions the conflict does not carry signal.

**Reversible default (ADR 0085):** extend this tuple if further register-scoped ids are added;
collapse into :data:`GLOBALLY_UNIQUE` only if a new id type is confirmed globally unique.
"""

ANCHOR_PROPERTIES: tuple[str, ...] = GLOBALLY_UNIQUE + JURISDICTION_SCOPED
"""Union of both anchor tiers — the full set used by ADR 0079 and referenced by ADR 0080's
``benchmark.identity_keys``.  Kept as a union so external importers still see all nine ids.

**Reversible default** — revisit triggers (ADR 0079 §Reversibility, ADR 0085 §Reversibility):

1. Slice 4's label-sufficiency report shows recall too thin → widen the set.
2. Cross-source format drift causes missed matches → add ``registry.identifier.clean`` pass.
3. A false-positive ``match`` observed for any anchor type → drop it (or move to
   :data:`JURISDICTION_SCOPED`).
4. Any promotion step → a new ADR is required (human-sign-off-gated).
"""

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_JURISDICTION_PROPS: tuple[str, ...] = ("jurisdiction", "country")
"""FtM property names read for jurisdiction corroboration (ADR 0085 §Decision 2)."""


def _canonical(left: str, right: str) -> tuple[str, str]:
    """Order a pair canonically (``left <= right``) — matches ``gold._canonical``."""
    return (left, right) if left <= right else (right, left)


def _anchor_values(entity: FtmEntity, prop: str) -> frozenset[str]:
    """Non-empty FtM-clean values for anchor property *prop* on *entity*.

    Uses ``quiet=True`` so a property absent from this entity's schema (e.g. ``isin`` on a
    ``Company``) returns an empty list instead of raising ``InvalidData``.  Values are
    already FtM-cleaned by ``make_entity`` — no second normalisation pass (ADR 0079
    §Alternatives / §Reversibility).
    """
    return frozenset(str(v) for v in entity.get(prop, quiet=True) if str(v))


def _jurisdiction_values(entity: FtmEntity) -> frozenset[str]:
    """Non-empty, case-folded union of ``jurisdiction`` and ``country`` values for *entity*.

    Case-folded (lowercased) so that ``'GB'`` and ``'gb'`` are treated as the same value —
    FtM does not always normalise country codes on ``make_entity``.
    """
    vals: set[str] = set()
    for prop in _JURISDICTION_PROPS:
        for v in entity.get(prop, quiet=True):
            s = str(v).strip().lower()
            if s:
                vals.add(s)
    return frozenset(vals)


def _jurisdictions_corroborate(a_jur: frozenset[str], b_jur: frozenset[str]) -> bool:
    """Return ``True`` iff both sides have at least one jurisdiction/country value AND they share
    at least one (case-folded) value — i.e. the two records are plausibly in the same register.

    Empty on either side (jurisdiction unknown) → ``False`` (no corroboration, abstain).
    """
    return bool(a_jur) and bool(b_jur) and bool(a_jur & b_jur)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_silver_pairs(entities: Sequence[FtmEntity]) -> list[GoldPair]:
    """Derive canonical-anchor silver label pairs from a collection of FtM entities.

    Implements the POSITIVE + NEGATIVE + CONTRADICTION + ABSTAIN rules from ADR 0079 and the
    anchor-tiering + contradiction-order fix from ADR 0085:

    **Tier rules (ADR 0085):**

    * :data:`GLOBALLY_UNIQUE` anchors: a shared value is **positive-eligible**; conflicting
      values (both non-empty, disjoint) are **negative-eligible** — regardless of source or
      jurisdiction.
    * :data:`JURISDICTION_SCOPED` anchors (``registrationNumber``): the same value is
      positive-eligible **only if** ``jurisdiction``/``country`` corroborates (both sides
      non-empty and sharing a value); similarly, a conflict is negative-eligible only when
      jurisdiction corroborates.  Absent or disjoint jurisdiction → abstain for this anchor.

    **Classification order (ADR 0085 Finding 2 — contradiction before source check):**

    1. ``has_shared`` — any positive-eligible signal active (per tier rules above).
    2. ``has_conflict`` — any negative-eligible signal active (per tier rules above).
    3. ``has_shared AND has_conflict`` → **DROP** (contradiction) — never emit either label,
       regardless of source.
    4. ``has_shared AND distinct sources`` → ``"match"``.
    5. ``has_shared AND same source`` → **ABSTAIN** (the distinct-source gate *downgrades* a
       clean positive to abstain; it must **NEVER** convert it to ``"non_match"``).
    6. ``has_conflict`` (only) → ``"non_match"`` (source-independent, per ADR 0079 §Decision 4).
    7. Otherwise → **ABSTAIN**.

    Returns:
        A canonically ordered (``left_id <= right_id``), de-duplicated ``list[GoldPair]``,
        all carrying ``source=SILVER_SOURCE`` and ``clerical_score=None``.  Output is
        deterministic and order-independent in the input entity sequence.

    **IMPORTANT — N1 (non-circularity):** this function accepts *only* entities.  It has no
    ``score`` / ``probability`` / ``match_probability`` / ``threshold`` / ``linker`` parameter
    and references no scoring symbol.  It never calls ``score_pairs``.
    """
    # Filter to entities that have an id (id-less entities carry no resolvable identity).
    records: list[FtmEntity] = [e for e in entities if e.id is not None]
    n = len(records)
    if n < 2:
        return []

    # Pre-compute anchor values, jurisdiction values, and source_ids once per entity
    # (O(n) cache; candidate generation + pair loop are BLOCKED — see below, ADR 0085 / WP-1 D2).
    anchor_cache: dict[str, dict[str, frozenset[str]]] = {}
    jurisdiction_cache: dict[str, frozenset[str]] = {}
    source_cache: dict[str, str | None] = {}
    for e in records:
        eid = e.id
        assert eid is not None  # guarded above
        anchor_cache[eid] = {prop: _anchor_values(e, prop) for prop in ANCHOR_PROPERTIES}
        jurisdiction_cache[eid] = _jurisdiction_values(e)
        prov = get_provenance(e)
        source_cache[eid] = prov.source_id if prov is not None else None

    # ---------------------------------------------------------------------------------------
    # Candidate generation — anchor-key BLOCKING (WP-1 D2, ``docs/reviews/
    # GATE_WP1_MEASUREMENT_SPEC.md``), replacing the full O(n²) cross-product. For each anchor
    # property, ``S_prop`` is the set of entity ids carrying >=1 (non-empty) value for that
    # property; the candidate set is the union, over every ``ANCHOR_PROPERTIES`` entry, of ALL
    # within-``S_prop`` pairs (canonical-keyed, deduplicated) — NOT just pairs that happen to
    # SHARE the identical value, because the classifier below also needs to see CONFLICTING
    # (disjoint, both-non-empty) value pairs within a tier to emit ``non_match``.
    #
    # EXACT-EQUIVALENCE ARGUMENT (proven byte-for-byte by
    # ``tests/property/test_prop_silver_blocking.py``): a pair (a, b) outside EVERY
    # ``S_prop × S_prop`` combination has, for EVERY anchor property, at least one side with an
    # EMPTY value set — precisely the ``if not av or not bv: continue`` guard the per-pair
    # classifier below applies to every tier. Such a pair therefore has ``has_shared ==
    # has_conflict == False`` for every property in BOTH the globally-unique and the
    # jurisdiction-scoped tier, in the ORIGINAL unblocked loop too — it always ABSTAINS. Omitting
    # it from the candidate set is therefore byte-identical to generating and then classifying it:
    # emitting nothing for a pair that was always going to emit nothing.
    #
    # RESIDUAL REVISIT TRIGGER: the labelled NEGATIVES this module produces are, by construction,
    # inherently pairwise WITHIN one anchor block (S_prop for whichever tier conflicted) — there
    # is no cross-block negative sampling. If any single ``S_prop`` grows past roughly 10k members
    # (e.g. a globally-popular anchor property saturating one dataset), the within-block
    # O(|S_prop|²) candidate count itself becomes the bottleneck and negative sampling would need
    # its own ADR (this module does not attempt that — flagged here as the known scaling edge).
    # ---------------------------------------------------------------------------------------
    candidate_keys: set[tuple[str, str]] = set()
    for prop in ANCHOR_PROPERTIES:
        populated = [eid for eid in anchor_cache if anchor_cache[eid][prop]]
        for i in range(len(populated)):
            for j in range(i + 1, len(populated)):
                candidate_keys.add(_canonical(populated[i], populated[j]))

    # Evaluate every candidate pair (the UNCHANGED per-pair classification, ADR 0085 ordering) and
    # collect into a canonical-keyed dict.
    by_pair: dict[tuple[str, str], GoldPair] = {}
    for key in candidate_keys:
        a_id, b_id = key

        a_anchors = anchor_cache[a_id]
        b_anchors = anchor_cache[b_id]
        a_jur = jurisdiction_cache[a_id]
        b_jur = jurisdiction_cache[b_id]
        a_src = source_cache[a_id]
        b_src = source_cache[b_id]

        # Step 1: compute has_shared + has_conflict INDEPENDENTLY of the source check
        # (ADR 0085 Finding 2 — contradiction detection must precede the source gate).
        has_shared = False
        has_conflict = False

        # Globally-unique tier: a shared value alone is definitive; a conflict is definitive.
        for prop in GLOBALLY_UNIQUE:
            av = a_anchors[prop]
            bv = b_anchors[prop]
            if not av or not bv:
                continue  # at least one side empty — no signal for this anchor type
            if av & bv:
                has_shared = True
            else:
                has_conflict = True

        # Jurisdiction-scoped tier: signal only when jurisdiction/country corroborates.
        if _jurisdictions_corroborate(a_jur, b_jur):
            for prop in JURISDICTION_SCOPED:
                av = a_anchors[prop]
                bv = b_anchors[prop]
                if not av or not bv:
                    continue
                if av & bv:
                    has_shared = True
                else:
                    has_conflict = True
        # else: jurisdiction absent or disjoint → all jurisdiction-scoped anchors abstain

        # Step 2: classify (contradiction checked BEFORE the source gate — ADR 0085).
        if has_shared and has_conflict:
            # Contradiction: drop entirely — never emit as either label.
            continue
        elif has_shared:
            # Positive signal present: match iff distinct sources; abstain if same source.
            # NEVER emit non_match for a same-source clean positive (ADR 0085 Finding 2).
            if a_src is not None and b_src is not None and a_src != b_src:
                left, right = key
                by_pair[key] = GoldPair(
                    left_id=left,
                    right_id=right,
                    label="match",
                    source=SILVER_SOURCE,
                    clerical_score=None,
                )
            # else: same source → abstain (no label emitted)
        elif has_conflict:
            # Negative signal only — source-independent (ADR 0079 §Decision 4).
            left, right = key
            by_pair[key] = GoldPair(
                left_id=left,
                right_id=right,
                label="non_match",
                source=SILVER_SOURCE,
                clerical_score=None,
            )
        # else: abstain — no label emitted

    # Deterministic ordering (mirrors gold.build_gold_pairs).
    return sorted(by_pair.values(), key=lambda p: (p.left_id, p.right_id))


def persist_silver_pairs(session: Session, pairs: Sequence[GoldPair]) -> int:
    """Persist silver pairs to ``er_gold_pair`` (idempotent on ``uq_er_gold_pair``).

    **N3 write-boundary guard:** asserts every pair carries ``source=SILVER_SOURCE`` and
    ``clerical_score=None`` before delegating to
    :func:`~worldmonitor.resolution.gold.persist_gold_pairs`.

    Delegation consequences (all intended — ADR 0079 §Decision 7):

    * **Distinguishable** — rows carry ``source="canonical_silver"``, apart from
      ``"uncertainty"`` / ``"os_pairs"`` / human gold.
    * **Append-only + human-precedence** — ``ON CONFLICT DO NOTHING`` on ``uq_er_gold_pair
      (left_id, right_id)`` means an existing human/curated row is **never overwritten**.
    * **No migration** — ``er_gold_pair.source`` is a free-text ``String(32)``;
      ``"canonical_silver"`` (16 chars) fits.

    Returns the number of pairs offered for insert (not the rows actually written, since some
    may be no-ops on conflict).
    """
    for pair in pairs:
        if pair.source != SILVER_SOURCE:
            raise ValueError(
                f"persist_silver_pairs: pair ({pair.left_id!r}, {pair.right_id!r}) has "
                f"source={pair.source!r} — expected {SILVER_SOURCE!r} (N3 guard: silver rows "
                f"must be tagged with SILVER_SOURCE)"
            )
        if pair.clerical_score is not None:
            raise ValueError(
                f"persist_silver_pairs: pair ({pair.left_id!r}, {pair.right_id!r}) has "
                f"clerical_score={pair.clerical_score!r} — must be None (N3 guard: silver "
                f"labels are never a function of any model score)"
            )
    return persist_gold_pairs(session, pairs)
