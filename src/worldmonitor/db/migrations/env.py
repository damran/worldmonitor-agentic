"""Alembic environment for the worldmonitor schema (ADR 0030).

Resolution order for the target database:
1. a live ``connection`` passed in ``config.attributes`` (how ``migrate_to_head``
   shares the app's engine);
2. the ``sqlalchemy.url`` config option (the ``alembic`` CLI);
3. the process settings DSN.

``target_metadata`` is the live ORM metadata so ``--autogenerate`` and the
convergence test compare the migrations against the models.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from worldmonitor.db.models import Base
from worldmonitor.settings import get_settings

target_metadata = Base.metadata


def _url() -> str:
    return context.config.get_main_option("sqlalchemy.url") or get_settings().sqlalchemy_dsn


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run() -> None:
    if context.is_offline_mode():
        context.configure(
            url=_url(),
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    shared = context.config.attributes.get("connection")
    if isinstance(shared, Connection):
        _run(shared)
    else:
        engine = create_engine(_url(), poolclass=pool.NullPool)
        with engine.connect() as connection:
            _run(connection)


run()
