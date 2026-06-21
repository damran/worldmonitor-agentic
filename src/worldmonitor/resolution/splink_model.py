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

import pandas as pd
from followthemoney import registry
from splink import DuckDBAPI, Linker, SettingsCreator, block_on

from worldmonitor.ontology.ftm import FtmEntity

# Splink is chatty on stdout/stderr; keep the pipeline quiet.
logging.getLogger("splink").setLevel(logging.ERROR)

# Default prior and the high-confidence merge threshold (conservative: a clear
# name match alone is not enough — it needs corroboration to clear this).
DEFAULT_PRIOR = 0.001
DEFAULT_PREDICT_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class ScoredPair:
    """A candidate duplicate pair with its match probability."""

    left_id: str
    right_id: str
    probability: float


def _name_comparison() -> dict[str, Any]:
    return {
        "output_column_name": "name",
        "comparison_levels": [
            {
                "sql_condition": '"name_l" IS NULL OR "name_r" IS NULL',
                "is_null_level": True,
                "label_for_charts": "null",
            },
            {
                "sql_condition": '"name_l" = "name_r"',
                "label_for_charts": "exact",
                "m_probability": 0.99,
                "u_probability": 0.0001,
            },
            {
                "sql_condition": 'jaro_winkler_similarity("name_l", "name_r") >= 0.92',
                "label_for_charts": "jw>=.92",
                "m_probability": 0.88,
                "u_probability": 0.003,
            },
            {
                "sql_condition": 'jaro_winkler_similarity("name_l", "name_r") >= 0.82',
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


def _flatten(entity: FtmEntity) -> dict[str, Any]:
    """Project an FtM entity onto the flat columns Splink compares.

    ``quiet=True`` so properties absent from a given schema (e.g. ``birthDate``
    on a Company) yield ``None`` rather than raising.
    """
    name = entity.first("name", quiet=True) or entity.caption
    countries = entity.get_type_values(registry.country)
    return {
        "unique_id": entity.id,
        "name": name.lower() if name else None,
        "country": countries[0] if countries else None,
        "birth_date": entity.first("birthDate", quiet=True),
        "wikidata_id": entity.first("wikidataId", quiet=True),
    }


def score_pairs(
    entities: Sequence[FtmEntity],
    *,
    prior: float = DEFAULT_PRIOR,
    predict_threshold: float = DEFAULT_PREDICT_THRESHOLD,
) -> list[ScoredPair]:
    """Block + score candidate duplicate pairs among ``entities``."""
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
        ],
        blocking_rules_to_generate_predictions=[
            block_on("country"),
            block_on("substr(name, 1, 4)"),
            block_on("wikidata_id"),
        ],
        probability_two_random_records_match=prior,
    )
    # Splink accepts a DataFrame at runtime; its type hint only admits table names.
    linker = Linker(frame, settings, db_api=DuckDBAPI())  # pyright: ignore[reportArgumentType]
    predictions = linker.inference.predict(threshold_match_probability=predict_threshold)
    result = predictions.as_pandas_dataframe()
    return [
        ScoredPair(
            left_id=str(record["unique_id_l"]),
            right_id=str(record["unique_id_r"]),
            probability=float(record["match_probability"]),
        )
        for record in result.to_dict("records")
    ]
