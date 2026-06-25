"""Gate E (slice-1) — fail-closed sensitivity guard: topics-first deny-by-default.

ADR ``docs/decisions/0047-fail-closed-sensitivity-guard.md`` inverts ADR 0020's hand-maintained
``SENSITIVE_TOPICS`` denylist (which fails OPEN — it caught only 10 of FtM 4.9.2's 28
``registry.topic.RISKS`` codes) to **deny-by-default**: a cluster is held for review unless it is
provably benign.

Stage 1 (this slice — PURE, no graph): a cluster member is sensitive iff

* it carries any of FtM's own counterparty-risk codes (``topic_codes & registry.topic.RISKS``
  non-empty — catches all 28, tracking the FtM pin automatically), **OR**
* it carries an **off-ontology** topic code unknown to ``registry.topic.names`` (an enricher / CTI /
  crypto vocabulary the FtM model has never seen) — *unknown ⇒ sensitive*, the inversion hinge
  (ADR 0047 Decision 2).

The risk set is loaded PROGRAMMATICALLY from FtM (``from followthemoney.types import registry``);
there is no hand-maintained denylist. Stage 2 (k-hop graph sensitivity) and Stage 3 (Chow abstain
band) are slice-2: ``needs_review`` accepts a keyword-only ``neo4j`` handle (defaulting ``None``) so
the pure unit path is unchanged, and Stage 2 is a no-op until slice-2 wires it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from followthemoney.types import registry

from worldmonitor.ontology.anchors import anchor_conflicts_across
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.resolution.merge import ResolvedCluster

if TYPE_CHECKING:  # pragma: no cover - typing-only import; avoids a runtime graph dependency
    from worldmonitor.graph.neo4j_client import Neo4jClient

# Maximum number of source entities a cluster may collapse without human review (ADR 0020 size half,
# G5 — NOT inverted by this gate; conservative-by-default).
MAX_AUTO_MERGE_SIZE = 10

# The LEGACY ADR-0020 denylist (review.py:22, now deleted as a sensitivity source of truth). It is
# retained here ONLY to model "what an approving human could already have understood as sensitive"
# for the reason-scoped approved-group-exemption fence (ADR 0047 Decision 5, choice A) — NOT to
# decide sensitivity (that is the programmatic registry.topic.RISKS path in is_sensitive). A code in
# this set (or matching the role.pep*/sanction* prefix rule) was caught by the OLD guard, so a prior
# approval of such a cluster was made WITH the sensitivity visible — it is not a STALE exemption and
# stays exemptible. Deny-by-default never reads this set; see is_newly_broadened_sensitive.
_LEGACY_SENSITIVE_TOPICS = frozenset(
    {"sanction", "sanction.linked", "poi", "crime", "crime.fraud", "crime.terror", "wanted"}
)


def _legacy_is_sensitive(entity: FtmEntity) -> bool:
    """Replay the deleted ADR-0020 guard: was this entity sensitive to the OLD denylist + prefix?

    Used solely by the reason-scoped exemption fence (ADR 0047 Decision 5) to tell a sensitivity an
    approval COULD have considered (legacy-caught) from one it could not (newly-broadened). This is
    NOT the sensitivity decision — :func:`is_sensitive` (programmatic ``registry.topic.RISKS``) is.
    """
    topic_codes = set(entity.get("topics", quiet=True))
    if topic_codes & _LEGACY_SENSITIVE_TOPICS:
        return True
    return any(code.startswith("role.pep") or code.startswith("sanction") for code in topic_codes)


def is_newly_broadened_sensitive(entity: FtmEntity) -> bool:
    """True iff ``entity`` is sensitive under deny-by-default but was MISSED by the legacy denylist.

    This is the reason-scoped fence for the approved-group exemption (ADR 0047 Decision 5). It
    implements the user's decision *"re-review a newly-detected sensitivity once"*: a sensitivity
    the legacy guard MISSED (e.g. ``role.rca`` / ``crime.war`` / an off-ontology code) could NOT
    have been considered by a prior approval, so a stale approval must NOT un-flag it — the cluster
    re-parks for a fresh human look. A sensitivity the legacy guard ALREADY caught (e.g.
    ``sanction``) WAS visible at approval time and stays exemptible (preserving the existing
    approve->promote path). The mechanism is scoped to legacy-visibility: an already-reviewable
    sensitivity is not re-parked, which is why this is "once," not forever-churn.

    KNOWN PROPERTY (intended, conservative — NOT a bug): on RE-INGEST / re-resolution of a
    newly-broadened sensitive cluster the fence still fires (it keys on legacy-visibility, not on
    "was ever approved"), so a re-ingested newly-sensitive entity is re-reviewed each time it
    re-forms. That is the deliberate fail-closed posture; the alternative (recording per-approval
    review rationale) is the heavier reason-scoped variant explicitly deferred in ADR 0047.
    """
    return is_sensitive(entity) and not _legacy_is_sensitive(entity)


def is_sensitive(entity: FtmEntity) -> bool:
    """True if ``entity`` carries any FtM risk topic or an off-ontology (unknown) topic code.

    Deny-by-default (ADR 0047). ``registry.topic.RISKS`` is FtM's own counterparty-risk tag (28
    codes in FtM 4.9.2), loaded programmatically so a pin bump tracks automatically; a code not in
    ``registry.topic.names`` at all is treated as sensitive (unknown ⇒ sensitive). This subsumes and
    replaces the legacy ``SENSITIVE_TOPICS`` denylist + the ``role.pep*``/``sanction*`` prefix rule
    (every code they matched is in ``RISKS``).
    """
    # quiet=True: schemata without a `topics` property (e.g. Sanction) -> no topics, no raise.
    topic_codes = set(entity.get("topics", quiet=True))
    if not topic_codes:
        return False
    if topic_codes & registry.topic.RISKS:
        return True
    # Off-ontology: any code FtM has never seen is sensitive (the inversion hinge).
    return any(code not in registry.topic.names for code in topic_codes)


def needs_review(
    cluster: ResolvedCluster,
    by_id: Mapping[str, FtmEntity],
    *,
    neo4j: Neo4jClient | None = None,
) -> tuple[bool, str]:
    """Return ``(flagged, reason)`` for a cluster under the catastrophic-merge guard.

    Flags (parks) a merged cluster when it is oversized (> ``MAX_AUTO_MERGE_SIZE`` sources), any
    member is sensitive (deny-by-default — :func:`is_sensitive`), or its members carry CONFLICTING
    single-valued canonical anchors (Gate B-5 / ADR 0040, fork (C) HYBRID). Singletons are never
    flagged (nothing is being merged).

    ``neo4j`` is a keyword-only handle for slice-2's Stage-2 k-hop graph sensitivity; in slice-1 it
    is accepted but unused (Stage 2 is a no-op), so the pure Stage-1 path is unchanged whether or
    not a graph client is threaded in.
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
