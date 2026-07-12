"""Seed curated connector instances so a fresh stack ingests real data (ADR 0115, Slice A).

A fresh WorldMonitor stack boots with an empty ``connector_instance`` table, so the driver has
nothing to ingest and the graph stays empty. This module seeds a curated starter set — real
RSS/Atom news feeds across categories, a bounded OpenSanctions dataset (real resolved
Person/Company/Sanction entities), and the Bluesky firehose (seeded *disabled* — the operator
flips it on from the Integrations UI when they want the live social pulse).

**Idempotent by construction.** Each instance's primary key is a deterministic ``uuid5`` of
``(connector_id, natural_key)``, so re-running never duplicates a row and never clobbers an
operator's later edits: an already-present id is skipped, leaving its ``status`` / ``next_run`` /
``config`` untouched. (Deleting a seeded row and re-seeding *will* re-create it enabled — disable,
don't delete, to keep a seed connector off.)

Run as ``python -m worldmonitor.db.seed`` (needs ``CONFIG_ENCRYPTION_KEY`` + ``POSTGRES_DSN`` in
the environment, exactly like ``python -m worldmonitor.db.migrate``), or via the compose ``seed``
one-shot service. The curated set below is a starter — expand it from the OSINT source inventory
or the koala73 feed-list harvest (``docs/fable-review/91_OG_HARVEST_BACKLOG.md``).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.crypto import ConfigCipher
from worldmonitor.db.engine import engine_from_settings, session_factory
from worldmonitor.db.models import ConnectorInstance
from worldmonitor.settings import get_settings

logger = logging.getLogger(__name__)

# Fixed namespace for deterministic seed ids. NEVER change this — it is the anchor for
# idempotency; a new value would orphan every previously-seeded row and re-create duplicates.
_SEED_NAMESPACE = uuid.UUID("6f6d0e2c-3d1a-4a9b-9c2e-1b0d5f7a2c40")


@dataclass(frozen=True, slots=True)
class SeedSpec:
    """One curated connector instance to seed.

    ``natural_key`` is the stable identity *within* a connector (a feed's URL, a dataset name,
    ``"firehose"`` for a single stream) — it makes the id deterministic without depending on the
    (Fernet-nondeterministic) encrypted config. ``category`` is informational only (the driver
    never reads it; it is not stored — the feeds schema forbids extra keys).
    """

    connector_id: str
    natural_key: str
    config: dict[str, Any]
    enabled: bool = True
    category: str = ""

    @property
    def instance_id(self) -> str:
        return str(uuid.uuid5(_SEED_NAMESPACE, f"{self.connector_id}:{self.natural_key}"))


# Curated RSS/Atom feeds: (category, feed_url, max_items). Per-feed failures are isolated by the
# driver (one dead feed backs off; it never aborts a tick), so a stale URL here is self-healing.
# USGS / GDACS are GeoRSS sources that carry native coordinates — high value once geo-parsing
# lands (Slice B/C); today they still import as Articles.
_CURATED_FEEDS: tuple[tuple[str, str, int], ...] = (
    # World / geopolitics
    ("world", "https://feeds.bbci.co.uk/news/world/rss.xml", 60),
    ("world", "https://www.aljazeera.com/xml/rss/all.xml", 60),
    ("world", "https://www.theguardian.com/world/rss", 60),
    ("world", "https://www.france24.com/en/rss", 50),
    ("world", "https://rss.dw.com/rdf/rss-en-world", 50),
    ("world", "https://news.un.org/feed/subscribe/en/news/all/rss.xml", 50),
    ("world", "https://feeds.npr.org/1004/rss.xml", 50),
    # Conflict / security / humanitarian
    ("conflict", "https://www.longwarjournal.org/feed", 40),
    ("conflict", "https://reliefweb.int/updates/rss.xml", 50),
    # Cyber / tech
    ("cyber", "https://krebsonsecurity.com/feed/", 30),
    ("cyber", "https://feeds.feedburner.com/TheHackersNews", 40),
    ("cyber", "https://www.bleepingcomputer.com/feed/", 40),
    ("tech", "https://feeds.arstechnica.com/arstechnica/index", 40),
    # Finance / economy
    ("finance", "https://www.cnbc.com/id/100003114/device/rss/rss.html", 40),
    ("finance", "https://www.cnbc.com/id/100727362/device/rss/rss.html", 40),
    # Energy / commodities
    ("energy", "https://oilprice.com/rss/main", 40),
    # Disaster / geo (native GeoRSS coordinates)
    ("disaster", "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.atom", 100),
    ("disaster", "https://www.gdacs.org/xml/rss.xml", 60),
)


def _feed_specs() -> list[SeedSpec]:
    return [
        SeedSpec("feeds", url, {"feed_url": url, "max_items": max_items}, True, category)
        for category, url, max_items in _CURATED_FEEDS
    ]


# The full curated seed set: every feed + a bounded OpenSanctions dataset + the Bluesky firehose
# (disabled by default — high-volume, off-topic for a news dashboard until an operator opts in).
SEED_CONNECTORS: tuple[SeedSpec, ...] = (
    *_feed_specs(),
    SeedSpec(
        "opensanctions",
        "us_ofac_sdn",
        {"dataset": "us_ofac_sdn", "limit": 3000},
        enabled=True,
        category="sanctions",
    ),
    SeedSpec("bluesky", "firehose", {}, enabled=False, category="social"),
)


def seed(
    sessions: sessionmaker[Session],
    *,
    cipher: ConfigCipher,
    specs: Sequence[SeedSpec] = SEED_CONNECTORS,
) -> tuple[list[SeedSpec], list[SeedSpec]]:
    """Insert any not-yet-present curated instances; return ``(inserted, skipped)``.

    An instance already present (by its deterministic id) is left exactly as it is — this is what
    makes re-seeding safe against operator edits.
    """
    inserted: list[SeedSpec] = []
    skipped: list[SeedSpec] = []
    with sessions() as db:
        for spec in specs:
            if db.get(ConnectorInstance, spec.instance_id) is not None:
                skipped.append(spec)
                continue
            db.add(
                ConnectorInstance(
                    id=spec.instance_id,
                    connector_id=spec.connector_id,
                    config_encrypted=cipher.encrypt(json.dumps(spec.config)),
                    status="enabled" if spec.enabled else "disabled",
                )
            )
            inserted.append(spec)
        db.commit()
    return inserted, skipped


def main() -> None:
    """Seed the database named by the process settings, then exit."""
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    cipher = ConfigCipher.from_settings(settings)
    sessions = session_factory(engine_from_settings(settings))
    inserted, skipped = seed(sessions, cipher=cipher)
    logger.info(
        "seed complete: %d inserted, %d already present (%d curated total)",
        len(inserted),
        len(skipped),
        len(SEED_CONNECTORS),
    )
    for spec in inserted:
        state = "enabled" if spec.enabled else "disabled"
        logger.info(
            "  + %-14s %-9s %s (%s)", spec.connector_id, state, spec.natural_key, spec.category
        )


if __name__ == "__main__":
    main()
