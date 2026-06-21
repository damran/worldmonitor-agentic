"""SQLAlchemy engine + session helpers and the schema bootstrap.

A thin, typed surface so the rest of the codebase never imports SQLAlchemy
construction directly. The **production** schema path is :func:`migrate_to_head`
(Alembic, ADR 0030); :func:`create_all` remains a test/dev convenience that
produces the same schema as ``alembic upgrade head`` (proven by the convergence
tests).
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.models import Base
from worldmonitor.settings import Settings, get_settings

# In-package migrations (ship with the wheel).
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_BASELINE_REVISION = "0001_baseline"


def make_engine(dsn: str) -> Engine:
    """Create an :class:`Engine` for ``dsn`` (expects a ``+psycopg`` driver)."""
    return create_engine(dsn)


def engine_from_settings(settings: Settings | None = None) -> Engine:
    """Create an :class:`Engine` from the process settings."""
    return make_engine((settings or get_settings()).sqlalchemy_dsn)


def create_all(engine: Engine) -> None:
    """Create all tables defined on :class:`Base` (test/dev only; see :func:`migrate_to_head`)."""
    Base.metadata.create_all(engine)


def _alembic_config(engine: Engine) -> Config:
    """An Alembic config targeting ``engine``'s database (URL-driven, env.py manages
    its own connection + transaction so DDL is committed unambiguously)."""
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    # render the password (str(url) masks it); escape % for ConfigParser interpolation.
    url = engine.url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", url)
    return config


def migrate_to_head(engine: Engine) -> None:
    """Bring ``engine``'s database to the latest schema via Alembic (ADR 0030).

    Production schema management — and the adoption path for a database created
    before Alembic existed:

    * an Alembic-managed database (has ``alembic_version``) → ``upgrade head``;
    * a database with no ``er_queue_item`` → a fresh install → built from base;
    * an existing ``er_queue_item`` that already has ``entity_id`` → already at the
      current schema → stamped at head (no DDL);
    * a **pre-runway** ``er_queue_item`` (no ``entity_id``) → stamped at the baseline
      then upgraded, so it converges on the fresh-install schema. This is the
      previously-broken case (``create_all`` never ALTERed an existing table).
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    config = _alembic_config(engine)
    if "alembic_version" in tables or "er_queue_item" not in tables:
        command.upgrade(config, "head")
    elif "entity_id" in {column["name"] for column in inspector.get_columns("er_queue_item")}:
        command.stamp(config, "head")
    else:
        command.stamp(config, _BASELINE_REVISION)
        command.upgrade(config, "head")


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured session factory bound to ``engine``."""
    return sessionmaker(bind=engine)
