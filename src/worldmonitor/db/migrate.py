"""CLI entrypoint: bring the configured database to the latest schema (ADR 0030).

Run as ``python -m worldmonitor.db.migrate`` (or in a deploy step). Unlike a bare
``alembic upgrade head``, this handles first-time Alembic **adoption** of a
pre-runway database — see :func:`worldmonitor.db.engine.migrate_to_head`.
"""

from __future__ import annotations

import logging

from worldmonitor.db.engine import engine_from_settings, migrate_to_head

logger = logging.getLogger(__name__)


def main() -> None:
    """Migrate the database named by the process settings to head, then exit."""
    logging.basicConfig(level=logging.INFO)
    engine = engine_from_settings()
    try:
        migrate_to_head(engine)
        logger.info("database is at the latest schema (alembic head)")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
