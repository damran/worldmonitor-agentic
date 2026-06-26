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

# The single source of truth for the landing-key path sanitizer — REUSED, never duplicated (spec
# §4.1), so the derived erase prefix is provably a true prefix of the real ingest key.
from worldmonitor.runner.ingest import _safe_segment  # pyright: ignore[reportPrivateUsage]
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ErasureResult:
    """Per-store counts from one cross-store erase — the audit ``TaskRun.stats`` payload (§4.4).

    The field order is the locked audit-stats key order (``source_id``, ``authorized_by``, then the
    seven per-store counts): :meth:`as_dict` is what lands in ``TaskRun.stats``.
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
    """
    connector_id, _, dataset = source_id.partition(":")
    segments = [connector_id]
    if dataset:
        segments.append(_safe_segment(dataset))
    return "/".join(segments) + "/"


def _redact_queue(session: Session, source_id: str) -> int:
    """Redact ``ErQueueItem.raw_entity`` to a non-PII shell for the source's rows; keep the shell.

    A row's source is decided by parsing its ``raw_entity`` back into an FtM entity and reading its
    single-source provenance (the same round-trip resolution uses). An already-redacted shell has no
    ``schema`` (and no parseable provenance) so it is skipped — idempotent; a different source's row
    has a different ``source_id`` so it is left byte-identical — source-scoped. Returns the count of
    rows actually redacted.
    """
    redacted = 0
    for row in session.execute(select(ErQueueItem)).scalars().all():
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
    authorized_by: str,
) -> ErasureResult:
    """Remove ``source_id``'s PII from landing + ER queue + dead-letter + the graph (GDPR erasure).

    Idempotent, source-scoped, runtime-authorized (``authorized_by`` required), audited via one
    ``TaskRun(kind="erase")`` row carrying the operator + per-store counts. PRESERVES the
    ``canonical_id_ledger`` and every human-decision row (``ResolverJudgement`` / ``SignOff`` /
    ``MergeAudit``) — no un-merge / re-cluster / split / resurrection. The DB writes (queue /
    dead-letter redaction + the audit row) are staged on ``session`` for the CALLER to commit; the
    landing + graph removals are applied immediately.
    """
    run = TaskRun(id=str(uuid.uuid4()), kind="erase", status="running")
    session.add(run)
    try:
        prefix = _landing_prefix(source_id)
        landing_objects_deleted = landing.delete_prefix(prefix)
        queue_rows_redacted = _redact_queue(session, source_id)
        dead_letters_redacted = _redact_dead_letters(session, landing.bucket, prefix)
        graph = erase_source_graph(neo4j, source_id)

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
        )
        run.status = "ok"
        run.finished_at = datetime.now(UTC)
        run.stats = result.as_dict()
        return result
    except Exception:
        run.status = "error"
        run.finished_at = datetime.now(UTC)
        raise
