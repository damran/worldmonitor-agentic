"""Dual-write writers for the statement + decision spine (Gate 2a / ADR 0099).

Persists the fused ``StatementEntity`` evidence (one row per claim) and one
decision row per promoted merge at the existing promote point in
``resolution/pipeline.py``.  Pure ``session.add`` helpers — the CALLER commits
(the same idiom as ``resolution/audit.record_merge``).  Rows commit atomically
with ``merge_audit`` / ``canonical_id_ledger`` and roll back together on any
failure.

Why a separate module (not ``resolution/audit.py``): ADR 0095 treats the
statement log + decision log as ONE SoR spine the Gate-3 projector reads together;
the fusion + reliability enrichment projection is substantive; ``audit.py`` stays
the tiny ``merge_audit`` / ``merge_alert`` helper it is.

Append-only invariants (ADR 0099 / G1):
* The writers only INSERT (``session.add``); no UPDATE or DELETE is ever issued.
* The ``id`` pseudo-statement is excluded so no ``worldmonitor``-dataset row enters
  the statement log.
* ``supersedes`` / ``superseded_by`` stay ``NULL`` in step 1 (the un-merge /
  belief-revision write path is a Gate 3 concern).
* ``scope`` is set to ``"default"`` on every written row (the server_default value);
  it is UNENFORCED — no code reads or filters on it (ADR 0099 Decision A).

Model and migration MUST agree byte-for-byte (``tests/integration/test_migrations.py``
drift guard, ADR 0030).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from worldmonitor.db.models import ContextClaimRecord, DecisionRecord, StatementRecord
from worldmonitor.ontology.anchors import get_anchors
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.provenance.model import get_provenance
from worldmonitor.resolution.merge import ResolvedCluster, fuse_statement_entity

logger = logging.getLogger(__name__)

# The method tag for a map-time anchor claim (ADR 0106 §1/§4). The enricher interface (not
# wired in P1) uses "enricher:<name>@<version>".
_CONNECTOR_MAP_METHOD = "connector:map"

# Gate WPI-1 (ADR 0112): the reserved existence-claim sentinel `prop`. Emitted (one row per
# member) when a promoted entity has ZERO FtM properties, so the fold still materialises a bare
# node instead of silently dropping it (the fold groups by statement rows only). Migration-free:
# reuses the existing `prop`/`value` columns.
WM_EXISTS = "wm:exists"


def fuse_statement_rows(
    cluster: ResolvedCluster,
    by_id: Mapping[str | None, Any],
) -> list[StatementRecord]:
    """Build one :class:`StatementRecord` per claim from the fused cluster.

    The single canonical projection: calls
    :func:`~worldmonitor.resolution.merge.fuse_statement_entity` and iterates
    the resulting ``StatementEntity.statements``, skipping the ``id`` pseudo-property
    (which carries the construction Dataset, not a source dataset).  ``reliability`` is
    enriched via a back-read on the member's Provenance; all other fields come directly
    off the ``Statement``.

    Rider-1 (ADR 0112, non-empty ``source_id`` guard): a statement row's contributing member
    must have a stamped :class:`~worldmonitor.provenance.model.Provenance` with a non-empty
    ``source_id`` — otherwise it is **skipped-and-log**ged (mirrors the context lane's
    no-provenance skip) rather than written, so no row's ``dataset`` is ever a source-unreachable
    ``member.id``/empty fallback (P2's erasure scrub reaches rows by ``dataset == source_id``).

    Gate WPI-1 (ADR 0112, existence-claim disposition): when ``fuse_statement_entity`` returns
    ``None`` (every cluster member is propertyless), this emits ONE existence-claim
    :class:`StatementRecord` per (source-reachable) member via :func:`_existence_claim_rows`
    instead of returning ``[]`` — so a promoted zero-prop entity still leaves a foldable spine row.

    This is the SINGLE authoritative projection — consumed by both the persist path
    (``record_statements``) and the property test (P-STMT-1 oracle independence check).
    """
    # Build a string-keyed subset for the fusion call (pipeline's by_id may have None keys)
    str_by_id: dict[str, FtmEntity] = {k: v for k, v in by_id.items() if k is not None}
    fused = fuse_statement_entity(cluster.canonical_id, list(cluster.member_ids), str_by_id)
    if fused is None:
        # Every member was propertyless (zero-prop disposition, ADR 0112) — emit one
        # existence claim per (source-reachable) member instead of an empty projection.
        return _existence_claim_rows(cluster, str_by_id)

    rows: list[StatementRecord] = []
    for statement in fused.statements:
        if statement.prop == "id":
            continue  # exclude the id pseudo-property (carries construction Dataset, not a source)

        # Enrich reliability from the contributing member's Provenance (G1 — real, not invented).
        # ``entity_id`` from _member_statements is set to ``member.id or canonical_id``.
        member = str_by_id.get(statement.entity_id)
        prov = get_provenance(member) if member is not None else None
        if prov is None or not prov.source_id:
            # Rider-1 (ADR 0112): source-unreachable — skip-and-log rather than write a row
            # whose dataset would be a member.id-keyed fallback or empty (mirrors the context
            # lane's no-provenance skip below).
            logger.warning(
                "statement: skipping claim capture for entity_id=%r (canonical_id=%r) — no "
                "stamped provenance / empty source_id (ADR 0112 rider-1, never written "
                "source-unreachable)",
                statement.entity_id,
                statement.canonical_id,
            )
            continue

        rows.append(
            StatementRecord(
                id=str(uuid.uuid4()),
                statement_id=str(statement.id),
                canonical_id=str(statement.canonical_id),
                entity_id=str(statement.entity_id),
                schema=str(statement.schema),
                prop=str(statement.prop),
                value=str(statement.value),
                dataset=str(statement.dataset),
                reliability=prov.reliability,
                retrieved_at=statement.first_seen,
                raw_pointer=statement.origin,
                first_seen=statement.first_seen,
                last_seen=statement.last_seen,
                method=None,
                scope="default",
            )
        )
    return rows


def _existence_claim_rows(
    cluster: ResolvedCluster,
    str_by_id: dict[str, FtmEntity],
) -> list[StatementRecord]:
    """Build one existence-claim :class:`StatementRecord` per member (ADR 0112 disposition (a)).

    Emitted from :func:`fuse_statement_rows` when the normal per-property projection is empty
    (every cluster member is propertyless): one row per member, carrying the reserved sentinel
    ``prop`` = :data:`WM_EXISTS`, an empty ``value``, and the full G1 quad off that member's own
    :class:`~worldmonitor.provenance.model.Provenance` — so the fold (``reconstruct_entities``)
    still has >= 1 row to group on and materialises a bare node.

    Rider-1: a member with no stamped provenance / empty ``source_id`` is skipped-and-logged (the
    same guard as the normal lane above) — never written with a source-unreachable ``dataset``.

    Rider-2: ``entity_id`` = ``member.id or cluster.canonical_id`` — the zero-prop(-zero-anchor)
    member's OWN id, so it is derivable from the log (P2's ``decision.member_ids`` redaction path
    can reach it; for a singleton, which writes no ``decision`` row, this is the ONLY place the id
    enters the spine).

    ``statement_id`` is a deterministic ``sha256(canonical_id \\x00 entity_id \\x00 WM_EXISTS \\x00
    dataset)`` hash — distinct per member, idempotent on re-observation (ADR 0100 D3 dedup).
    """
    rows: list[StatementRecord] = []
    for member_id in cluster.member_ids:
        member = str_by_id.get(member_id)
        if member is None:
            continue
        prov = get_provenance(member)
        if prov is None or not prov.source_id:
            logger.warning(
                "statement: skipping existence-claim capture for entity_id=%r "
                "(canonical_id=%r) — no stamped provenance / empty source_id (ADR 0112 "
                "rider-1, never written source-unreachable)",
                member_id,
                cluster.canonical_id,
            )
            continue

        entity_id = member.id or cluster.canonical_id
        dataset = prov.source_id
        statement_id = hashlib.sha256(
            f"{cluster.canonical_id}\x00{entity_id}\x00{WM_EXISTS}\x00{dataset}".encode()
        ).hexdigest()
        rows.append(
            StatementRecord(
                id=str(uuid.uuid4()),
                statement_id=statement_id,
                canonical_id=cluster.canonical_id,
                entity_id=entity_id,
                schema=member.schema.name,
                prop=WM_EXISTS,
                value="",
                dataset=dataset,
                reliability=prov.reliability or None,
                retrieved_at=prov.retrieved_at or None,
                raw_pointer=prov.source_record or None,
                first_seen=prov.retrieved_at or None,
                last_seen=None,
                method=None,
                scope="default",
            )
        )
    return rows


def record_statements(
    session: Session,
    cluster: ResolvedCluster,
    by_id: Mapping[str | None, Any],
) -> None:
    """``session.add`` each statement row from :func:`fuse_statement_rows` (caller commits).

    Append-only: pure ``session.add``; no UPDATE or DELETE. Rows commit atomically
    with ``merge_audit`` / ``canonical_id_ledger`` at the ``pipeline.py`` per-batch
    commit and roll back together on any failure. Idempotency (a committed batch's
    items are never re-loaded) is provided by the same B-1 guarantee
    ``record_merge`` relies on.
    """
    for row in fuse_statement_rows(cluster, by_id):
        session.add(row)


def record_decision(
    session: Session,
    cluster: ResolvedCluster,
    *,
    reason: str,
    decided_by: str = "auto:resolver",
) -> None:
    """``session.add`` one :class:`DecisionRecord` for a promoted merge (caller commits).

    Guard: **no-op for singletons** (``cluster.is_merge=False``) — calling this for a
    singleton is safe and writes nothing (P-STMT-3b invariant). Only promoted merges
    (≥2 members) write a decision row.

    Append-only: ``supersedes`` / ``superseded_by`` stay ``NULL`` in step 1 (the
    un-merge / belief-revision write path is a Gate 3 concern, ADR 0099 §Deferred).
    ``decided_by`` DEFAULTS to ``"auto:resolver"`` — the automated decider identity,
    keeping the pipeline's call byte-behaviour-identical. The reserved human-decision
    path (Gate P3 / ADR 0108) passes ``decided_by=f"operator:{approver}"`` from
    ``resolution/signoff.py``'s ``approve()`` so a rebuild can attribute a merge to the
    human who approved it, distinct from the automated resolver's decisions.
    """
    if not cluster.is_merge:
        return  # no-op for singletons — safe to call unconditionally (P-STMT-3b)

    session.add(
        DecisionRecord(
            id=str(uuid.uuid4()),
            canonical_id=cluster.canonical_id,
            kind="merge",
            member_ids=list(cluster.member_ids),
            score=cluster.score,
            decided_by=decided_by,
            evidence={"reason": reason} if reason else None,
            supersedes=None,
            superseded_by=None,
            scope="default",
        )
    )


def fuse_context_claim_rows(
    canonical_id: str,
    members: Iterable[FtmEntity],
) -> list[ContextClaimRecord]:
    """Build one :class:`ContextClaimRecord` per ``(member, anchor key, value)`` claim.

    Per-member grain (ADR 0106 §1/§3): NOT the merged ``cluster.entity`` — capturing from the
    merged entity would lose the per-member ``dataset`` attribution the P2 erasure scrub needs
    and would fold a cross-member anchor conflict prematurely (``merge_context`` union would
    make :func:`~worldmonitor.ontology.anchors.get_anchors` OMIT a conflicting key before it is
    ever captured). Each member's OWN (single-valued) anchors are read directly.

    A member with no stamped :class:`~worldmonitor.provenance.model.Provenance`, an empty
    ``retrieved_at``, or an empty ``source_id`` (ADR 0112 rider-1: source-unreachable — P2's
    erasure scrub reaches rows by ``dataset == source_id``) has its anchors **skipped and
    logged** — never written naked (INV-CTX-PROV). A member with no anchors yields zero rows.

    This is the SINGLE authoritative projection — consumed by both the persist path
    (:func:`record_context_claims`) and the P-CTX-1 property-test oracle-independence check.
    """
    rows: list[ContextClaimRecord] = []
    for member in members:
        prov = get_provenance(member)
        if prov is None or not prov.retrieved_at or not prov.source_id:
            logger.warning(
                "context_claim: skipping anchor capture for entity_id=%r (canonical_id=%r) — "
                "no stamped provenance / no retrieved_at / empty source_id (ADR 0106 §3 / "
                "ADR 0112 rider-1, never written naked or source-unreachable)",
                member.id,
                canonical_id,
            )
            continue

        dataset = prov.source_id
        entity_id = member.id or canonical_id
        for field, value in get_anchors(member).items():
            rows.append(
                ContextClaimRecord(
                    id=str(uuid.uuid4()),
                    canonical_id=canonical_id,
                    entity_id=entity_id,
                    key=field,
                    value=value,
                    dataset=dataset,
                    method=_CONNECTOR_MAP_METHOD,
                    retrieved_at=prov.retrieved_at,
                    scope="default",
                )
            )
    return rows


def record_context_claims(
    session: Session,
    canonical_id: str,
    members: Iterable[FtmEntity],
) -> None:
    """``session.add`` each context-claim row from :func:`fuse_context_claim_rows` (caller commits).

    Append-only: pure ``session.add``; no UPDATE or DELETE, ever. Additive evidence banking
    only — mutates no FtM entity and writes to no other lane (INV-CTX-NONMUTATION /
    INV-CTX-APPENDONLY, ADR 0106 §2.a.4).
    """
    for row in fuse_context_claim_rows(canonical_id, members):
        session.add(row)
