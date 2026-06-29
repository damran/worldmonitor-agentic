"""Dialect-aware migration lock-timeout guard (ADR 0084, audit M-5).

Provides :func:`apply_migration_timeouts`, a pure helper called by ``env.py``
BEFORE every migration batch.  Postgres-only GUCs are silently skipped on
non-Postgres dialects (SQLite, etc.) — issuing ``SET LOCAL`` on SQLite raises
a syntax error that would break the unit-test suite's in-memory migrations.

This module is a regular importable Python module (it is NOT executed by
alembic as a script), so tests can import it directly without side-effects.
"""

from __future__ import annotations

from sqlalchemy import Connection, text

from worldmonitor.settings import get_settings


def apply_migration_timeouts(connection: Connection) -> None:
    """Issue ``SET LOCAL lock_timeout`` (and optionally ``statement_timeout``) on the
    connection *before* running Alembic migrations.

    **Postgres-only:** if ``connection.dialect.name != "postgresql"`` this function
    returns immediately without executing anything.  This is intentional: SQLite (used
    in unit tests) does not support ``SET LOCAL`` and would raise a syntax error.

    **SET LOCAL:** the timeouts are scoped to the current migration transaction; they
    revert to their session-level values when the transaction commits or rolls back.
    This prevents the setting from bleeding onto the shared app connection that may be
    passed in via ``config.attributes["connection"]``.

    **Opt-out via 0:** ``migration_lock_timeout_ms = 0`` means "do not set" — the
    Postgres default (no timeout) is preserved.  Useful when running the migrate-while-
    stopped procedure from the runbook where no live traffic competes for the DDL lock.

    See ``docs/runbooks/migrations.md`` and ``docs/decisions/0084-online-migration-safety.md``.
    """
    if connection.dialect.name != "postgresql":
        return

    settings = get_settings()

    if settings.migration_lock_timeout_ms > 0:
        # Fail fast if the DDL lock cannot be acquired — prefer a fast abort over stalling
        # the driver's enqueue path for an indefinite period (M-5 finding).
        connection.execute(
            text(f"SET LOCAL lock_timeout = '{settings.migration_lock_timeout_ms}ms'")
        )

    if settings.migration_statement_timeout_ms > 0:
        # Hard wall-clock cap on any single SQL statement in the migration transaction.
        # OFF by default (0) because some migrations (e.g. long index builds) are legitimately slow.
        connection.execute(
            text(f"SET LOCAL statement_timeout = '{settings.migration_statement_timeout_ms}ms'")
        )
