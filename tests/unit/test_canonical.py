"""Unit tests for ``resolution/canonical.py`` (Gate B-front / ADR 0044).

Supporting tests beyond the primary oracle (``tests/test_stable_id.py``): the durable-id
PRECEDENCE (QID > LEI > regNo > taxNo), the regNo/taxNo FtM-``identifier`` normalization (ADR
0039), the per-tier anchor-conflict fall-through (ADR 0040), the ``wm_anchor_*`` context source,
the ``mint`` / ``wmc-``-fallback shapes, and the read-only ``resolve_durable_id`` + promote-time
``record_durable_id`` derivation contract the pipeline runs. DB-free except the ledger cases, which
use an in-memory SQLite Session over just ``canonical_id_ledger`` (Docker-free).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution import canonical

Q_A = "Q42"
Q_B = "Q777"
LEI_A = "5493001KJTIIGC8Y1R12"
LEI_B = "529900T8BM49AURSDO55"


def _company(
    entity_id: str,
    *,
    wikidata_id: str | None = None,
    lei: str | None = None,
    registration_number: str | None = None,
    tax_number: str | None = None,
) -> FtmEntity:
    props: dict[str, list[str]] = {"name": ["Acme Corporation Ltd"], "jurisdiction": ["us"]}
    if wikidata_id is not None:
        props["wikidataId"] = [wikidata_id]
    if lei is not None:
        props["leiCode"] = [lei]
    if registration_number is not None:
        props["registrationNumber"] = [registration_number]
    if tax_number is not None:
        props["taxNumber"] = [tax_number]
    return make_entity(
        {"id": entity_id, "schema": "Company", "properties": props, "datasets": ["t"]}
    )


@pytest.fixture
def ledger_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    canonical.CanonicalIdLedger.__table__.create(engine)
    with Session(engine) as session:
        yield session


# --- precedence -----------------------------------------------------------------------------


def test_qid_wins_over_lei_regno_taxno() -> None:
    members = [_company("a", wikidata_id=Q_A, lei=LEI_A, registration_number="R1", tax_number="T1")]
    assert canonical.pick_anchor(members) == f"qid:{Q_A}"


def test_lei_wins_when_no_qid() -> None:
    members = [_company("a", lei=LEI_A, registration_number="R1", tax_number="T1")]
    assert canonical.pick_anchor(members) == f"lei:{LEI_A}"


def test_regno_wins_when_no_qid_or_lei() -> None:
    members = [_company("a", registration_number="R1", tax_number="T1")]
    assert canonical.pick_anchor(members) == "regno:R1"


def test_taxno_is_last_resort() -> None:
    members = [_company("a", tax_number="T1")]
    assert canonical.pick_anchor(members) == "taxno:T1"


def test_no_anchor_returns_none() -> None:
    assert canonical.pick_anchor([_company("a")]) is None


# --- normalization (ADR 0039) ---------------------------------------------------------------


def test_regno_normalized_via_ftm_identifier_type() -> None:
    """Whitespace/format differences in a regNo are cleaned via the FtM ``identifier`` type, so two
    members carrying the SAME id with trivial differences do NOT count as a conflict."""
    members = [
        _company("a", registration_number=" 12345 "),
        _company("b", registration_number="12345"),
    ]
    assert canonical.pick_anchor(members) == "regno:12345"


def test_same_id_as_regno_and_taxno_reconciles_to_regno_tier() -> None:
    """The QID/LEI tiers are empty; regNo and taxNo each carry the SAME normalized id on different
    records — the regNo tier (higher precedence) is a single clean value, so it wins."""
    members = [_company("a", registration_number="GOV-9"), _company("b", tax_number="GOV-9")]
    assert canonical.pick_anchor(members) == "regno:GOV-9"


# --- anchor-conflict guard (ADR 0040) -------------------------------------------------------


def test_two_qids_fall_through_to_lei() -> None:
    members = [_company("a", wikidata_id=Q_A, lei=LEI_A), _company("b", wikidata_id=Q_B, lei=LEI_A)]
    anchor = canonical.pick_anchor(members)
    assert anchor == f"lei:{LEI_A}"


def test_two_qids_and_two_leis_fall_through_to_regno() -> None:
    members = [
        _company("a", wikidata_id=Q_A, lei=LEI_A, registration_number="R1"),
        _company("b", wikidata_id=Q_B, lei=LEI_B, registration_number="R1"),
    ]
    assert canonical.pick_anchor(members) == "regno:R1"


def test_all_tiers_conflicting_returns_none() -> None:
    members = [
        _company("a", wikidata_id=Q_A, lei=LEI_A, registration_number="R1", tax_number="T1"),
        _company("b", wikidata_id=Q_B, lei=LEI_B, registration_number="R2", tax_number="T2"),
    ]
    assert canonical.pick_anchor(members) is None


def test_conflict_never_picks_index_zero() -> None:
    """The catastrophic-merge back-door D5: a conflicting QID tier must never yield EITHER QID."""
    members = [_company("a", wikidata_id=Q_A), _company("b", wikidata_id=Q_B)]
    anchor = canonical.pick_anchor(members)
    assert anchor != f"qid:{Q_A}"
    assert anchor != f"qid:{Q_B}"
    assert anchor is None  # both QIDs, nothing to fall through to


# --- wm_anchor_* context source -------------------------------------------------------------


def test_reads_anchor_from_context() -> None:
    entity = _company("a")
    entity.context["wm_anchor_wikidata_id"] = [Q_A]
    assert canonical.pick_anchor([entity]) == f"qid:{Q_A}"


def test_context_and_property_agree_is_not_a_conflict() -> None:
    entity = _company("a", wikidata_id=Q_A)
    entity.context["wm_anchor_wikidata_id"] = [Q_A]
    assert canonical.pick_anchor([entity]) == f"qid:{Q_A}"


# --- validity ------------------------------------------------------------------------------


def test_invalid_qid_is_ignored() -> None:
    """A non-QID string in ``wikidataId`` fails ``is_qid`` and is not treated as a QID anchor."""
    members = [_company("a", wikidata_id="not-a-qid", lei=LEI_A)]
    assert canonical.pick_anchor(members) == f"lei:{LEI_A}"


# --- shapes --------------------------------------------------------------------------------


def test_mint_shape() -> None:
    minted = canonical.mint()
    assert minted.startswith("wm-mint-")
    assert canonical.mint() != minted  # fresh uuid each call


# --- derivation contract (resolve_durable_id / record_durable_id) --------------------------


def test_resolve_durable_id_prefers_anchor(ledger_session: Session) -> None:
    members = [_company("m1", wikidata_id=Q_A), _company("m2", wikidata_id=Q_A)]
    durable = canonical.resolve_durable_id(ledger_session, members, fallback_id="wmc-deadbeef")
    assert durable == f"qid:{Q_A}"
    assert not durable.startswith("wmc-")


def test_resolve_durable_id_falls_back_to_wmc_when_unanchored(ledger_session: Session) -> None:
    """A merge with NO usable anchor keeps the ``wmc-`` idempotency fingerprint as its durable id —
    the ONLY path ``wmc-`` is the durable id (DENY D1: never DERIVED from a hash, reused as-is)."""
    members = [_company("m1"), _company("m2")]
    durable = canonical.resolve_durable_id(ledger_session, members, fallback_id="wmc-deadbeef")
    assert durable == "wmc-deadbeef"


def test_resolve_durable_id_adopts_existing(ledger_session: Session) -> None:
    members = [_company("m1", wikidata_id=Q_A)]
    first = canonical.resolve_durable_id(ledger_session, members, fallback_id="wmc-x")
    canonical.record_durable_id(ledger_session, first, member_ids=("m1",))
    # Re-ingest with fresh member id adopts the recorded durable id.
    again = canonical.resolve_durable_id(
        ledger_session, [_company("m9", wikidata_id=Q_A)], fallback_id="wmc-y"
    )
    assert again == first == f"qid:{Q_A}"


def test_record_durable_id_records_self_and_member_aliases(ledger_session: Session) -> None:
    canonical.record_durable_id(
        ledger_session, f"qid:{Q_A}", member_ids=("m1", "m2"), prior_id="wmc-prior"
    )
    assert canonical.resolve_durable(ledger_session, f"qid:{Q_A}") == f"qid:{Q_A}"
    assert canonical.resolve_durable(ledger_session, "m1") == f"qid:{Q_A}"
    assert canonical.resolve_durable(ledger_session, "m2") == f"qid:{Q_A}"
    # The prior wmc- fingerprint resolves to the surviving durable id (alias-on-read).
    assert canonical.resolve_durable(ledger_session, "wmc-prior") == f"qid:{Q_A}"


def test_record_durable_id_for_mint_records_mint_kind(ledger_session: Session) -> None:
    minted = canonical.mint()
    canonical.record_durable_id(ledger_session, minted, member_ids=(minted,))
    row = (
        ledger_session.query(canonical.CanonicalIdLedger)
        .filter_by(canonical_id=minted, canonical_alias=minted)
        .one()
    )
    assert row.anchor_kind == "mint"


def test_record_durable_id_is_idempotent(ledger_session: Session) -> None:
    for _ in range(2):
        canonical.record_durable_id(ledger_session, f"qid:{Q_A}", member_ids=("m1", "m2"))
    rows = ledger_session.query(canonical.CanonicalIdLedger).all()
    pairs = {(r.canonical_id, r.canonical_alias) for r in rows}
    assert pairs == {(f"qid:{Q_A}", f"qid:{Q_A}"), (f"qid:{Q_A}", "m1"), (f"qid:{Q_A}", "m2")}
    assert len(rows) == 3  # no duplicates


def test_singleton_member_equal_to_durable_records_only_self(ledger_session: Session) -> None:
    """A singleton keyed under its own id records only the self-row (member == durable)."""
    canonical.record_durable_id(ledger_session, "solo", member_ids=("solo",))
    rows = ledger_session.query(canonical.CanonicalIdLedger).all()
    assert {(r.canonical_id, r.canonical_alias) for r in rows} == {("solo", "solo")}
