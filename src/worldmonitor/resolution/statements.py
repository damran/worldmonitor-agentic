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

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from worldmonitor.db.models import DecisionRecord, StatementRecord
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.provenance.model import get_provenance
from worldmonitor.resolution.merge import ResolvedCluster, fuse_statement_entity


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
    off the ``Statement``.  Returns ``[]`` when the fusion yields nothing.

    This is the SINGLE authoritative projection — consumed by both the persist path
    (``record_statements``) and the property test (P-STMT-1 oracle independence check).
    """
    # Build a string-keyed subset for the fusion call (pipeline's by_id may have None keys)
    str_by_id: dict[str, FtmEntity] = {k: v for k, v in by_id.items() if k is not None}
    fused = fuse_statement_entity(cluster.canonical_id, list(cluster.member_ids), str_by_id)
    if fused is None:
        return []

    rows: list[StatementRecord] = []
    for statement in fused.statements:
        if statement.prop == "id":
            continue  # exclude the id pseudo-property (carries construction Dataset, not a source)

        # Enrich reliability from the contributing member's Provenance (G1 — real, not invented).
        # ``entity_id`` from _member_statements is set to ``member.id or canonical_id``.
        member = str_by_id.get(statement.entity_id)
        reliability: str | None = None
        if member is not None:
            prov = get_provenance(member)
            if prov is not None:
                reliability = prov.reliability

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
                reliability=reliability,
                retrieved_at=statement.first_seen,
                raw_pointer=statement.origin,
                first_seen=statement.first_seen,
                last_seen=statement.last_seen,
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
) -> None:
    """``session.add`` one :class:`DecisionRecord` for a promoted merge (caller commits).

    Guard: **no-op for singletons** (``cluster.is_merge=False``) — calling this for a
    singleton is safe and writes nothing (P-STMT-3b invariant). Only promoted merges
    (≥2 members) write a decision row.

    Append-only: ``supersedes`` / ``superseded_by`` stay ``NULL`` in step 1 (the
    un-merge / belief-revision write path is a Gate 3 concern, ADR 0099 §Deferred).
    ``decided_by`` is ``"auto:resolver"`` — the automated decider identity; the
    human-decision path (``decided_by=<operator>``) is a later gate.
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
            decided_by="auto:resolver",
            evidence={"reason": reason} if reason else None,
            supersedes=None,
            superseded_by=None,
            scope="default",
        )
    )
