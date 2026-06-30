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
    orphan_bytes: int
    """Total size in bytes of orphaned candidate objects — the disk-growth signal.

    ALWAYS computed, even in report-only mode: this makes the Prometheus gauge
    (``worldmonitor_landing_orphan_bytes``) meaningful whether or not deletion is enabled.
    Named ``orphan_bytes`` to reflect that this is bytes of orphan *candidates identified*,
    computed even when no deletion occurs (ADR 0086 D3).
    """


def select_orphan_candidates(
    objects: list[dict[str, Any]],
    referenced_uris: set[str],
    *,
    now: datetime,
    min_age_seconds: float,
) -> list[dict[str, Any]]:
    """Pure classifier: select orphan candidates from a list of landing objects.

    No DB or S3 access.  An object is a candidate iff ALL of the following hold:

    1. Its ``"uri"`` key is NOT in ``referenced_uris``  (reference beats age, unconditionally).
    2. Its ``"LastModified"`` is not None  (None → treated as recent → never a candidate).
    3. Its age ``(now - LastModified).total_seconds() > min_age_seconds``  (past grace window).

    Args:
        objects:          List of landing-object dicts.  Each dict carries the fields returned
                          by ``LandingStore.list_objects_with_metadata()`` PLUS a pre-built
                          ``"uri"`` key (``f"s3://<bucket>/<key>"``).
        referenced_uris:  Set of pre-built ``s3://`` URI strings from the DB reference tables
                          (``er_queue_item.source_record ∪ ingest_dead_letter.source_record``).
        now:              The reference instant for age calculation (UTC-aware).
        min_age_seconds:  Grace window in seconds.  Objects at or within this age are NEVER
                          candidates (``0.0`` means any age > 0 is a candidate).

    Returns:
        List of candidate object dicts (same references as in ``objects``).

    Called by :func:`gc_landing_orphans`; extracted so the property suite can pin the pure
    classification decision without S3/DB I/O (ADR 0086 Change 2c).
    """
    candidates: list[dict[str, Any]] = []
    for obj in objects:
        # SAFETY INVARIANT (G1 provenance): referenced objects are NEVER candidates.
        # Reference check precedes the age check — a very old referenced object is still safe.
        if obj["uri"] in referenced_uris:
            continue

        # SAFETY INVARIANT: objects with no LastModified are treated as recent (conservative).
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

    return candidates


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
    raw_objects: list[dict[str, Any]] = landing.list_objects_with_metadata()
    scanned = len(raw_objects)

    # Pre-build "uri" key for each object — the contract required by select_orphan_candidates.
    # gc_landing_orphans owns the URI construction; the pure helper consumes it opaquely.
    objects: list[dict[str, Any]] = [
        {**obj, "uri": f"s3://{landing.bucket}/{obj['Key']}"} for obj in raw_objects
    ]

    # ------------------------------------------------------------------ #
    # 2. Build the referenced-URI set from BOTH reference tables.
    #
    # ER-QUEUE-NEVER-HARD-DELETED (ADR 0086 D2):
    #   The ErQueueItem reference query is UNFILTERED — all rows, ALL statuses (pending,
    #   resolved, pending_review, invalid, …).  Omitting Neo4j provenance pointers is SAFE
    #   ONLY while er_queue rows are NEVER hard-deleted: a resolved/processed row's
    #   source_record persists forever, so the landing object it points at remains referenced.
    #   If a hard-delete (or TTL purge) is EVER added to er_queue, the GC MUST ALSO union
    #   Neo4j prov_source_id pointers into referenced_uris before any deletion is attempted.
    #   This invariant is load-bearing: a status-filtered reference query would silently
    #   orphan live provenance bytes.  See docs/decisions/0086-landing-gc-safety.md.
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
    # 3. Classify each object via the pure helper (no behaviour change vs. inline loop).
    # ------------------------------------------------------------------ #
    candidates = select_orphan_candidates(
        objects, referenced_uris, now=now, min_age_seconds=min_age_seconds
    )

    # Count objects whose URI appears in the reference set (separately from candidates).
    referenced_count = sum(1 for obj in objects if obj["uri"] in referenced_uris)

    orphaned = len(candidates)
    # orphan_bytes is ALWAYS computed — the disk-growth signal for Prometheus.
    orphan_bytes = sum(int(obj.get("Size", 0)) for obj in candidates)

    # ------------------------------------------------------------------ #
    # 4. Delete only when requested, batched <=1000, fail-loud on partial errors.
    # ------------------------------------------------------------------ #
    deleted = 0
    if delete and candidates:
        keys_to_delete = [c["Key"] for c in candidates]
        deleted = landing.delete_keys(keys_to_delete)

    logger.info(
        "gc_landing_orphans: scanned=%d referenced=%d orphaned=%d deleted=%d orphan_bytes=%d",
        scanned,
        referenced_count,
        orphaned,
        deleted,
        orphan_bytes,
    )

    return GcStats(
        scanned=scanned,
        referenced=referenced_count,
        orphaned=orphaned,
        deleted=deleted,
        orphan_bytes=orphan_bytes,
    )
