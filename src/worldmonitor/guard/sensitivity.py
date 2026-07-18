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
from functools import lru_cache
from typing import TYPE_CHECKING

from followthemoney.types import registry

from worldmonitor.ontology.anchors import anchor_conflicts_across
from worldmonitor.ontology.ftm import FtmEntity
from worldmonitor.resolution.merge import ResolvedCluster
from worldmonitor.settings import get_settings

if TYPE_CHECKING:  # pragma: no cover - typing-only import; avoids a runtime graph dependency
    from worldmonitor.graph.neo4j_client import Neo4jClient

# Maximum number of source entities a cluster may collapse without human review (ADR 0020 size half,
# G5 — NOT inverted by this gate; conservative-by-default).
MAX_AUTO_MERGE_SIZE = 10

# Non-exemptibility (the approved-group-exemption fence in ``pipeline.py``) is computed by the
# STRUCTURED probe :func:`has_nonexemptible_sensitivity` (Gate E slice-3; spec §15 / ADR 0047
# Decision 5 refinement), NOT by substring-matching ``needs_review``'s returned reason. The probe
# evaluates ALL THREE newly-detectable sensitivity signals a prior approval could not have
# considered — a newly-broadened TOPIC (:func:`is_newly_broadened_sensitive`), Stage-2 k-hop graph
# proximity (:func:`_risk_within_khop`), and the Stage-3 Chow abstain band — INDEPENDENTLY of
# ``needs_review``'s first-flag short-circuit, so an exemptible-first flag (size / anchor-conflict /
# legacy-caught topic) can never MASK a co-occurring non-exemptible signal (Finding B / E-MASK). The
# human-readable Stage-2/Stage-3 reason strings stay (they are the audit / ``record_merge`` reason)
# but are NO LONGER load-bearing for the fence — the deleted ``is_nonexemptible_reason`` + marker
# constants substring-matched a free-text reason that also embeds hostile data-bearing fields
# (``member_id``, anchor VALUES — Finding F), which the structured probe removes entirely.


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
    ``registry.topic.names`` at all is treated as sensitive (unknown ⇒ sensitive).

    A topic ``code`` is sensitive iff (a) ``code ∈ registry.topic.RISKS``, OR (b) a **DOT-ANCESTOR**
    of ``code`` is a RISKS code (a sub-classification inherits its parent's risk, e.g.
    ``role.pep.natl`` → ``role.pep`` ∈ RISKS), OR (c) ``code ∉ registry.topic.names``. Clause (b) is
    the PEP/sub-code fix (ADR 0047 Post-merge fix, 2026-06-26): exact ``& RISKS`` membership
    silently dropped the RISKS-parented sub-codes (``role.pep.natl/intl/frmr``, ``crime.cyber``,
    ``crime.env``, ``crime.traffick.drug/human``), which live in ``registry.topic.names`` but not in
    ``RISKS`` — so the unknown-hinge missed them too. This corrects Decision 1's false *"every code
    they matched is in RISKS"* claim (the legacy ``role.pep*`` PREFIX matched those sub-codes; they
    are NOT in ``RISKS``). The risk source stays programmatic, so a future RISKS-parented sub-code
    is covered with no code change.
    """
    # quiet=True: schemata without a `topics` property (e.g. Sanction) -> no topics, no raise.
    topic_codes = set(entity.get("topics", quiet=True))
    if not topic_codes:
        return False
    if topic_codes & registry.topic.RISKS:
        return True
    # A sub-classification of a risk topic inherits its parent's risk: a code whose dot-ANCESTOR is
    # a RISKS code (e.g. role.pep.natl -> role.pep) is sensitive, even though FtM lists it in names
    # but not in `RISKS`. Restores the legacy role.pep*/sanction* prefix coverage the exact-set
    # check dropped (cross-line audit vs Workflow A; corrects ADR 0047's "every code in RISKS"
    # claim). The trailing-dot `r + "."` makes it a TRUE ancestor ('role.pepXYZ' would NOT match).
    if any(
        code == r or code.startswith(r + ".") for code in topic_codes for r in registry.topic.RISKS
    ):
        return True
    # Off-ontology: any code FtM has never seen is sensitive (the inversion hinge).
    return any(code not in registry.topic.names for code in topic_codes)


@lru_cache(maxsize=1)
def _risk_labels() -> tuple[str, ...]:
    """The Neo4j node labels ftmg assigns to FtM ``registry.topic.RISKS`` codes (Config-derived).

    ftmg encodes a topic code as a PascalCase node label via ``generate_topic_labels`` /
    ``config.nodes.topics[code].label`` (e.g. ``sanction → Sanction``, ``crime.war → CrimeWar``,
    ``export.control.linked → ExportControlLinked``); ``gds.py:27`` keys ``is_sanctioned`` off the
    ``"Sanction"`` label. We build the SAME ftmg ``Configuration`` the writer builds
    (``graph/writer.py`` :func:`write_entities`) and read each RISKS code's label off it, so the
    Stage-2 k-hop disjunction TRACKS the FtM/ftmg pin automatically and never hardcodes the casing
    (VERIFIED_API.md "Gate E" k-hop label record). Imported lazily so a pure Stage-1 unit run never
    pulls in the graph/ftmg dependency; cached because the label set is immutable for the process.
    """
    from pathlib import Path

    from ftmg.config import Configuration, DatabaseConfig  # type: ignore[import-untyped]

    # The Configuration's db creds drive only label/transform logic; no connection is opened here
    # (we read the static topic-label map, never the driver). Built identically to graph/writer.py's
    # ``_ftmg_config`` so the Stage-2 risk labels match exactly what the writer projects onto nodes.
    settings = get_settings()
    pw = settings.neo4j_password.get_secret_value()
    config = Configuration(
        path=Path("."),  # unused: we read config.nodes.topics, never load_entities()
        db=DatabaseConfig(url=settings.neo4j_uri, username=settings.neo4j_user, password=pw),
    )
    return tuple(sorted({config.nodes.topics[code].label for code in registry.topic.RISKS}))


def _risk_within_khop(neo4j: Neo4jClient, member_id: str, depth: int) -> bool:
    """True iff a non-ghost risk-labelled node lies within ``depth`` hops of ``member_id``.

    Stage 2 (Gate E / ADR 0047 Decision 3.2). ``depth`` is the int-validated in-code config
    constant ``settings.sensitivity_khop_depth`` — it is **f-string-INLINED** into the ``[*1..K]``
    variable-length bound (Neo4j forbids a ``$param`` there; inlining a validated ``int`` keeps
    ``execute_read``'s ``LiteralString`` cast sound). The matched durable ``member_id`` is passed as
    a ``$param`` (it is data, never interpolated — DENY E-CYPHER).

    ``:Ghost`` exclusion (HARD INV, ADR 0046 / spec §3.3): a ghost neither flags NOR bridges. The
    path predicate ``NONE(g IN nodes(p) WHERE g:Ghost)`` excludes a ghost on EVERY node along the
    path, so a ghost is never a sensitivity signal AND no path traverses THROUGH one (terminate-at,
    never through). A ``member_id`` not present in the graph ⇒ the MATCH binds nothing ⇒
    ``count == 0`` ⇒ clean no-flag, never an error (k-hop runs before ``write_entities`` — see
    the VERIFIED_API.md ordering note, T5d).
    """
    # Validate it is a plain int BEFORE inlining into the [*1..K] bound (config, never external
    # input). ``type(depth) is not int`` rejects a bool (an int subclass — ``[*1..True]`` would be
    # nonsense) and any non-int slipping past typing, keeping execute_read's LiteralString sound.
    if type(depth) is not int:
        raise TypeError(
            "sensitivity_khop_depth must be an int (it is inlined into the Cypher bound)"
        )
    if depth <= 0:  # Stage-2 kill-switch (k == 0): member node only — Stage 1 already read it.
        return False
    risk_label_pred = " OR ".join(f"r:{label}" for label in _risk_labels())
    # ``depth`` is the validated in-code int inlined into the var-length bound; ``$id`` is data.
    query = (
        f"MATCH p = (n {{id: $id}})-[*1..{depth}]-(r) "
        f"WHERE NONE(g IN nodes(p) WHERE g:Ghost) AND ({risk_label_pred}) "
        "RETURN count(r) > 0 AS flagged"
    )
    rows = neo4j.execute_read(query, id=member_id)
    return bool(rows[0]["flagged"]) if rows else False


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

    Stage 1 (topics — pure) runs FIRST and unconditionally. When a ``neo4j`` handle is threaded in
    AND ``settings.sensitivity_khop_depth > 0``, Stage 2 (k-hop graph sensitivity) flags a member
    structurally adjacent to a non-ghost risk node (ADR 0047 Decision 3.2). Stage 3 (the Chow 1970
    abstain band over ``ResolvedCluster.score``) parks a marginal-confidence cluster that survives
    Stages 1-2 when its score falls in the configured ``[abstain_low, abstain_high)`` band — a
    DISTINCT park-vs-auto-merge axis on an already-formed cluster, NEVER the merge threshold. The
    pure ``neo4j=None`` path is byte-identical to slice-1 plus the (default-OFF) band check.
    """
    if not cluster.is_merge:
        return False, ""
    if len(cluster.member_ids) > MAX_AUTO_MERGE_SIZE:
        return (
            True,
            f"cluster of {len(cluster.member_ids)} exceeds auto-merge limit {MAX_AUTO_MERGE_SIZE}",
        )
    # Stage 1 — topics-first (pure, no graph), runs unconditionally before the graph stage.
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

    settings = get_settings()

    # Stage 2 — k-hop graph sensitivity (Neo4j). Skipped when no graph handle is threaded in (the
    # pure unit path) or when the kill-switch is set (k == 0). Closes the edge-less / structural
    # fail-open: a topic-clean member within k hops of a non-ghost risk node is flagged. Reason is
    # DISTINCT from the Stage-1 topic reason (VERIFIED_API.md) so the exemption fence treats it as a
    # non-legacy-visible (NOT stale-exemptible) sensitivity.
    if neo4j is not None and settings.sensitivity_khop_depth > 0:
        for member_id in cluster.member_ids:
            if _risk_within_khop(neo4j, member_id, settings.sensitivity_khop_depth):
                return (
                    True,
                    f"member {member_id} is within {settings.sensitivity_khop_depth} "
                    "graph hop(s) of a risk-labelled node",
                )

    # Stage 3 — Chow (1970) reject-option abstain band over the cluster's ALREADY-COMPUTED score.
    # A merged, non-oversized, anchor-clean, not-otherwise-flagged cluster whose weakest-link match
    # probability is MARGINAL parks for review. Half-open ``[low, high)``: inclusive low, exclusive
    # high. The default ``low == high == 0.92`` is an empty interval (band OFF) — no NEW park. This
    # is a DISTINCT axis from the merge threshold (spec §3.4 / DENY E-THRESHOLD): it reads, never
    # shifts, ``DEFAULT_MERGE_THRESHOLD`` (it does not read it at all).
    if settings.sensitivity_abstain_low <= cluster.score < settings.sensitivity_abstain_high:
        return (
            True,
            f"cluster score {cluster.score:.3f} is in the marginal-confidence abstain band "
            f"[{settings.sensitivity_abstain_low}, {settings.sensitivity_abstain_high})",
        )
    return False, ""


def has_nonexemptible_sensitivity(
    cluster: ResolvedCluster,
    by_id: Mapping[str, FtmEntity],
    *,
    neo4j: Neo4jClient | None = None,
) -> bool:
    """True iff the cluster carries a sensitivity a prior identity-approval could NOT have
    considered — evaluated INDEPENDENTLY of needs_review's first-flag short-circuit (spec §15;
    ADR 0047 Dec 5 refinement). Newly-broadened TOPIC sensitivity OR Stage-2 k-hop graph
    proximity OR the Stage-3 Chow abstain band. Size, anchor-conflict, and legacy-caught topic
    flags are EXEMPTIBLE and never make this True. Strictly stricter -> person-NEUTRAL."""
    for member_id in cluster.member_ids:
        member = by_id.get(member_id)
        if member is not None and is_newly_broadened_sensitive(member):
            return True
    settings = get_settings()
    if neo4j is not None and settings.sensitivity_khop_depth > 0:
        for member_id in cluster.member_ids:
            if _risk_within_khop(neo4j, member_id, settings.sensitivity_khop_depth):
                return True
    return settings.sensitivity_abstain_low <= cluster.score < settings.sensitivity_abstain_high
