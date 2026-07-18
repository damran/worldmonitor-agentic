"""Curated connector seed — spec integrity (ADR 0115, Slice A).

The seeded configs must be VALID for their connectors (a typo'd feed config would silently fail to
ingest), and every seed id must be deterministic + unique (the property idempotency rides on). The
DB-level idempotency is covered by ``tests/integration/test_seed.py``.
"""

from __future__ import annotations

from worldmonitor.api.main import _discover_registry
from worldmonitor.db.seed import SEED_CONNECTORS, SeedSpec


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
