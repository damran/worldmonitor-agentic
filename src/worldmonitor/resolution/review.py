"""Catastrophic-merge guard — never auto-merge sensitive or oversized clusters.

CLAUDE.md: *multiple independent agreements before merging; human review for
high-impact merges; never auto-merge a sensitive entity.* A merge goes to the
human review queue (not auto-promoted) when the cluster is large (>10 sources), any
member is sensitive, or its members carry CONFLICTING single-valued canonical anchors
(Gate B-5 / ADR 0040, fork (C) HYBRID).

The **sensitivity** axis is now owned by ``worldmonitor.guard.sensitivity`` (Gate E / ADR 0047):
this module's ``is_sensitive`` / ``needs_review`` **delegate** into it. The legacy hardcoded
``SENSITIVE_TOPICS`` denylist (fail-open — caught only 10 of FtM's 28 risk codes) is **deleted**;
the guard now reads FtM's own ``registry.topic.RISKS`` programmatically and treats any off-ontology
topic code as sensitive (deny-by-default). The size park (``MAX_AUTO_MERGE_SIZE``) and the
anchor-conflict park (ADR 0040) are unchanged — both live in the guard's ``needs_review``.
"""

from __future__ import annotations

from worldmonitor.guard.sensitivity import (
    MAX_AUTO_MERGE_SIZE,
    is_newly_broadened_sensitive,
    is_sensitive,
    needs_review,
)

__all__ = [
    "MAX_AUTO_MERGE_SIZE",
    "is_newly_broadened_sensitive",
    "is_sensitive",
    "needs_review",
]
