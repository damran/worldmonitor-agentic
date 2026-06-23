"""Splink (DuckDB) adapter — pairwise Fellegi-Sunter scoring for entity resolution.

Splink owns blocking + probabilistic comparison. For v0 the model uses
*expert-set* m/u weights (transparent weighted scoring, per CLAUDE.md "transparent
weighted first, then Bayesian"); unsupervised EM training is a gated upgrade for
when there is enough data to estimate parameters reliably. Splink ships no type
stubs, so it is imported only here behind relaxed boundary reports; the public
surface (:func:`score_pairs`) is fully typed.
"""

# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import fingerprints
import pandas as pd
from followthemoney import model, registry
from followthemoney.exc import InvalidData
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

from worldmonitor.ontology.ftm import FtmEntity

# Splink is chatty on stdout/stderr; keep the pipeline quiet.
logging.getLogger("splink").setLevel(logging.ERROR)

# Default prior and the high-confidence merge threshold (conservative: a clear
# name match alone is not enough — it needs corroboration to clear this).
DEFAULT_PRIOR = 0.001
DEFAULT_PREDICT_THRESHOLD = 0.5

# Generic descriptors that ``fingerprints.remove_types`` strips down to a single bare token,
# collapsing two DISTINCT orgs onto the same exact-name key (Gate B-3 / ADR 0039, Defect 1).
# Each entry is a token observed as the SOLE survivor of ``remove_types`` on a real corporate
# name whose only non-legal-form word is itself generic — e.g. "International Trading Co Ltd"
# and "Import Export Trading Co Ltd" BOTH reduce to "trading". When such a single generic token
# is all that remains, we fall back to the richer ``fingerprints.generate`` key (legal forms
# still stripped) so the distinguishing tokens (intl / imp / exp / brand) survive. The list is
# deliberately TIGHT: only descriptors that are (a) generic enough to recur across unrelated
# firms and (b) routinely the lone non-legal-form token on real company names. Multi-token
# keys (e.g. "komplekt legion", "group holdings") are never affected — only the single-token
# pathological case.
_GENERIC_NAME_TOKENS = frozenset(
    {
        "trading",  # "International Trading Co Ltd" -> "trading" (reproduced, ADR 0039)
        "group",  # "X Group Ltd" -> "group"
        "holdings",  # "X Holdings Ltd" -> "holdings"
        "general",  # "X General Trading LLC" residue -> "general"
        "global",  # "X Global Ltd" -> "global"
        "company",  # bare "Company"-as-brand residue
        "international",  # "X International Ltd" -> "international"
        "services",  # "X Services Ltd" -> "services"
        "industries",  # "X Industries Ltd" -> "industries"
        "enterprise",  # "X Enterprise Ltd" -> "enterprise"
        "enterprises",  # "X Enterprises Ltd" -> "enterprises"
        "import",  # "X Import Ltd" -> "import"
        "export",  # "X Export Ltd" -> "export"
    }
)


@dataclass(frozen=True, slots=True)
class ScoredPair:
    """A candidate duplicate pair with its match probability."""

    left_id: str
    right_id: str
    probability: float


def _name_comparison() -> dict[str, Any]:
    # Compares the script-stable name FINGERPRINT (see `_name_fingerprint`), not the raw
    # name — so two records of one entity that store names in different scripts still hit
    # the exact level. jaro_winkler handles near-fingerprints (a typo / a dropped token).
    return {
        "output_column_name": "name_fp",
        "comparison_levels": [
            {
                "sql_condition": '"name_fp_l" IS NULL OR "name_fp_r" IS NULL',
                "is_null_level": True,
                "label_for_charts": "null",
            },
            {
                "sql_condition": '"name_fp_l" = "name_fp_r"',
                "label_for_charts": "exact",
                "m_probability": 0.99,
                "u_probability": 0.0001,
            },
            {
                "sql_condition": 'jaro_winkler_similarity("name_fp_l", "name_fp_r") >= 0.92',
                "label_for_charts": "jw>=.92",
                "m_probability": 0.88,
                "u_probability": 0.003,
            },
            {
                "sql_condition": 'jaro_winkler_similarity("name_fp_l", "name_fp_r") >= 0.82',
                "label_for_charts": "jw>=.82",
                "m_probability": 0.6,
                "u_probability": 0.03,
            },
            {
                "sql_condition": "ELSE",
                "label_for_charts": "else",
                "m_probability": 0.04,
                "u_probability": 0.95,
            },
        ],
    }


def _exact_comparison(column: str, *, m: float, u: float) -> dict[str, Any]:
    return {
        "output_column_name": column,
        "comparison_levels": [
            {
                "sql_condition": f'"{column}_l" IS NULL OR "{column}_r" IS NULL',
                "is_null_level": True,
                "label_for_charts": "null",
            },
            {
                "sql_condition": f'"{column}_l" = "{column}_r"',
                "label_for_charts": "exact",
                "m_probability": m,
                "u_probability": u,
            },
            {
                "sql_condition": "ELSE",
                "label_for_charts": "else",
                "m_probability": 1 - m,
                "u_probability": 1 - u,
            },
        ],
    }


# ASCII Unit Separator (0x1F). Used to pack the multi-valued ``reg_id`` SET into a single
# non-null VARCHAR column (see ``_distinguishing_ids``). It is a SAFE delimiter because the FtM
# ``identifier`` type's ``clean`` strips ALL C0 control characters (verified: ``"a\x1fb"`` ->
# ``"ab"``), so a cleaned identifier can NEVER itself contain 0x1F — there is no escaping/
# collision hazard. DuckDB ``string_split(col, chr(31))`` reconstitutes the set for overlap.
_REG_ID_SEP = "\x1f"


def _distinguishing_ids(entity: FtmEntity) -> str:
    """Normalized SET of distinguishing government identifiers, packed into one VARCHAR.

    Projects the entity's FtM ``identifier``-typed ``registrationNumber`` AND ``taxNumber``
    values into one order-independent, de-duplicated, 0x1F-joined string of cleaned ids
    (ADR 0039 §3.1.1). Both properties feed the SAME column on purpose: the same government id
    stored as ``registrationNumber`` on one record and ``taxNumber`` on another must be treated
    as the SAME id (cross-field clash detection, INV-1b). Values are cleaned via the FtM
    ``identifier`` type so trivial differences (``"12345"`` vs ``" 12345 "``) do not clash.

    Returns the EMPTY STRING ``""`` (never ``None``) when neither property carries a value.
    Projecting a non-null VARCHAR sentinel — rather than ``None`` — is load-bearing for
    type stability (ADR 0039, builder note): when an ENTIRE batch is id-less, an all-``None``
    pandas column is inferred by DuckDB as ``INTEGER``, which then fails to bind ``len(...)`` /
    ``string_split(...)`` in the comparison SQL and crashes ``score_pairs`` for every id-less
    batch. An all-empty-string column is always inferred as ``VARCHAR``. The comparison's null
    level keys off ``len(reg_id) = 0`` (the empty sentinel), so a missing id stays neutral and
    never penalizes a genuine duplicate (INV-3b).
    """
    raw: list[str] = []
    for prop in ("registrationNumber", "taxNumber"):
        raw.extend(entity.get(prop, quiet=True))
    cleaned: set[str] = set()
    for value in raw:
        normalized = registry.identifier.clean(value)
        if normalized:
            cleaned.add(normalized)
    # Sorted for determinism; the SQL compares as sets (string_split), so order is not
    # load-bearing. Empty set -> "" so the column is a never-all-null VARCHAR (see docstring).
    return _REG_ID_SEP.join(sorted(cleaned))


def _distinguishing_id_comparison() -> dict[str, Any]:
    # Negative-evidence comparison over the multi-valued ``reg_id`` set (ADR 0039 §3.1).
    # Splink scores are multiplicative Bayes factors (match weight log2(m/u)); the CLASH
    # level uses m << u so a present-but-disjoint government id ACTIVELY lowers the posterior
    # below the 0.92 merge boundary — the strongest "these are distinct legal persons" signal.
    #
    # ``reg_id`` is a non-null VARCHAR: the 0x1F-joined set of cleaned ids, or "" when the
    # record has no id (see ``_distinguishing_ids`` — the empty-string sentinel keeps the whole
    # column VARCHAR even for an all-id-less batch, which an all-NULL column would not).
    # ``string_split(reg_id, chr(31))`` reconstitutes each side's id SET in SQL.
    #
    # Level order is load-bearing: the null level is evaluated FIRST (either side empty), so a
    # missing id on either side can NEVER fall through to the clash branch. ``list_has_any`` =>
    # the two id sets share >=1 id (overlap, NOT a clash — INV-3/multivalued-overlap); the ELSE
    # branch is therefore reached ONLY when both sets are non-empty and DISJOINT (the clash).
    split_l = 'string_split("reg_id_l", chr(31))'
    split_r = 'string_split("reg_id_r", chr(31))'
    return {
        "output_column_name": "reg_id",
        "comparison_levels": [
            {
                "sql_condition": (
                    '"reg_id_l" IS NULL OR "reg_id_r" IS NULL '
                    'OR len("reg_id_l") = 0 OR len("reg_id_r") = 0'
                ),
                "is_null_level": True,
                "label_for_charts": "null",
            },
            {
                "sql_condition": f"list_has_any({split_l}, {split_r})",
                "label_for_charts": "shared-id",
                "m_probability": 0.95,
                "u_probability": 0.01,
            },
            {
                "sql_condition": "ELSE",
                "label_for_charts": "clash",
                "m_probability": 0.0005,
                "u_probability": 0.30,
            },
        ],
    }


def _name_fingerprint(entity: FtmEntity) -> str | None:
    """Script-stable name key for matching, or ``None`` for a no-name entity.

    ``entity.first("name")`` returns the SORT-FIRST of the multi-valued name, which flips
    alphabet for bilingual records (Cyrillic vs Latin), so two records of ONE entity
    projected different names and never matched — the multi-script ER miss (review;
    ADR 0035). The ``fingerprints`` library (the OpenSanctions / nomenklatura ER stack,
    already a locked dependency) transliterates to Latin, sorts tokens, and strips
    legal-form words, so both records reduce to the SAME key — e.g. both
    ``"ООО Легион Комплект"`` and ``"LIMITED LIABILITY COMPANY LEGION KOMPLEKT"`` →
    ``"komplekt legion"``. Keyed off ``caption`` (FtM's deterministic best name) but
    guarded on a real ``name`` value so a no-name entity (e.g. ``Sanction``, whose caption
    falls back to a programme code) stays ``None`` and never matches on an empty name.

    KNOWN GAP (deferred, ADR 0035): ``fingerprints`` renders abjad scripts (Arabic/Persian)
    as lossy consonant skeletons, so it is not a reliable *sole* key for those. The robust
    follow-up is nomenklatura ``LogicV2`` as a post-blocking re-scorer (its own ADR); it is
    a row-wise Python matcher that does not vectorise in DuckDB, so it is out of scope here.
    """
    if not entity.first("name", quiet=True):
        return None
    fingerprint = fingerprints.generate(entity.caption)
    if not fingerprint:
        return None
    stripped = fingerprints.remove_types(fingerprint)
    if not stripped:
        # ``remove_types`` removed everything (the name was only legal-form / generic words):
        # keep the richer legal-form-only key rather than collapsing to None, so the entity
        # still blocks and still contributes country/id/wikidata signals (ADR 0039 §3.2).
        return fingerprint or None
    # Generic-token guard (ADR 0039 §3.2 / Defect 1): if ``remove_types`` over-stripped the
    # name to a SINGLE generic descriptor (e.g. "trading"), that bare token must NOT serve as
    # the sole exact-name key — two distinct firms would collide on it. Fall back to the richer
    # ``generate`` key (legal forms still stripped, distinguishing tokens like intl/imp/exp
    # retained). Multi-token keys and single non-generic tokens are untouched.
    tokens = stripped.split()
    if len(tokens) == 1 and tokens[0] in _GENERIC_NAME_TOKENS:
        return fingerprint
    return stripped


def _flatten(entity: FtmEntity) -> dict[str, Any]:
    """Project an FtM entity onto the flat columns Splink compares.

    ``quiet=True`` so properties absent from a given schema (e.g. ``birthDate``
    on a Company) yield ``None`` rather than raising.
    """
    countries = entity.get_type_values(registry.country)
    return {
        "unique_id": entity.id,
        "name_fp": _name_fingerprint(entity),
        "country": countries[0] if countries else None,
        "birth_date": entity.first("birthDate", quiet=True),
        "wikidata_id": entity.first("wikidataId", quiet=True),
        "reg_id": _distinguishing_ids(entity),
    }


def _schema_compatible(left: FtmEntity, right: FtmEntity) -> bool:
    """Whether two entities *could* be the same node — i.e. their schemas can merge.

    Two FtM schemas merge only when one descends from the other (``Company`` + ``Organization``
    → ``Company``); siblings with no common schema (``Organization`` + ``Person``,
    ``Organization`` + ``Vessel``) cannot, and ``model.common_schema`` raises ``InvalidData``.
    The name fingerprint is name-only, so a company named after its owner
    (``"X Import-Export Company"`` and Person ``"X"``) collides on the key — gating candidate
    pairs on schema compatibility stops that **distinct-entity over-merge** and, downstream,
    the batch-aborting ``InvalidData`` that merging incompatible schemas would raise.
    """
    try:
        model.common_schema(left.schema, right.schema)
    except InvalidData:
        return False
    return True


def score_pairs(
    entities: Sequence[FtmEntity],
    *,
    prior: float = DEFAULT_PRIOR,
    predict_threshold: float = DEFAULT_PREDICT_THRESHOLD,
) -> list[ScoredPair]:
    """Block + score candidate duplicate pairs among ``entities`` (schema-incompatible
    pairs — e.g. an ``Organization`` and a ``Person`` sharing a name — are dropped: they
    are distinct nodes that cannot merge)."""
    if len(entities) < 2:
        return []
    frame = pd.DataFrame([_flatten(entity) for entity in entities])
    settings = SettingsCreator(
        link_type="dedupe_only",
        comparisons=[
            _name_comparison(),
            _exact_comparison("country", m=0.85, u=0.15),
            _exact_comparison("birth_date", m=0.9, u=0.02),
            _exact_comparison("wikidata_id", m=0.999, u=0.000005),
            _distinguishing_id_comparison(),
        ],
        blocking_rules_to_generate_predictions=[
            block_on("country"),
            block_on("substr(name_fp, 1, 4)"),
            block_on("wikidata_id"),
        ],
        probability_two_random_records_match=prior,
    )
    # Splink accepts a DataFrame at runtime; its type hint only admits table names.
    linker = Linker(frame, settings, db_api=DuckDBAPI())  # pyright: ignore[reportArgumentType]
    predictions = linker.inference.predict(threshold_match_probability=predict_threshold)
    result = predictions.as_pandas_dataframe()
    by_id = {entity.id: entity for entity in entities}
    pairs: list[ScoredPair] = []
    for record in result.to_dict("records"):
        left_id, right_id = str(record["unique_id_l"]), str(record["unique_id_r"])
        if not _schema_compatible(by_id[left_id], by_id[right_id]):
            continue  # distinct nodes (e.g. a company named after its owner) — never a match
        pairs.append(
            ScoredPair(
                left_id=left_id,
                right_id=right_id,
                probability=float(record["match_probability"]),
            )
        )
    return pairs
