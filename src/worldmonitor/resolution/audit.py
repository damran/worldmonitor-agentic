"""Persist resolution decisions to the merge-audit trail."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from worldmonitor.db.models import MergeAlert, MergeAudit
from worldmonitor.resolution.merge import ResolvedCluster


def record_merge(
    session: Session,
    cluster: ResolvedCluster,
    *,
    decision: str,
    reason: str = "",
) -> MergeAudit:
    """Write one :class:`MergeAudit` row for a resolution decision (caller commits)."""
    audit = MergeAudit(
        id=str(uuid.uuid4()),
        canonical_id=cluster.canonical_id,
        source_ids=list(cluster.member_ids),
        score=cluster.score,
        decision=decision,
        reason=reason,
    )
    session.add(audit)
    return audit


def record_merge_alert(
    session: Session,
    cluster: ResolvedCluster,
    *,
    reason: str,
) -> MergeAlert:
    """Write one :class:`MergeAlert` row for a flagged-but-merged cluster (caller commits).

    Used only in ``MERGE_GUARD_MODE="alert"`` (ADR 0024): the durable trail of
    every catastrophic-guard-flagged cluster that was merged anyway.
    """
    alert = MergeAlert(
        id=str(uuid.uuid4()),
        canonical_id=cluster.canonical_id,
        source_ids=list(cluster.member_ids),
        reason=reason,
        score=cluster.score,
    )
    session.add(alert)
    return alert
