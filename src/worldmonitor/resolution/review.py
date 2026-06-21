"""Catastrophic-merge guard — never auto-merge sensitive or oversized clusters.

CLAUDE.md: *multiple independent agreements before merging; human review for
high-impact merges; never auto-merge a sensitive entity.* A merge goes to the
human review queue (not auto-promoted) when the cluster is large (>10 sources) or
any member is a PEP / sanctioned / criminal entity.
"""

from __future__ import annotations

from collections.abc import Mapping

from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.resolution.merge import ResolvedCluster

# Maximum number of source entities a cluster may collapse without human review.
MAX_AUTO_MERGE_SIZE = 10

# OpenSanctions topics that mark an entity as high-sensitivity.
SENSITIVE_TOPICS = frozenset(
    {"sanction", "sanction.linked", "poi", "crime", "crime.fraud", "crime.terror", "wanted"}
)


def is_sensitive(entity: FtmEntity) -> bool:
    """True if the entity is a PEP, sanctioned, or otherwise flagged."""
    topics = set(entity.get("topics"))
    if topics & SENSITIVE_TOPICS:
        return True
    return any(topic.startswith("role.pep") or topic.startswith("sanction") for topic in topics)


def needs_review(cluster: ResolvedCluster, by_id: Mapping[str, FtmEntity]) -> tuple[bool, str]:
    """Return ``(flagged, reason)`` for a cluster under the catastrophic-merge guard.

    Singletons are never flagged (nothing is being merged).
    """
    if not cluster.is_merge:
        return False, ""
    if len(cluster.member_ids) > MAX_AUTO_MERGE_SIZE:
        return (
            True,
            f"cluster of {len(cluster.member_ids)} exceeds auto-merge limit {MAX_AUTO_MERGE_SIZE}",
        )
    for member_id in cluster.member_ids:
        member = by_id.get(member_id)
        if member is not None and is_sensitive(member):
            return True, f"member {member_id} is a sensitive (PEP/sanctioned) entity"
    return False, ""
