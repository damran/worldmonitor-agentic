"""Unit tests for Gate 3a-i — ``reconstruct_entities`` (pure fold, no containers).

Tests the PURE ``reconstruct_entities`` function (no DB, no Neo4j), which is the
heart of the fold algorithm (ADR 0100 D3).  Each test constructs ``StatementRecord``
objects by hand and asserts the precise invariants the fold must uphold.

All tests in this file are RED at collection time because the import of
``reconstruct_entities`` from ``worldmonitor.resolution.projector`` fails — that
module does not exist yet.  That is the correct, intended TDD failure mode.

Invariants covered
------------------
T-PROJ-UNIT-1  2-source Company group: one entity, union values, correct prov_*
               representative (min entity_id), multi-source witness map, no "id" prop.
T-PROJ-UNIT-2  Duplicate ``statement_id`` rows collapse (dedup) — one contribution, not two.
T-PROJ-UNIT-3  Entity-typed property values are rewritten through ``survivor_of``; non-mapped
               ids are left verbatim (global referent rewrite, ADR 0100 D2).
T-PROJ-UNIT-4  ``survivor_of`` folds an aliased canonical X → survivor Y via the real ledger
               (``resolve_durable``): all rows group under Y, none under X.
T-PROJ-UNIT-5  NULL reliability/retrieved_at/raw_pointer members still produce G1-valid
               ``Provenance`` (dataset is NOT NULL → ``prov_*`` is always non-empty;
               ``write_entities`` never fails closed).
"""

from __future__ import annotations

import uuid
from typing import Any

from followthemoney import model as ftm_model
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from worldmonitor.db.models import Base, StatementRecord
from worldmonitor.provenance.model import get_provenance, provenance_node_properties, witness_map
from worldmonitor.resolution.canonical import record_alias, record_canonical, resolve_durable
from worldmonitor.resolution.projector import reconstruct_entities  # gate: RED until builder lands

# ---------------------------------------------------------------------------
# SQLite JSONB shim (idempotent if already registered by another test module)
# ---------------------------------------------------------------------------


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sqlite_session() -> Session:
    """Fresh in-memory SQLite session with all ORM tables — one per test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _stmt(
    canonical_id: str,
    entity_id: str,
    schema: str,
    prop: str,
    value: str,
    dataset: str,
    *,
    statement_id: str | None = None,
    reliability: str | None = "B",
    retrieved_at: str | None = "2026-01-01T00:00:00Z",
    raw_pointer: str | None = None,
) -> StatementRecord:
    """Build a minimal ``StatementRecord`` for unit testing ``reconstruct_entities``.

    ``seq`` is intentionally not set — it is a server-assigned IDENTITY column
    (ADR 0100 D1) and must never be set by application code.
    """
    return StatementRecord(
        id=str(uuid.uuid4()),
        statement_id=statement_id or str(uuid.uuid4()),
        canonical_id=canonical_id,
        entity_id=entity_id,
        schema=schema,
        prop=prop,
        value=value,
        dataset=dataset,
        reliability=reliability,
        retrieved_at=retrieved_at,
        raw_pointer=raw_pointer,
    )


# ===========================================================================
# T-PROJ-UNIT-1: 2-source Company group → one entity, union values, prov_* + witnesses
# ===========================================================================


def test_two_source_company_group_reconstruction() -> None:
    """reconstruct_entities over a 2-member Company group.

    Invariants:
    - Exactly ONE entity under the canonical_id.
    - Schema == "Company".
    - Values == sorted union from BOTH sources.
    - prov_* representative = min(entity_id) member's quad ("a1" < "a2").
    - Witness map = {prop: {both datasets}} (Tier-1, multi-source).
    - "id" pseudo-property NEVER in entity.properties.
    - provenance_node_properties(entity) non-empty — G1 node invariant.
    """
    rows = [
        _stmt(
            "canon-C1",
            "a1",
            "Company",
            "name",
            "Acme",
            "src-A",
            reliability="A",
            retrieved_at="2026-01-01T00:00:00Z",
            raw_pointer="s3://landing/a1.json",
        ),
        _stmt(
            "canon-C1",
            "a1",
            "Company",
            "jurisdiction",
            "us",
            "src-A",
            reliability="A",
            retrieved_at="2026-01-01T00:00:00Z",
            raw_pointer="s3://landing/a1.json",
        ),
        _stmt(
            "canon-C1",
            "a2",
            "Company",
            "name",
            "Acme Corp",
            "src-B",
            reliability="C",
            retrieved_at="2026-02-01T00:00:00Z",
            raw_pointer="s3://landing/a2.json",
        ),
        _stmt(
            "canon-C1",
            "a2",
            "Company",
            "jurisdiction",
            "us",
            "src-B",
            reliability="C",
            retrieved_at="2026-02-01T00:00:00Z",
            raw_pointer="s3://landing/a2.json",
        ),
    ]

    entities = reconstruct_entities(rows, lambda cid: cid)

    # --- cardinality ---
    assert len(entities) == 1, (
        f"T-PROJ-UNIT-1: expected 1 entity for 2-source Company group, got {len(entities)}"
    )
    entity = entities[0]

    # --- identity + schema ---
    assert entity.id == "canon-C1", (
        f"T-PROJ-UNIT-1: entity.id={entity.id!r} — must be canonical_id 'canon-C1'"
    )
    assert entity.schema.name == "Company", (
        f"T-PROJ-UNIT-1: schema={entity.schema.name!r} — must be 'Company'"
    )

    # --- property values = union ---
    assert set(entity.get("name")) == {"Acme", "Acme Corp"}, (
        f"T-PROJ-UNIT-1: name values={set(entity.get('name'))!r} — must be {{'Acme','Acme Corp'}}"
    )
    assert set(entity.get("jurisdiction")) == {"us"}, (
        f"T-PROJ-UNIT-1: jurisdiction={set(entity.get('jurisdiction'))!r} — must be {{'us'}}"
    )

    # --- id pseudo-property MUST be absent ---
    assert "id" not in entity.properties, (
        "T-PROJ-UNIT-1: 'id' pseudo-property must never appear in entity.properties "
        "(excluded by the fold per ADR 0100 D3)"
    )

    # --- prov_* = min(entity_id) = 'a1' member's quad ---
    prov = get_provenance(entity)
    assert prov is not None, "T-PROJ-UNIT-1: G1 — provenance must be present"
    assert prov.source_id == "src-A", (
        f"T-PROJ-UNIT-1: prov.source_id={prov.source_id!r} — "
        "min(entity_id)='a1' whose dataset='src-A' is the representative"
    )
    assert prov.reliability == "A", (
        f"T-PROJ-UNIT-1: prov.reliability={prov.reliability!r} — must be 'a1' member's 'A'"
    )
    assert prov.retrieved_at == "2026-01-01T00:00:00Z", (
        f"T-PROJ-UNIT-1: prov.retrieved_at={prov.retrieved_at!r} — must be 'a1' member's timestamp"
    )
    assert prov.source_record == "s3://landing/a1.json", (
        f"T-PROJ-UNIT-1: prov.source_record={prov.source_record!r} — must be 'a1' member's pointer"
    )

    # --- Tier-1 witness map: {prop: {src-A, src-B}} ---
    wm = witness_map(entity)
    assert wm.get("name") == {"src-A", "src-B"}, (
        f"T-PROJ-UNIT-1: witness_map['name']={wm.get('name')!r} — must be {{'src-A','src-B'}}"
    )
    assert wm.get("jurisdiction") == {"src-A", "src-B"}, (
        f"T-PROJ-UNIT-1: witness_map['jurisdiction']={wm.get('jurisdiction')!r} — "
        "must be {'src-A','src-B'}"
    )

    # --- G1: prov_* non-empty → write_entities never fails closed ---
    node_props = provenance_node_properties(entity)
    assert node_props, (
        "T-PROJ-UNIT-1: G1 VIOLATED — provenance_node_properties is empty; "
        "write_entities would raise NodeProvenanceError (ADR 0060)"
    )
    assert "prov_source_id" in node_props, (
        "T-PROJ-UNIT-1: G1 — 'prov_source_id' must be present in node properties"
    )


# ===========================================================================
# T-PROJ-UNIT-2: Duplicate statement_id rows collapse (dedup — ADR 0100 D3 step 1)
# ===========================================================================


def test_duplicate_statement_id_collapses() -> None:
    """Duplicate ``statement_id`` rows (same content hash, distinct UUID PKs) → one contribution.

    The dedup step removes the duplicate BEFORE grouping so the resulting entity is
    identical to the single-row case.  At-least-once delivery of the same statement must
    be a no-op (ADR 0100 D3 / D4 / P-FOLD-3 semantic).

    Also checks that no exception is raised (robustness against duplicate delivery).
    """
    shared_stmt_id = "hash-dedup-test-deadbeef0000cafebabe"

    rows_with_dup = [
        _stmt(
            "canon-D",
            "e1",
            "Company",
            "name",
            "Dedup Corp",
            "src-A",
            statement_id=shared_stmt_id,
        ),
        _stmt(
            "canon-D",
            "e1",
            "Company",
            "name",
            "Dedup Corp",
            "src-A",
            statement_id=shared_stmt_id,  # EXACT duplicate statement_id
        ),
    ]
    rows_single = [
        _stmt(
            "canon-D",
            "e1",
            "Company",
            "name",
            "Dedup Corp",
            "src-A",
            statement_id=shared_stmt_id,
        ),
    ]

    entities_dup = reconstruct_entities(rows_with_dup, lambda cid: cid)
    entities_single = reconstruct_entities(rows_single, lambda cid: cid)

    assert len(entities_dup) == 1, (
        f"T-PROJ-UNIT-2: expected 1 entity after dedup, got {len(entities_dup)}"
    )
    assert len(entities_single) == 1

    dup_entity = entities_dup[0]
    single_entity = entities_single[0]

    # Values must be identical (dedup; not doubled by the duplicate row)
    dup_name_vals = sorted(str(v) for v in dup_entity.get("name"))
    single_name_vals = sorted(str(v) for v in single_entity.get("name"))
    assert dup_name_vals == single_name_vals, (
        "T-PROJ-UNIT-2: DEDUP VIOLATED — duplicate statement_id produced different name values "
        f"than single row: dup={dup_name_vals!r} vs single={single_name_vals!r}. "
        "A duplicate row with the same statement_id must not add a second contribution."
    )

    assert dup_entity.id == single_entity.id == "canon-D", (
        "T-PROJ-UNIT-2: entity.id mismatch after dedup"
    )

    # G1 still upheld after dedup
    assert get_provenance(dup_entity) is not None, (
        "T-PROJ-UNIT-2: G1 — provenance must be present even after dedup path"
    )


# ===========================================================================
# T-PROJ-UNIT-3: Entity-typed value rewritten through survivor_of (D2 global referent rewrite)
# ===========================================================================


def test_entity_typed_value_rewritten_through_survivor_of() -> None:
    """Entity-typed property values are rewritten through ``survivor_of`` (ADR 0100 D2).

    An Ownership.owner naming a merged-away source id must be rewritten to the survivor.
    A non-mapped id (not in the alias map) is left verbatim.

    Verifies that:
    - 'merged-away-id' → 'survivor-id' via survivor_of (entity-typed prop is rewritten)
    - 'asset-co-1' is left verbatim (not in aliases → identity mapping)
    - 'merged-away-id' does NOT appear in the final entity's owner values
    """
    aliases: dict[str, str] = {"merged-away-id": "survivor-id"}
    survivor_of = lambda cid: aliases.get(cid, cid)  # noqa: E731

    rows = [
        _stmt(
            "own-A",
            "own-A",
            "Ownership",
            "owner",
            "merged-away-id",
            "src-A",
            reliability="B",
            retrieved_at="2026-01-01T00:00:00Z",
        ),
        # asset = "asset-co-1" — entity-typed but NOT in aliases → left verbatim
        _stmt(
            "own-A",
            "own-A",
            "Ownership",
            "asset",
            "asset-co-1",
            "src-A",
            reliability="B",
            retrieved_at="2026-01-01T00:00:00Z",
        ),
    ]

    entities = reconstruct_entities(rows, survivor_of)

    assert len(entities) == 1, f"T-PROJ-UNIT-3: expected 1 Ownership entity, got {len(entities)}"
    entity = entities[0]
    assert entity.id == "own-A"
    assert entity.schema.name == "Ownership"

    owner_values = list(entity.get("owner"))
    asset_values = list(entity.get("asset"))

    assert "survivor-id" in owner_values, (
        f"T-PROJ-UNIT-3: GLOBAL REFERENT REWRITE FAILED — 'survivor-id' not in owner values "
        f"{owner_values!r}. Entity-typed owner='merged-away-id' must be rewritten to "
        "'survivor-id' via survivor_of (ADR 0100 D2)."
    )
    assert "merged-away-id" not in owner_values, (
        f"T-PROJ-UNIT-3: STALE REFERENT — 'merged-away-id' still present in owner values "
        f"{owner_values!r}. Merged-away source ids must not survive the global referent rewrite."
    )
    assert "asset-co-1" in asset_values, (
        f"T-PROJ-UNIT-3: 'asset-co-1' should be left verbatim (not in aliases), "
        f"got asset_values={asset_values!r}"
    )
    assert "survivor-id" not in asset_values, (
        f"T-PROJ-UNIT-3: 'survivor-id' must NOT appear in asset values (only owner is aliased), "
        f"got asset_values={asset_values!r}"
    )


# ===========================================================================
# T-PROJ-UNIT-4: survivor_of folds aliased canonical X → survivor Y (single group under Y)
# ===========================================================================


def test_survivor_of_aliases_canonical_x_to_survivor_y() -> None:
    """``survivor_of`` folds aliased canonical X → survivor Y: all rows group under Y.

    Uses a real SQLite session with ``resolve_durable`` (the same alias-on-read path
    the projector uses at fold time) so the ledger path is exercised end-to-end.

    Invariant: when record_alias(session, Y, X) maps X → Y in the ledger, a corpus
    with statements under BOTH X and Y folds into EXACTLY ONE entity under Y — no
    entity under X, and the combined values from both groups appear on Y.
    """
    session = _sqlite_session()

    # Set up ledger: Y is the survivor (has a self-row); X aliases to Y
    record_canonical(session, "survivor-Y")
    record_alias(session, "survivor-Y", "superseded-X")
    session.flush()

    survivor_of = lambda cid: resolve_durable(session, cid) or cid  # noqa: E731

    rows_x = [
        _stmt("superseded-X", "ex1", "Company", "name", "X Company", "src-X"),
        _stmt("superseded-X", "ex2", "Company", "jurisdiction", "de", "src-X"),
    ]
    rows_y = [
        _stmt("survivor-Y", "ey1", "Company", "name", "Y Corp", "src-Y"),
    ]

    entities = reconstruct_entities(rows_x + rows_y, survivor_of)

    # Must produce EXACTLY ONE entity — all rows fold under survivor Y
    assert len(entities) == 1, (
        f"T-PROJ-UNIT-4: SUPERSESSION FAILED — expected 1 entity (all under Y), "
        f"got {len(entities)} entities. Aliased canonical 'superseded-X' must fold "
        "into survivor 'survivor-Y', producing a single merged node with both groups' values."
    )
    entity = entities[0]

    assert entity.id == "survivor-Y", (
        f"T-PROJ-UNIT-4: entity.id={entity.id!r} — all rows must fold under survivor 'survivor-Y'"
    )

    # Combined values from BOTH X and Y rows must appear
    name_values = set(entity.get("name"))
    assert "X Company" in name_values, (
        "T-PROJ-UNIT-4: 'X Company' (from superseded-X rows) missing from name values "
        f"{name_values!r}"
    )
    assert "Y Corp" in name_values, (
        f"T-PROJ-UNIT-4: 'Y Corp' (from survivor-Y rows) missing from name values {name_values!r}"
    )

    # No entity should be produced under superseded-X
    x_ids = [e.id for e in entities if e.id == "superseded-X"]
    assert not x_ids, (
        "T-PROJ-UNIT-4: entity with id='superseded-X' found in output — "
        "superseded canonical must produce NO entity (all rows fold under survivor Y)"
    )

    session.close()


# ===========================================================================
# T-PROJ-UNIT-5: NULL quad → G1-valid Provenance (dataset is NOT NULL → always present)
# ===========================================================================


def test_null_quad_members_produce_g1_valid_provenance() -> None:
    """NULL reliability/retrieved_at/raw_pointer still produce G1-valid Provenance.

    dataset is NOT NULL in the statement spine (G1 guarantee, ADR 0099).  The fold
    MUST reconstruct a non-empty Provenance from the dataset alone, with NULL fields
    falling back to empty strings — not None, not invented values.  This ensures
    ``provenance_node_properties`` is always non-empty so ``write_entities`` never
    raises ``NodeProvenanceError`` on a fold-reconstructed entity (fail-closed G1,
    ADR 0060).
    """
    rows = [
        StatementRecord(
            id=str(uuid.uuid4()),
            statement_id=str(uuid.uuid4()),
            canonical_id="C-null-quad",
            entity_id="e-null",
            schema="Company",
            prop="name",
            value="NullQuad Corp",
            dataset="src-null-test",  # NOT NULL — this is the G1 anchor
            reliability=None,  # genuinely unstamped member
            retrieved_at=None,
            raw_pointer=None,
        ),
    ]

    entities = reconstruct_entities(rows, lambda cid: cid)

    assert len(entities) == 1, (
        f"T-PROJ-UNIT-5: expected 1 entity for null-quad group, got {len(entities)}"
    )
    entity = entities[0]

    prov = get_provenance(entity)
    assert prov is not None, (
        "T-PROJ-UNIT-5: G1 VIOLATED — provenance must be present even when reliability/"
        "retrieved_at/raw_pointer are NULL. dataset='src-null-test' is the anchor that "
        "guarantees a non-null source_id (ADR 0100 D3 / ADR 0099 NOT-NULL constraint)."
    )
    assert prov.source_id == "src-null-test", (
        f"T-PROJ-UNIT-5: prov.source_id={prov.source_id!r} — must use dataset as source_id"
    )
    # NULL fields → empty strings (Provenance fallback, NOT None, NOT invented values)
    assert prov.reliability == "", (
        f"T-PROJ-UNIT-5: prov.reliability={prov.reliability!r} — NULL reliability → '' "
        "(Provenance(reliability='') per ADR 0100 D3 null-fallback)"
    )
    assert prov.retrieved_at == "", (
        f"T-PROJ-UNIT-5: prov.retrieved_at={prov.retrieved_at!r} — NULL retrieved_at → ''"
    )
    assert prov.source_record == "", (
        f"T-PROJ-UNIT-5: prov.source_record={prov.source_record!r} — NULL raw_pointer → ''"
    )

    # G1: prov_* must be non-empty — write_entities MUST NOT fail closed on this entity
    node_props = provenance_node_properties(entity)
    assert node_props, (
        "T-PROJ-UNIT-5: G1 VIOLATED — provenance_node_properties is empty for null-quad entity; "
        "write_entities would raise NodeProvenanceError (ADR 0060). dataset='src-null-test' "
        "guarantees a non-empty prov_source_id — the fold MUST reconstruct this."
    )
    assert node_props.get("prov_source_id") == "src-null-test", (
        f"T-PROJ-UNIT-5: G1 — prov_source_id={node_props.get('prov_source_id')!r} in node props, "
        "expected 'src-null-test'"
    )


# ===========================================================================
# T-PROJ-UNIT-6: Mixed-schema group → common schema (F1, ADR 0100)
# ===========================================================================


def test_mixed_schema_fold_uses_ftm_common_schema() -> None:
    """reconstruct_entities uses ftm_model.common_schema for a mixed-schema survivor group.

    When a survivor group contains rows with DIFFERENT-but-compatible schemas (e.g.
    Company rows and Organization rows merged into the same canonical_id), the fold MUST
    produce an entity whose schema is the FtM COMMON schema (the most specific compatible
    type), not an arbitrary ``rows[0].schema``.

    For Company + Organization:
    - Company extends Organization (Company is the more specific descendant)
    - ftm_model.common_schema("Company", "Organization") == "Company"
    - This is symmetric: common_schema("Organization", "Company") == "Company" too

    PRE-FIX REGRESSION (``rows[0].schema`` path):
    - If rows are [Organization row, Company row]: schema = "Organization" (WRONG — too broad)
    - If rows are [Company row, Organization row]: schema = "Company" (coincidentally correct)
    The old code was order-dependent.  The new code uses ``common_schema`` and is
    order-independent.  This test asserts BOTH orderings produce "Company".

    In-band check: the resulting entity has the correct schema so that entity-typed
    property resolution and FtM validation do not silently widen the type to a supertype.
    """
    # --- Forward order: Company row first, then Organization row ---
    rows_co_first = [
        _stmt("mixed-schema-C", "m1", "Company", "name", "MixedCo", "src-A"),
        _stmt("mixed-schema-C", "m2", "Organization", "name", "MixedCo Org", "src-B"),
    ]
    entities_co_first = reconstruct_entities(rows_co_first, lambda cid: cid)

    assert len(entities_co_first) == 1, (
        f"T-PROJ-UNIT-6 (Company-first): expected 1 entity, got {len(entities_co_first)}"
    )
    entity_co_first = entities_co_first[0]
    assert entity_co_first.schema.name == "Company", (
        "T-PROJ-UNIT-6 MIXED-SCHEMA FAILED (Company-first order): "
        f"entity.schema={entity_co_first.schema.name!r}, expected 'Company'. "
        "ftm_model.common_schema('Company', 'Organization') == 'Company' (Company is the "
        "more specific descendant). The fold MUST use common_schema, not rows[0].schema "
        "(ADR 0100 F1 fix)."
    )

    # --- Reversed order: Organization row first, Company row second ---
    # Against rows[0].schema, this would produce "Organization" (WRONG / too broad).
    # The fixed common_schema path produces "Company" regardless of order.
    rows_org_first = [
        _stmt("mixed-schema-C", "m2", "Organization", "name", "MixedCo Org", "src-B"),
        _stmt("mixed-schema-C", "m1", "Company", "name", "MixedCo", "src-A"),
    ]
    entities_org_first = reconstruct_entities(rows_org_first, lambda cid: cid)

    assert len(entities_org_first) == 1, (
        f"T-PROJ-UNIT-6 (Organization-first): expected 1 entity, got {len(entities_org_first)}"
    )
    entity_org_first = entities_org_first[0]
    assert entity_org_first.schema.name == "Company", (
        "T-PROJ-UNIT-6 MIXED-SCHEMA ORDER-DEPENDENCE BUG (Organization-first order): "
        f"entity.schema={entity_org_first.schema.name!r}, expected 'Company'. "
        "With rows[0].schema the pre-fix code would return 'Organization' here (the first "
        "row's schema). The fixed projector uses ftm_model.common_schema iteratively, "
        "producing 'Company' regardless of row order (ADR 0100 F1)."
    )

    # --- Confirm the two orderings produce the same schema (order-independence) ---
    assert entity_co_first.schema.name == entity_org_first.schema.name, (
        "T-PROJ-UNIT-6 ORDER-INDEPENDENCE VIOLATED: "
        f"Company-first → {entity_co_first.schema.name!r}, "
        f"Org-first → {entity_org_first.schema.name!r}. "
        "reconstruct_entities MUST produce the same schema regardless of row order "
        "(ADR 0100 F1: use ftm_model.common_schema, not rows[0].schema)."
    )

    # --- Verify via ftm_model directly (regression anchor) ---
    expected_common = ftm_model.common_schema("Company", "Organization").name
    assert expected_common == "Company", (
        f"T-PROJ-UNIT-6 PRECONDITION: ftm_model.common_schema('Company','Organization') "
        f"returned {expected_common!r} — expected 'Company'. "
        "This test's premise depends on Company being the common schema of Company+Organization."
    )
    assert entity_co_first.schema.name == expected_common, (
        f"T-PROJ-UNIT-6: entity schema {entity_co_first.schema.name!r} != "
        f"ftm_model.common_schema result {expected_common!r}"
    )
