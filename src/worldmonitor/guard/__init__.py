"""Fail-closed sensitivity guard (Gate E).

The guard owns the catastrophic-merge sensitivity decision: it inverts ADR 0020's
hand-maintained ``SENSITIVE_TOPICS`` denylist (fail-open) to **deny-by-default**
(``hold-unless-provably-benign``). ``resolution/review.py`` delegates into it.

Slice-1 ships Stage 1 (topics-first, pure, no graph): a member is sensitive iff it
carries any FtM ``registry.topic.RISKS`` code OR an off-ontology topic code unknown to
``registry.topic.names`` (unknown ⇒ sensitive). See
``docs/decisions/0047-fail-closed-sensitivity-guard.md`` and
``docs/reviews/GATE_E_SENSITIVITY_GUARD_SPEC.md``.
"""

from __future__ import annotations

from worldmonitor.guard.sensitivity import is_sensitive, needs_review

__all__ = ["is_sensitive", "needs_review"]
