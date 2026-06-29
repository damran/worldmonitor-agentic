"""Unit tests for ``resolution.silver`` — canonical-anchor SILVER labels (ADR 0079, ADR 0085).

Covers:
* Example pairs (positive, negative, abstain, contradiction, same-source) — deterministic
  fixtures that document the rules and serve as fast regression guards.
* Jurisdiction-scoped anchor tier (ADR 0085 Finding 1): ``registrationNumber`` requires
  jurisdiction/country corroboration for both positive and negative signals.
* Contradiction-order fix (ADR 0085 Finding 2): has_shared + has_conflict → DROP regardless
  of source; same-source contradictions must never be emitted as ``non_match``.
* Idempotent-persist — re-persisting the same silver pairs on the same ``er_gold_pair`` rows
  is a no-op (``ON CONFLICT DO NOTHING``).
* Human-row-precedence — an existing human/gold row on the same ``(left_id, right_id)`` is
  NOT overwritten by a silver persist (append-only + human > silver invariant).
* Score-reject — ``persist_silver_pairs`` rejects any pair carrying a non-None
  ``clerical_score`` or a wrong ``source`` (the N3 write-boundary guard).

The Postgres-persist path uses an in-memory SQLite database so the test is fully Docker-free.
The SQLAlchemy ORM setup mirrors the approach used in ``tests/unit/test_gold.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErGoldPair
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import Provenance, stamp
from worldmonitor.resolution.gold import GoldPair
from worldmonitor.resolution.silver import (
    ANCHOR_PROPERTIES,
    GLOBALLY_UNIQUE,
    JURISDICTION_SCOPED,
    SILVER_SOURCE,
    build_silver_pairs,
    persist_silver_pairs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prov(source_id: str, entity_id: str) -> Provenance:
    return Provenance(
        source_id=source_id,
        retrieved_at="2026-06-29T00:00:00Z",
        reliability="B",
        source_record=f"s3://landing/{entity_id}.json",
    )


def _entity(entity_id: str, source_id: str, **props: list[str]):
    """A provenance-stamped FtM Company entity with the given extra properties."""
    base: dict[str, list[str]] = {"name": ["Acme Corp"]}
    base.update(props)
    e = make_entity({"id": entity_id, "schema": "Company", "properties": base})
    return stamp(e, _prov(source_id, entity_id))


@pytest.fixture()
def session():
    """An in-memory SQLite session for the er_gold_pair table (no Docker required).

    Only ``er_gold_pair`` is created — the other tables use PostgreSQL-only JSONB columns
    that SQLite's DDL compiler cannot handle.  This is the same strategy the gold unit tests
    use: they explicitly note that the Postgres-persist path is covered by the integration suite.
    """
    from worldmonitor.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    # Create only the table we need; other tables (er_queue_item, task_run, …) use
    # PostgreSQL-only JSONB columns that SQLite rejects. The ``tables=`` kwarg tells
    # SQLAlchemy to only emit DDL for the listed table objects.
    Base.metadata.create_all(engine, tables=[ErGoldPair.__table__])  # type: ignore[arg-type]
    with Session(engine) as sess:
        yield sess


# ---------------------------------------------------------------------------
# ANCHOR_PROPERTIES — confirm the constant is wired correctly
# ---------------------------------------------------------------------------


def test_anchor_properties_includes_expected_ids() -> None:
    """The ANCHOR_PROPERTIES tuple must include all nine IDs declared in ADR 0079 §Decision 1."""
    expected = {
        "wikidataId",
        "leiCode",
        "registrationNumber",
        "ogrnCode",
        "innCode",
        "swiftBic",
        "isin",
        "okpoCode",
        "permId",
    }
    assert expected <= set(ANCHOR_PROPERTIES), (
        f"ANCHOR_PROPERTIES missing expected IDs: {expected - set(ANCHOR_PROPERTIES)}"
    )


def test_anchor_tier_constants_are_consistent() -> None:
    """ADR 0085: GLOBALLY_UNIQUE + JURISDICTION_SCOPED == ANCHOR_PROPERTIES (union = full set)."""
    assert set(GLOBALLY_UNIQUE) | set(JURISDICTION_SCOPED) == set(ANCHOR_PROPERTIES), (
        "ANCHOR_PROPERTIES must equal the union of GLOBALLY_UNIQUE and JURISDICTION_SCOPED"
    )
    # No overlap between tiers
    assert not (set(GLOBALLY_UNIQUE) & set(JURISDICTION_SCOPED)), (
        "An anchor must not appear in both GLOBALLY_UNIQUE and JURISDICTION_SCOPED"
    )
    # registrationNumber is jurisdiction-scoped (key ADR 0085 classification)
    assert "registrationNumber" in JURISDICTION_SCOPED
    assert "registrationNumber" not in GLOBALLY_UNIQUE
    # BIC/LEI/ISIN/QID are globally unique
    for gid in ("wikidataId", "leiCode", "isin", "swiftBic"):
        assert gid in GLOBALLY_UNIQUE, f"{gid!r} must be in GLOBALLY_UNIQUE"


# ---------------------------------------------------------------------------
# Example: POSITIVE — globally-unique anchors (shared, distinct sources)
# ---------------------------------------------------------------------------


def test_example_positive_shared_lei_distinct_sources() -> None:
    """Two companies from distinct sources sharing the same leiCode → label="match"."""
    a = _entity("e1", "src-OFAC", leiCode=["LEI-XYZ123"])
    b = _entity("e2", "src-OpenCorp", leiCode=["LEI-XYZ123"])
    pairs = build_silver_pairs([a, b])
    assert len(pairs) == 1
    assert pairs[0].label == "match"
    assert pairs[0].source == SILVER_SOURCE
    assert pairs[0].clerical_score is None
    assert pairs[0].left_id <= pairs[0].right_id


# ---------------------------------------------------------------------------
# Example: POSITIVE — jurisdiction-scoped (registrationNumber + jurisdiction)
# ---------------------------------------------------------------------------


def test_example_positive_shared_registration_number_with_jurisdiction() -> None:
    """Two companies from distinct sources sharing a registrationNumber AND the same jurisdiction
    → label="match" (ADR 0085: jurisdiction corroborates the shared value).
    """
    a = _entity("e1", "src-A", registrationNumber=["GB123456"], jurisdiction=["gb"])
    b = _entity("e2", "src-B", registrationNumber=["GB123456"], jurisdiction=["gb"])
    pairs = build_silver_pairs([a, b])
    assert any(p.label == "match" for p in pairs), (
        f"shared regNo + same jurisdiction + distinct sources must produce match; got {pairs}"
    )


def test_example_positive_shared_registration_number_absent_jurisdiction_abstains() -> None:
    """Two companies sharing a registrationNumber but with NO jurisdiction set → abstain.

    ADR 0085 Finding 1: a shared registrationNumber without jurisdiction corroboration is NOT
    a positive signal (the same number can legitimately exist in two different countries).
    """
    a = _entity("e1", "src-A", registrationNumber=["GB123456"])
    b = _entity("e2", "src-B", registrationNumber=["GB123456"])
    pairs = build_silver_pairs([a, b])
    assert not any(p.label == "match" for p in pairs), (
        f"shared regNo with absent jurisdiction must NOT produce match; got {pairs}"
    )


def test_example_positive_shared_registration_number_different_jurisdiction_abstains() -> None:
    """Two companies sharing a registrationNumber but with DIFFERENT jurisdictions → abstain.

    The same number string can appear in two different national registers; without a shared
    jurisdiction, the value is not a cross-source positive signal.
    """
    a = _entity("e1", "src-A", registrationNumber=["12345"], jurisdiction=["gb"])
    b = _entity("e2", "src-B", registrationNumber=["12345"], jurisdiction=["de"])
    pairs = build_silver_pairs([a, b])
    assert not any(p.label == "match" for p in pairs), (
        f"shared regNo with different jurisdictions must NOT produce match; got {pairs}"
    )


def test_example_positive_shared_registration_number_country_corroboration() -> None:
    """Jurisdiction corroboration via ``country`` (not just ``jurisdiction``) also suffices."""
    a = _entity("e1", "src-A", registrationNumber=["US-67890"], country=["us"])
    b = _entity("e2", "src-B", registrationNumber=["US-67890"], country=["us"])
    pairs = build_silver_pairs([a, b])
    assert any(p.label == "match" for p in pairs), (
        f"shared regNo + same country + distinct sources must produce match; got {pairs}"
    )


# ---------------------------------------------------------------------------
# Example: SAME SOURCE (no match even with shared anchor)
# ---------------------------------------------------------------------------


def test_example_same_source_no_match() -> None:
    """Two companies from the SAME source sharing a leiCode → no match (within-source excluded)."""
    a = _entity("e1", "src-OFAC", leiCode=["LEI-ABC"])
    b = _entity("e2", "src-OFAC", leiCode=["LEI-ABC"])
    pairs = build_silver_pairs([a, b])
    assert not any(p.label == "match" for p in pairs)


# ---------------------------------------------------------------------------
# Example: NEGATIVE (conflicting anchors)
# ---------------------------------------------------------------------------


def test_example_negative_conflicting_lei() -> None:
    """Two companies with distinct (non-empty) leiCode values → label="non_match"."""
    a = _entity("e1", "src-A", leiCode=["LEI-AAAA"])
    b = _entity("e2", "src-B", leiCode=["LEI-BBBB"])
    pairs = build_silver_pairs([a, b])
    assert len(pairs) == 1
    assert pairs[0].label == "non_match"
    assert pairs[0].source == SILVER_SOURCE
    assert pairs[0].clerical_score is None


def test_example_negative_conflicting_registration_number_same_jurisdiction() -> None:
    """Two companies with DIFFERENT registrationNumbers in the SAME jurisdiction → non_match.

    ADR 0085: when jurisdiction corroborates (same register), a conflicting regNo is a definitive
    negative — two real entities in the same register cannot share a registration number.
    """
    a = _entity("e1", "src-A", registrationNumber=["REG-111"], jurisdiction=["gb"])
    b = _entity("e2", "src-B", registrationNumber=["REG-222"], jurisdiction=["gb"])
    pairs = build_silver_pairs([a, b])
    assert any(p.label == "non_match" for p in pairs), (
        f"conflicting regNo + same jurisdiction must produce non_match; got {pairs}"
    )


def test_example_negative_conflicting_registration_number_absent_jurisdiction_abstains() -> None:
    """Two companies with DIFFERENT registrationNumbers but NO jurisdiction set → abstain.

    ADR 0085 Finding 1: without jurisdiction corroboration, a conflicting registrationNumber
    is NOT a negative signal — the numbers may belong to different registers entirely.
    """
    a = _entity("e1", "src-A", registrationNumber=["REG-111"])
    b = _entity("e2", "src-A", registrationNumber=["REG-222"])
    pairs = build_silver_pairs([a, b])
    assert not any(p.label == "non_match" for p in pairs), (
        f"conflicting regNo with absent jurisdiction must NOT produce non_match; got {pairs}"
    )


def test_example_negative_conflicting_registration_number_different_jurisdiction_abstains() -> None:
    """Conflicting registrationNumbers across DIFFERENT jurisdictions → abstain (not non_match)."""
    a = _entity("e1", "src-A", registrationNumber=["REG-111"], jurisdiction=["gb"])
    b = _entity("e2", "src-B", registrationNumber=["REG-222"], jurisdiction=["de"])
    pairs = build_silver_pairs([a, b])
    assert not any(p.label == "non_match" for p in pairs), (
        f"conflicting regNo with different jurisdictions must NOT produce non_match; got {pairs}"
    )


def test_example_negative_same_source_conflicting_globally_unique_anchor() -> None:
    """Conflict is source-INDEPENDENT for globally-unique anchors: same source, different leiCode
    → non_match.
    """
    a = _entity("e1", "src-A", leiCode=["LEI-AAAA"])
    b = _entity("e2", "src-A", leiCode=["LEI-BBBB"])
    pairs = build_silver_pairs([a, b])
    assert any(p.label == "non_match" for p in pairs), (
        f"conflicting globally-unique anchor same-source must produce non_match; got {pairs}"
    )


# ---------------------------------------------------------------------------
# Example: ABSTAIN (no anchor overlap, no conflict)
# ---------------------------------------------------------------------------


def test_example_abstain_no_anchors() -> None:
    """Entities with no anchor property set → no silver label (abstain)."""
    a = _entity("e1", "src-A")
    b = _entity("e2", "src-B")
    pairs = build_silver_pairs([a, b])
    assert pairs == []


def test_example_abstain_non_overlapping_anchor_types() -> None:
    """Entity A has leiCode; entity B has registrationNumber only → no shared type → abstain."""
    a = _entity("e1", "src-A", leiCode=["LEI-XXX"])
    b = _entity("e2", "src-B", registrationNumber=["REG-YYY"])
    pairs = build_silver_pairs([a, b])
    assert pairs == []


# ---------------------------------------------------------------------------
# Example: CONTRADICTION (positive on P AND negative on Q → dropped)
# ---------------------------------------------------------------------------


def test_example_contradiction_dropped_globally_unique() -> None:
    """Pair shares leiCode (globally-unique positive) AND has conflicting ogrnCode (globally-unique
    negative) → dropped as contradiction (ADR 0085 §Decision 3).

    Both anchor properties are globally-unique; the contradiction is definitive regardless of
    source or jurisdiction.
    """
    a = _entity("e1", "src-A", leiCode=["LEI-SAME"], ogrnCode=["OGRN-111"])
    b = _entity("e2", "src-B", leiCode=["LEI-SAME"], ogrnCode=["OGRN-222"])
    pairs = build_silver_pairs([a, b])
    assert pairs == [], f"contradiction pair must be dropped, got {pairs}"


def test_example_no_contradiction_regnum_absent_jurisdiction() -> None:
    """leiCode shared (globally-unique positive) + registrationNumber conflicting (jurisdiction-
    scoped, NO jurisdiction) → NOT a contradiction → emits ``"match"`` on leiCode alone.

    ADR 0085 Finding 1: a registrationNumber conflict without jurisdiction corroboration is not
    a negative signal.  The pair is therefore NOT contradictory and correctly emits 'match'.
    This test documents the **behavioural difference** from the pre-ADR 0085 code, which
    incorrectly treated registrationNumber as globally-unique and would have dropped this pair.
    """
    a = _entity("e1", "src-A", leiCode=["LEI-SAME"], registrationNumber=["REG-111"])
    b = _entity("e2", "src-B", leiCode=["LEI-SAME"], registrationNumber=["REG-222"])
    pairs = build_silver_pairs([a, b])
    assert len(pairs) == 1, f"expected 1 match pair (not dropped), got {pairs}"
    assert pairs[0].label == "match"


def test_example_contradiction_dropped_regnum_with_jurisdiction() -> None:
    """leiCode shared (globally-unique positive) + registrationNumber conflicting WITH same
    jurisdiction → contradiction → dropped.

    When jurisdiction corroborates, the registrationNumber conflict is a valid negative signal,
    so the pair becomes pos+neg → dropped.
    """
    a = _entity(
        "e1", "src-A", leiCode=["LEI-SAME"], registrationNumber=["REG-111"], jurisdiction=["gb"]
    )
    b = _entity(
        "e2", "src-B", leiCode=["LEI-SAME"], registrationNumber=["REG-222"], jurisdiction=["gb"]
    )
    pairs = build_silver_pairs([a, b])
    assert pairs == [], (
        "contradiction pair (leiCode shared + regNo conflict + same jurisdiction) "
        f"must be dropped, got {pairs}"
    )


# ---------------------------------------------------------------------------
# Finding 2 — same-source contradiction must be DROPPED (not non_match)
# ---------------------------------------------------------------------------


def test_finding2_same_source_contradiction_dropped() -> None:
    """ADR 0085 Finding 2 regression: same-source pair with a shared globally-unique anchor AND
    a conflicting globally-unique anchor must be DROPPED — NOT emitted as ``non_match``.

    **Old-code bug (pre-ADR 0085):** same-source pair → is_positive=False (no distinct-source
    match) + is_negative=True (conflict) → incorrectly emitted as ``non_match``.  The new
    classification evaluates ``has_shared`` independently of source, so
    ``has_shared=True AND has_conflict=True`` → DROP (contradiction) regardless of source.
    """
    # Same source, so old code would: no positive (same src) → is_negative=True → non_match.
    a = _entity("e1", "src-A", leiCode=["LEI-SAME"], ogrnCode=["OGRN-111"])
    b = _entity("e2", "src-A", leiCode=["LEI-SAME"], ogrnCode=["OGRN-222"])
    pairs = build_silver_pairs([a, b])
    assert pairs == [], f"same-source contradiction must be dropped, got {pairs}"
    assert not any(p.label == "non_match" for p in pairs), "must NOT emit non_match"
    assert not any(p.label == "match" for p in pairs), "must NOT emit match"


def test_finding2_same_source_clean_positive_abstains_not_non_match() -> None:
    """A same-source pair with a shared globally-unique anchor and NO conflict → ABSTAIN (not
    non_match and not match).

    This is the plain same-source rule (no contradiction); the distinct-source gate only
    downgrades a positive to abstain — it must NEVER produce non_match.
    """
    a = _entity("e1", "src-A", leiCode=["LEI-SAME"])
    b = _entity("e2", "src-A", leiCode=["LEI-SAME"])
    pairs = build_silver_pairs([a, b])
    assert not any(p.label == "non_match" for p in pairs), (
        "same-source clean positive must not produce non_match"
    )
    assert not any(p.label == "match" for p in pairs), (
        "same-source clean positive must not produce match"
    )


# ---------------------------------------------------------------------------
# Canonical ordering + no self-pair + de-dup
# ---------------------------------------------------------------------------


def test_canonical_ordering_and_no_self_pair() -> None:
    """Every emitted pair has left_id <= right_id and is not a self-pair."""
    entities = [
        _entity("z-entity", "src-A", leiCode=["LEI-SHARED"]),
        _entity("a-entity", "src-B", leiCode=["LEI-SHARED"]),
    ]
    pairs = build_silver_pairs(entities)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.left_id == "a-entity"
    assert pair.right_id == "z-entity"
    assert pair.left_id <= pair.right_id


def test_no_duplicate_pairs_in_output() -> None:
    """With three entities sharing the same anchor value + two from distinct sources, each unique
    (left_id, right_id) appears at most once."""
    a = _entity("e1", "src-A", leiCode=["LEI-SHARED"])
    b = _entity("e2", "src-B", leiCode=["LEI-SHARED"])
    c = _entity("e3", "src-A", leiCode=["LEI-SHARED"])  # same source as a
    pairs = build_silver_pairs([a, b, c])
    keys = [(p.left_id, p.right_id) for p in pairs]
    assert len(keys) == len(set(keys)), f"duplicate pairs in output: {keys}"


# ---------------------------------------------------------------------------
# Silver-pair invariants — source and clerical_score
# ---------------------------------------------------------------------------


def test_all_emitted_pairs_carry_silver_source_and_null_score() -> None:
    """All emitted pairs must have source=SILVER_SOURCE and clerical_score=None (N3).

    Uses a globally-unique anchor (leiCode) to guarantee pairs ARE actually emitted —
    registrationNumber without jurisdiction would abstain under ADR 0085 rules.
    """
    a = _entity("e1", "src-A", leiCode=["LEI-001"])
    b = _entity("e2", "src-B", leiCode=["LEI-001"])
    c = _entity("e3", "src-A", leiCode=["LEI-002"])
    pairs = build_silver_pairs([a, b, c])
    assert pairs, "expected at least one emitted pair (leiCode is globally-unique)"
    for pair in pairs:
        assert pair.source == SILVER_SOURCE, f"wrong source: {pair.source!r}"
        assert pair.clerical_score is None, f"non-None clerical_score: {pair.clerical_score}"


# ---------------------------------------------------------------------------
# Persist: idempotent (ON CONFLICT DO NOTHING)
# ---------------------------------------------------------------------------


def test_persist_silver_pairs_idempotent(session: Session) -> None:
    """Re-persisting the same silver pairs is a no-op — the unique constraint is honoured."""
    a = _entity("e1", "src-A", leiCode=["LEI-IDEM"])
    b = _entity("e2", "src-B", leiCode=["LEI-IDEM"])
    pairs = build_silver_pairs([a, b])
    assert pairs  # there should be at least one pair

    first = persist_silver_pairs(session, pairs)
    session.flush()
    second = persist_silver_pairs(session, pairs)
    session.flush()

    assert first == second == len(pairs)
    count = session.query(ErGoldPair).filter_by(source=SILVER_SOURCE).count()
    assert count == len(pairs), f"expected {len(pairs)} rows, found {count}"


# ---------------------------------------------------------------------------
# Persist: human-row-precedence (existing human/gold row is NOT overwritten)
# ---------------------------------------------------------------------------


def test_persist_silver_does_not_overwrite_human_row(session: Session) -> None:
    """An existing human-gold row on the same (left_id, right_id) survives a silver persist.

    The silver pair has the opposite label to the human gold row — if the conflict-do-nothing
    is correct, the human label wins.
    """
    import uuid

    # Write a human gold row first.
    human_row = ErGoldPair(
        id=str(uuid.uuid4()),
        left_id="e1",
        right_id="e2",
        label="non_match",  # human says: NOT a match
        source="os_pairs",
        clerical_score=None,
    )
    session.add(human_row)
    session.flush()

    # Silver pair says "match" for the SAME (left_id, right_id).
    silver_pair = GoldPair(
        left_id="e1",
        right_id="e2",
        label="match",
        source=SILVER_SOURCE,
        clerical_score=None,
    )
    persist_silver_pairs(session, [silver_pair])
    session.flush()

    # The row in the DB must still be the human one (non_match, os_pairs).
    row = session.query(ErGoldPair).filter_by(left_id="e1", right_id="e2").one()
    assert row.label == "non_match", f"human label was overwritten; got {row.label!r}"
    assert row.source == "os_pairs", f"human source was overwritten; got {row.source!r}"


# ---------------------------------------------------------------------------
# Persist: N3 write-boundary guard — reject pair with wrong source or a score
# ---------------------------------------------------------------------------


def test_persist_rejects_pair_with_wrong_source(session: Session) -> None:
    """persist_silver_pairs must raise ValueError if a pair carries source != SILVER_SOURCE."""
    bad_pair = GoldPair(
        left_id="e1",
        right_id="e2",
        label="match",
        source="uncertainty",  # wrong source
        clerical_score=None,
    )
    with pytest.raises(ValueError, match="source"):
        persist_silver_pairs(session, [bad_pair])


def test_persist_rejects_pair_with_non_null_clerical_score(session: Session) -> None:
    """persist_silver_pairs must raise ValueError if a pair carries a non-None clerical_score
    (N3 guard: silver labels must never carry a model score).
    """
    bad_pair = GoldPair(
        left_id="e1",
        right_id="e2",
        label="match",
        source=SILVER_SOURCE,
        clerical_score=0.87,  # non-None: forbidden
    )
    with pytest.raises(ValueError, match="clerical_score"):
        persist_silver_pairs(session, [bad_pair])


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_build_silver_pairs_empty_input() -> None:
    assert build_silver_pairs([]) == []


def test_build_silver_pairs_single_entity() -> None:
    a = _entity("e1", "src-A", leiCode=["LEI-SOLO"])
    assert build_silver_pairs([a]) == []
