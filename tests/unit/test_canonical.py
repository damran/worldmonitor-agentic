"""Unit tests for ``resolution/canonical.py`` (Gate B-front / ADR 0044 + Gate CID-fix / ADR 0048).

Supporting tests beyond the primary oracle (``tests/test_stable_id.py``): the durable-id
PRECEDENCE (QID > LEI > regNo > taxNo), the regNo/taxNo FtM-``identifier`` normalization (ADR
0039), the per-tier anchor-conflict fall-through (ADR 0040), the ``wm_anchor_*`` context source,
the ``mint`` / ``wmc-``-fallback shapes, and the read-only ``resolve_durable_id`` + promote-time
``record_durable_id`` derivation contract the pipeline runs. DB-free except the ledger cases, which
use an in-memory SQLite Session over just ``canonical_id_ledger`` (Docker-free).

Gate CID-fix / ADR 0048 — the durable id is now an FtM-clean, INJECTIVE entity reference
``wm-anchor-<kind>-<encoded-value>`` (the old ``f"{kind}:{value}"`` colon form cleaned to ``None``
as an edge endpoint -> edges silently dropped). Two NEW HARD INVARIANTS are pinned here:

  * FtM-CLEAN: for every anchor kind AND every adversarial value,
    ``registry.entity.clean(id) == id`` (incl. ``''`` / ``'.'`` / ``'-'`` / trailing punctuation /
    embedded ``/`` ``:`` space). DENY D-CLEAN.
  * INJECTIVITY (the person-safety property): two DISTINCT raw ``(kind, value)`` pairs NEVER map to
    one id -- guaranteed by disjoint per-kind prefixes, a verbatim clean branch, and a hashed branch
    that digests the ORIGINAL value (so a sanitisation-collision like ``HRB/12`` vs ``HRB-12`` still
    differs). DENY D-INJ. The guard CONSTRUCTS the traps (random text misses them).

The encoder is the pure helper ``canonical._anchor_id(kind, value) -> str`` (gate spec §3.2 /
``.claude/gate.scope``); it does not exist on the pre-fix tree, so the ``_anchor_id``-driven tests
are RED until the builder lands it. The colon-format equality assertions are RED because they now
pin the ``wm-anchor-`` target.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from followthemoney import registry
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.resolution import canonical

Q_A = "Q42"
Q_B = "Q777"
LEI_A = "5493001KJTIIGC8Y1R12"
LEI_B = "529900T8BM49AURSDO55"

# The four anchor kinds (ADR 0048 §3.1) and the SHA-256 hash-tail shape (``-<12 hex>``) that
# partitions the clean vs hashed id namespaces -- ``[0-9a-f]`` keeps the tail itself FtM-clean.
_KINDS = ("qid", "lei", "regno", "taxno")
_HASH_TAIL = re.compile(r"-[0-9a-f]{12}$")

# Adversarial value class (gate spec §7.2): empty, lone punctuation, trailing punctuation, embedded
# path/colon/space separators, a hash-tail twin, and a legible QID + 20-char LEI.
# ``registry.entity`` rejects a colon and a trailing ``'.'``/``'-'`` (verified on followthemoney
# 4.9.2) -- exactly why the OLD ``f"{kind}:{value}"`` serialization dropped edges at the endpoint.
_ADVERSARIAL = (
    "",
    ".",
    "-",
    "ABC.",
    "ABC-",
    "HRB/12-345",
    "HRB:12",
    "HRB 12 345",
    "a-0123456789ab",
    Q_A,
    LEI_A,
)
# The hostile regNo/taxNo subset that survives ``registry.identifier.clean`` non-empty, so it
# actually REACHES the encoder through the public ``pick_anchor`` path (``''`` is dropped by
# ``identifier.clean`` and so never produces an id via ``pick_anchor`` -- it is covered only by the
# direct ``_anchor_id`` matrix below).
_REACHABLE_HOSTILE = (
    ".",
    "-",
    "ABC.",
    "ABC-",
    "HRB/12-345",
    "HRB:12",
    "HRB 12 345",
    "a-0123456789ab",
    "GOV-9",
    "12345",
)


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
    assert canonical.pick_anchor(members) == f"wm-anchor-qid-{Q_A}"


def test_lei_wins_when_no_qid() -> None:
    members = [_company("a", lei=LEI_A, registration_number="R1", tax_number="T1")]
    assert canonical.pick_anchor(members) == f"wm-anchor-lei-{LEI_A}"


def test_regno_wins_when_no_qid_or_lei() -> None:
    members = [_company("a", registration_number="R1", tax_number="T1")]
    assert canonical.pick_anchor(members) == "wm-anchor-regno-R1"


def test_taxno_is_last_resort() -> None:
    members = [_company("a", tax_number="T1")]
    assert canonical.pick_anchor(members) == "wm-anchor-taxno-T1"


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
    assert canonical.pick_anchor(members) == "wm-anchor-regno-12345"


def test_same_id_as_regno_and_taxno_reconciles_to_regno_tier() -> None:
    """The QID/LEI tiers are empty; regNo and taxNo each carry the SAME normalized id on different
    records — the regNo tier (higher precedence) is a single clean value, so it wins."""
    members = [_company("a", registration_number="GOV-9"), _company("b", tax_number="GOV-9")]
    assert canonical.pick_anchor(members) == "wm-anchor-regno-GOV-9"


# --- anchor-conflict guard (ADR 0040) -------------------------------------------------------


def test_two_qids_fall_through_to_lei() -> None:
    members = [_company("a", wikidata_id=Q_A, lei=LEI_A), _company("b", wikidata_id=Q_B, lei=LEI_A)]
    anchor = canonical.pick_anchor(members)
    assert anchor == f"wm-anchor-lei-{LEI_A}"


def test_two_qids_and_two_leis_fall_through_to_regno() -> None:
    members = [
        _company("a", wikidata_id=Q_A, lei=LEI_A, registration_number="R1"),
        _company("b", wikidata_id=Q_B, lei=LEI_B, registration_number="R1"),
    ]
    assert canonical.pick_anchor(members) == "wm-anchor-regno-R1"


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
    assert anchor != f"wm-anchor-qid-{Q_A}"
    assert anchor != f"wm-anchor-qid-{Q_B}"
    assert anchor is None  # both QIDs, nothing to fall through to


# --- wm_anchor_* context source -------------------------------------------------------------


def test_reads_anchor_from_context() -> None:
    entity = _company("a")
    entity.context["wm_anchor_wikidata_id"] = [Q_A]
    assert canonical.pick_anchor([entity]) == f"wm-anchor-qid-{Q_A}"


def test_context_and_property_agree_is_not_a_conflict() -> None:
    entity = _company("a", wikidata_id=Q_A)
    entity.context["wm_anchor_wikidata_id"] = [Q_A]
    assert canonical.pick_anchor([entity]) == f"wm-anchor-qid-{Q_A}"


# --- validity ------------------------------------------------------------------------------


def test_invalid_qid_is_ignored() -> None:
    """A non-QID string in ``wikidataId`` fails ``is_qid`` and is not treated as a QID anchor."""
    members = [_company("a", wikidata_id="not-a-qid", lei=LEI_A)]
    assert canonical.pick_anchor(members) == f"wm-anchor-lei-{LEI_A}"


# --- shapes --------------------------------------------------------------------------------


def test_mint_shape() -> None:
    minted = canonical.mint()
    assert minted.startswith("wm-mint-")
    assert canonical.mint() != minted  # fresh uuid each call


# === Gate CID-fix / ADR 0048 — FtM-CLEAN durable id (DENY D-CLEAN) ===========================


def test_anchor_id_is_a_valid_ftm_reference_for_every_kind_and_value() -> None:
    """HARD INV (FtM-CLEAN): the pure encoder ``_anchor_id`` yields an FtM entity FIXED POINT for
    EVERY kind x EVERY adversarial value (so it never drops as a node id or edge endpoint).

    Drives the ``_anchor_id`` helper directly (gate spec §3.2): the helper does not validate, so
    it covers the full matrix -- including ``''`` / ``'.'`` / ``'-'`` which
    ``registry.identifier.clean`` strips before they could reach ``pick_anchor``. RED pre-fix (the
    helper does not exist yet).
    """
    for kind in _KINDS:
        for value in _ADVERSARIAL:
            durable = canonical._anchor_id(kind, value)
            assert durable.startswith(f"wm-anchor-{kind}-"), (
                f"{kind}/{value!r}: id must carry the kind-namespaced prefix, got {durable!r}"
            )
            assert registry.entity.clean(durable) == durable, (
                f"{kind}/{value!r} -> {durable!r} is NOT an FtM entity fixed point (would drop)"
            )


def test_anchor_canonical_id_is_a_valid_ftm_reference() -> None:
    """HARD INV (FtM-CLEAN), end-to-end via ``pick_anchor``: the durable id a real anchored member
    yields cleans through ``registry.entity`` UNCHANGED.

    Pre-fix ``pick_anchor`` returns the colon form (``qid:Q42`` / ``regno:HRB/12-345`` ...), and
    ``registry.entity.clean`` returns ``None`` on a colon -> the assertion FAILS (RED for the RIGHT
    reason: the shipped serialization is not an FtM reference). Post-fix every id is a fixed point.
    """
    members: list[FtmEntity] = [
        _company("q", wikidata_id=Q_A),  # QID accepts only an ``is_qid`` value
        _company("l", lei=LEI_A),  # LEI accepts only the 20-char shape
    ]
    for index, value in enumerate(_REACHABLE_HOSTILE):
        members.append(_company(f"r{index}", registration_number=value))
        members.append(_company(f"t{index}", tax_number=value))

    for member in members:
        durable = canonical.pick_anchor([member])
        assert durable is not None, "the member carries a usable anchor -> an id must be derived"
        assert durable.startswith("wm-anchor-"), f"unexpected serialization: {durable!r}"
        assert registry.entity.clean(durable) == durable, (
            f"{durable!r} is not an FtM entity reference -> it would drop as an edge endpoint"
        )


def test_anchor_id_cid5_trailing_punctuation_cleans_unchanged() -> None:
    """CID-5 regression: an already-``[A-Za-z0-9.-]`` value ending in ``'.'``/``'-'`` is the class
    A's first fix missed -- ``wm-anchor-regno-ABC.`` itself fails ``registry.entity.clean``, so the
    encoder MUST hash it. Both ids clean unchanged AND land in the hashed namespace (``-<12 hex>``).
    """
    for kind, value in (("regno", "ABC."), ("taxno", "ABC-")):
        durable = canonical._anchor_id(kind, value)
        assert registry.entity.clean(durable) == durable, (
            f"{kind}/{value!r} -> {durable!r} must be FtM-clean (CID-5)"
        )
        assert _HASH_TAIL.search(durable), (
            f"{kind}/{value!r}: a trailing-punctuation value MUST be hashed to stay FtM-clean"
        )


# === Gate CID-fix / ADR 0048 — INJECTIVITY (person-safety, DENY D-INJ) =======================


def test_anchor_id_injective_with_constructed_collision_traps() -> None:
    """HARD INV (INJECTIVITY): two DISTINCT raw values NEVER collapse to one id. Constructs the
    traps that random text misses (a non-injective sanitize is a silent cross-entity merge the
    catastrophic-merge guard never sees). RED pre-fix (the helper does not exist yet).
    """
    # Sanitisation-collision pair: distinct raw values that strip-to-hyphen to the SAME ``<safe>``
    # (``re.sub(r'[^A-Za-z0-9.-]','-', x)`` maps both ``HRB/12`` and ``HRB-12`` -> ``HRB-12``). A
    # naive sanitize WITHOUT the original-value digest would collapse them -- the CRIT-1 hazard.
    assert canonical._anchor_id("regno", "HRB/12") != canonical._anchor_id("regno", "HRB-12")

    # Hash-tail twin: a clean value whose VERBATIM id would already end in ``-<12 hex>`` is forced
    # into the hashed namespace (clause c) so it can never alias a hostile value's hashed id.
    clean_twin = canonical._anchor_id("regno", "a-0123456789ab")
    collision_twin = canonical._anchor_id("regno", "a/0123456789ab")
    assert clean_twin != collision_twin
    assert _HASH_TAIL.search(clean_twin), (
        "clause (c) must force the hash-tail twin into the hash NS"
    )

    # Cross-kind disjointness: the SAME value under a different kind yields a different id.
    assert canonical._anchor_id("regno", "12345") != canonical._anchor_id("taxno", "12345")

    # A broad set of DISTINCT raw values maps to a set of DISTINCT ids (no silent collapse).
    values = (
        "",
        ".",
        "-",
        "HRB/12",
        "HRB-12",
        "HRB/12-345",
        "HRB-12-345",
        "a-0123456789ab",
        "a/0123456789ab",
        "GOV-9",
        "12345",
    )
    ids = [canonical._anchor_id("regno", value) for value in values]
    assert len(set(ids)) == len(values), f"non-injective encoder: {ids}"


def test_pick_anchor_injective_over_distinct_regno_values() -> None:
    """HARD INV (INJECTIVITY) through the PUBLIC path: distinct regNo values (incl. the
    sanitisation-collision and hash-tail pairs) yield DISTINCT durable ids via ``pick_anchor``.

    Pre-fix the colon form is already injective (it embeds the raw value), so this is GREEN -- it
    is the post-fix guard that a NAIVE strip-to-hyphen encoder (which WOULD collapse ``HRB/12`` and
    ``HRB-12``) is rejected end-to-end (DENY D-INJ).
    """
    values = (
        "HRB/12",
        "HRB-12",
        "HRB/12-345",
        "HRB-12-345",
        "a-0123456789ab",
        "a/0123456789ab",
        "GOV-9",
        "12345",
    )
    ids = [
        canonical.pick_anchor([_company(f"m{i}", registration_number=v)])
        for i, v in enumerate(values)
    ]
    assert None not in ids, "every member carries a usable regNo anchor"
    assert len(set(ids)) == len(values), (
        f"distinct regNo values must yield DISTINCT durable ids (no cross-entity collapse): {ids}"
    )


# --- derivation contract (resolve_durable_id / record_durable_id) --------------------------


def test_resolve_durable_id_prefers_anchor(ledger_session: Session) -> None:
    members = [_company("m1", wikidata_id=Q_A), _company("m2", wikidata_id=Q_A)]
    durable = canonical.resolve_durable_id(ledger_session, members, fallback_id="wmc-deadbeef")
    assert durable == f"wm-anchor-qid-{Q_A}"
    assert not durable.startswith("wmc-")


def test_resolve_durable_id_falls_back_to_wmc_when_unanchored(ledger_session: Session) -> None:
    """A merge with NO usable anchor keeps the ``wmc-`` idempotency fingerprint as its durable id —
    the ONLY path ``wmc-`` is the durable id (DENY D1: never DERIVED from a hash, reused as-is)."""
    members = [_company("m1"), _company("m2")]
    durable = canonical.resolve_durable_id(ledger_session, members, fallback_id="wmc-deadbeef")
    assert durable == "wmc-deadbeef"


def test_resolve_durable_id_adopts_existing(ledger_session: Session) -> None:
    """RE-INGEST STABILITY (HARD INV): a re-formed anchored cluster (fresh member ids) ADOPTS the
    same durable id -- no churn, no second canonical."""
    members = [_company("m1", wikidata_id=Q_A)]
    first = canonical.resolve_durable_id(ledger_session, members, fallback_id="wmc-x")
    canonical.record_durable_id(ledger_session, first, member_ids=("m1",))
    # Re-ingest with fresh member id adopts the recorded durable id.
    again = canonical.resolve_durable_id(
        ledger_session, [_company("m9", wikidata_id=Q_A)], fallback_id="wmc-y"
    )
    assert again == first == f"wm-anchor-qid-{Q_A}"


def test_record_durable_id_records_self_and_member_aliases(ledger_session: Session) -> None:
    durable = f"wm-anchor-qid-{Q_A}"
    canonical.record_durable_id(
        ledger_session, durable, member_ids=("m1", "m2"), prior_id="wmc-prior"
    )
    assert canonical.resolve_durable(ledger_session, durable) == durable
    assert canonical.resolve_durable(ledger_session, "m1") == durable
    assert canonical.resolve_durable(ledger_session, "m2") == durable
    # The prior wmc- fingerprint resolves to the surviving durable id (alias-on-read).
    assert canonical.resolve_durable(ledger_session, "wmc-prior") == durable


def test_record_durable_id_parses_anchor_kind_from_wm_anchor_prefix(
    ledger_session: Session,
) -> None:
    """The ledger parse follows the format change (gate spec §3.5): the anchor kind is read from the
    ``wm-anchor-<kind>-`` prefix (the colon discriminator is gone). RED pre-fix -- the shipped
    ``durable_id.partition(":")`` parse sees no colon and records an EMPTY kind for the new id.
    """
    durable = f"wm-anchor-qid-{Q_A}"
    canonical.record_durable_id(ledger_session, durable, member_ids=("m1",))
    row = (
        ledger_session.query(canonical.CanonicalIdLedger)
        .filter_by(canonical_id=durable, canonical_alias=durable)
        .one()
    )
    assert row.anchor_kind == "qid"
    assert row.anchor_value == Q_A


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
    durable = f"wm-anchor-qid-{Q_A}"
    for _ in range(2):
        canonical.record_durable_id(ledger_session, durable, member_ids=("m1", "m2"))
    rows = ledger_session.query(canonical.CanonicalIdLedger).all()
    pairs = {(r.canonical_id, r.canonical_alias) for r in rows}
    assert pairs == {(durable, durable), (durable, "m1"), (durable, "m2")}
    assert len(rows) == 3  # no duplicates


def test_singleton_member_equal_to_durable_records_only_self(ledger_session: Session) -> None:
    """A singleton keyed under its own id records only the self-row (member == durable)."""
    canonical.record_durable_id(ledger_session, "solo", member_ids=("solo",))
    rows = ledger_session.query(canonical.CanonicalIdLedger).all()
    assert {(r.canonical_id, r.canonical_alias) for r in rows} == {("solo", "solo")}
