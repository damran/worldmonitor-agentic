"""Reproducible gold-pair set for the ER measurement harness (ADR 0043 / Gate A, slice-1).

A small, **seeded, deterministic** set of labelled (``match`` / ``non_match``) record pairs the
evaluation harness (:mod:`worldmonitor.resolution.eval`) scores the resolver against. It is the
regression instrument every later ER gate measures over. It is person-NEUTRAL: it stores id
references plus a clerical label in the ``er_gold_pair`` Postgres table — never a graph mutation
(the harness READS, never writes, the graph).

Construction (gate spec §7):

* **Stratified uncertainty sampling** over the **0.5-0.95** Splink-score band. Candidate pairs
  are scored with the LIVE :func:`score_pairs` (the expert-set v0 model — slice-1 measures it,
  it is not changed); the decision-boundary band is split into fixed strata and a seeded RNG
  draws a bounded sample per stratum (most-informative labels live near the boundary).
* A seeded **OS-Pairs-style** hard-case set — the OpenSanctions ER idiom of known-hard pairs:
  same-name / different-entity, transliteration variants, shared-but-clashing ids.

By construction the set **must** include:

* at least one **over-merge trap** — two DISTINCT gold entities the resolver is tempted to fuse
  (a same-name / different-entity ``non_match``), and
* at least one **blocking-conditional** pair — a gold pair lying OUTSIDE every blocking rule
  (different country, different 4-char name prefix, no shared ``wikidataId``), so the §5.3
  pairwise blind spot is exercisable.

Everything is seeded; :func:`build_gold_pairs` is reproducible run-to-run for a fixed seed.
"""

from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErGoldPair
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.resolution.splink_model import ScoredPair, score_pairs

# The decision-boundary band where the model is least certain and labels most informative
# (gate spec §7). Pairs scored inside [LOW, HIGH] are candidates for uncertainty sampling.
UNCERTAINTY_LOW: float = 0.5
UNCERTAINTY_HIGH: float = 0.95

# Sampling controls — fixed so the set is small + reproducible.
DEFAULT_SEED: int = 0xA43  # ADR 0043
_STRATA: int = 3  # number of equal-width strata across the uncertainty band
_PER_STRATUM: int = 4  # max pairs drawn per stratum


@dataclass(frozen=True, slots=True)
class GoldPair:
    """One labelled gold record-pair (canonically ordered ``left_id <= right_id``).

    ``label`` is ``"match"`` | ``"non_match"``; ``source`` records HOW the pair was sampled
    (``"uncertainty"`` | ``"os_pairs"``); ``clerical_score`` is the Splink match probability
    that produced it (``None`` for a deterministic OS-Pairs hard case with no Splink score) —
    it maps to Splink's ``clerical_match_score`` in the labels table.
    """

    left_id: str
    right_id: str
    label: str
    source: str
    clerical_score: float | None = None


def _canonical(left: str, right: str) -> tuple[str, str]:
    """Order a pair canonically (``left <= right``) — matches ``uq_er_gold_pair``."""
    return (left, right) if left <= right else (right, left)


def _stratified_uncertainty_sample(
    scored: Sequence[ScoredPair], rng: random.Random
) -> list[GoldPair]:
    """Stratified uncertainty sample over the 0.5-0.95 Splink-score band (gate spec §7).

    The band is split into ``_STRATA`` equal-width strata; within each, a seeded RNG draws up
    to ``_PER_STRATUM`` pairs (sorted first for determinism, then shuffled by ``rng`` so the
    draw depends only on the seed, not on Splink's row order). A pair scored >= the mid-band is
    provisionally labelled ``"match"``, below it ``"non_match"`` — a clerical *prior* the human
    reviewer would confirm; the harness only needs a reproducible labelled set to measure over.
    """
    band = [pair for pair in scored if UNCERTAINTY_LOW <= pair.probability <= UNCERTAINTY_HIGH]
    band.sort(key=lambda p: (p.probability, p.left_id, p.right_id))
    width = (UNCERTAINTY_HIGH - UNCERTAINTY_LOW) / _STRATA
    midpoint = (UNCERTAINTY_LOW + UNCERTAINTY_HIGH) / 2.0

    out: list[GoldPair] = []
    for stratum in range(_STRATA):
        lo = UNCERTAINTY_LOW + stratum * width
        hi = lo + width if stratum < _STRATA - 1 else UNCERTAINTY_HIGH
        bucket = [p for p in band if lo <= p.probability <= hi]
        rng.shuffle(bucket)
        for pair in bucket[:_PER_STRATUM]:
            left, right = _canonical(pair.left_id, pair.right_id)
            out.append(
                GoldPair(
                    left_id=left,
                    right_id=right,
                    label="match" if pair.probability >= midpoint else "non_match",
                    source="uncertainty",
                    clerical_score=pair.probability,
                )
            )
    return out


# --------------------------------------------------------------------------------------------
# Seeded OS-Pairs-style hard-case set (gate spec §7).
# --------------------------------------------------------------------------------------------
# The OpenSanctions ER pairs idiom: known-hard cases that exercise the metrics' worst failures.
# Each entry is (left_id, right_id, label, note). The set is DETERMINISTIC (no RNG) and, by
# construction, contains BOTH required traps:
#   * the OVER-MERGE TRAP — two distinct gold entities (a same-name/different-entity non_match)
#     the resolver is tempted to fuse; and
#   * the BLOCKING-CONDITIONAL pair — a gold pair outside every blocking rule (different country,
#     different name-prefix, no shared wikidata) so §5.3 is exercisable.
# These ids reference records the caller's frame supplies; they are id references, never graph
# mutations. The note documents WHY each pair is hard.
_OS_PAIRS: tuple[tuple[str, str, str, str], ...] = (
    # OVER-MERGE TRAP: same name, DIFFERENT real entities -> must stay non_match.
    ("os_overmerge_a", "os_overmerge_b", "non_match", "same-name/different-entity over-merge trap"),
    # BLOCKING-CONDITIONAL: distinct entities outside every blocking rule (different country,
    # different name-prefix, no shared wikidata) -> the pairwise metric is structurally blind to
    # this pair, so the cluster metrics must catch it (gate spec §5.3).
    (
        "os_blockcond_a",
        "os_blockcond_b",
        "non_match",
        "blocking-conditional: outside every blocking rule",
    ),
    # Transliteration variant of ONE entity -> a true match the model may miss.
    ("os_translit_a", "os_translit_b", "match", "transliteration variant of one entity"),
    # Shared-but-CLASHING government ids -> distinct entities despite a name collision.
    ("os_clashid_a", "os_clashid_b", "non_match", "shared-name but clashing registration ids"),
)

# The two ids that, by construction, MUST be present as traps (consumed by tests / the harness).
OVER_MERGE_TRAP: tuple[str, str] = ("os_overmerge_a", "os_overmerge_b")
BLOCKING_CONDITIONAL_PAIR: tuple[str, str] = ("os_blockcond_a", "os_blockcond_b")


def _os_pairs_hard_cases() -> list[GoldPair]:
    """The deterministic OS-Pairs-style hard-case set (no RNG — fully reproducible)."""
    out: list[GoldPair] = []
    for left, right, label, _note in _OS_PAIRS:
        cl, cr = _canonical(left, right)
        out.append(GoldPair(left_id=cl, right_id=cr, label=label, source="os_pairs"))
    return out


def _seed_from(seed: int, entities: Sequence[FtmEntity]) -> int:
    """Derive a stable RNG seed from the base seed + the entity-id set.

    Folding the (sorted) entity ids into the seed makes the draw reproducible for a fixed
    input but distinct across different gold frames, while staying deterministic run-to-run.
    """
    ids = "\x1f".join(sorted(entity.id or "" for entity in entities))
    digest = hashlib.sha256(f"{seed}:{ids}".encode()).hexdigest()
    return int(digest[:16], 16)


def build_gold_pairs(
    entities: Sequence[FtmEntity],
    *,
    seed: int = DEFAULT_SEED,
) -> list[GoldPair]:
    """Build the seeded, deterministic gold-pair set (gate spec §7).

    Combines the stratified uncertainty sample over the live model's 0.5-0.95 score band with
    the deterministic OS-Pairs-style hard-case set. The OS-Pairs set GUARANTEES (by
    construction) the required over-merge trap and blocking-conditional pair are present, so the
    returned set is never blind to either even when no scored candidate happens to land in the
    band. Pairs are canonically ordered and de-duplicated on ``(left_id, right_id)`` (OS-Pairs
    take precedence — a hand-curated label beats a sampled prior). Fully reproducible for a
    fixed ``seed`` + entity set.
    """
    rng = random.Random(_seed_from(seed, entities))
    scored = score_pairs(entities) if len(entities) >= 2 else []
    sampled = _stratified_uncertainty_sample(scored, rng)
    hard_cases = _os_pairs_hard_cases()

    by_pair: dict[tuple[str, str], GoldPair] = {}
    for pair in sampled:
        by_pair[(pair.left_id, pair.right_id)] = pair
    for pair in hard_cases:  # OS-Pairs override a sampled prior on the same pair.
        by_pair[(pair.left_id, pair.right_id)] = pair
    # Deterministic ordering of the final set.
    return sorted(by_pair.values(), key=lambda p: (p.left_id, p.right_id))


def persist_gold_pairs(session: Session, pairs: Sequence[GoldPair]) -> int:
    """Persist gold pairs to ``er_gold_pair`` (idempotent on ``uq_er_gold_pair``).

    Mirrors the durable-judgement idiom (``ResolverJudgement``): ON CONFLICT DO NOTHING on the
    canonical ``(left_id, right_id)`` unique, so re-running the seeded builder keeps the existing
    rows rather than violating the constraint. Returns the number of pairs offered for insert.
    This WRITES the gold-pair table only — never the graph and never any live merge value.
    """
    if not pairs:
        return 0
    values = [
        {
            "id": str(uuid.uuid4()),
            "left_id": pair.left_id,
            "right_id": pair.right_id,
            "label": pair.label,
            "source": pair.source,
            "clerical_score": pair.clerical_score,
        }
        for pair in pairs
    ]
    session.execute(
        pg_insert(ErGoldPair).values(values).on_conflict_do_nothing(constraint="uq_er_gold_pair")
    )
    return len(values)
