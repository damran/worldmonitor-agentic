"""Unit tests for Gate WPI-1 — zero-prop-entity disposition (ADR 0112).

Fast, pure, no-DB oracle arm for ``INV-ZEROPROP-DISPOSITION`` + rider-1 (source-reachability)
+ rider-2 (erased-member-id derivability).

Pins (ADR 0112 Mechanism / Decision (a)):

* ``fuse_statement_rows(cluster, by_id)`` emits exactly ONE existence-claim ``StatementRecord``
  PER MEMBER (reserved sentinel ``prop`` = the module constant ``WM_EXISTS = "wm:exists"``,
  ``value = ""``) when the normal per-property projection would return ``[]`` (every member
  propertyless) — the deterministic ``statement_id`` is
  ``sha256(canonical_id \\x00 entity_id \\x00 WM_EXISTS \\x00 dataset)``.
* rider-1 — a zero-prop member whose ``Provenance.source_id`` is EMPTY is skipped-and-logged:
  no row is written for it, and no OTHER row's ``dataset`` is ever empty or a ``member.id``-keyed
  fallback (source-unreachable — P2's erasure scrub could never reach it via
  ``dataset == source_id``).
* rider-2 — the zero-prop member's own id is DERIVABLE from the log: it is present as an
  existence claim's ``entity_id`` even when the survivor's ``canonical_id`` differs from every
  member id (the real-merge shape).
* ``reconstruct_entities`` (``resolution/projector.py``) on a sentinel(``WM_EXISTS``)-only row
  group still materialises a BARE node (empty FtM properties, empty witness map — no
  ``prov_witnesses``) — it skips ``WM_EXISTS`` for prop/witness assignment EXACTLY like it
  already skips ``prop == "id"``, but the survivor group is non-empty (it now HAS a row), so a
  node is built.

All tests are RED today: ``from worldmonitor.resolution.statements import WM_EXISTS`` fails with
``ImportError`` — that module constant does not exist until the builder adds it (ADR 0112
Mechanism) — so this file fails to collect at all (the repo's gate-import idiom, mirrors the
``ProjectionCheckpoint`` import comment in ``tests/integration/test_projector.py``).
"""

from __future__ import annotations

import hashlib

from worldmonitor.ontology.ftm import FtmEntity, make_entity
from worldmonitor.provenance.model import Provenance, get_provenance, stamp
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.resolution.projector import reconstruct_entities
from worldmonitor.resolution.statements import (  # gate import: RED until builder adds WM_EXISTS
    WM_EXISTS,
    fuse_statement_rows,
)


def _zeroprop_member(
    member_id: str, *, schema: str = "Person", source_id: str = "src:zp"
) -> FtmEntity:
    """A zero-prop (and, since no anchor is ever set, zero-anchor too) stamped FtM entity."""
    entity = make_entity({"id": member_id, "schema": schema, "properties": {}})
    stamp(
        entity,
        Provenance(
            source_id=source_id,
            retrieved_at="2026-07-12T00:00:00Z",
            reliability="A",
            source_record=f"s3://landing/{member_id}.json",
        ),
    )
    return entity


def _cluster(canonical_id: str, member_ids: tuple[str, ...], entity: FtmEntity) -> ResolvedCluster:
    return ResolvedCluster(
        canonical_id=canonical_id, member_ids=member_ids, entity=entity, score=1.0
    )


# ===========================================================================
# INV-ZEROPROP-DISPOSITION — fuse_statement_rows emits one WM_EXISTS row per member
# ===========================================================================


def test_fuse_statement_rows_zero_prop_emits_one_wm_exists_row_per_member() -> None:
    m1 = _zeroprop_member("zp-u-m1", source_id="src:zp-u-1")
    m2 = _zeroprop_member("zp-u-m2", source_id="src:zp-u-2")
    by_id = {"zp-u-m1": m1, "zp-u-m2": m2}
    cluster = _cluster("zp-u-canon", ("zp-u-m1", "zp-u-m2"), m1)

    rows = fuse_statement_rows(cluster, by_id)

    assert len(rows) == 2, (
        f"expected exactly 1 WM_EXISTS row PER MEMBER (2 members) for a zero-prop cluster, "
        f"got {len(rows)} row(s): {[(r.entity_id, r.prop) for r in rows]}"
    )
    by_entity = {r.entity_id: r for r in rows}
    assert set(by_entity) == {"zp-u-m1", "zp-u-m2"}

    for member_id, source_id in (("zp-u-m1", "src:zp-u-1"), ("zp-u-m2", "src:zp-u-2")):
        row = by_entity[member_id]
        assert row.prop == WM_EXISTS == "wm:exists"
        assert row.value == ""
        assert row.canonical_id == "zp-u-canon"
        assert row.schema == "Person"
        assert row.dataset == source_id
        expected_id = hashlib.sha256(
            f"zp-u-canon\x00{member_id}\x00{WM_EXISTS}\x00{source_id}".encode()
        ).hexdigest()
        assert row.statement_id == expected_id, (
            "statement_id must be the deterministic sha256(canonical_id\\x00entity_id\\x00"
            f"WM_EXISTS\\x00dataset) hash; expected {expected_id!r}, got {row.statement_id!r}"
        )


def test_fuse_statement_rows_zero_prop_singleton_emits_exactly_one_row() -> None:
    m1 = _zeroprop_member("zp-u-single")
    by_id = {"zp-u-single": m1}
    cluster = _cluster("zp-u-single", ("zp-u-single",), m1)

    rows = fuse_statement_rows(cluster, by_id)

    assert len(rows) == 1, (
        f"a zero-prop SINGLETON must leave exactly 1 WM_EXISTS row, got {len(rows)}"
    )
    assert rows[0].entity_id == "zp-u-single"
    assert rows[0].canonical_id == "zp-u-single"
    assert rows[0].prop == WM_EXISTS


def test_fuse_statement_rows_nonzero_prop_cluster_unaffected() -> None:
    """Sanity: a cluster that DOES have real properties is untouched by the WM_EXISTS branch
    (it is only taken when the normal projection would return [])."""
    m1 = make_entity(
        {"id": "zp-u-real", "schema": "Company", "properties": {"name": ["Real Corp"]}}
    )
    stamp(
        m1,
        Provenance(
            source_id="src:zp-u-real",
            retrieved_at="2026-07-12T00:00:00Z",
            reliability="A",
            source_record="s3://landing/zp-u-real.json",
        ),
    )
    by_id = {"zp-u-real": m1}
    cluster = _cluster("zp-u-real", ("zp-u-real",), m1)

    rows = fuse_statement_rows(cluster, by_id)

    assert rows, "a real-property cluster must still emit its normal statement rows"
    assert all(r.prop != WM_EXISTS for r in rows), (
        "WM_EXISTS must NOT appear when the cluster has real properties to project"
    )


# ===========================================================================
# rider-1 — empty source_id is skipped-and-logged, never a source-unreachable row
# ===========================================================================


def test_rider1_empty_source_id_member_skipped_no_source_unreachable_row() -> None:
    valid = _zeroprop_member("zp-r1-valid", source_id="src:zp-r1-valid")
    empty = _zeroprop_member("zp-r1-empty", source_id="")
    by_id = {"zp-r1-valid": valid, "zp-r1-empty": empty}
    cluster = _cluster("zp-r1-canon", ("zp-r1-valid", "zp-r1-empty"), valid)

    rows = fuse_statement_rows(cluster, by_id)

    assert len(rows) == 1, (
        f"expected exactly 1 row (the empty-source_id member skipped-and-logged), got {len(rows)}"
    )
    assert rows[0].entity_id == "zp-r1-valid"
    assert rows[0].dataset == "src:zp-r1-valid"
    assert not any(r.entity_id == "zp-r1-empty" for r in rows), (
        "rider-1 VIOLATED: a row was written for the empty-source_id member"
    )
    assert not any(r.dataset in ("", "zp-r1-empty") for r in rows), (
        "rider-1 VIOLATED: a source-unreachable row (dataset='' or dataset=member.id) was "
        "written — P2's dataset==source_id erasure scrub could never reach it"
    )


def test_rider1_all_members_empty_source_id_yields_zero_rows() -> None:
    m1 = _zeroprop_member("zp-r1-all-1", source_id="")
    m2 = _zeroprop_member("zp-r1-all-2", source_id="")
    by_id = {"zp-r1-all-1": m1, "zp-r1-all-2": m2}
    cluster = _cluster("zp-r1-all-canon", ("zp-r1-all-1", "zp-r1-all-2"), m1)

    rows = fuse_statement_rows(cluster, by_id)

    assert rows == [], (
        f"a cluster where EVERY member has an empty source_id must yield ZERO rows "
        f"(nothing is source-reachable), got {len(rows)}"
    )


# ===========================================================================
# rider-2 — the zero-prop member id is derivable from the log (entity_id)
# ===========================================================================


def test_rider2_member_id_derivable_as_existence_claim_entity_id() -> None:
    # canonical_id DISTINCT from either member id (a real merge's content-addressed wmc- id),
    # so this proves the member id is recoverable from the log even though it never appears
    # as the survivor/canonical id itself.
    m1 = _zeroprop_member("zp-r2-m1", source_id="src:zp-r2-1")
    m2 = _zeroprop_member("zp-r2-m2", source_id="src:zp-r2-2")
    by_id = {"zp-r2-m1": m1, "zp-r2-m2": m2}
    cluster = _cluster("wmc-zp-r2-canon", ("zp-r2-m1", "zp-r2-m2"), m1)

    rows = fuse_statement_rows(cluster, by_id)

    entity_ids = {r.entity_id for r in rows}
    assert {"zp-r2-m1", "zp-r2-m2"} <= entity_ids, (
        "rider-2 VIOLATED: the zero-prop member ids must be present as existence-claim "
        f"entity_id values (log-derivable for erasure's decision.member_ids redaction path); "
        f"got entity_ids={sorted(entity_ids)!r}"
    )
    assert all(r.canonical_id == "wmc-zp-r2-canon" for r in rows)
    assert "wmc-zp-r2-canon" not in entity_ids, (
        "the members' OWN ids, not the survivor id, must be what is derivable per-member"
    )


# ===========================================================================
# reconstruct_entities — a sentinel-only row list still materialises a BARE node
# ===========================================================================


def test_reconstruct_entities_sentinel_only_group_produces_bare_node() -> None:
    m1 = _zeroprop_member("zp-u-fold-m1", source_id="src:zp-u-fold")
    by_id = {"zp-u-fold-m1": m1}
    cluster = _cluster("zp-u-fold-canon", ("zp-u-fold-m1",), m1)
    rows = fuse_statement_rows(cluster, by_id)
    assert rows, "precondition: fuse_statement_rows must emit >= 1 row for this fixture"

    entities = reconstruct_entities(rows, survivor_of=lambda cid: cid)

    assert len(entities) == 1, (
        f"a sentinel(WM_EXISTS)-only row group must materialise exactly 1 bare node, got "
        f"{len(entities)}"
    )
    entity = entities[0]
    assert entity.id == "zp-u-fold-canon"
    assert entity.schema.name == "Person"
    assert entity.properties == {}, (
        f"a bare zero-prop node must carry NO FtM properties (WM_EXISTS is skipped exactly "
        f"like 'id'), got {entity.properties!r}"
    )
    prov = get_provenance(entity)
    assert prov is not None and prov.source_id == "src:zp-u-fold", (
        "the bare node must still carry G1 prov_* (representative provenance off the member row)"
    )
    assert entity.context.get("wm_prov_witnesses") is None, (
        "a bare zero-prop node must have NO prov_witnesses — the witness map must stay empty "
        "(WM_EXISTS excluded from witness assignment exactly like 'id')"
    )


def test_reconstruct_entities_sentinel_and_id_pseudo_prop_only_still_bare() -> None:
    """A row list mixing the (always-excluded) 'id' pseudo-prop shape with a WM_EXISTS row for
    a SECOND member of the same survivor group still yields exactly one bare node (both
    exclusions compose, no double-count / no crash)."""
    m1 = _zeroprop_member("zp-u-mix-m1", source_id="src:zp-u-mix-1")
    m2 = _zeroprop_member("zp-u-mix-m2", source_id="src:zp-u-mix-2")
    by_id = {"zp-u-mix-m1": m1, "zp-u-mix-m2": m2}
    cluster = _cluster("zp-u-mix-canon", ("zp-u-mix-m1", "zp-u-mix-m2"), m1)
    rows = fuse_statement_rows(cluster, by_id)
    assert len(rows) == 2

    entities = reconstruct_entities(rows, survivor_of=lambda cid: cid)

    assert len(entities) == 1
    assert entities[0].properties == {}
