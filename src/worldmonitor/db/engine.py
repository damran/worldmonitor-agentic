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
from sqlalchemy import Engine, Inspector, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

from worldmonitor.db.models import Base
from worldmonitor.settings import Settings, get_settings

# In-package migrations (ship with the wheel).
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_BASELINE_REVISION = "0001_baseline"


class SchemaIncompleteError(RuntimeError):
    """A pre-Alembic database looks post-runway but is missing head tables/columns.

    Raised by :func:`migrate_to_head` when an unmanaged ``er_queue_item`` carries
    ``entity_id`` (so it is post-runway, not pre-runway) yet the live schema does
    not contain every table/column the ORM models (``Base.metadata`` == head)
    expect — the partial-restore hazard. Per ADR 0056 we fail closed: refuse to
    blind-stamp such a database as current rather than silently mark a partial
    restore fully-migrated (which would leave human-decision durability tables —
    ``sign_off``/``resolver_judgement`` — and the no-un-merge ledger absent).
    """


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


def _missing_schema_items(inspector: Inspector) -> list[str]:
    """Items the live schema lacks vs the ORM models (``Base.metadata`` == head).

    Returns a sorted list of ``"table"`` (a wholly-missing expected table) and
    ``"table.column"`` (a missing column on a present table) strings; an empty
    list means the database holds the full head schema (ADR 0056). Read-only —
    introspection plus a comparison, no DDL.
    """
    present_tables = set(inspector.get_table_names())
    missing: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in present_tables:
            missing.append(table_name)
            continue
        live_columns = {column["name"] for column in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in live_columns:
                missing.append(f"{table_name}.{column.name}")
    return sorted(missing)


def migrate_to_head(engine: Engine) -> None:
    """Bring ``engine``'s database to the latest schema via Alembic (ADR 0030).

    Production schema management — and the adoption path for a database created
    before Alembic existed:

    * an Alembic-managed database (has ``alembic_version``) → ``upgrade head``;
    * a database with no ``er_queue_item`` → a fresh install → built from base;
    * an existing ``er_queue_item`` that already has ``entity_id`` (so it is
      post-runway) → adopted ONLY if the full head schema is present → stamped at
      head (no DDL). If the schema is incomplete (a partial restore) →
      :class:`SchemaIncompleteError` (ADR 0056: fail closed, never blind-stamp);
    * a **pre-runway** ``er_queue_item`` (no ``entity_id``) → stamped at the baseline
      then upgraded, so it converges on the fresh-install schema. This is the
      previously-broken case (``create_all`` never ALTERed an existing table).
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    config = _alembic_config(engine)
    if "alembic_version" in tables or "er_queue_item" not in tables:
        command.upgrade(config, "head")
        return
    has_entity_id = "entity_id" in {
        column["name"] for column in inspector.get_columns("er_queue_item")
    }
    if has_entity_id:
        # Post-runway, unmanaged DB. Adopt by stamping head ONLY when the full
        # head schema is present; refuse a partial restore (ADR 0056).
        missing = _missing_schema_items(inspector)
        if missing:
            raise SchemaIncompleteError(
                "refusing to adopt a partial/inconsistent database as current — "
                f"missing: {missing}; resolve the restore before bootstrapping (ADR 0056)"
            )
        command.stamp(config, "head")
    else:
        command.stamp(config, _BASELINE_REVISION)
        command.upgrade(config, "head")


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured session factory bound to ``engine``."""
    return sessionmaker(bind=engine)
