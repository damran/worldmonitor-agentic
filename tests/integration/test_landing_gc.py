"""Integration tests for the landing-zone orphan GC (ADR 0083 / audit M-6).

Uses real testcontainer MinIO + Postgres to verify the full GC pass end-to-end:
  I-1  delete=True: only the old-unreferenced object is deleted; the referenced object
       and the recent-unreferenced object survive; GcStats counts are exact.
  I-2  delete=False: nothing is deleted, but the orphan count and orphan_bytes are still
       reported (disk-growth signal).
  I-3  GC is a no-op when all objects are referenced.
  I-4  GC is a no-op when all orphans are within the grace window.

Real MinIO: the LastModified timestamp is set by the server at PUT time, so tests that
need the "old unreferenced" vs "recent unreferenced" distinction use a brief time.sleep()
between PUTs to create a genuine LastModified gap.
"""

from __future__ import annotations

import time
import uuid

import pytest

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem, IngestDeadLetter
from worldmonitor.runner.gc import gc_landing_orphans
from worldmonitor.storage.landing import LandingStore

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _landing_store(minio: tuple[str, str, str]) -> LandingStore:
    """A LandingStore on a fresh per-test bucket."""
    endpoint, access_key, secret_key = minio
    store = LandingStore.connect(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=f"gc-test-{uuid.uuid4().hex[:8]}",
    )
    store.ensure_bucket()
    return store


def _sessions_from_dsn(postgres_dsn: str):  # type: ignore[no-untyped-def]
    engine = make_engine(postgres_dsn)
    create_all(engine)
    return engine, session_factory(engine)


# --------------------------------------------------------------------------- #
# I-1: delete=True — referenced + old-orphan + recent-orphan
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_gc_delete_true_only_old_orphan_deleted(
    minio: tuple[str, str, str],
    postgres_dsn: str,
) -> None:
    """I-1: With three objects — referenced, old-unreferenced, recent-unreferenced —
    only the old-unreferenced is deleted. GcStats is exact."""
    landing = _landing_store(minio)
    _, sessions = _sessions_from_dsn(postgres_dsn)

    GRACE = 1.0  # 1-second grace window for the test

    # 1. Put the "referenced" object and insert an ErQueueItem row for it.
    ref_key = "conn/ds/referenced.json"
    ref_uri = landing.put(ref_key, b'{"id":"ref"}')

    # 2. Put the "old unreferenced" object (will become old after the sleep below).
    old_key = "conn/ds/old_orphan.json"
    landing.put(old_key, b'{"id":"old"}')

    # 3. Sleep so the "old" object has a LastModified significantly older than GRACE.
    time.sleep(GRACE + 0.5)  # 1.5 s total — old_orphan is now > GRACE seconds old

    # 4. Put the "recent unreferenced" object (LastModified is within the grace window).
    recent_key = "conn/ds/recent_orphan.json"
    landing.put(recent_key, b'{"id":"recent"}')

    # 5. Create the ErQueueItem reference for the ref object.
    with sessions() as session:
        session.add(
            ErQueueItem(
                id=str(uuid.uuid4()),
                connector_id="conn",
                raw_entity={"id": "ref"},
                source_record=ref_uri,
                status="pending",
            )
        )
        session.commit()

    # 6. Run GC with delete=True.
    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=GRACE, delete=True)

    # 7. Assertions on GcStats.
    assert stats.scanned == 3, f"expected 3 objects scanned, got {stats.scanned}"
    assert stats.referenced == 1, f"expected 1 referenced, got {stats.referenced}"
    assert stats.orphaned == 1, f"expected 1 orphan (old), got {stats.orphaned}"
    assert stats.deleted == 1, f"expected 1 deleted, got {stats.deleted}"
    assert stats.orphan_bytes > 0, "orphan_bytes must be > 0 (the old orphan's size)"

    # 8. Verify the surviving objects are still present.
    remaining = set(landing.list_keys())
    assert ref_key in remaining, "referenced object must survive GC"
    assert recent_key in remaining, "recent orphan must survive GC (within grace window)"
    assert old_key not in remaining, "old orphan must be deleted"


# --------------------------------------------------------------------------- #
# I-2: delete=False — report-only; orphan stats computed, nothing deleted
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_gc_delete_false_nothing_deleted_but_stats_reported(
    minio: tuple[str, str, str],
    postgres_dsn: str,
) -> None:
    """I-2: delete=False: nothing is deleted; orphan count and orphan_bytes are still computed."""
    landing = _landing_store(minio)
    _, sessions = _sessions_from_dsn(postgres_dsn)

    GRACE = 1.0

    # Put the "old" orphan
    old_key = "conn/ds/old_report_only.json"
    landing.put(old_key, b'{"id":"old"}')

    time.sleep(GRACE + 0.5)

    # Put a "recent" orphan (within grace)
    recent_key = "conn/ds/recent_report_only.json"
    landing.put(recent_key, b'{"id":"recent"}')

    # Run GC report-only
    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=GRACE, delete=False)

    # Orphan is reported but NOT deleted
    assert stats.orphaned == 1, f"expected 1 orphan, got {stats.orphaned}"
    assert stats.deleted == 0, "delete=False must not delete anything"
    assert stats.orphan_bytes > 0, "orphan_bytes must be > 0 even in report-only mode"

    # Both objects still exist
    remaining = set(landing.list_keys())
    assert old_key in remaining, "old orphan must NOT be deleted in report-only mode"
    assert recent_key in remaining, "recent orphan must NOT be deleted in report-only mode"


# --------------------------------------------------------------------------- #
# I-3: all referenced — no orphans
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_gc_all_referenced_no_orphans(
    minio: tuple[str, str, str],
    postgres_dsn: str,
) -> None:
    """I-3: when all landing objects are referenced, GC reports 0 orphans and deletes nothing."""
    landing = _landing_store(minio)
    _, sessions = _sessions_from_dsn(postgres_dsn)

    keys_and_uris: list[tuple[str, str]] = []
    for i in range(3):
        k = f"conn/ds/item-{i}.json"
        uri = landing.put(k, f'{{"id": "item-{i}"}}'.encode())
        keys_and_uris.append((k, uri))

    with sessions() as session:
        for idx, (_k, uri) in enumerate(keys_and_uris):
            session.add(
                ErQueueItem(
                    id=str(uuid.uuid4()),
                    connector_id="conn",
                    raw_entity={"id": f"item-{idx}"},
                    source_record=uri,
                    status="pending",
                )
            )
        session.commit()

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=0.0, delete=True)

    assert stats.scanned == 3
    assert stats.referenced == 3
    assert stats.orphaned == 0
    assert stats.deleted == 0
    assert stats.orphan_bytes == 0


# --------------------------------------------------------------------------- #
# I-4: all within grace window — no deletions even with delete=True
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_gc_all_within_grace_window_none_deleted(
    minio: tuple[str, str, str],
    postgres_dsn: str,
) -> None:
    """I-4: all objects within the grace window -> nothing deleted even with delete=True."""
    landing = _landing_store(minio)
    _, sessions = _sessions_from_dsn(postgres_dsn)

    # Put unreferenced objects RIGHT NOW — they're within a 1-hour grace window
    for i in range(2):
        landing.put(f"conn/ds/fresh-{i}.json", b'{"id": "x"}')

    with sessions() as session:
        # min_age_seconds = 3600 (1 hour) — all objects are < 1s old → no candidates
        stats = gc_landing_orphans(session, landing, min_age_seconds=3600.0, delete=True)

    assert stats.orphaned == 0
    assert stats.deleted == 0


# --------------------------------------------------------------------------- #
# I-5: dead-letter reference protects object
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_gc_dead_letter_reference_protects_object(
    minio: tuple[str, str, str],
    postgres_dsn: str,
) -> None:
    """I-5: an object referenced by IngestDeadLetter.source_record survives GC."""
    landing = _landing_store(minio)
    _, sessions = _sessions_from_dsn(postgres_dsn)

    key = "conn/ds/dl_protected.json"
    uri = landing.put(key, b'{"id":"dl"}')

    with sessions() as session:
        session.add(
            IngestDeadLetter(
                id=str(uuid.uuid4()),
                connector_id="conn",
                source_key=key,
                source_record=uri,
                stage="map",
                error="boom",
            )
        )
        session.commit()

    # min_age=0 → any object is "old", but this one is referenced
    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=0.0, delete=True)

    assert stats.referenced == 1
    assert stats.orphaned == 0
    assert stats.deleted == 0
    assert key in set(landing.list_keys()), "dead-letter-referenced object must survive GC"
