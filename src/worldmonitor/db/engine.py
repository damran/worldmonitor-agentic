"""SQLAlchemy engine + session helpers and the schema bootstrap.

A thin, typed surface so the rest of the codebase never imports SQLAlchemy
construction directly. ``create_all`` is idempotent — safe to call on startup
until migrations (Alembic) are introduced.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.models import Base
from worldmonitor.settings import Settings, get_settings


def make_engine(dsn: str) -> Engine:
    """Create an :class:`Engine` for ``dsn`` (expects a ``+psycopg`` driver)."""
    return create_engine(dsn)


def engine_from_settings(settings: Settings | None = None) -> Engine:
    """Create an :class:`Engine` from the process settings."""
    return make_engine((settings or get_settings()).sqlalchemy_dsn)


def create_all(engine: Engine) -> None:
    """Idempotently create all tables defined on :class:`Base`."""
    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured session factory bound to ``engine``."""
    return sessionmaker(bind=engine)
