"""Gate B-4a slice-2 — FAILING-FIRST oracle for the judge's DENY (1 BLOCKER + 2 safety gaps).

The adversarial judge denied the cross-store GDPR erase (``erasure.py`` / ``storage/landing.py``,
ADR 0049) on three counts; this file pins all three as RED-now tests, independent of the fix:

  * **BLOCKER — landing erase pagination.** ``LandingStore.list_keys`` does ONE
    ``list_objects_v2`` (S3/MinIO cap = 1000 keys per page, no ``ContinuationToken`` loop), so
    ``delete_prefix`` (and therefore ``erase_source``'s landing sweep + its audited
    ``landing_objects_deleted`` count) only ever sees / erases the first 1000 objects. Any real
    source exceeds 1000 → PII silently survives a GDPR erase. The integration test below seeds
    1100 objects on a live MinIO and proves all 1100 must be listed, erased, and counted.
  * **SAFETY — over-delete guard.** A ``source_id`` lacking ``':'`` (a bare connector_id) is
    turned by ``_landing_prefix`` into a bare ``"connector/"`` prefix that would sweep EVERY
    dataset under that connector (and ``""`` → ``"/"`` sweeps the WHOLE bucket). Must be rejected.
  * **SAFETY — blank authorization.** ``erase_source(..., authorized_by="")`` (or whitespace) is
    currently accepted and audited as ``""`` — a weak/forgeable trail. Must be rejected.

The two safety tests are pure-unit (Docker-free, UNMARKED) so they run in the default quality job;
the pagination test is ``integration``-marked (MinIO testcontainer). None of these modify the 13
frozen erasure tests; the pagination fix + the two guards must keep T1–T7 green.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from worldmonitor.erasure import (  # pyright: ignore[reportPrivateUsage]
    _landing_prefix,
    erase_source,
)
from worldmonitor.storage.landing import LandingStore

# ================================================ Docker-free pure-logic guards (no mark)


class _NoStore:
    """A store stand-in that fails LOUDLY on ANY attribute access.

    Lets a guard test prove ``erase_source`` rejects bad arguments BEFORE touching a single store:
    a correct guard raises ``ValueError`` up-front, so none of these traps ever fire. Pre-fix,
    ``erase_source`` stages its ``TaskRun`` audit row first (``session.add``), so the trap fires
    with an ``AssertionError`` instead of the required ``ValueError`` — RED for the right reason.
    """

    def __init__(self, label: str) -> None:
        self.__dict__["_label"] = label

    def __getattr__(self, name: str) -> object:
        label = self.__dict__.get("_label", "?")
        raise AssertionError(
            f"erase_source touched the {label!r} store (.{name}) before validating its arguments"
        )


@pytest.mark.parametrize("bad_source_id", ["connectoronly", ""])
def test_landing_prefix_rejects_connector_wide_source_id(bad_source_id: str) -> None:
    """OVER-DELETE GUARD. A ``source_id`` with no ``':'`` must be REJECTED, never turned into a
    bare ``"connector/"`` prefix (sweeps every dataset under the connector) — and ``""`` must not
    become ``"/"`` (sweeps the whole bucket). RED now: ``_landing_prefix`` returns those prefixes
    instead of raising (``"connectoronly"`` -> ``"connectoronly/"``, ``""`` -> ``"/"``)."""
    with pytest.raises(ValueError):
        _landing_prefix(bad_source_id)


@pytest.mark.parametrize("bad_source_id", ["connectoronly", ""])
def test_erase_source_rejects_connector_wide_source_id_before_touching_stores(
    bad_source_id: str,
) -> None:
    """OVER-DELETE GUARD via the public entrypoint. ``erase_source`` on a connector-wide
    ``source_id`` raises ``ValueError`` BEFORE touching any store (no landing sweep, no audit row).
    RED now: validation is absent, so the call reaches ``session.add`` (the ``_NoStore`` trap)."""
    with pytest.raises(ValueError):
        erase_source(
            neo4j=_NoStore("neo4j"),  # type: ignore[arg-type]
            session=_NoStore("session"),  # type: ignore[arg-type]
            landing=_NoStore("landing"),  # type: ignore[arg-type]
            source_id=bad_source_id,
            authorized_by="dpo@worldmonitor",
        )


@pytest.mark.parametrize("blank_auth", ["", " ", "   ", "\t", "\n"])
def test_erase_source_rejects_blank_authorization_before_touching_stores(blank_auth: str) -> None:
    """BLANK-AUTH GUARD. ``erase_source`` with an empty / whitespace-only ``authorized_by`` raises
    ``ValueError`` BEFORE touching any store — an erase can never be run (or audited) without a
    named human operator. RED now: a blank operator is accepted, so the call reaches
    ``session.add`` (the ``_NoStore`` trap) and would persist an ``authorized_by=""`` audit row."""
    with pytest.raises(ValueError):
        erase_source(
            neo4j=_NoStore("neo4j"),  # type: ignore[arg-type]
            session=_NoStore("session"),  # type: ignore[arg-type]
            landing=_NoStore("landing"),  # type: ignore[arg-type]
            source_id="testsrc:people",
            authorized_by=blank_auth,
        )


# ========================================================= integration scaffolding (MinIO)


def _landing(minio: tuple[str, str, str]) -> LandingStore:
    """A LandingStore on a per-test bucket (a shared MinIO would otherwise bleed across tests)."""
    endpoint, access_key, secret_key = minio
    store = LandingStore.connect(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=f"landing-{uuid.uuid4().hex[:8]}",
    )
    store.ensure_bucket()
    return store


def _true_object_count(landing: LandingStore, prefix: str) -> int:
    """Count objects under ``prefix`` via a boto3 paginator — an oracle INDEPENDENT of the SUT's
    own (currently 1000-capped) ``list_keys``, so the precondition is provably non-vacuous."""
    paginator = landing.client.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=landing.bucket, Prefix=prefix):
        count += len(page.get("Contents", []))
    return count


def _seed_objects(landing: LandingStore, prefix: str, n: int) -> None:
    """Put ``n`` tiny PII objects under ``prefix`` (threaded; boto3 clients are thread-safe)."""

    def _put(i: int) -> None:
        landing.put(f"{prefix}rec-{i:05d}.json", b'{"pii": 1}')

    with ThreadPoolExecutor(max_workers=16) as pool:
        for _ in pool.map(_put, range(n)):
            pass


# =========================================================================== BLOCKER (pagination)


@pytest.mark.integration
def test_landing_list_and_delete_paginate_past_the_1000_key_cap(
    minio: tuple[str, str, str],
) -> None:
    """BLOCKER. With > 1000 objects under one source's landing prefix, ``list_keys`` must page past
    the S3/MinIO 1000-keys-per-page cap, and ``delete_prefix`` must erase EVERY object and return
    the TRUE count (the value audited as ``landing_objects_deleted``) — not the 1000-key cap.

    RED now (live MinIO): a single ``list_objects_v2`` sees only 1000 keys, so ``delete_prefix``
    erases 1000 and returns 1000, leaving 100 PII objects behind. GREEN after a
    ``ContinuationToken`` loop.
    """
    landing = _landing(minio)
    prefix = "testsrc/bulk/"
    total = 1100  # strictly greater than the S3/MinIO 1000-keys-per-list-page cap

    _seed_objects(landing, prefix, total)

    # Non-vacuous precondition via the INDEPENDENT paginator oracle: > 1000 objects really exist.
    assert _true_object_count(landing, prefix) == total, "seeding must place > 1000 objects"

    # Observe every relevant quantity WITHOUT asserting, so a single failure surfaces ALL of the
    # evidence (the 1000-key cap, the under-count, and the 100 objects left behind).
    listed_before = len(landing.list_keys(prefix=prefix))
    deleted = landing.delete_prefix(prefix)
    remaining = _true_object_count(landing, prefix)
    listed_after = landing.list_keys(prefix=prefix)

    assert (listed_before, deleted, remaining, listed_after) == (total, total, 0, []), (
        f"S3/MinIO 1000-key page cap not paged: list_keys saw {listed_before}/{total}, "
        f"delete_prefix erased+returned {deleted}/{total} and LEFT {remaining} PII objects "
        f"({len(listed_after)} still listed) — needs a ContinuationToken loop"
    )
