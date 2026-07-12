"""Curated connector seed — DB idempotency (ADR 0115, Slice A).

Runs against an ephemeral Postgres (testcontainers). Proves the seed is safe to re-run on every
``docker compose up``: it inserts the curated set once, re-running inserts nothing new, and an
operator's later edit (a disabled feed) survives a re-seed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import create_all, make_engine, session_factory
from worldmonitor.db.models import ConnectorInstance
from worldmonitor.db.seed import SEED_CONNECTORS, SeedSpec, seed

pytestmark = pytest.mark.integration


def _count(sessions: object) -> int:
    with sessions() as db:  # type: ignore[operator]
        return db.execute(select(func.count()).select_from(ConnectorInstance)).scalar_one()


def test_seed_is_idempotent_and_preserves_operator_edits(postgres_dsn: str) -> None:
    engine = make_engine(postgres_dsn)
    create_all(engine)
    sessions = session_factory(engine)
    cipher = ConfigCipher(ConfigCipher.generate_key())

    # First seed: inserts the whole curated set.
    inserted, skipped = seed(sessions, cipher=cipher)
    assert len(inserted) == len(SEED_CONNECTORS)
    assert skipped == []
    assert _count(sessions) == len(SEED_CONNECTORS)

    # The Bluesky firehose is seeded disabled; a curated feed is enabled.
    with sessions() as db:
        bluesky_id = SeedSpec("bluesky", "firehose", {}).instance_id
        assert db.get(ConnectorInstance, bluesky_id).status == "disabled"

    # An operator disables a seeded feed.
    target = next(spec for spec in SEED_CONNECTORS if spec.connector_id == "feeds")
    with sessions() as db:
        row = db.get(ConnectorInstance, target.instance_id)
        assert row.status == "enabled"
        row.status = "disabled"
        db.commit()

    # Re-seed: nothing new, no duplicates, and the operator's edit survives untouched.
    inserted2, skipped2 = seed(sessions, cipher=cipher)
    assert inserted2 == []
    assert len(skipped2) == len(SEED_CONNECTORS)
    assert _count(sessions) == len(SEED_CONNECTORS)
    with sessions() as db:
        assert db.get(ConnectorInstance, target.instance_id).status == "disabled"

    engine.dispose()
