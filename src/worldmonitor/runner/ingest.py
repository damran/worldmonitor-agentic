"""Ingest orchestration: collect → landing zone → map → ER queue.

The connector contract stays clean (collect raw, map to FtM-with-provenance);
this function wires it to storage and the database. Every raw record lands in
object storage first, so the provenance pointer is real before any entity is
enqueued (ADR 0021). Connectors never touch the graph — candidates go to the ER
queue and L3 (resolution) owns canonicalization.

The drive of ``collect()`` is **bounded and windowed** (ADR 0027, audit gap G8):

* **Windowed commits** — land/map/enqueue in windows of ``commit_every`` records
  and commit each window, so a long import persists progress and a mid-run failure
  keeps the completed windows.
* **Bounded collection** — the run stops after ``timeout`` wall-clock seconds
  (``<= 0`` disables) or ``max_records`` records (``None`` = no cap), so a
  ``collect()`` that never returns can't hang the run. The deadline is cooperative
  (checked between records); a connector that blocks forever *inside* one
  ``next()`` needs subprocess/thread isolation — that is the streaming driver's job.
* **Dead-letter** — a record that fails to land or map is recorded in
  ``ingest_dead_letter`` and skipped; one bad record never aborts the whole run.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem, IngestDeadLetter
from worldmonitor.plugins.base import Connector
from worldmonitor.provenance.model import Provenance
from worldmonitor.settings import get_settings
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)

# Cap a dead-letter error summary so one pathological record can't bloat a row.
_ERROR_SUMMARY_MAX = 2000


@dataclass(frozen=True, slots=True)
class IngestStats:
    """Counts from one ingest run."""

    collected: int
    landed: int
    queued: int
    dead_lettered: int
    """Records recorded in ``ingest_dead_letter`` (land/map failed) and skipped."""
    windows: int
    """Number of committed windows (ADR 0027)."""
    stopped_reason: str
    """Why collection stopped: ``"exhausted"`` | ``"max_records"`` | ``"timeout"``."""


def _record_dead_letter(
    session: Session,
    *,
    tenant_id: str,
    connector_id: str,
    source_key: str,
    source_record: str | None,
    stage: str,
    exc: Exception,
) -> None:
    """Add an ``ingest_dead_letter`` row for a failed record (caller commits)."""
    session.add(
        IngestDeadLetter(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            connector_id=connector_id,
            source_key=source_key,
            source_record=source_record,
            stage=stage,
            error=f"{type(exc).__name__}: {exc}"[:_ERROR_SUMMARY_MAX],
        )
    )
    logger.warning(
        "ingest dead-letter [%s/%s] stage=%s key=%s: %s",
        tenant_id,
        connector_id,
        stage,
        source_key,
        exc,
    )


def run_ingest(
    connector: Connector,
    config: Mapping[str, Any],
    *,
    tenant_id: str,
    landing: LandingStore,
    session: Session,
    reliability: str = "B",
    commit_every: int | None = None,
    timeout: float | None = None,
    max_records: int | None = None,
) -> IngestStats:
    """Run ``connector`` for ``tenant_id``: land raw records and enqueue candidates.

    Collection is bounded and windowed (ADR 0027). ``commit_every`` / ``timeout`` /
    ``max_records`` override the ``INGEST_*`` settings; ``timeout <= 0`` disables the
    wall-clock deadline and ``max_records=None`` means no record cap. A record that
    fails to land or map is dead-lettered (recorded + skipped), never aborting the run.
    """
    settings = get_settings()
    window = commit_every if commit_every is not None else settings.ingest_commit_every
    deadline_s = timeout if timeout is not None else settings.ingest_timeout_seconds
    cap = max_records if max_records is not None else settings.ingest_max_records

    connector_id = connector.manifest.connector_id
    dataset = str(config.get("dataset", ""))
    source_id = f"{connector_id}:{dataset}".rstrip(":")

    landing.ensure_bucket()
    collected = landed = queued = dead_lettered = windows = 0
    since_commit = 0
    stopped_reason = "exhausted"
    start = time.monotonic()

    def _commit_window() -> None:
        nonlocal windows, since_commit
        if since_commit:
            session.commit()
            windows += 1
            since_commit = 0

    for record in connector.collect(config):
        collected += 1
        since_commit += 1
        key = "/".join(filter(None, [tenant_id, connector_id, dataset, f"{record.key}.json"]))

        try:
            uri = landing.put(key, record.data, content_type=record.content_type)
            landed += 1
        except Exception as exc:
            # Hostile data / flaky storage: never let one record abort the run.
            _record_dead_letter(
                session,
                tenant_id=tenant_id,
                connector_id=connector_id,
                source_key=record.key,
                source_record=None,
                stage="land",
                exc=exc,
            )
            dead_lettered += 1
        else:
            provenance = Provenance(
                source_id=source_id,
                retrieved_at=record.retrieved_at,
                reliability=reliability,
                source_record=uri,
            )
            try:
                entities = list(connector.map(record, provenance=provenance))
            except Exception as exc:
                # The raw already landed (uri), so the dead-letter is replayable.
                _record_dead_letter(
                    session,
                    tenant_id=tenant_id,
                    connector_id=connector_id,
                    source_key=record.key,
                    source_record=uri,
                    stage="map",
                    exc=exc,
                )
                dead_lettered += 1
            else:
                for entity in entities:
                    # Idempotent enqueue (ADR 0029 / A6): a re-ingest of the same
                    # landing record + FtM entity id is a no-op, so a crash/restart
                    # never double-enqueues. ``queued`` counts only NEW rows.
                    # RETURNING tells us a row was *actually* inserted (a row comes
                    # back) vs skipped by ON CONFLICT (nothing returned) — reliable
                    # where rowcount is not for ON CONFLICT DO NOTHING.
                    inserted = session.execute(
                        pg_insert(ErQueueItem)
                        .values(
                            id=str(uuid.uuid4()),
                            tenant_id=tenant_id,
                            connector_id=connector_id,
                            entity_id=entity.id,
                            raw_entity=entity.to_dict(),
                            source_record=uri,
                            status="pending",
                        )
                        .on_conflict_do_nothing(constraint="uq_er_queue_dedup")
                        .returning(ErQueueItem.id)
                    ).first()
                    if inserted is not None:
                        queued += 1

        if since_commit >= window:
            _commit_window()
        if cap is not None and collected >= cap:
            stopped_reason = "max_records"
            break
        if deadline_s > 0 and (time.monotonic() - start) >= deadline_s:
            stopped_reason = "timeout"
            break

    _commit_window()
    if stopped_reason != "exhausted":
        logger.warning(
            "ingest stopped early [%s/%s]: %s after %d record(s)",
            tenant_id,
            connector_id,
            stopped_reason,
            collected,
        )
    return IngestStats(
        collected=collected,
        landed=landed,
        queued=queued,
        dead_lettered=dead_lettered,
        windows=windows,
        stopped_reason=stopped_reason,
    )
