"""Curated connector seed — spec integrity (ADR 0115, Slice A).

The seeded configs must be VALID for their connectors (a typo'd feed config would silently fail to
ingest), and every seed id must be deterministic + unique (the property idempotency rides on). The
DB-level idempotency is covered by ``tests/integration/test_seed.py``.

Gate S-4 slice 1 (reliability threading, ADR 0120 / GATE_S4_RANSOMWARE_LIVE_SPEC.md §5, §9 AC1)
extends this module with ``test_seed_spec_reliability_round_trips_to_connector_instance``: a
``SeedSpec(reliability=...)`` must persist onto ``ConnectorInstance.reliability`` (NULL for a spec
that omits it, so every pre-S4 seed row's persisted behaviour is byte-identical). This drives
``seed()`` against an in-memory SQLite database (no Docker) — the same idiom as
``tests/unit/test_statements.py``'s ``sqlite_session`` fixture.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from worldmonitor.api.main import _discover_registry
from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.models import Base, ConnectorInstance
from worldmonitor.db.seed import SEED_CONNECTORS, SeedSpec, seed


def test_every_seed_config_is_valid_for_its_connector() -> None:
    """Each spec's config passes its connector's JSON-Schema validation (catches config typos)."""
    registry = _discover_registry()
    for spec in SEED_CONNECTORS:
        plugin = registry.get(spec.connector_id)
        # Raises jsonschema.ValidationError if the seeded config is invalid for this connector.
        plugin.validate_config(spec.config)


def test_seed_ids_are_unique() -> None:
    ids = [spec.instance_id for spec in SEED_CONNECTORS]
    assert len(ids) == len(set(ids)), "seed instance ids must be unique (idempotency depends on it)"


def test_seed_ids_are_deterministic() -> None:
    """The id derives only from (connector_id, natural_key); an identical spec yields it again."""
    for spec in SEED_CONNECTORS:
        twin = SeedSpec(
            connector_id=spec.connector_id,
            natural_key=spec.natural_key,
            config=dict(spec.config),
            enabled=spec.enabled,
            category=spec.category,
        )
        assert twin.instance_id == spec.instance_id


def test_bluesky_is_seeded_disabled() -> None:
    """The high-volume firehose ships disabled — an operator opts in from the Integrations UI."""
    bluesky = [spec for spec in SEED_CONNECTORS if spec.connector_id == "bluesky"]
    assert bluesky, "expected a bluesky seed spec"
    assert all(not spec.enabled for spec in bluesky)


def test_curated_feed_set_is_multi_category() -> None:
    """The seed spans categories (world/conflict/cyber/finance/energy/disaster) — not one topic."""
    categories = {spec.category for spec in SEED_CONNECTORS if spec.connector_id == "feeds"}
    assert len(categories) >= 5, f"expected a broad category spread, got {categories}"


def test_feed_urls_are_unique() -> None:
    """A duplicate URL would seed two instances polling the same feed (double ingest)."""
    urls = [spec.natural_key for spec in SEED_CONNECTORS if spec.connector_id == "feeds"]
    assert len(urls) == len(set(urls)), "duplicate feed URL in _CURATED_FEEDS"


def test_feed_breadth_floor() -> None:
    """WP-2a (2026-07-18) expanded the set to ~50; a regression below the floor should be loud."""
    feeds = [spec for spec in SEED_CONNECTORS if spec.connector_id == "feeds"]
    assert len(feeds) >= 45, f"expected >=45 curated feeds, got {len(feeds)}"
    assert all(spec.enabled for spec in feeds), "curated feeds seed enabled"


# ---------------------------------------------------------------------------
# Gate S-4 slice 1 — reliability threading (AC1)
# ---------------------------------------------------------------------------


# SQLite JSONB shim — same idiom as tests/unit/test_statements.py: Base.metadata spans several
# JSONB-columned tables (ErQueueItem.raw_entity, TaskRun.stats, ...) that create_all must still be
# able to stand up on an in-memory SQLite database (no Docker needed for this unit test).
@compiles(JSONB, "sqlite")
def _jsonb_as_sqlite_json(_element: Any, _compiler: Any, **_kw: Any) -> str:
    return "JSON"


def test_seed_spec_reliability_round_trips_to_connector_instance() -> None:
    """AC1 (GATE_S4_RANSOMWARE_LIVE_SPEC.md §9, ADR 0120 slice 1): ``SeedSpec(reliability="E")``
    persists to ``ConnectorInstance.reliability``; a spec that omits ``reliability`` writes NULL —
    every existing (pre-S4) seed row's persisted column stays untouched (``None``).
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    cipher = ConfigCipher(ConfigCipher.generate_key())

    graded = SeedSpec(
        "ransomware_live",
        "recentvictims",
        {"dataset": "recentvictims"},
        enabled=True,
        category="cti",
        reliability="E",
    )
    ungraded = SeedSpec(
        "feeds",
        "https://example.test/s4-slice1-feed.xml",
        {"feed_url": "https://example.test/s4-slice1-feed.xml"},
    )

    inserted, skipped = seed(sessions, cipher=cipher, specs=[graded, ungraded])
    assert skipped == []
    assert {spec.instance_id for spec in inserted} == {graded.instance_id, ungraded.instance_id}

    with sessions() as db:
        graded_row = db.get(ConnectorInstance, graded.instance_id)
        ungraded_row = db.get(ConnectorInstance, ungraded.instance_id)
        assert graded_row is not None and ungraded_row is not None
        assert graded_row.reliability == "E", (
            f"a SeedSpec(reliability='E') must persist as 'E' on the ConnectorInstance row, "
            f"got {graded_row.reliability!r}"
        )
        assert ungraded_row.reliability is None, (
            "a SeedSpec with no reliability must write NULL, not a fabricated default — got "
            f"{ungraded_row.reliability!r}"
        )
    engine.dispose()
