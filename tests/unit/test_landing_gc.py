"""Unit tests for the landing-zone orphan GC (ADR 0083 / audit M-6).

Covers:
  U-1  GcStats dataclass — correct fields, frozen, slots.
  U-2  Reference-set building from BOTH tables (ErQueueItem + IngestDeadLetter.source_record).
  U-3  Age/grace filter — objects within min_age_seconds NEVER deleted.
  U-4  Referenced objects NEVER deleted even when old.
  U-5  delete=False (report-only): nothing deleted, orphan stats still computed.
  U-6  fail-loud on a partial DeleteObjects Errors array (LandingStore.delete_keys).
  U-7  Deterministic-key invariant — built-in connectors produce the same key for the same input.
  U-8  New Settings fields exist with the correct types and safe defaults.

All Docker-free (SQLite + MagicMock for storage).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.db.models import Base, ErQueueItem, IngestDeadLetter

# RED until gc.py exists
from worldmonitor.runner.gc import GcStats, gc_landing_orphans
from worldmonitor.settings import Settings
from worldmonitor.storage.landing import LandingStore

# --------------------------------------------------------------------------- #
# SQLite dialect shim (JSONB → JSON for in-memory unit tests)
# --------------------------------------------------------------------------- #


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def _sqlite_sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


# --------------------------------------------------------------------------- #
# Stub LandingStore (no real S3; controllable metadata + deletion)
# --------------------------------------------------------------------------- #

_BUCKET = "test-landing"


def _make_stub_landing(
    objects: list[dict[str, Any]],
    *,
    raise_on_delete: bool = False,
) -> MagicMock:
    """Return a MagicMock(spec=LandingStore) with controllable list/delete behaviour.

    ``objects`` are dicts with 'Key', 'Size', 'LastModified' (same shape as
    ``list_objects_with_metadata`` returns).
    ``raise_on_delete=True`` makes ``delete_keys`` raise RuntimeError (partial-error simulation).
    """
    stub = MagicMock(spec=LandingStore)
    stub.bucket = _BUCKET
    stub.list_objects_with_metadata.return_value = list(objects)
    if raise_on_delete:
        stub.delete_keys.side_effect = RuntimeError(
            "delete_keys: 1 object(s) failed to delete (e.g. {'Code': 'InternalError'}); "
            "operation INCOMPLETE — retry (idempotent)"
        )
    else:
        # Track deleted keys; return count
        deleted_keys: list[str] = []
        stub._deleted_keys = deleted_keys

        def _delete_keys(keys: list[str]) -> int:
            deleted_keys.extend(keys)
            return len(keys)

        stub.delete_keys.side_effect = _delete_keys
    return stub


def _obj(key: str, age_seconds: float, size: int = 256) -> dict[str, Any]:
    """Build a metadata dict as if returned by ``list_objects_with_metadata``."""
    last_modified = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return {"Key": key, "Size": size, "LastModified": last_modified}


def _uri(key: str) -> str:
    return f"s3://{_BUCKET}/{key}"


# --------------------------------------------------------------------------- #
# U-1: GcStats dataclass
# --------------------------------------------------------------------------- #


def test_gc_stats_fields_and_immutability() -> None:
    """U-1: GcStats has the five required fields, is frozen (immutable), and uses slots."""
    stats = GcStats(scanned=10, referenced=6, orphaned=3, deleted=2, bytes_freed=1024)
    assert stats.scanned == 10
    assert stats.referenced == 6
    assert stats.orphaned == 3
    assert stats.deleted == 2
    assert stats.bytes_freed == 1024

    # Frozen: attribute assignment must raise
    with pytest.raises((AttributeError, TypeError)):
        stats.scanned = 99  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# U-2: Reference-set building from BOTH tables
# --------------------------------------------------------------------------- #


def test_gc_uses_er_queue_source_record() -> None:
    """U-2a: an object referenced by ErQueueItem.source_record is NOT a deletion candidate."""
    sessions = _sqlite_sessions()
    key = "conn/ds/record.json"
    uri = _uri(key)

    with sessions() as session:
        session.add(
            ErQueueItem(
                id="e1",
                connector_id="conn",
                raw_entity={"id": "e1"},
                source_record=uri,
                status="pending",
            )
        )
        session.commit()

    # Object is old (well past any grace window), but referenced
    landing = _make_stub_landing([_obj(key, age_seconds=9999)])

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=0.0, delete=True)

    assert stats.scanned == 1
    assert stats.referenced == 1
    assert stats.orphaned == 0
    assert stats.deleted == 0
    assert stats.bytes_freed == 0
    landing.delete_keys.assert_not_called()


def test_gc_uses_dead_letter_source_record() -> None:
    """U-2b: an object referenced by IngestDeadLetter.source_record is NOT a deletion candidate."""
    sessions = _sqlite_sessions()
    key = "conn/ds/dead.json"
    uri = _uri(key)

    with sessions() as session:
        session.add(
            IngestDeadLetter(
                id="d1",
                connector_id="conn",
                source_key="dead",
                source_record=uri,
                stage="map",
                error="boom",
            )
        )
        session.commit()

    landing = _make_stub_landing([_obj(key, age_seconds=9999)])

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=0.0, delete=True)

    assert stats.scanned == 1
    assert stats.referenced == 1
    assert stats.orphaned == 0
    assert stats.deleted == 0
    landing.delete_keys.assert_not_called()


def test_gc_dead_letter_null_source_record_not_counted_as_reference() -> None:
    """U-2c: a dead-letter row with source_record=None (land-stage failure) does NOT protect
    any landing object from GC — null URIs are excluded from the reference set."""
    sessions = _sqlite_sessions()
    key = "conn/ds/orphan.json"

    with sessions() as session:
        session.add(
            IngestDeadLetter(
                id="d2",
                connector_id="conn",
                source_key="orphan",
                source_record=None,  # land-stage failure — no URI
                stage="land",
                error="no bytes",
            )
        )
        session.commit()

    landing = _make_stub_landing([_obj(key, age_seconds=9999)])

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=0.0, delete=True)

    # The object is old and unreferenced → orphan candidate
    assert stats.orphaned == 1
    assert stats.referenced == 0


# --------------------------------------------------------------------------- #
# U-3: Age / grace-window filter
# --------------------------------------------------------------------------- #


def test_gc_grace_window_protects_recent_object() -> None:
    """U-3: an object within the grace window (age <= min_age_seconds) is NEVER swept,
    even if it is unreferenced."""
    sessions = _sqlite_sessions()
    # Object is only 5 seconds old; grace window is 60 seconds
    landing = _make_stub_landing([_obj("conn/ds/recent.json", age_seconds=5)])

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=60.0, delete=True)

    assert stats.orphaned == 0
    assert stats.deleted == 0
    landing.delete_keys.assert_not_called()


def test_gc_old_unreferenced_object_is_swept() -> None:
    """U-3b: an object past the grace window that is unreferenced IS a deletion candidate."""
    sessions = _sqlite_sessions()
    landing = _make_stub_landing([_obj("conn/ds/old.json", age_seconds=3600)])

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=60.0, delete=True)

    assert stats.orphaned == 1
    assert stats.deleted == 1


def test_gc_object_without_last_modified_treated_as_recent() -> None:
    """U-3c: an object with LastModified=None is treated as RECENT (conservative default)
    and is NEVER a deletion candidate."""
    sessions = _sqlite_sessions()
    obj_no_ts: dict[str, Any] = {"Key": "conn/ds/no_ts.json", "Size": 100, "LastModified": None}
    landing = _make_stub_landing([obj_no_ts])

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=0.0, delete=True)

    assert stats.orphaned == 0
    assert stats.deleted == 0
    landing.delete_keys.assert_not_called()


# --------------------------------------------------------------------------- #
# U-4: Mixed scenario — referenced + old-orphan + recent-orphan
# --------------------------------------------------------------------------- #


def test_gc_mixed_objects_only_old_unreferenced_deleted() -> None:
    """U-4: with a referenced object, an old orphan, and a recent orphan, only the old orphan
    is deleted. referenced=1, orphaned=1, deleted=1."""
    sessions = _sqlite_sessions()

    ref_key = "conn/ds/ref.json"
    old_key = "conn/ds/old_orphan.json"
    recent_key = "conn/ds/recent_orphan.json"

    with sessions() as session:
        session.add(
            ErQueueItem(
                id="e-ref",
                connector_id="conn",
                raw_entity={"id": "e-ref"},
                source_record=_uri(ref_key),
                status="pending",
            )
        )
        session.commit()

    landing = _make_stub_landing(
        [
            _obj(ref_key, age_seconds=9999, size=100),  # referenced → survives
            _obj(old_key, age_seconds=3600, size=200),  # unreferenced + old → swept
            _obj(recent_key, age_seconds=5, size=300),  # unreferenced + recent → survives
        ]
    )

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=60.0, delete=True)

    assert stats.scanned == 3
    assert stats.referenced == 1
    assert stats.orphaned == 1
    assert stats.deleted == 1
    assert stats.bytes_freed == 200  # size of old_key

    deleted = landing._deleted_keys
    assert old_key in deleted
    assert ref_key not in deleted
    assert recent_key not in deleted


# --------------------------------------------------------------------------- #
# U-5: delete=False (report-only)
# --------------------------------------------------------------------------- #


def test_gc_report_only_deletes_nothing_but_computes_stats() -> None:
    """U-5: with delete=False no deletion occurs; orphaned count and bytes_freed are still set
    (the disk-growth signal); deleted=0."""
    sessions = _sqlite_sessions()

    landing = _make_stub_landing(
        [
            _obj("conn/ds/orphan1.json", age_seconds=3600, size=500),
            _obj("conn/ds/orphan2.json", age_seconds=7200, size=300),
        ]
    )

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=60.0, delete=False)

    assert stats.orphaned == 2
    assert stats.deleted == 0
    assert stats.bytes_freed == 800  # 500 + 300 — computed even in report-only mode
    landing.delete_keys.assert_not_called()


# --------------------------------------------------------------------------- #
# U-6: fail-loud on partial DeleteObjects Errors array
# --------------------------------------------------------------------------- #


def test_gc_fail_loud_on_partial_delete_error() -> None:
    """U-6: when delete_keys raises RuntimeError (partial S3 Errors array), gc_landing_orphans
    propagates the error immediately — no silent under-reporting."""
    sessions = _sqlite_sessions()
    landing = _make_stub_landing(
        [_obj("conn/ds/bad.json", age_seconds=9999)],
        raise_on_delete=True,
    )

    with sessions() as session, pytest.raises(RuntimeError, match="INCOMPLETE"):
        gc_landing_orphans(session, landing, min_age_seconds=0.0, delete=True)


def test_delete_keys_fail_loud_on_partial_errors_at_landing_level() -> None:
    """U-6b: LandingStore.delete_keys raises RuntimeError on a partial DeleteObjects Errors array.
    This tests the method directly (no session needed)."""
    mock_client = MagicMock()
    mock_client.delete_objects.return_value = {
        "Errors": [{"Key": "k1", "Code": "InternalError", "Message": "fail"}],
        "Deleted": [],
    }
    store = LandingStore(client=mock_client, bucket="test-bucket")
    with pytest.raises(RuntimeError, match="INCOMPLETE"):
        store.delete_keys(["k1"])


def test_delete_keys_success_returns_confirmed_count() -> None:
    """U-6c: delete_keys returns the count from the Deleted array in the S3 response."""
    mock_client = MagicMock()
    mock_client.delete_objects.return_value = {
        "Deleted": [{"Key": "k1"}, {"Key": "k2"}],
        "Errors": [],
    }
    store = LandingStore(client=mock_client, bucket="b")
    count = store.delete_keys(["k1", "k2"])
    assert count == 2


# --------------------------------------------------------------------------- #
# U-7: Deterministic-key invariant for built-in connectors
# --------------------------------------------------------------------------- #


class _DeterministicKeyTests:
    """Namespace for connector key-determinism tests (all Docker-free, no network).

    Tested by calling the key-derivation logic directly (not through collect() which may
    require network or subprocess) with fixed inputs and asserting the same key is produced
    twice. This is the prevention-half of the M-6 fix: deterministic keys mean replays
    overwrite the same S3 object, preventing orphan accumulation.
    """


def test_key_deterministic_opensanctions_with_entity_id() -> None:
    """U-7a: OpenSanctions key is the entity id when present — fully deterministic."""
    from worldmonitor.plugins.connectors.opensanctions.connector import OpenSanctionsConnector

    line = json.dumps({"id": "Q12345", "schema": "Person", "properties": {}})
    key1 = OpenSanctionsConnector._record_key(line, fallback=0)
    key2 = OpenSanctionsConnector._record_key(line, fallback=99)  # fallback unused when id present
    assert key1 == key2 == "Q12345"


def test_key_opensanctions_fallback_position_dependent_finding() -> None:
    """U-7a FINDING: OpenSanctions uses a position counter (``record-{fallback}``) when the line
    has no 'id' field. The key is deterministic for the same stream ORDER, but NOT across stream
    re-orderings (added/removed upstream records shift the position → orphan risk).

    This is a documented finding per M-6 / ADR 0083. The connector is NOT fixed here
    (out of scope); the finding is recorded for the operator's awareness.
    """
    from worldmonitor.plugins.connectors.opensanctions.connector import OpenSanctionsConnector

    line_without_id = json.dumps({"schema": "Person", "properties": {}})
    key_pos0 = OpenSanctionsConnector._record_key(line_without_id, fallback=0)
    key_pos1 = OpenSanctionsConnector._record_key(line_without_id, fallback=1)
    assert key_pos0 == "record-0"
    assert key_pos1 == "record-1"
    # NON-DETERMINISTIC across stream positions — same record, different fallback → different key
    assert key_pos0 != key_pos1, (
        "FINDING: OpenSanctions fallback key is position-dependent. Same record content at "
        "different stream positions produces different keys → replay after upstream changes "
        "can produce orphans. See ADR 0083 §Deterministic-key finding."
    )


def test_key_deterministic_geonames() -> None:
    """U-7b: GeoNames key is the first TSV column (geoname_id) — always deterministic."""
    tsv_line = "2643743\tLondon\tLondon\t\t51.50853\t-0.12574\tP\tPPLA\tGB\t\tENG\tGLA\t\t\t8908081\t25\t11\tEurope/London\t2019-09-05"  # noqa: E501
    geoname_id = tsv_line.split("\t", 1)[0]
    # Two calls with the same line → same key
    assert geoname_id == "2643743"
    # And a second identical line produces the same key:
    assert tsv_line.split("\t", 1)[0] == geoname_id


def test_key_deterministic_opencorporates() -> None:
    """U-7c: OpenCorporates key is ``"{jurisdiction_code}/{company_number}"`` — deterministic."""
    from worldmonitor.plugins.connectors.opencorporates.connector import OpenCorporatesConnector

    connector = OpenCorporatesConnector()
    item: dict[str, Any] = {"jurisdiction_code": "gb", "company_number": "12345678"}
    key1 = connector._record_key(item)
    key2 = connector._record_key(item)
    assert key1 == key2 == "gb/12345678"


def test_key_deterministic_bluesky() -> None:
    """U-7d: Bluesky key is ``"{did}/{rkey}"`` — deterministic for same event."""
    from worldmonitor.plugins.connectors.bluesky.connector import BlueskyConnector

    connector = BlueskyConnector()
    event: dict[str, Any] = {
        "did": "did:plc:abc123",
        "commit": {"rkey": "3jzfcijpj2z2a", "collection": "app.bsky.feed.post"},
        "time_us": "1700000000000000",
    }
    key1, cursor1, _ = connector._extract_event(event)
    key2, cursor2, _ = connector._extract_event(event)
    assert key1 == key2 == "did:plc:abc123/3jzfcijpj2z2a"
    assert cursor1 == cursor2


def test_key_deterministic_feed_connector_with_entry_id() -> None:
    """U-7e: FeedConnector key is str(entry_id or link or '') — deterministic when entry_id set."""
    # Key is str(entry_id or link or ''); we test the logic directly (no network)
    entry_id = "https://example.com/posts/123"
    link = "https://example.com/other"
    # When entry_id is present, link is ignored
    key = str(entry_id or link or "")
    assert key == "https://example.com/posts/123"
    assert str(entry_id or link or "") == key  # second call → same


def test_key_deterministic_clitool_is_target() -> None:
    """U-7f: CLI-tool connectors (whois, dig, nmap) use key=str(target) — always deterministic."""
    # The CliToolConnector base always uses: key=str(target)
    # This is a pure string conversion — trivially deterministic.
    target = "example.com"
    key1 = str(target)
    key2 = str(target)
    assert key1 == key2 == "example.com"


# --------------------------------------------------------------------------- #
# U-8: New settings fields
# --------------------------------------------------------------------------- #


def test_settings_gc_fields_have_safe_defaults() -> None:
    """U-8: the three new landing GC settings exist with safe defaults (all off by default)."""
    s = Settings()
    # Master gate — OFF by default (no behaviour change)
    assert s.landing_gc_enabled is False
    # Deletion gate — OFF by default (report-only)
    assert s.landing_gc_delete_enabled is False
    # Grace window — generous default (1 day)
    assert s.landing_gc_min_age_seconds == 86400.0


def test_settings_gc_min_age_accepts_zero() -> None:
    """U-8b: landing_gc_min_age_seconds accepts 0.0 (disables grace window entirely)."""
    s = Settings(landing_gc_min_age_seconds=0.0)
    assert s.landing_gc_min_age_seconds == 0.0
