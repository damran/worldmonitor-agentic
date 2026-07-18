"""Gate B-4a slice-2 — cross-store GDPR source erasure (``erase_source``, ADR 0049).

``erase_source(*, neo4j, session, landing, source_id, authorized_by)`` removes ONE source's
contribution from all four stores a person's record lands in — the landing zone (MinIO), the ER
queue (``er_queue_item.raw_entity``), the dead-letter table (``ingest_dead_letter``), and the Neo4j
graph — idempotently, source-scoped, runtime-authorized, and audited via one
``TaskRun(kind="erase")`` row. It is the **one sanctioned exception to append-only** (CLAUDE.md /
ADR 0045 §4): it performs only the GDPR-mandated removal of the erased source's own data while
**never** touching the ``canonical_id_ledger``, ``ResolverJudgement`` / ``SignOff`` / ``MergeAudit``
rows, or re-clustering / splitting / resurrecting a survivor (no-un-merge sub-invariant preserved).

``authorized_by`` is a REQUIRED keyword-only argument (no default): erasure deletes a real person's
data — legitimate as the subject's GDPR right, catastrophic as an evasion vector — so each run names
the human operator who authorized it, recorded in the audit row. The function is never wired into
any autonomous / agent path (the Phase-2 API/MCP surface sits behind a Zitadel operator role).

Gate P2 (ADR 0107) additionally reaches the Gate-2a/P1 SoR log spine: after the existing
``erase_source_graph`` prune, :func:`~worldmonitor.resolution.erasure_scrub.scrub_log_lanes`
DELETEs the erased source's ``statement``/``context_claim`` rows + redacts ``decision.member_ids``
(staged on ``session``, caller commits — the same split as the queue/dead-letter redaction below),
then :func:`~worldmonitor.resolution.erasure_scrub.prune_live_to_fold` completes the live-graph
value/anchor removal on every touched survivor (Neo4j, immediate). See
``worldmonitor.resolution.erasure_scrub``'s module docstring for the cross-store non-atomicity +
idempotent-retry-recovers contract this ordering relies on.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem, IngestDeadLetter, TaskRun
from worldmonitor.graph.neo4j_client import Neo4jClient
from worldmonitor.graph.ops import erase_source_graph
from worldmonitor.ontology.ftm import make_entity
from worldmonitor.provenance.model import get_provenance
from worldmonitor.resolution.erasure_scrub import prune_live_to_fold, scrub_log_lanes

# The single source of truth for the landing-key path sanitizer — REUSED, never duplicated (spec
# §4.1), so the derived erase prefix is provably a true prefix of the real ingest key.
from worldmonitor.runner.ingest import _safe_segment  # pyright: ignore[reportPrivateUsage]
from worldmonitor.settings import get_settings
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ErasureResult:
    """Per-store counts from one cross-store erase — the audit ``TaskRun.stats`` payload (§4.4).

    The field order is the locked audit-stats key order (``source_id``, ``authorized_by``, then the
    seven per-store counts): :meth:`as_dict` is what lands in ``TaskRun.stats``. Gate P2 (ADR 0107)
    extends this ADDITIVELY — the four SoR-log scrub counts are APPENDED after the original seven;
    no existing field is renamed, reordered, or removed (``test_erasure.py``'s locked
    ``_STATS_KEYS``/``_COUNT_KEYS`` stay satisfied, including the idempotent second-erase
    zero-count run).
    """

    source_id: str
    authorized_by: str
    nodes_deleted: int
    nodes_pruned: int
    props_retracted: int
    edges_deleted: int
    queue_rows_redacted: int
    landing_objects_deleted: int
    dead_letters_redacted: int
    # Gate P2 (ADR 0107) — additive SoR-log scrub counts (§4.4 extension, spec _SCRUB_COUNT_KEYS).
    statements_scrubbed: int = 0
    context_claims_scrubbed: int = 0
    decisions_redacted: int = 0
    survivors_value_pruned: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Serialize to the non-PII audit-stats mapping (a dataset name, an operator id, counts)."""
        return asdict(self)


def _landing_prefix(source_id: str) -> str:
    """Derive a source's landing-object prefix, mirroring the ingest key scheme (single source).

    A provenance ``source_id`` is ``"{connector_id}:{dataset}"``; the ingest runner lands a record
    at ``"{connector_id}/{_safe_segment(dataset)}/{_safe_segment(key)}.json"`` (``runner/ingest``).
    This REUSES that ``_safe_segment`` sanitizer (no duplicated sanitizer — the derived prefix is a
    TRUE prefix of the real ingest key), and ``/``-terminates so a prefix delete is collision-safe
    (erasing ``"ofac:sdn"`` → ``"ofac/sdn/"`` never sweeps ``"ofac-eu:sdn"`` → ``"ofac-eu/sdn/"``).

    OVER-DELETE GUARD: a ``source_id`` without BOTH a non-empty ``connector_id`` AND a non-empty
    ``dataset`` is REFUSED. A bare ``connector_id`` (no ``':'``), an empty dataset (``"conn:"`` —
    the trailing-colon case), an empty connector (``":ds"``), or ``""`` would each derive a
    connector-wide ``"connector/"`` (or whole-bucket ``"/"``) prefix sweeping EVERY dataset under
    that connector — never an intended GDPR erasure scope, so it raises rather than over-deleting.
    """
    connector_id, sep, dataset = source_id.partition(":")
    if not sep or not connector_id or not dataset.strip():
        raise ValueError(
            "erase: source_id must be '<connector_id>:<dataset>' with a non-empty connector AND "
            f"dataset; refusing the connector-wide / whole-bucket prefix {source_id!r} would derive"
        )
    return "/".join([connector_id, _safe_segment(dataset)]) + "/"


def _redact_queue(session: Session, source_id: str) -> int:
    """Redact ``ErQueueItem.raw_entity`` to a non-PII shell for the source's rows; keep the shell.

    A row's source is decided by parsing its ``raw_entity`` back into an FtM entity and reading its
    single-source provenance (the same round-trip resolution uses). An already-redacted shell has no
    ``schema`` (and no parseable provenance) so it is skipped — idempotent; a different source's row
    has a different ``source_id`` so it is left byte-identical — source-scoped. Returns the count of
    rows actually redacted.

    The SQL pre-filter on ``connector_id`` is a strict superset of the parse-based match: the single
    enqueue path (``runner/ingest.py::run_ingest``) stamps the row's ``connector_id`` column and the
    provenance ``source_id = "<connector_id>:<dataset>"`` from the same connector run, so every row
    whose parsed provenance can match lives under that connector — the same trust base as
    ``_redact_dead_letters``'s landing-URI prefix match. The parse below remains the authoritative
    per-row check (dataset scoping); the pre-filter only stops a whole-table scan + FtM parse.
    """
    connector_id = source_id.partition(":")[0]
    redacted = 0
    rows = session.execute(
        select(ErQueueItem).where(ErQueueItem.connector_id == connector_id)
    ).scalars()
    for row in rows.all():
        raw = row.raw_entity
        if "schema" not in raw:
            continue  # already a non-PII shell (idempotent no-op)
        try:
            entity = make_entity(raw)
        except Exception:  # malformed / unknown schema — nothing parseable to scope on
            logger.warning("erase: skipping un-parseable er_queue_item raw_entity")
            continue
        provenance = get_provenance(entity)
        if provenance is not None and provenance.source_id == source_id:
            row.raw_entity = {"erased": True, "source_id": source_id}
            redacted += 1
    return redacted


def _redact_dead_letters(session: Session, bucket: str, prefix: str) -> int:
    """Redact ``IngestDeadLetter.error`` to ``""`` for the source's map-stage rows; keep the shell.

    A map-stage dead-letter's ``source_record`` points at the landed raw PII (``s3://<bucket>/...``);
    its ``error`` may carry a PII fragment. Match by the source's ``s3://<bucket>/<prefix>``
    (``/``-terminated → collision-safe) and redact the error. An already-``""`` error is skipped
    (idempotent). Returns the count of rows actually redacted.
    """
    uri_prefix = f"s3://{bucket}/{prefix}"
    redacted = 0
    for row in session.execute(select(IngestDeadLetter)).scalars().all():
        if (
            row.source_record is not None
            and row.source_record.startswith(uri_prefix)
            and row.error != ""
        ):
            row.error = ""
            redacted += 1
    return redacted


def erase_source(
    *,
    neo4j: Neo4jClient,
    session: Session,
    landing: LandingStore,
    source_id: str,
    authorized_by: str = "",
) -> ErasureResult:
    """Remove ``source_id``'s PII from landing + ER queue + dead-letter + the graph (GDPR erasure).

    Idempotent, source-scoped, runtime-authorized (``authorized_by`` required), audited via one
    ``TaskRun(kind="erase")`` row carrying the operator + per-store counts. PRESERVES the
    ``canonical_id_ledger`` and every human-decision row (``ResolverJudgement`` / ``SignOff`` /
    ``MergeAudit``) — no un-merge / re-cluster / split / resurrection. The DB writes (queue /
    dead-letter redaction + the audit row) are staged on ``session`` for the CALLER to commit; the
    landing + graph removals are applied immediately.
    """
    # Validate BEFORE touching any store or staging the audit row: neither a connector-wide /
    # whole-bucket source_id nor a blank authorization may reach session / landing / neo4j.
    connector_id, sep, dataset = source_id.partition(":")
    if not sep or not connector_id or not dataset.strip():
        raise ValueError(
            "erase_source: source_id must be '<connector_id>:<dataset>' with a non-empty "
            f"connector AND dataset; refusing a connector-wide/whole-bucket erase ({source_id!r})"
        )
    if get_settings().is_enforced("erasure_authorization"):
        if not authorized_by.strip():
            raise ValueError(
                "erase_source: authorized_by must name the human operator who authorized the "
                "erase (non-blank); a GDPR erase can never run (or be audited) anonymously"
            )
    elif not authorized_by.strip():
        # Enforcement switch OFF (ADR 0109): allow an unauthorized erase, but never silently.
        logger.warning(
            "erase_source: authorization enforcement is OFF — running an UNAUTHORIZED erase "
            "of %s (audited with a blank authorized_by)",
            source_id,
        )

    run = TaskRun(id=str(uuid.uuid4()), kind="erase", status="running")
    session.add(run)
    try:
        prefix = _landing_prefix(source_id)
        landing_objects_deleted = landing.delete_prefix(prefix)
        queue_rows_redacted = _redact_queue(session, source_id)
        dead_letters_redacted = _redact_dead_letters(session, landing.bucket, prefix)
        graph = erase_source_graph(neo4j, source_id)

        # Gate P2 (ADR 0107): reach the Gate-2a/P1 SoR log spine AFTER the existing graph prune —
        # scrub_log_lanes DELETEs/redacts, staged on `session` for the caller's commit (same split
        # as queue/dead-letter above); prune_live_to_fold then completes the live value/anchor
        # removal (Neo4j, immediate). See erasure_scrub's module docstring for the cross-store
        # non-atomicity + idempotent-retry-recovers contract this ordering relies on.
        scrub = scrub_log_lanes(session, source_id)
        prune_live_to_fold(session, neo4j, scrub)

        result = ErasureResult(
            source_id=source_id,
            authorized_by=authorized_by,
            nodes_deleted=graph.nodes_deleted,
            nodes_pruned=graph.nodes_pruned,
            props_retracted=graph.props_retracted,
            edges_deleted=graph.edges_deleted,
            queue_rows_redacted=queue_rows_redacted,
            landing_objects_deleted=landing_objects_deleted,
            dead_letters_redacted=dead_letters_redacted,
            statements_scrubbed=scrub.statements_scrubbed,
            context_claims_scrubbed=scrub.context_claims_scrubbed,
            decisions_redacted=scrub.decisions_redacted,
            survivors_value_pruned=len(scrub.touched_survivors),
        )
        run.status = "ok"
        run.finished_at = datetime.now(UTC)
        run.stats = result.as_dict()
        return result
    except Exception:
        run.status = "error"
        run.finished_at = datetime.now(UTC)
        raise
