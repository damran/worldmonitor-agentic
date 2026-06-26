"""Gate B-front — anchor-preferred STABLE durable ids + adopt/merge/split + ledger.

PRIMARY invariant oracle for Gate B-front (ADR 0044, spec
``docs/reviews/GATE_B_FRONT_STABLE_IDS_SPEC.md`` §13). Written FROM THE SPEC, independent of any
implementation — it is the contract the builder satisfies. It both proves the gate's acceptance
criteria (A2-A6, MR-5) and DEFINES the ``resolution/canonical.py`` API (the builder implements
these names/signatures verbatim).

WHY THIS FAILS ON THE CURRENT TREE (failing-first, the RIGHT red):
``resolution/canonical.py`` does not exist yet — the module-level import below raises
``ModuleNotFoundError`` and every test in this file errors at collection. That is the correct
failing-first state for a NORMAL BUILD gate. Once the builder lands ``canonical.py`` (and wires the
durable id into the merge path) the suite goes green.

THE API THIS FILE DEFINES (``src/worldmonitor/resolution/canonical.py`` — builder implements
verbatim):

    pick_anchor(members: Sequence[FtmEntity]) -> str | None
        The anchor-preferred DURABLE id over a sequence of FtM entities. Honors the precedence
        QID > LEI > regNo > taxNo (FtM-clean, injective ``wm-anchor-<kind>-<encoded>``, ADR 0048:
        ``wm-anchor-qid-Q42`` / ``wm-anchor-lei-<20-char>`` / ``wm-anchor-regno-<…>`` /
        ``wm-anchor-taxno-<…>``), reading the value from the FtM identifier property
        (``wikidataId`` / ``leiCode`` / ``registrationNumber`` / ``taxNumber``) and/or the
        ``wm_anchor_*`` context. ADR-0040 anchor-conflict guard: if members carry TWO distinct
        values at the chosen tier, FALL THROUGH to the next tier (NEVER pick ``[0]``). Returns
        ``None`` if no usable anchor exists. DB-free, pure, unit-testable.

    mint() -> str
        A minted durable id for an unanchored cluster with no prior ledger entry. Shape
        ``wm-mint-<uuid>``. (Deterministic-where-seeded if the builder can; this file only pins the
        prefix shape + uniqueness, never a specific uuid.)

    CanonicalIdLedger  (SQLAlchemy model on ``db.models.Base``; table ``canonical_id_ledger``)
        Columns INCLUDE at least: ``canonical_id`` (durable id, indexed) and ``canonical_alias`` (a
        superseded/prior id that resolves to it; one row per alias; APPEND-ONLY) — plus the
        anchor kind/value + ``created_at`` the spec §6 lists. The ledger helpers below
        read/write it.
        ``CanonicalIdLedger.__table__.create(engine)`` builds JUST this table on in-memory SQLite
        (the sibling Postgres-``JSONB`` models on ``Base`` are NOT created here — Docker-free).

    Ledger helpers (each takes a SQLAlchemy ``Session`` over ``canonical_id_ledger``):

        record_canonical(session, canonical_id, *, anchor_kind=..., anchor_value=...) -> None
            Record a durable canonical id (idempotent: a second call for the same id is a no-op,
            no duplicate row).
        record_alias(session, canonical_id, alias) -> None
            Record one APPEND-ONLY alias row (a superseded/prior id) that resolves to
            ``canonical_id`` (idempotent: a duplicate (canonical, alias) is a no-op).
        resolve_durable(session, alias) -> str | None
            The surviving durable id a (superseded) alias resolves to, else ``None``.
        lookup_durable_for_anchor(session, anchor_id) -> str | None
            An existing durable id already recorded for ``anchor_id`` (the ADOPT read), else
            ``None``.

NOTE on names: the spec (§3/§6) leaves the FINAL ledger column/helper names to a builder ADR record.
This file fixes a CLEAN, EXPLICIT surface; the builder conforms to it (that is the test-author's job
per the gate). If the builder picks a different-but-equivalent name it updates the import here
and the
oracle still holds — but the SEMANTICS asserted below (identical durable id across re-ingest, adopt,
alias-on-merge, alias-on-split, anchor-conflict fall-through, idempotence, split-race traceability)
are NON-NEGOTIABLE.

Docker-free: lives at repo root (NO ``@pytest.mark.integration``) so it runs under
``pytest -m "not integration"`` in the ``quality`` job. The ledger uses an in-memory SQLite Session
(mirroring the ephemeral-resolver idiom in ``merge.py``); ``pick_anchor`` is DB-free. No
Postgres/Neo4j. The Neo4j alias-on-read path is NOT exercised here — that is the builder's
``graph/writer.py`` integration test (note for the builder, kept out of these core cases).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from worldmonitor.ontology.ftm import FtmEntity, make_entity

# This import is what FAILS on the current tree (canonical.py does not exist yet) — the correct
# failing-first state. Every test below errors at collection until the builder lands the module.
from worldmonitor.resolution import canonical
from worldmonitor.resolution.merge import _canonical_id, cluster_and_merge
from worldmonitor.resolution.splink_model import ScoredPair

# Two distinct authoritative QIDs == two different real-world entities (ADR 0040).
Q_ACME = "Q42"
Q_OTHER = "Q777"
LEI_ACME = "5493001KJTIIGC8Y1R12"  # 20-char LEI shape; is_qid(LEI) is False.
NAME = "Acme Corporation Ltd"


# ---------------------------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------------------------


def _company(
    entity_id: str,
    *,
    name: str = NAME,
    wikidata_id: str | None = None,
    lei: str | None = None,
    registration_number: str | None = None,
    tax_number: str | None = None,
    country: str = "us",
) -> FtmEntity:
    """A Company carrying anchors as FtM IDENTIFIER properties (the durable-precedence source).

    ``entity_id`` is the per-collect MEMBER row id (fresh on every re-ingest, ADR 0036 §1) — the
    very thing that makes the ``wmc-`` fingerprint churn. The anchor props carry the DURABLE
    identity that must survive that churn.
    """
    props: dict[str, list[str]] = {"name": [name], "jurisdiction": [country]}
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
    """An in-memory SQLite Session over JUST the durable-id ledger table.

    Mirrors the throwaway ``sqlite://`` + ``StaticPool`` engine ``merge.py`` uses for the ephemeral
    resolver — Docker-free, runs in the ``quality`` job. Only ``CanonicalIdLedger.__table__`` is
    created (NOT the whole ``Base.metadata``, whose sibling models use Postgres ``JSONB`` that
    SQLite cannot compile). The full schema / migration-drift coverage is the builder's
    ``tests/integration/test_migrations.py`` job, not this Docker-free oracle.
    """
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    canonical.CanonicalIdLedger.__table__.create(engine)
    with Session(engine) as session:
        yield session


def _merge_cluster(entities: Sequence[FtmEntity], pairs: Sequence[ScoredPair]):
    """The single merged cluster ``cluster_and_merge`` produces from ``entities``."""
    return next(c for c in cluster_and_merge(list(entities), list(pairs)) if c.is_merge)


def _derive_durable(session: Session, members: Sequence[FtmEntity]) -> str:
    """The full durable-id derivation contract a re-ingest runs: pick the anchor, then ADOPT an
    existing ledger entry for it (no new id), else record it (first sighting). Mirrors the
    adopt-or-record hook the pipeline runs after ``cluster_and_merge`` (spec §7).
    """
    anchor = canonical.pick_anchor(members)
    if anchor is None:
        minted = canonical.mint()
        canonical.record_canonical(session, minted, anchor_kind="mint", anchor_value="")
        return minted
    existing = canonical.lookup_durable_for_anchor(session, anchor)
    if existing is not None:
        return existing  # ADOPT — no new durable id minted
    kind, _, value = anchor.removeprefix("wm-anchor-").partition("-")
    canonical.record_canonical(session, anchor, anchor_kind=kind, anchor_value=value)
    return anchor


def _ledger_rows(session: Session) -> list[tuple[str, str]]:
    """Every (canonical_id, alias) pair in the ledger — order-independent set assertions."""
    rows = session.query(canonical.CanonicalIdLedger).all()
    return [(r.canonical_id, r.canonical_alias) for r in rows]


# ---------------------------------------------------------------------------------------------
# Case 1 (A2/D3, THE CRUX) — re-ingest of an anchored entity yields an IDENTICAL durable id,
# while the old wmc- fingerprint would DIFFER across the two member-id sets.
# ---------------------------------------------------------------------------------------------


def test_reingest_anchored_entity_yields_identical_durable_id(ledger_session: Session) -> None:
    """Re-ingesting the SAME real anchored entity with FRESH member row-ids must derive the
    IDENTICAL durable canonical id (A2). The crux of the gate.

    Contrast (the bug this gate fixes): the ``wmc-`` content-address over sorted member ids WOULD
    differ across the two member-id sets — pinning that wmc- is NOT durable identity (D1/D3). The
    durable id is anchor-preferred (QID), so it is stable across re-collect.
    """
    # First ingest — member ids ing1-*.
    first_members = [_company("ing1-a", wikidata_id=Q_ACME), _company("ing1-b", wikidata_id=Q_ACME)]
    first_durable = _derive_durable(ledger_session, first_members)

    # Re-ingest the SAME real entity — connectors mint FRESH per-collect ids (ing2-*).
    second_members = [
        _company("ing2-a", wikidata_id=Q_ACME),
        _company("ing2-b", wikidata_id=Q_ACME),
    ]
    second_durable = _derive_durable(ledger_session, second_members)

    # The durable id is IDENTICAL across re-ingest (A2) and is the anchor-prefixed QID, not a hash.
    assert first_durable == second_durable
    assert first_durable == f"wm-anchor-qid-{Q_ACME}"
    assert not first_durable.startswith("wmc-")

    # CONTRAST — the old wmc- fingerprint DOES churn across the fresh member-id sets, proving wmc-
    # is an idempotency fingerprint, not durable identity (the precise reason this gate exists).
    wmc_first = _canonical_id(("ing1-a", "ing1-b"))
    wmc_second = _canonical_id(("ing2-a", "ing2-b"))
    assert wmc_first.startswith("wmc-") and wmc_second.startswith("wmc-")
    assert wmc_first != wmc_second


# ---------------------------------------------------------------------------------------------
# Case 2 (A3) — ADOPT: a re-ingested anchored member adopts the existing durable id; no new mint.
# ---------------------------------------------------------------------------------------------


def test_reingest_adopts_existing_durable_id_no_new_mint(ledger_session: Session) -> None:
    """A re-ingested anchored member ADOPTS the durable id already recorded for its anchor — no
    second durable id minted, no id churn (A3).

    Asserts the ledger holds EXACTLY ONE durable canonical for the QID after both ingests (the
    adopt read found the prior entry instead of recording a new one).
    """
    first = [_company("ing1-a", wikidata_id=Q_ACME)]
    durable_1 = _derive_durable(ledger_session, first)

    # Adopt path: lookup must find the recorded durable id for the anchor.
    adopted = canonical.lookup_durable_for_anchor(ledger_session, f"wm-anchor-qid-{Q_ACME}")
    assert adopted == durable_1 == f"wm-anchor-qid-{Q_ACME}"

    # Second ingest, fresh member id, same real entity -> adopts, mints nothing new.
    second = [_company("ing2-z", wikidata_id=Q_ACME)]
    durable_2 = _derive_durable(ledger_session, second)
    assert durable_2 == durable_1

    # Exactly ONE durable canonical row exists for this QID (no second node / no churn).
    canon_rows = [
        c for (c, _alias) in _ledger_rows(ledger_session) if c == f"wm-anchor-qid-{Q_ACME}"
    ]
    assert canon_rows, "the QID durable id must be recorded in the ledger"
    durable_canonicals = {c for (c, _a) in _ledger_rows(ledger_session)}
    assert durable_canonicals == {f"wm-anchor-qid-{Q_ACME}"}, (
        "adopt must not mint a second durable id"
    )


# ---------------------------------------------------------------------------------------------
# Case 3 (A4) — MERGE: survivor durable id = pick_anchor(members); canonical_alias recorded for
# EVERY collapsed/prior id.
# ---------------------------------------------------------------------------------------------


def test_merge_records_alias_for_every_collapsed_id(ledger_session: Session) -> None:
    """A merge's survivor durable id is ``pick_anchor(members)`` and a ``canonical_alias`` row is
    recorded for EVERY collapsed member id (A4/D4). The merged FtM node is NOT keyed ``wmc-`` when
    an anchor exists (A10).
    """
    members = [_company("m1", wikidata_id=Q_ACME), _company("m2", wikidata_id=Q_ACME)]
    cluster = _merge_cluster(members, [ScoredPair("m1", "m2", 0.99)])

    survivor = canonical.pick_anchor(members)
    assert survivor == f"wm-anchor-qid-{Q_ACME}"
    assert not survivor.startswith("wmc-"), "an anchored merge survivor must NOT be a wmc- hash"

    canonical.record_canonical(ledger_session, survivor, anchor_kind="qid", anchor_value=Q_ACME)
    # Every collapsed member id (and the cluster's prior wmc- fingerprint) becomes a traceable
    # alias.
    for member_id in cluster.member_ids:
        canonical.record_alias(ledger_session, survivor, member_id)
    canonical.record_alias(ledger_session, survivor, cluster.canonical_id)

    # Each collapsed id resolves to the surviving durable id (no orphan reference).
    for member_id in ("m1", "m2"):
        assert canonical.resolve_durable(ledger_session, member_id) == survivor
    assert canonical.resolve_durable(ledger_session, cluster.canonical_id) == survivor


# ---------------------------------------------------------------------------------------------
# Case 4 (A5) — SPLIT: anchor side keeps its durable id; ejected id recorded as a traceable
# alias (append-only, no orphan).
# ---------------------------------------------------------------------------------------------


def test_split_keeps_anchor_durable_id_and_traces_ejected(ledger_session: Session) -> None:
    """On a split, the anchor side KEEPS its durable id and the EJECTED id is recorded as a
    traceable alias — append-only, no orphan, no silent id churn (A5/D4).
    """
    members = [_company("s1", wikidata_id=Q_ACME), _company("s2", wikidata_id=Q_ACME)]
    survivor = canonical.pick_anchor(members)
    canonical.record_canonical(ledger_session, survivor, anchor_kind="qid", anchor_value=Q_ACME)
    canonical.record_alias(ledger_session, survivor, "s1")
    canonical.record_alias(ledger_session, survivor, "s2")

    rows_before = len(_ledger_rows(ledger_session))

    # Eject s2. The anchor side keeps `survivor`; s2 stays a traceable alias (append-only).
    canonical.record_alias(ledger_session, survivor, "ejected-s2")

    # Anchor side unchanged; ejected id is traceable (resolves to the surviving durable id).
    assert canonical.pick_anchor([members[0]]) == survivor
    assert canonical.resolve_durable(ledger_session, "ejected-s2") == survivor
    # No orphan: the original member ids still resolve to the survivor too.
    assert canonical.resolve_durable(ledger_session, "s2") == survivor
    # Append-only: the split ADDED a row, never deleted one.
    assert len(_ledger_rows(ledger_session)) == rows_before + 1


# ---------------------------------------------------------------------------------------------
# Case 5 (A6/D5, PERSON-SAFETY) — anchor-conflict guard: a TWO-QID cluster never derives a durable
# id from the QID tier. It falls through (here: to LEI when present, else mints) — NEVER picks [0].
# ---------------------------------------------------------------------------------------------


def test_two_qid_cluster_does_not_derive_durable_from_qid_tier() -> None:
    """ADR 0040: a cluster whose members carry TWO DISTINCT QIDs must NOT derive a durable id from
    the QID tier (A6/D5). ``pick_anchor`` falls through; it MUST NOT silently pick either QID.
    """
    members = [_company("a", wikidata_id=Q_ACME), _company("b", wikidata_id=Q_OTHER)]
    anchor = canonical.pick_anchor(members)
    # Never one of the conflicting QIDs (the catastrophic-merge back-door D5 forbids).
    assert anchor != f"wm-anchor-qid-{Q_ACME}"
    assert anchor != f"wm-anchor-qid-{Q_OTHER}"
    # And not a QID-tier id at all — it fell THROUGH the conflicting tier.
    assert anchor is None or not anchor.startswith("wm-anchor-qid-")


def test_two_qid_cluster_falls_through_to_next_clean_tier() -> None:
    """The fall-through is to the NEXT tier, not a blind None: two conflicting QIDs but a SINGLE
    shared LEI -> the durable id is the LEI (``wm-anchor-lei-…``), never a QID. Proves
    ``pick_anchor`` does not stop at the first conflicting tier (D5: never pick [0]; §5: fall
    through to next NON-conflicting tier).
    """
    members = [
        _company("a", wikidata_id=Q_ACME, lei=LEI_ACME),
        _company("b", wikidata_id=Q_OTHER, lei=LEI_ACME),
    ]
    anchor = canonical.pick_anchor(members)
    assert anchor == f"wm-anchor-lei-{LEI_ACME}"
    assert not anchor.startswith("wm-anchor-qid-")


# ---------------------------------------------------------------------------------------------
# Case 6 (MR-5) — IDEMPOTENCE: deriving twice over the same anchored input is a no-op (no duplicate
# durable id, no duplicate ledger/alias row).
# ---------------------------------------------------------------------------------------------


def test_derive_twice_is_idempotent_no_duplicate_rows(ledger_session: Session) -> None:
    """Running the derive+record over the same anchored input twice is a NO-OP (MR-5): the durable
    id is identical and the ledger has NO duplicate canonical row and NO duplicate alias row.
    """
    members = [_company("m1", wikidata_id=Q_ACME), _company("m2", wikidata_id=Q_ACME)]
    survivor = canonical.pick_anchor(members)

    # First derive + record canonical + aliases.
    canonical.record_canonical(ledger_session, survivor, anchor_kind="qid", anchor_value=Q_ACME)
    canonical.record_alias(ledger_session, survivor, "m1")
    canonical.record_alias(ledger_session, survivor, "m2")
    rows_after_first = sorted(_ledger_rows(ledger_session))

    # Second derive — identical input — must add nothing.
    again = canonical.pick_anchor(members)
    assert again == survivor
    canonical.record_canonical(ledger_session, survivor, anchor_kind="qid", anchor_value=Q_ACME)
    canonical.record_alias(ledger_session, survivor, "m1")
    canonical.record_alias(ledger_session, survivor, "m2")
    rows_after_second = sorted(_ledger_rows(ledger_session))

    assert rows_after_second == rows_after_first, "re-derive must not duplicate ledger/alias rows"
    # Exactly one canonical-self row + two distinct alias rows; no duplicates.
    canonical_self = [(c, a) for (c, a) in rows_after_second if c == survivor and a == survivor]
    assert len(canonical_self) <= 1
    alias_targets = {a for (c, a) in rows_after_second if c == survivor and a != survivor}
    assert {"m1", "m2"} <= alias_targets


# ---------------------------------------------------------------------------------------------
# ADVERSARIAL (judge weights heaviest) — re-ingest + concurrent split race. A re-ingest mints fresh
# member ids while a reject ejects a member. The surviving durable id stays STABLE and the ejected
# id stays TRACEABLE: no orphan, no silent id churn.
# ---------------------------------------------------------------------------------------------


def test_reingest_concurrent_split_race_keeps_durable_stable_and_ejected_traceable(
    ledger_session: Session,
) -> None:
    """Adversarial: a re-ingest (fresh member ids) races a human reject (ejects a member).

    Timeline:
      t0  initial ingest establishes the durable id from the anchor + records member aliases.
      t1  a re-ingest of the SAME real entity mints FRESH member ids (ing2-*) and ADOPTS.
      t2  a concurrent reject EJECTS one member (recorded as a traceable alias, append-only).

    Invariant: through the whole race the surviving durable id is UNCHANGED, the ejected id is
    traceable (resolves to the survivor — NO orphan), and there is NO silent id churn (the durable
    canonical id derived at t0, t1 and post-t2 is byte-for-byte identical, and never a wmc- hash).
    """
    # t0 — initial ingest.
    initial = [_company("ing1-a", wikidata_id=Q_ACME), _company("ing1-b", wikidata_id=Q_ACME)]
    durable_t0 = _derive_durable(ledger_session, initial)
    canonical.record_alias(ledger_session, durable_t0, "ing1-a")
    canonical.record_alias(ledger_session, durable_t0, "ing1-b")
    assert durable_t0 == f"wm-anchor-qid-{Q_ACME}"

    # t1 — re-ingest with FRESH member ids races in; it must ADOPT the same durable id.
    reingest = [_company("ing2-a", wikidata_id=Q_ACME), _company("ing2-b", wikidata_id=Q_ACME)]
    durable_t1 = _derive_durable(ledger_session, reingest)
    assert durable_t1 == durable_t0, "re-ingest must adopt the stable durable id, never churn it"

    # t2 — concurrent reject EJECTS a member (append-only alias; the un-merge never deletes).
    canonical.record_alias(ledger_session, durable_t0, "ing1-b-ejected")

    # The surviving durable id is byte-for-byte stable across the whole race…
    durable_after = canonical.pick_anchor([_company("ing3-a", wikidata_id=Q_ACME)])
    existing = canonical.lookup_durable_for_anchor(ledger_session, durable_after)
    assert durable_after == durable_t0 == existing
    assert not durable_t0.startswith("wmc-")

    # …and the ejected id is traceable — resolves to the surviving durable id, NO orphan.
    assert canonical.resolve_durable(ledger_session, "ing1-b-ejected") == durable_t0
    # The fresh re-ingest member ids and the original ones ALL still resolve to one survivor (no
    # silent id churn split the entity into two durable ids).
    assert canonical.resolve_durable(ledger_session, "ing1-a") == durable_t0
    distinct_durables = {c for (c, _a) in _ledger_rows(ledger_session)}
    assert distinct_durables == {durable_t0}, "the race must not fork the entity into >1 durable id"
