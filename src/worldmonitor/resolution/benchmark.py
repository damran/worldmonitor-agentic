"""External-benchmark FLOOR for the ER measurement harness (ADR 0080, G7 slice 3).

Data attribution:
  OS-Pairs: © OpenSanctions contributors. Licence CC BY-NC 4.0.
  URL: https://data.opensanctions.org/contrib/training/pairs-20251209.json.gz
  Docs: https://www.opensanctions.org/docs/opensource/pairs/
  Published reference: nomenklatura RegressionV1 = 91.33 % F1 (arXiv 2603.11051).

  Febrl synthetic data via the 'recordlinkage' package (BSD/ANUOS licence, zero PII).

This module is a PURE REPORT module.  It computes and RETURNS a floor number; it writes
NOTHING to er_gold_pair, merge.py, any threshold / EM-weight, the Splink model, or the
graph.  The floor is sanity-only — NOT a promotion input.  score_fn is INJECTED
(INV-IMPORT-PURITY): benchmark.py references no scoring symbol; the real run wires
score_pairs from outside; tests pass a stub.

The contamination guard (drop_contaminated) is LOAD-BEARING: it excludes every benchmark
pair whose entity identity overlaps our silver/gold partition and reports the count
dropped — never silently truncates.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import gzip
import json
import logging
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution.eval import _harmonic_mean  # pyright: ignore[reportPrivateUsage]
from worldmonitor.resolution.silver import ANCHOR_PROPERTIES

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

BENCHMARK_SOURCE: str = "external_benchmark"
"""The ``source`` tag for all benchmark floor labels (sub-source records which dataset)."""

OS_PAIRS_URL: str = "https://data.opensanctions.org/contrib/training/pairs-20251209.json.gz"
"""Download URL for the OS-Pairs labelled pair set (CC BY-NC 4.0, OpenSanctions)."""


# ---------------------------------------------------------------------------
# Common shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BenchmarkPair:
    """One labelled pair from an external benchmark dataset.

    ``label`` is ``"match"`` | ``"non_match"``.  ``source`` is always
    :data:`BENCHMARK_SOURCE` (``"external_benchmark"``).  ``sub_source`` records which
    dataset (``"os_pairs"`` | ``"febrl1"`` .. ``"febrl4"``).
    """

    left: FtmEntity
    right: FtmEntity
    label: str
    sub_source: str
    source: str = field(default=BENCHMARK_SOURCE)


# ---------------------------------------------------------------------------
# FloorMetrics result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FloorMetrics:
    """Pairwise floor metrics returned by :func:`evaluate_floor`.

    ``n`` is the number of pairs scored **after** decontamination.
    ``n_dropped_contaminated`` is the number of pairs excluded by the contamination guard.
    ``over_merge_rate`` is ``FP / (TP + FP)`` (pairwise floor — fraction of predicted
    matches that the benchmark calls distinct); ``0.0`` when no pairs are predicted match.
    This is the *pairwise* analog of ``eval.over_merge_rate`` (which is cluster-level).
    """

    precision: float
    recall: float
    f1: float
    over_merge_rate: float
    n: int
    n_dropped_contaminated: int


# ---------------------------------------------------------------------------
# Contamination guard (LOAD-BEARING — INV-CONTAM)
# ---------------------------------------------------------------------------


def identity_keys(entity: FtmEntity) -> frozenset[str]:
    """Return the identity key set for *entity*: its id plus all anchor property values.

    Reuses :data:`~worldmonitor.resolution.silver.ANCHOR_PROPERTIES` (single source of
    truth shared with the silver deriver) so the anchor set never diverges.
    """
    keys: set[str] = set()
    if entity.id is not None:
        keys.add(entity.id)
    for prop in ANCHOR_PROPERTIES:
        for val in entity.get(prop, quiet=True):
            s = str(val)
            if s:
                keys.add(s)
    return frozenset(keys)


def drop_contaminated(
    pairs: Iterable[BenchmarkPair],
    our_keys: AbstractSet[str],
) -> tuple[list[BenchmarkPair], int]:
    """Exclude benchmark pairs whose identity overlaps our silver/gold partition.

    For each pair, if ``identity_keys(left) | identity_keys(right)`` intersects
    ``our_keys``, the pair is dropped and counted.  The invariant
    ``len(kept) + n_dropped == len(input)`` always holds (no silent truncation).
    The dropped count is logged to ``logging.getLogger(__name__)`` (stderr discipline).

    Returns:
        ``(kept, n_dropped)`` — the surviving pairs and the exact drop count.
    """
    kept: list[BenchmarkPair] = []
    n_dropped = 0
    for pair in pairs:
        combined = identity_keys(pair.left) | identity_keys(pair.right)
        if combined.isdisjoint(our_keys):
            kept.append(pair)
        else:
            n_dropped += 1
    if n_dropped:
        log.warning(
            "drop_contaminated: dropped %d benchmark pairs that overlapped our "
            "silver/gold partition (identity_keys intersection with our_keys).",
            n_dropped,
        )
    return kept, n_dropped


# ---------------------------------------------------------------------------
# Floor evaluator (INV-FLOOR-MATH, INV-IMPORT-PURITY)
# ---------------------------------------------------------------------------


def evaluate_floor(
    pairs: Iterable[BenchmarkPair],
    score_fn: Callable[[FtmEntity, FtmEntity], float],
    threshold: float,
    *,
    contamination_keys: AbstractSet[str] = frozenset(),
) -> FloorMetrics:
    """Compute pairwise floor metrics for our matcher on *pairs*.

    Runs :func:`drop_contaminated` first (the floor is always on decontaminated data).
    For each kept pair, ``predicted_match = score_fn(left, right) >= threshold``.

    Metrics (INV-FLOOR-MATH):
    * ``precision = TP / (TP + FP)``
    * ``recall    = TP / (TP + FN)``
    * ``f1`` via :func:`~worldmonitor.resolution.eval._harmonic_mean` (reused, not
      re-implemented, so the divide-by-zero guard is shared).
    * ``over_merge_rate = FP / (TP + FP)`` — pairwise floor fraction of predicted
      matches that the benchmark calls distinct; ``0.0`` when no predicted matches.

    ``score_fn`` is INJECTED (INV-IMPORT-PURITY): this function bakes in no model;
    tests pass a stub; the ops run wires score_pairs externally.
    """
    all_pairs = list(pairs)
    kept, n_dropped = drop_contaminated(all_pairs, contamination_keys)

    tp = fp = fn = 0
    for pair in kept:
        predicted = score_fn(pair.left, pair.right) >= threshold
        gold_match = pair.label == "match"
        if predicted and gold_match:
            tp += 1
        elif predicted and not gold_match:
            fp += 1
        elif not predicted and gold_match:
            fn += 1
        # TN: not predicted, not gold_match — no action needed

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = _harmonic_mean(precision, recall)
    omr = fp / (tp + fp) if (tp + fp) > 0 else 0.0

    return FloorMetrics(
        precision=precision,
        recall=recall,
        f1=f1,
        over_merge_rate=omr,
        n=len(kept),
        n_dropped_contaminated=n_dropped,
    )


# ---------------------------------------------------------------------------
# OS-Pairs importer (INV-JUDGEMENT-MAP, INV-FTM-NATIVE)
# ---------------------------------------------------------------------------

_JUDGEMENT_MAP: dict[str, str] = {
    "positive": "match",
    "negative": "non_match",
}


def _iter_lines(source: str | os.PathLike[str] | Iterable[str]) -> Iterator[str]:
    """Yield text lines from a local path (plain or .gz) or an in-memory iterable."""
    if isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                yield from fh
        else:
            with path.open("r", encoding="utf-8") as fh:
                yield from fh
    else:
        yield from source


def load_os_pairs(
    source: str | os.PathLike[str] | Iterable[str],
) -> Iterator[BenchmarkPair]:
    """Parse OS-Pairs line-JSON and yield :class:`BenchmarkPair` objects.

    Accepts a local file path (``*.json`` or ``*.json.gz``, transparently
    decompressed) **or** an in-memory iterable of JSON strings (for hermetic tests).

    Judgement mapping (INV-JUDGEMENT-MAP):
    * ``"positive"`` → ``label="match"``
    * ``"negative"`` → ``label="non_match"``
    * Any other value (``"unsure"``, ``"no_judgement"``, missing) is **skipped**,
      never coerced.

    ``left``/``right`` are built via :func:`~worldmonitor.ontology.ftm.make_entity`
    (INV-FTM-NATIVE) — extra OpenSanctions keys (``caption``, ``datasets``) are ignored
    by FtM.
    """
    for raw_line in _iter_lines(source):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue

        judgement = obj.get("judgement", "")
        label = _JUDGEMENT_MAP.get(judgement)
        if label is None:
            continue  # skip unsure / no_judgement / unknown (INV-JUDGEMENT-MAP)

        try:
            left = make_entity(obj["left"])
            right = make_entity(obj["right"])
        except Exception:  # noqa: BLE001
            continue  # malformed entity dict — skip

        yield BenchmarkPair(
            left=left,
            right=right,
            label=label,
            sub_source="os_pairs",
        )


def fetch_os_pairs(cache_dir: Path | None = None) -> Path:
    """Download the OS-Pairs bulk file on demand and return the local path.

    The file is streamed from :data:`OS_PAIRS_URL` into *cache_dir* (default:
    ``$WM_BENCHMARK_CACHE`` or ``~/.cache/worldmonitor/benchmark``) only if absent.
    Pull-only: our data never leaves.  The bulk file must be ``.gitignored`` and is
    **never committed** (licence + hermeticity).  This function is **not called by
    tests**.
    """
    import httpx  # already a runtime dep

    if cache_dir is None:
        env_cache = os.environ.get("WM_BENCHMARK_CACHE")
        cache_dir = (
            Path(env_cache) if env_cache else Path.home() / ".cache" / "worldmonitor" / "benchmark"
        )
    cache_dir.mkdir(parents=True, exist_ok=True)

    filename = OS_PAIRS_URL.rsplit("/", 1)[-1]
    dest = cache_dir / filename
    if dest.exists():
        log.info("fetch_os_pairs: using cached %s", dest)
        return dest

    log.info("fetch_os_pairs: downloading %s -> %s", OS_PAIRS_URL, dest)
    with httpx.stream("GET", OS_PAIRS_URL, follow_redirects=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=65536):
                fh.write(chunk)
    log.info("fetch_os_pairs: done, %d bytes", dest.stat().st_size)
    return dest


# ---------------------------------------------------------------------------
# Febrl importer (lazy recordlinkage import — INV-FTM-NATIVE)
# ---------------------------------------------------------------------------


def _febrl_record_to_entity(record_id: str, row: Mapping[str, Any]) -> FtmEntity:
    """Map a Febrl record row to an FtM ``Person`` entity (no recordlinkage import).

    This mapper is split out so hermetic unit tests can exercise it with a synthetic
    dict without importing ``recordlinkage``.

    Fields mapped (all optional — missing/empty values are skipped):
    * ``given_name`` → ``firstName``
    * ``surname`` → ``lastName``
    * ``given_name`` + ``surname`` combined → ``name``
    * ``date_of_birth`` → ``birthDate``
    """
    props: dict[str, list[str]] = {}

    given = str(row.get("given_name") or "").strip()
    surname = str(row.get("surname") or "").strip()
    dob = str(row.get("date_of_birth") or "").strip()

    if given:
        props["firstName"] = [given]
    if surname:
        props["lastName"] = [surname]
    full_name = " ".join(part for part in (given, surname) if part)
    if full_name:
        props["name"] = [full_name]
    if dob:
        props["birthDate"] = [dob]

    return make_entity({"id": record_id, "schema": "Person", "properties": props})


def load_febrl(
    dataset: str = "febrl1",
    *,
    negatives: int = 0,
    seed: int = 0,
) -> Iterator[BenchmarkPair]:
    """Yield :class:`BenchmarkPair` objects from a Febrl synthetic dataset.

    Uses the ``recordlinkage`` package (lazy-imported — optional/dev group only).
    Raises a clear :class:`ImportError` naming the optional group when absent.

    Each gold link (true duplicate pair) yields a ``match`` pair.  A seeded sample of
    ``negatives`` non-link pairs yields ``non_match`` pairs (default ``negatives=0``
    ⇒ matches only, the deterministic minimum).

    Parameters:
        dataset: one of ``"febrl1"`` .. ``"febrl4"``
        negatives: number of non-match (negative) pairs to sample
        seed: RNG seed for the negative sample
    """
    try:
        import recordlinkage.datasets as rl_datasets
    except (ImportError, ModuleNotFoundError) as exc:
        raise ImportError(
            "recordlinkage is required for load_febrl() but is not installed. "
            "Install it with:  uv sync --group benchmark   or   pip install recordlinkage"
        ) from exc

    loaders = {
        "febrl1": rl_datasets.load_febrl1,
        "febrl2": rl_datasets.load_febrl2,
        "febrl3": rl_datasets.load_febrl3,
        "febrl4": rl_datasets.load_febrl4,
    }
    if dataset not in loaders:
        raise ValueError(f"Unknown Febrl dataset {dataset!r}; expected one of {list(loaders)}")

    df, links = loaders[dataset](return_links=True)  # type: ignore[call-arg]

    # Build FtM entities for every record (keyed by Febrl record_id string).
    entities: dict[str, FtmEntity] = {}
    for rec_id, row in df.iterrows():
        entities[str(rec_id)] = _febrl_record_to_entity(str(rec_id), dict(row))

    # Yield match pairs from the gold link set.
    for left_id, right_id in links:
        left_ent = entities.get(str(left_id))
        right_ent = entities.get(str(right_id))
        if left_ent is None or right_ent is None:
            continue
        yield BenchmarkPair(
            left=left_ent,
            right=right_ent,
            label="match",
            sub_source=dataset,
        )

    # Optionally sample non-match pairs from cross-link record pairs.
    if negatives > 0:
        import random

        rng = random.Random(seed)
        all_ids = list(entities.keys())
        link_set = {(str(a), str(b)) for a, b in links} | {(str(b), str(a)) for a, b in links}
        candidates = [
            (a, b)
            for i, a in enumerate(all_ids)
            for b in all_ids[i + 1 :]
            if (a, b) not in link_set
        ]
        rng.shuffle(candidates)
        for left_id, right_id in candidates[:negatives]:
            left_ent = entities[left_id]
            right_ent = entities[right_id]
            yield BenchmarkPair(
                left=left_ent,
                right=right_ent,
                label="non_match",
                sub_source=dataset,
            )
