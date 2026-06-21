"""Persist resolution decisions to the merge-audit trail."""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from worldmonitor.db.models import MergeAudit
from worldmonitor.resolution.merge import ResolvedCluster


def record_merge(
    session: Session,
    cluster: ResolvedCluster,
    *,
    tenant_id: str,
    decision: str,
    reason: str = "",
) -> MergeAudit:
    """Write one :class:`MergeAudit` row for a resolution decision (caller commits)."""
    audit = MergeAudit(
        id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        canonical_id=cluster.canonical_id,
        source_ids=list(cluster.member_ids),
        score=cluster.score,
        decision=decision,
        reason=reason,
    )
    session.add(audit)
    return audit
