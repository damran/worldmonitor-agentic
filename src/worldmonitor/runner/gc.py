"""Landing-zone orphan GC (ADR 0083 / audit finding M-6).

Context
-------
``landing.put(key, ...)`` precedes the windowed ``session.commit()`` in ``run_ingest``.
A crash between the put and the commit leaves a landing object with no referencing DB row —
a *landing-zone orphan*.  With a **deterministic** ``record.key``, a replay overwrites the
same S3 key so the orphan is naturally overwritten.  The GC is the backstop for any residual
orphan (e.g. historic non-determinism, or a genuinely-crashed mid-window that was never
replayed).

Reference-based GC, not TTL
----------------------------
An age/lifecycle TTL is **wrong** for landing objects: the raw bytes are provenance and must
persist as long as any entity derived from them does.  The GC therefore checks whether the
object's ``s3://`` URI appears in either reference table:

* ``er_queue_item.source_record`` — non-nullable; every enqueued candidate references it.
* ``ingest_dead_letter.source_record`` — nullable; a map-stage dead-letter references it too.

An object absent from BOTH is a candidate — IF it is also older than the grace window.

Grace window (race closure)
---------------------------
A put-before-commit race produces a *very recent* object.  The ``min_age_seconds`` grace
window ensures such an object is never swept on the same pass: the GC only deletes objects
whose ``LastModified`` age exceeds ``min_age_seconds``.  Objects with no ``LastModified``
metadata are treated as RECENT (conservative default) and never swept.

Default: report-only
--------------------
Deletion is gated behind ``landing_gc_delete_enabled=False`` (default off).  The driver
gates the pass itself behind ``landing_gc_enabled=False`` (master off).  The DISK-GROWTH
SIGNAL (orphan count + orphan bytes) is **always** computed and exposed via Prometheus even
in report-only mode.

Deletion safety invariants (adversarially reviewed)
----------------------------------------------------
* A **referenced** object is NEVER a deletion candidate — the reference check precedes the
  age check.
* A **recent** object (age ≤ ``min_age_seconds``) is NEVER a deletion candidate.
* An object with **no LastModified metadata** is treated as recent and never swept.
* Deletion happens **only** when ``delete=True``; report-only mode is purely read.
* Deletion batches ≤ 1000 keys (the S3 cap); a non-empty ``Errors`` array raises
  ``RuntimeError`` immediately — fail-loud-on-partial-error, same discipline as
  ``LandingStore.delete_prefix``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from worldmonitor.db.models import ErQueueItem, IngestDeadLetter
from worldmonitor.storage.landing import LandingStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GcStats:
    """Per-pass statistics from :func:`gc_landing_orphans`."""

    scanned: int
    """Total landing objects enumerated this pass."""
    referenced: int
    """Objects whose S3 URI appears in at least one reference-table row."""
    orphaned: int
    """Objects that are unreferenced AND older than the grace window (deletion candidates)."""
    deleted: int
    """Objects actually deleted this pass; 0 when ``delete=False`` (report-only)."""
    bytes_freed: int
    """Total size in bytes of orphaned candidate objects — the disk-growth signal.

    ALWAYS computed, even in report-only mode: this makes the Prometheus gauge
    (``worldmonitor_landing_orphan_bytes``) meaningful whether or not deletion is enabled.
    In delete mode and a clean (no-error) pass, this equals the actual bytes freed.
    """


def gc_landing_orphans(
    session: Session,
    landing: LandingStore,
    *,
    min_age_seconds: float,
    delete: bool,
) -> GcStats:
    """Scan the landing zone for unreferenced orphans and optionally delete them.

    See module docstring for the full safety analysis.

    Args:
        session:          An open SQLAlchemy session (used **read-only**; no commits).
        landing:          The landing store to scan (and optionally delete from).
        min_age_seconds:  Grace window in seconds.  Objects younger than this are NEVER
                          swept, closing the put-before-commit race.  ``0.0`` disables
                          the grace window (all unreferenced objects are candidates).
        delete:           ``True`` → delete candidates in <=1000-key batches (fail-loud on
                          partial errors); ``False`` → report-only, no deletion.

    Returns:
        :class:`GcStats` with per-pass counts.
    """
    now = datetime.now(UTC)

    # ------------------------------------------------------------------ #
    # 1. List ALL landing objects with metadata (paged past the 1000-key cap).
    # ------------------------------------------------------------------ #
    objects: list[dict[str, Any]] = landing.list_objects_with_metadata()
    scanned = len(objects)

    # ------------------------------------------------------------------ #
    # 2. Build the referenced-URI set from BOTH reference tables in one pass.
    #    ErQueueItem.source_record is non-nullable (always a str).
    #    IngestDeadLetter.source_record is nullable (null on land-stage failure).
    # ------------------------------------------------------------------ #
    er_uris: set[str] = set(session.execute(select(ErQueueItem.source_record)).scalars())
    dl_uris: set[str] = {
        uri
        for uri in session.execute(
            select(IngestDeadLetter.source_record).where(
                IngestDeadLetter.source_record.is_not(None)
            )
        ).scalars()
        if uri is not None
    }
    referenced_uris: set[str] = er_uris | dl_uris

    # ------------------------------------------------------------------ #
    # 3. Classify each object.
    # ------------------------------------------------------------------ #
    referenced_count = 0
    candidates: list[dict[str, Any]] = []

    for obj in objects:
        key: str = obj["Key"]
        uri = f"s3://{landing.bucket}/{key}"

        # SAFETY INVARIANT: referenced objects are NEVER candidates.
        if uri in referenced_uris:
            referenced_count += 1
            continue

        # SAFETY INVARIANT: objects with no LastModified are treated as recent.
        last_modified: datetime | None = obj.get("LastModified")
        if last_modified is None:
            continue

        # Normalise to UTC (S3/MinIO always returns UTC-aware datetimes; be defensive).
        if last_modified.tzinfo is None:
            last_modified = last_modified.replace(tzinfo=UTC)

        age = (now - last_modified).total_seconds()

        # SAFETY INVARIANT: objects within the grace window are NEVER candidates.
        if age <= min_age_seconds:
            continue

        candidates.append(obj)

    orphaned = len(candidates)
    # bytes_freed is ALWAYS computed — the disk-growth signal for Prometheus.
    bytes_freed = sum(int(obj.get("Size", 0)) for obj in candidates)

    # ------------------------------------------------------------------ #
    # 4. Delete only when requested, batched <=1000, fail-loud on partial errors.
    # ------------------------------------------------------------------ #
    deleted = 0
    if delete and candidates:
        keys_to_delete = [c["Key"] for c in candidates]
        deleted = landing.delete_keys(keys_to_delete)

    logger.info(
        "gc_landing_orphans: scanned=%d referenced=%d orphaned=%d deleted=%d bytes_freed=%d",
        scanned,
        referenced_count,
        orphaned,
        deleted,
        bytes_freed,
    )

    return GcStats(
        scanned=scanned,
        referenced=referenced_count,
        orphaned=orphaned,
        deleted=deleted,
        bytes_freed=bytes_freed,
    )
