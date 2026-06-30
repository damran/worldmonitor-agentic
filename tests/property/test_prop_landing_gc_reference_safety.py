"""Primary property/metamorphic tests for Gate B — landing-zone GC reference safety.

ADR: ``docs/decisions/0086-landing-gc-safety.md``
Gate spec: ``docs/reviews/GATE_B_LANDING_GC_SAFETY_SPEC.md``

This file pins three non-negotiable invariants for the ``select_orphan_candidates`` pure helper
and the ``gc_landing_orphans`` function against arbitrary inputs (150 examples each, deadline=None
because SQLite setup in P-ER-STATUS exceeds the 200 ms default deadline on busy runners).

Assumed helper signature (builder must match exactly):

    def select_orphan_candidates(
        objects: list[dict],
        referenced_uris: set[str],
        *,
        now: datetime,
        min_age_seconds: float,
    ) -> list[dict]:
        ...

Contract on ``objects``: each dict carries exactly the fields returned by
``LandingStore.list_objects_with_metadata()`` PLUS a pre-built ``"uri"`` key (the full
``s3://<bucket>/<key>`` string that the caller (``gc_landing_orphans``) constructs via
``f"s3://{landing.bucket}/{obj['Key']}"`` before forwarding to the pure helper).
``referenced_uris`` contains pre-built ``s3://`` URI strings already derived from the DB
reference tables (``er_queue_item.source_record ∪ ingest_dead_letter.source_record``).

Properties:

- **P-REF** (G1 safety core): for ANY object whose ``uri`` is in ``referenced_uris``, that
  object is NEVER returned in ``select_orphan_candidates(...)`` — regardless of its age or
  the grace window.  Reference beats age, unconditionally.

- **P-MM-MONOTONE** (metamorphic): enlarging the reference set (R ⊆ R') can only SHRINK the
  orphan-candidate set.  Adding a reference can never make a previously-non-candidate object
  appear as a candidate.

- **P-ER-STATUS** (exact review finding, ADR 0086 D2): an object whose URI appears as the
  ``source_record`` of an ``ErQueueItem`` of ANY status (pending, resolved, pending_review,
  invalid) is never returned as an orphan candidate by ``gc_landing_orphans``.  The ER
  reference query must remain status-UNFILTERED.

All three are RED on the current tree because ``select_orphan_candidates`` does not yet exist
in ``worldmonitor.runner.gc`` — the module-level import below raises ``ImportError`` at
collection time, failing the entire file for the RIGHT reason.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.db.models import Base, ErQueueItem

# --------------------------------------------------------------------------- #
# This import is the GATE.  It will fail with ImportError on current code
# because ``select_orphan_candidates`` does not yet exist in gc.py.
# Every test in this file is RED until the builder adds the pure helper.
# --------------------------------------------------------------------------- #
from worldmonitor.runner.gc import gc_landing_orphans, select_orphan_candidates  # noqa: E402
from worldmonitor.storage.landing import LandingStore

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

# deadline=None: each property test may involve SQLite setup (P-ER-STATUS) or
# datetime arithmetic across 150 examples; the default 200 ms deadline flakes on
# loaded runners.  Bounded by max_examples=150; assertions unchanged.
_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# --------------------------------------------------------------------------- #
# SQLite dialect shim (JSONB → JSON for in-memory sessions used by P-ER-STATUS)
# --------------------------------------------------------------------------- #


@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_BUCKET = "test-gc-prop"

# Fixed "now" for P-REF and P-MM-MONOTONE (pure-function tests).  Ages are relative
# to this point so the tests are deterministic regardless of wall-clock time.
_FIXED_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)

# All known ErQueueItem status values (derived from pipeline.py + signoff.py).
# If a new status is introduced, add it here; P-ER-STATUS will then cover it.
ER_QUEUE_STATUSES: frozenset[str] = frozenset({"pending", "resolved", "pending_review", "invalid"})

# --------------------------------------------------------------------------- #
# Strategies
# --------------------------------------------------------------------------- #


@st.composite
def landing_object(draw: st.DrawFn, *, bucket: str = _BUCKET) -> dict[str, Any]:
    """Generate a landing object dict shaped like ``list_objects_with_metadata()`` output,
    with an additional pre-built ``"uri"`` field.

    Covers:
    - very young objects (age ~ 0)
    - very old objects (age up to ~2.3 days)
    - a mix of sizes
    Key space: 10 000 distinct keys → ``unique_by`` on ``uri`` prevents collisions within a list.
    """
    idx = draw(st.integers(min_value=0, max_value=9_999))
    connector = draw(st.sampled_from(["conn-a", "conn-b", "conn-c"]))
    key = f"{connector}/record-{idx:04d}.json"
    size = draw(st.integers(min_value=0, max_value=1_000_000))
    age_seconds = draw(
        st.floats(min_value=0.0, max_value=200_000.0, allow_nan=False, allow_infinity=False)
    )
    last_modified = _FIXED_NOW - timedelta(seconds=age_seconds)
    uri = f"s3://{bucket}/{key}"
    return {
        "Key": key,
        "Size": size,
        "LastModified": last_modified,
        "uri": uri,
    }


# --------------------------------------------------------------------------- #
# Helpers for P-ER-STATUS (uses real gc_landing_orphans + SQLite + stub LandingStore)
# --------------------------------------------------------------------------- #


def _sqlite_sessions_fresh() -> sessionmaker[Session]:
    """Create a fresh in-memory SQLite engine + sessions factory.

    Each call returns a NEW engine (and thus a NEW database), so P-ER-STATUS
    examples are fully isolated from each other during Hypothesis shrinking.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _stub_landing(
    objects: list[dict[str, Any]],
    *,
    bucket: str = _BUCKET,
) -> MagicMock:
    """Return a MagicMock(spec=LandingStore) for use with gc_landing_orphans.

    Objects must have 'Key', 'Size', 'LastModified' (no 'uri' field — gc_landing_orphans
    builds the URI internally from ``landing.bucket`` + ``obj['Key']``).
    """
    stub = MagicMock(spec=LandingStore)
    stub.bucket = bucket
    stub.list_objects_with_metadata.return_value = list(objects)
    stub.delete_keys.return_value = 0
    return stub


def _old_obj(key: str, *, bucket: str = _BUCKET) -> dict[str, Any]:
    """Build a landing object dict (no uri field) that is very old (9 999 999 seconds)."""
    return {
        "Key": key,
        "Size": 256,
        "LastModified": datetime.now(UTC) - timedelta(seconds=9_999_999),
    }


# --------------------------------------------------------------------------- #
# P-REF: a referenced object is NEVER an orphan candidate (G1 safety core)
# --------------------------------------------------------------------------- #


@given(
    objects=st.lists(
        landing_object(),
        min_size=0,
        max_size=15,
        unique_by=lambda o: o["uri"],
    ),
    ref_indices=st.frozensets(st.integers(min_value=0, max_value=14), max_size=15),
    extra_uris=st.frozensets(
        st.text(alphabet="s3abcdef0123456789:/.-_", min_size=5, max_size=25),
        max_size=5,
    ),
    min_age_seconds=st.floats(
        min_value=0.0, max_value=300_000.0, allow_nan=False, allow_infinity=False
    ),
)
@_SETTINGS
def test_p_ref_referenced_object_never_orphan_candidate(
    objects: list[dict[str, Any]],
    ref_indices: frozenset[int],
    extra_uris: frozenset[str],
    min_age_seconds: float,
) -> None:
    """P-REF: for any object whose URI is in the reference set, it is NEVER a candidate.

    G1 safety core (ADR 0086): referenced ⇒ not-orphan, unconditionally — even if the
    object is billions of seconds old or the grace window is 0.  This property must hold
    for any combination of objects, reference subset, extra URIs, and grace window.
    """
    # Build the reference set: a subset of the objects' URIs + arbitrary extra URIs.
    referenced_uris: set[str] = {objects[i]["uri"] for i in ref_indices if i < len(objects)} | set(
        extra_uris
    )

    candidates = select_orphan_candidates(
        objects,
        referenced_uris,
        now=_FIXED_NOW,
        min_age_seconds=min_age_seconds,
    )
    candidate_keys: frozenset[str] = frozenset(c["Key"] for c in candidates)

    for obj in objects:
        if obj["uri"] in referenced_uris:
            age = (_FIXED_NOW - obj["LastModified"]).total_seconds()
            assert obj["Key"] not in candidate_keys, (
                f"P-REF (G1 safety core) VIOLATED: referenced object {obj['Key']!r} "
                f"(uri={obj['uri']!r}, age={age:.0f}s, size={obj['Size']}) "
                f"was returned as an orphan candidate with "
                f"min_age_seconds={min_age_seconds:.1f}. "
                f"Referenced objects must NEVER be candidates regardless of age. (ADR 0086)"
            )


# --------------------------------------------------------------------------- #
# P-MM-MONOTONE: enlarging the reference set never grows the candidate set
# --------------------------------------------------------------------------- #


@given(
    objects=st.lists(
        landing_object(),
        min_size=0,
        max_size=10,
        unique_by=lambda o: o["uri"],
    ),
    ref_subset_indices=st.frozensets(st.integers(min_value=0, max_value=9), max_size=10),
    extra_uris=st.frozensets(
        st.text(alphabet="s3abcdef0123456789:/.-_", min_size=5, max_size=25),
        max_size=5,
    ),
    min_age_seconds=st.floats(
        min_value=0.0, max_value=200_000.0, allow_nan=False, allow_infinity=False
    ),
)
@_SETTINGS
def test_p_mm_monotone_enlarging_reference_set_never_grows_candidates(
    objects: list[dict[str, Any]],
    ref_subset_indices: frozenset[int],
    extra_uris: frozenset[str],
    min_age_seconds: float,
) -> None:
    """P-MM-MONOTONE (metamorphic): candidates(R') ⊆ candidates(R) when R ⊆ R'.

    Adding a URI to the reference set can only REMOVE objects from the candidate set.
    It can never cause a previously-non-candidate to appear as a candidate.  This is the
    metamorphic counterpart of P-REF: if it fails, some code path makes a referenced object
    MORE deletable when references are added, which is logically impossible.
    """
    # R: a subset of the objects' URIs
    R: set[str] = {objects[i]["uri"] for i in ref_subset_indices if i < len(objects)}
    # R': R enlarged with extra URIs (R ⊆ R')
    R_prime: set[str] = R | set(extra_uris)

    candidates_R = select_orphan_candidates(
        objects, R, now=_FIXED_NOW, min_age_seconds=min_age_seconds
    )
    candidates_R_prime = select_orphan_candidates(
        objects, R_prime, now=_FIXED_NOW, min_age_seconds=min_age_seconds
    )

    keys_R: frozenset[str] = frozenset(c["Key"] for c in candidates_R)
    keys_R_prime: frozenset[str] = frozenset(c["Key"] for c in candidates_R_prime)

    assert keys_R_prime <= keys_R, (
        f"P-MM-MONOTONE VIOLATED: enlarging the reference set GREW the orphan-candidate set. "
        f"|R|={len(R)}, |R'|={len(R_prime)}, min_age={min_age_seconds:.1f}s. "
        f"New candidates in R' that were NOT in R: {keys_R_prime - keys_R!r}. "
        f"Adding references must only protect more objects (ADR 0086)."
    )


# --------------------------------------------------------------------------- #
# P-ER-STATUS: an object referenced by an ErQueueItem of ANY status is never a candidate
# --------------------------------------------------------------------------- #


@given(status=st.sampled_from(sorted(ER_QUEUE_STATUSES)))
@_SETTINGS
def test_p_er_status_any_er_row_status_blocks_orphan_candidate(status: str) -> None:
    """P-ER-STATUS: gc_landing_orphans treats ErQueueItems of ANY status as references.

    This is the exact review finding from the adversarial audit (ADR 0086 D2): the
    ER reference query must be status-UNFILTERED so that a ``resolved`` or ``pending_review``
    row still protects its ``source_record`` landing object from deletion.

    Generates across all ER statuses (pending, resolved, pending_review, invalid).
    Uses a real SQLite session to exercise the actual DB query in gc_landing_orphans.

    Failure mode if a WHERE status filter is added:
    - ErQueueItem rows with filtered-out statuses would not appear in the reference set
    - Their landing objects would be counted as orphaned (orphaned > 0, referenced == 0)
    - Assertion fails with specific status and counts
    """
    sessions = _sqlite_sessions_fresh()

    key = "conn/ds/er-status-guard.json"
    uri = f"s3://{_BUCKET}/{key}"

    with sessions() as session:
        session.add(
            ErQueueItem(
                id=f"er-guard-{status}",
                connector_id="conn-guard",
                raw_entity={"id": "guard"},
                source_record=uri,
                status=status,
            )
        )
        session.commit()

    landing = _stub_landing([_old_obj(key)])

    with sessions() as session:
        stats = gc_landing_orphans(session, landing, min_age_seconds=0.0, delete=False)

    assert stats.referenced == 1, (
        f"P-ER-STATUS VIOLATED: ErQueueItem with status={status!r} was NOT included in the "
        f"GC reference set. Landing object uri={uri!r} (age=9_999_999s) was NOT protected. "
        f"referenced={stats.referenced}, orphaned={stats.orphaned}. "
        f"The ER reference query must be status-UNFILTERED — a WHERE status filter would "
        f"exclude status={status!r} rows (ADR 0086 D2 / ER-QUEUE-NEVER-HARD-DELETED invariant)."
    )
    assert stats.orphaned == 0, (
        f"P-ER-STATUS VIOLATED: orphaned={stats.orphaned} despite object being referenced "
        f"by ErQueueItem with status={status!r}. Expected orphaned=0 (ADR 0086)."
    )
    assert stats.scanned == 1, f"P-ER-STATUS: expected scanned=1, got {stats.scanned}"


# --------------------------------------------------------------------------- #
# Smoke: the helper never crashes on the empty-objects / empty-refs edge cases
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "objects,referenced_uris",
    [
        ([], set()),
        ([], {"s3://bucket/some-key.json"}),
    ],
)
def test_select_orphan_candidates_empty_inputs_return_empty(
    objects: list[dict[str, Any]],
    referenced_uris: set[str],
) -> None:
    """Edge case: no objects → empty candidate list regardless of reference set.

    Also exercises the helper with empty inputs to catch off-by-one / None-dereference
    bugs that property tests might miss when the list is drawn as empty.
    """
    candidates = select_orphan_candidates(
        objects,
        referenced_uris,
        now=_FIXED_NOW,
        min_age_seconds=0.0,
    )
    assert candidates == [], f"select_orphan_candidates([]) must return [] (got {candidates!r})"
