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

from worldmonitor.ontology.anchors import CANONICAL_ID_FIELDS
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


def _anchor_clash_comparison() -> dict[str, Any]:
    # Negative-evidence comparison over the CANONICAL anchors (Gate B-5 / ADR 0040, Finding 1),
    # symmetric in shape with B-3's ``_distinguishing_id_comparison``. Each ``CANONICAL_ID_FIELDS``
    # anchor is single-valued and authoritative, so two records holding DISTINCT values for the
    # SAME field are, by definition, two different real-world entities — the catastrophic merge the
    # guard exists to prevent. The CLASH level uses m << u (the same shape ADR 0039 locked for the
    # reg-id clash: m=0.0005, u=0.30, BF ~0.001667) so a single anchor clash drives the posterior
    # below the 0.92 merge boundary even against name-exact × country-exact (and against an
    # additional shared ``wikidataId`` property after the Part-1 relax). The rule iterates ALL
    # CANONICAL_ID_FIELDS, so it is NOT hard-coded to wikidata (INV-1b).
    #
    # The columns are the ``anchor_<field>`` projection of the ``wm_anchor_<field>`` CONTEXT
    # (``_anchor_value`` / ``_flatten``), NOT the FtM ``wikidataId`` property — they are independent
    # signals (Finding 1 vs Findings 2/3). Each column is a non-null VARCHAR with ``''`` for a
    # missing anchor (type stability + neutral null), so the level conditions key off ``= ''``.
    #
    # Level order is load-bearing: the null level (no field is present on BOTH sides) is evaluated
    # FIRST, so a single-sided / absent anchor can NEVER reach the clash branch (a missing anchor is
    # neutral, never penalising — mirrors B-3 INV-3b). The clash level then fires when ANY field has
    # both sides present and DISTINCT. The ELSE level (some field present-and-equal, none clashing)
    # is neutral (m == u, BF 1): a shared anchor CONTEXT is not double-counted here (the FtM
    # property's exact level already scores a shared wikidata).
    fields = CANONICAL_ID_FIELDS
    both_present_any = " OR ".join(
        f"(\"{_ANCHOR_COL_PREFIX}{f}_l\" <> '' AND \"{_ANCHOR_COL_PREFIX}{f}_r\" <> '')"
        for f in fields
    )
    clash_any = " OR ".join(
        f"(\"{_ANCHOR_COL_PREFIX}{f}_l\" <> '' AND \"{_ANCHOR_COL_PREFIX}{f}_r\" <> '' "
        f'AND "{_ANCHOR_COL_PREFIX}{f}_l" <> "{_ANCHOR_COL_PREFIX}{f}_r")'
        for f in fields
    )
    return {
        "output_column_name": "anchor_clash",
        "comparison_levels": [
            {
                # No canonical anchor field is present on BOTH sides -> nothing to compare.
                "sql_condition": f"NOT ({both_present_any})",
                "is_null_level": True,
                "label_for_charts": "null",
            },
            {
                # Some field is present on both sides with DISTINCT values -> distinct entities.
                "sql_condition": clash_any,
                "label_for_charts": "clash",
                "m_probability": 0.0005,
                "u_probability": 0.30,
            },
            {
                # Some field present-and-equal on both sides, none clashing -> neutral (BF 1). The
                # shared wikidataId PROPERTY is corroborated by its own exact level, not here.
                "sql_condition": "ELSE",
                "label_for_charts": "shared-or-neutral",
                "m_probability": 0.5,
                "u_probability": 0.5,
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


# Column-name prefix for the projected canonical-anchor CONTEXT (Gate B-5 / ADR 0040, Slice 1).
# These columns are projected from ``entity.context["wm_anchor_<field>"]`` (ADR 0018 / anchors.py),
# NOT from FtM properties — the anchor-clash level reads the context, distinct from the existing
# ``wikidata_id`` exact level which reads the ``wikidataId`` FtM *property* (Finding 1 vs 2/3).
_ANCHOR_COL_PREFIX = "anchor_"


def _anchor_value(entity: FtmEntity, field: str) -> str:
    """Project one canonical anchor from the entity context into a clash-comparable VARCHAR.

    Returns the entity's single value for ``field`` (the FIRST value if the context already holds
    a list), or the EMPTY STRING ``""`` when the field is absent. The empty-string sentinel — never
    ``None`` — keeps the projected column a stable ``VARCHAR`` even for an all-anchorless batch
    (an all-``None`` pandas column is inferred as ``INTEGER`` and breaks the comparison SQL — the
    same type-stability concern as ``_distinguishing_ids``). The anchor-clash comparison keys its
    null level off ``= ''`` (either side absent), so a missing anchor stays neutral.
    """
    raw = entity.context.get(f"wm_anchor_{field}")
    if isinstance(raw, list):
        for candidate in raw:
            if isinstance(candidate, str) and candidate:
                return candidate
        return ""
    if isinstance(raw, str):
        return raw
    return ""


def _flatten(entity: FtmEntity) -> dict[str, Any]:
    """Project an FtM entity onto the flat columns Splink compares.

    ``quiet=True`` so properties absent from a given schema (e.g. ``birthDate``
    on a Company) yield ``None`` rather than raising.
    """
    countries = entity.get_type_values(registry.country)
    row: dict[str, Any] = {
        "unique_id": entity.id,
        "name_fp": _name_fingerprint(entity),
        "country": countries[0] if countries else None,
        "birth_date": entity.first("birthDate", quiet=True),
        "wikidata_id": entity.first("wikidataId", quiet=True),
        "reg_id": _distinguishing_ids(entity),
    }
    # Project the canonical-anchor CONTEXT (one column per CANONICAL_ID_FIELDS field) for the
    # anchor-clash comparison (Gate B-5 / ADR 0040, Finding 1). This is the wm_anchor_* context,
    # NOT the wikidataId FtM property above.
    for field in CANONICAL_ID_FIELDS:
        row[f"{_ANCHOR_COL_PREFIX}{field}"] = _anchor_value(entity, field)
    return row


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


def _candidate_settings(prior: float) -> SettingsCreator:
    """The candidate model's settings — the SAME comparison + blocking surface ``score_pairs``
    uses (reusing the shared ``_name_comparison`` / ``_exact_comparison`` / ``_distinguishing_id_
    comparison`` / ``_anchor_clash_comparison`` builders and the same blocking rules). The EM
    candidate differs from the live path ONLY in how its m/u weights are derived (estimated, not
    expert-set) — its structure is identical so a measured candidate is comparable like-for-like.
    """
    return SettingsCreator(
        link_type="dedupe_only",
        comparisons=[
            _name_comparison(),
            _exact_comparison("country", m=0.85, u=0.15),
            _exact_comparison("birth_date", m=0.9, u=0.02),
            _exact_comparison("wikidata_id", m=0.999, u=0.02),
            _distinguishing_id_comparison(),
            _anchor_clash_comparison(),
        ],
        blocking_rules_to_generate_predictions=[
            block_on("country"),
            block_on("substr(name_fp, 1, 4)"),
            block_on("wikidata_id"),
        ],
        probability_two_random_records_match=prior,
    )


def train_candidate_model(
    entities: Sequence[FtmEntity],
    *,
    prior: float = DEFAULT_PRIOR,
    seed: int = 42,
    max_pairs: float = 1_000_000.0,
    em_blocking_rule: str = "country",
) -> dict[str, Any]:
    """Train a MEASURED CANDIDATE Splink model via EM and return a loadable settings artefact.

    This is the ADR-0043 / Gate-A candidate-model path. It is a **measured candidate ONLY** — it
    is evaluated against the gold set by :mod:`worldmonitor.resolution.eval`, and it is **NOT**
    promoted into the live :func:`score_pairs` path. The expert-set v0 weights and blocking of
    :func:`score_pairs` are FROZEN in slice-1 (gate spec §8); promoting the EM weights is the
    separate, human-gated slice-2.

    Training order is fixed by the Splink API (``VERIFIED_API.md``): because
    ``estimate_parameters_using_expectation_maximisation`` defaults ``fix_u_probabilities=True``
    (EM updates only ``m``), ``u`` MUST be estimated FIRST. So:

    1. ``linker.training.estimate_u_using_random_sampling(max_pairs=..., seed=...)`` — seeded for
       reproducible ``u`` (the gold harness requires determinism).
    2. ``linker.training.estimate_parameters_using_expectation_maximisation(block_on(...))`` — EM
       refines ``m`` over the pairs the EM blocking rule generates.

    Returns the trained settings as a JSON-serialisable ``dict`` (``save_model_to_json()``), which
    is a loadable artefact: a fresh ``Linker(frame, settings_dict, db_api=...)`` reconstitutes the
    candidate so :mod:`worldmonitor.resolution.eval` can score it against the gold set (A2).
    """
    frame = pd.DataFrame([_flatten(entity) for entity in entities])
    settings = _candidate_settings(prior)
    # Splink accepts a DataFrame at runtime; its type hint only admits table names.
    linker = Linker(frame, settings, db_api=DuckDBAPI())  # pyright: ignore[reportArgumentType]
    # u FIRST (fix_u_probabilities defaults True so EM updates only m) — VERIFIED_API.md.
    linker.training.estimate_u_using_random_sampling(max_pairs=max_pairs, seed=seed)
    linker.training.estimate_parameters_using_expectation_maximisation(block_on(em_blocking_rule))
    # The trained m/u live on the linker's settings; export a loadable/evaluable artefact.
    return linker.misc.save_model_to_json()


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
            # Gate B-5 / ADR 0040 Part 1 (Findings 2 + 3): a shared ``wikidataId`` FtM property is
            # corroboration, NOT an override. The pre-B-5 ``u=0.000005`` gave Bayes factor 199 800,
            # which alone cleared 0.92 against total name disagreement (H-5) and even swamped a
            # CLASHING B-3 distinguishing id (Judge MEDIUM). ``u=0.02`` (a ~1-in-50 QID
            # mis-enrichment / collision rate, orders of magnitude above the old 5e-6) drops the BF
            # to ~50 — a strong corroborator (comparable to a shared registrationNumber's BF 95)
            # that can no longer ALONE clear the threshold against an active name disagreement, and
            # is vetoed by a present-but-clashing distinguishing id. A shared anchor WITH name
            # corroboration still merges (INV-2b). See ADR 0040 Builder record for measured scores.
            _exact_comparison("wikidata_id", m=0.999, u=0.02),
            _distinguishing_id_comparison(),
            _anchor_clash_comparison(),
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
