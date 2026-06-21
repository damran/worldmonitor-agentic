"""Integration test: OpenSanctions collect → landing zone → ER queue.

Streams a small *live* OpenSanctions dataset (capped) into ephemeral MinIO +
Postgres and asserts the raw lands and candidates are enqueued with provenance.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ErQueueItem
from worldmonitor.plugins.connectors.opensanctions import OpenSanctionsConnector
from worldmonitor.runner.ingest import run_ingest
from worldmonitor.storage.landing import LandingStore

pytestmark = pytest.mark.integration

_DATASET = "ie_unlawful_organizations"


def test_collect_land_queue(minio: tuple[str, str, str], postgres_dsn: str, tenant_id: str) -> None:
    endpoint, access_key, secret_key = minio
    landing = LandingStore.connect(
        endpoint=endpoint, access_key=access_key, secret_key=secret_key, bucket="landing"
    )
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)

    connector = OpenSanctionsConnector()
    with sessions() as session:
        stats = run_ingest(
            connector,
            {"dataset": _DATASET, "limit": 4},
            tenant_id=tenant_id,
            landing=landing,
            session=session,
        )

    assert stats.collected >= 1
    assert stats.collected == stats.landed == stats.queued

    # Raw records landed in MinIO under the tenant/connector prefix.
    keys = landing.list_keys(prefix=f"{tenant_id}/opensanctions/")
    assert len(keys) == stats.landed

    # Candidates are in the ER queue, tenant-scoped, with provenance + landing pointer.
    with sessions() as session:
        count = session.execute(
            select(func.count()).select_from(ErQueueItem).where(ErQueueItem.tenant_id == tenant_id)
        ).scalar_one()
        assert count == stats.queued

        # Tenant-scope this lookup: the ER queue is shared (session-scoped Postgres),
        # so an unfiltered limit(1) could pick another test's row.
        row = session.execute(
            select(ErQueueItem).where(ErQueueItem.tenant_id == tenant_id).limit(1)
        ).scalar_one()
        assert row.connector_id == "opensanctions"
        assert row.raw_entity["schema"]
        assert row.raw_entity["wm_prov_source_id"] == [f"opensanctions:{_DATASET}"]
        assert row.source_record.startswith("s3://landing/")

    engine.dispose()
