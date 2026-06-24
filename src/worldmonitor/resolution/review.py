"""Catastrophic-merge guard — never auto-merge sensitive or oversized clusters.

CLAUDE.md: *multiple independent agreements before merging; human review for
high-impact merges; never auto-merge a sensitive entity.* A merge goes to the
human review queue (not auto-promoted) when the cluster is large (>10 sources), any
member is a PEP / sanctioned / criminal entity, or its members carry CONFLICTING
single-valued canonical anchors (Gate B-5 / ADR 0040, fork (C) HYBRID).
"""

from __future__ import annotations

from collections.abc import Mapping

from worldmonitor.ontology.anchors import anchor_conflicts_across
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
    # quiet=True: schemata without a `topics` property (e.g. Sanction) -> no topics.
    topics = set(entity.get("topics", quiet=True))
    if topics & SENSITIVE_TOPICS:
        return True
    return any(topic.startswith("role.pep") or topic.startswith("sanction") for topic in topics)


def needs_review(cluster: ResolvedCluster, by_id: Mapping[str, FtmEntity]) -> tuple[bool, str]:
    """Return ``(flagged, reason)`` for a cluster under the catastrophic-merge guard.

    Flags (parks) a merged cluster when it is oversized (> ``MAX_AUTO_MERGE_SIZE`` sources), any
    member is sensitive (PEP / sanctioned / criminal), or its members carry CONFLICTING
    single-valued canonical anchors (Gate B-5 / ADR 0040, fork (C) HYBRID). Singletons are never
    flagged (nothing is being merged).
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
    # Anchor-conflict park (Gate B-5 / ADR 0040, fork (C) defense-in-depth). Computed over the
    # cluster's SOURCE members (``by_id``), NOT the merged ``cluster.entity`` whose merge_context
    # would union the conflicting values and ``get_anchors`` would mask (that masking is Finding 1).
    # This also catches the TRANSITIVE conflict pairwise scoring misses (A~M~Z via clean bridges).
    members = [member for mid in cluster.member_ids if (member := by_id.get(mid)) is not None]
    conflicts = anchor_conflicts_across(members)
    if conflicts:
        # Deterministic, human-readable lead: name each conflicting field and its distinct values.
        detail = "; ".join(
            f"{field}: {', '.join(values)}" for field, values in sorted(conflicts.items())
        )
        return True, f"members carry conflicting canonical anchors -> {detail}"
    return False, ""
