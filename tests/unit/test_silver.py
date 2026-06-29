"""Unit tests for ``resolution.silver`` — canonical-anchor SILVER labels (ADR 0079).

Covers:
* Example pairs (positive, negative, abstain, contradiction, same-source) — deterministic
  fixtures that document the rules and serve as fast regression guards.
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


# ---------------------------------------------------------------------------
# Example: POSITIVE (shared anchor, distinct sources)
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


def test_example_positive_shared_registration_number() -> None:
    """Two companies from distinct sources sharing a registrationNumber → match."""
    a = _entity("e1", "src-A", registrationNumber=["GB123456"])
    b = _entity("e2", "src-B", registrationNumber=["GB123456"])
    pairs = build_silver_pairs([a, b])
    assert any(p.label == "match" for p in pairs)


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


def test_example_negative_same_source_conflicting_anchor() -> None:
    """Conflict is source-INDEPENDENT: same source, different registrationNumber → non_match."""
    a = _entity("e1", "src-A", registrationNumber=["REG-111"])
    b = _entity("e2", "src-A", registrationNumber=["REG-222"])
    pairs = build_silver_pairs([a, b])
    assert any(p.label == "non_match" for p in pairs)


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


def test_example_contradiction_dropped() -> None:
    """Pair shares leiCode (positive) AND has conflicting registrationNumber (negative) →
    dropped.
    """
    a = _entity("e1", "src-A", leiCode=["LEI-SAME"], registrationNumber=["REG-111"])
    b = _entity("e2", "src-B", leiCode=["LEI-SAME"], registrationNumber=["REG-222"])
    pairs = build_silver_pairs([a, b])
    # The contradiction (pos on leiCode, neg on registrationNumber) drops the pair entirely.
    assert pairs == [], f"contradiction pair must be dropped, got {pairs}"


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
    """All emitted pairs must have source=SILVER_SOURCE and clerical_score=None (N3)."""
    a = _entity("e1", "src-A", registrationNumber=["REG-001"])
    b = _entity("e2", "src-B", registrationNumber=["REG-001"])
    c = _entity("e3", "src-A", registrationNumber=["REG-002"])
    pairs = build_silver_pairs([a, b, c])
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
