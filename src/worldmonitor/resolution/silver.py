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

The three non-circularity invariants (ADR 0079 §"The non-circularity invariant"):

* **N1** — ``build_silver_pairs`` accepts *only* FtM entities (no score / probability /
  threshold / linker parameter); this module references no scoring symbol and never calls
  ``score_pairs``.
* **N2** — labels are a pure function of (anchors, ``source_id``) alone; mutating a name or
  other non-anchor, non-``source_id`` field with anchors + source_id fixed leaves the emitted
  label set byte-identical.
* **N3** — every silver row carries ``clerical_score=None``; :func:`persist_silver_pairs`
  asserts this at the write boundary.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.provenance.model import get_provenance
from worldmonitor.resolution.gold import GoldPair, persist_gold_pairs

# ---------------------------------------------------------------------------
# Public constants (ADR 0079 — reversible defaults with revisit triggers)
# ---------------------------------------------------------------------------

SILVER_SOURCE: str = "canonical_silver"
"""The ``er_gold_pair.source`` tag for canonical-anchor silver labels.

Distinguishable from human gold (``"uncertainty"`` / ``"os_pairs"``) so the evaluation harness
and future promotion logic can select or exclude the silver partition explicitly.
"""

ANCHOR_PROPERTIES: tuple[str, ...] = (
    "wikidataId",
    "leiCode",
    "registrationNumber",
    "ogrnCode",
    "innCode",
    "swiftBic",
    "isin",
    "okpoCode",
    "permId",
)
"""Canonical-ID property set used for the positive + negative anchor rules (ADR 0079 §Decision 1).

All nine are confirmed FtM ``identifier``-typed properties in this repo's FtM 4.x model.

**Reversible default** — revisit triggers (ADR 0079 §Reversibility):

1. Slice 4's label-sufficiency report shows recall too thin → widen the set.
2. Cross-source format drift causes missed matches → add ``registry.identifier.clean`` pass.
3. A false-positive ``match`` observed for any anchor type → drop it from the set.
4. Any promotion step → a new ADR is required (human-sign-off-gated; this ADR does not
   authorise it).
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_silver_pairs(entities: Sequence[FtmEntity]) -> list[GoldPair]:
    """Derive canonical-anchor silver label pairs from a collection of FtM entities.

    Implements the POSITIVE + NEGATIVE + CONTRADICTION + ABSTAIN rules from ADR 0079:

    * **POSITIVE**: a pair ``(a, b)`` with distinct ids where some anchor property ``P`` has a
      shared non-empty value **and** ``get_provenance(a).source_id != get_provenance(b).source_id``
      (≥2 distinct sources — the cross-list signal).
    * **NEGATIVE**: a pair ``(a, b)`` where some anchor property ``P`` has both values non-empty
      and **disjoint** (conflicting authoritative ids of the same type).
    * **CONTRADICTION** (positive on ``P`` *and* negative on ``Q``): dropped — never emitted.
    * **ABSTAIN** (neither positive nor negative): no label.

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

    # Pre-compute anchor values and source_ids once per entity (O(n) cache, O(n²) pair loop).
    anchor_cache: dict[str, dict[str, frozenset[str]]] = {}
    source_cache: dict[str, str | None] = {}
    for e in records:
        eid = e.id
        assert eid is not None  # guarded above
        anchor_cache[eid] = {prop: _anchor_values(e, prop) for prop in ANCHOR_PROPERTIES}
        prov = get_provenance(e)
        source_cache[eid] = prov.source_id if prov is not None else None

    # Evaluate all O(n²) pairs and collect into a canonical-keyed dict (de-dup on (left, right)).
    by_pair: dict[tuple[str, str], GoldPair] = {}
    for i in range(n):
        for j in range(i + 1, n):
            a_id = records[i].id
            b_id = records[j].id
            assert a_id is not None and b_id is not None
            if a_id == b_id:
                continue  # no self-pair (should be impossible via the range, but safe)

            key = _canonical(a_id, b_id)
            if key in by_pair:
                continue  # already resolved for this (left, right) pair

            a_anchors = anchor_cache[a_id]
            b_anchors = anchor_cache[b_id]
            a_src = source_cache[a_id]
            b_src = source_cache[b_id]

            is_positive = False
            is_negative = False

            for prop in ANCHOR_PROPERTIES:
                av = a_anchors[prop]
                bv = b_anchors[prop]
                if not av or not bv:
                    continue  # at least one side empty — no signal for this anchor type

                shared = av & bv
                if shared:
                    # Shared value: positive iff the two records come from distinct sources.
                    if a_src is not None and b_src is not None and a_src != b_src:
                        is_positive = True
                    # (Same source: no label; within-source duplicates are excluded.)
                else:
                    # Both non-empty and disjoint: conflicting authoritative ids → negative
                    # (source-independent per ADR 0079 §Decision 4).
                    is_negative = True

            if is_positive and is_negative:
                # Contradiction: drop entirely — never emit as either label.
                continue
            elif is_positive:
                left, right = key
                by_pair[key] = GoldPair(
                    left_id=left,
                    right_id=right,
                    label="match",
                    source=SILVER_SOURCE,
                    clerical_score=None,
                )
            elif is_negative:
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
