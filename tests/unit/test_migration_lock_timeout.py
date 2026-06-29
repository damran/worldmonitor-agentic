"""Unit tests for the dialect-aware migration lock-timeout guard (ADR 0084, audit M-5).

These tests exercise :func:`worldmonitor.db._migration_guard.apply_migration_timeouts`
against mock connections — no real database is needed here.  The key invariants:

* **Non-Postgres dialects** (SQLite, etc.) → the function returns immediately without
  calling ``connection.execute`` at all (``SET LOCAL`` is not valid SQL on SQLite).
* **Postgres + positive ms** → ``SET LOCAL lock_timeout = '<n>ms'`` is executed.
* **Postgres + ms=0** → no ``SET LOCAL`` is issued (opt-out).
* **Postgres + statement_timeout positive** → ``SET LOCAL statement_timeout`` is executed.
* **Postgres + statement_timeout=0** → no statement_timeout SET is issued.

The tests are intentionally import-only (no testcontainer spin-up) so they run in the
fast ``pytest -m "not integration"`` path.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from worldmonitor.db._migration_guard import apply_migration_timeouts


def _mock_connection(dialect_name: str) -> MagicMock:
    """Return a mock SQLAlchemy Connection whose dialect.name is ``dialect_name``."""
    conn = MagicMock()
    conn.dialect.name = dialect_name
    return conn


def _mock_settings(
    *,
    lock_ms: int = 3000,
    stmt_ms: int = 0,
) -> MagicMock:
    """Return a mock Settings object with the given migration timeout values."""
    s = MagicMock()
    s.migration_lock_timeout_ms = lock_ms
    s.migration_statement_timeout_ms = stmt_ms
    return s


# ---------------------------------------------------------------------------
# Dialect-awareness: non-Postgres paths must NOT execute SET LOCAL
# ---------------------------------------------------------------------------


def test_sqlite_dialect_skips_all_sets() -> None:
    """On a SQLite connection, apply_migration_timeouts executes nothing."""
    conn = _mock_connection("sqlite")
    with patch(
        "worldmonitor.db._migration_guard.get_settings",
        return_value=_mock_settings(lock_ms=3000, stmt_ms=1000),
    ):
        apply_migration_timeouts(conn)

    conn.execute.assert_not_called()


@pytest.mark.parametrize(
    "dialect",
    ["mssql", "mysql", "oracle", "duckdb"],
    ids=["mssql", "mysql", "oracle", "duckdb"],
)
def test_non_postgres_dialect_skips_all_sets(dialect: str) -> None:
    """Any non-Postgres dialect is silently skipped, regardless of configured values."""
    conn = _mock_connection(dialect)
    with patch(
        "worldmonitor.db._migration_guard.get_settings",
        return_value=_mock_settings(lock_ms=5000, stmt_ms=5000),
    ):
        apply_migration_timeouts(conn)

    conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Postgres: lock_timeout
# ---------------------------------------------------------------------------


def test_postgres_positive_lock_timeout_issues_set_local() -> None:
    """Postgres + migration_lock_timeout_ms > 0 → SET LOCAL lock_timeout issued."""
    conn = _mock_connection("postgresql")
    with patch(
        "worldmonitor.db._migration_guard.get_settings",
        return_value=_mock_settings(lock_ms=3000, stmt_ms=0),
    ):
        apply_migration_timeouts(conn)

    conn.execute.assert_called_once()
    issued_sql = str(conn.execute.call_args[0][0])
    assert "SET LOCAL lock_timeout" in issued_sql
    assert "3000ms" in issued_sql


def test_postgres_lock_timeout_zero_skips_set_local() -> None:
    """migration_lock_timeout_ms=0 (opt-out) → no SET LOCAL lock_timeout on Postgres."""
    conn = _mock_connection("postgresql")
    with patch(
        "worldmonitor.db._migration_guard.get_settings",
        return_value=_mock_settings(lock_ms=0, stmt_ms=0),
    ):
        apply_migration_timeouts(conn)

    conn.execute.assert_not_called()


def test_postgres_lock_timeout_value_embedded_correctly() -> None:
    """The ms value is embedded in the SQL text, not a bind parameter (Postgres GUC syntax)."""
    conn = _mock_connection("postgresql")
    with patch(
        "worldmonitor.db._migration_guard.get_settings",
        return_value=_mock_settings(lock_ms=1500, stmt_ms=0),
    ):
        apply_migration_timeouts(conn)

    issued_sql = str(conn.execute.call_args[0][0])
    assert "1500ms" in issued_sql
    # Must be SET LOCAL (transaction-scoped), NOT bare SET (session-scoped)
    assert "LOCAL" in issued_sql


# ---------------------------------------------------------------------------
# Postgres: statement_timeout
# ---------------------------------------------------------------------------


def test_postgres_positive_statement_timeout_issues_set_local() -> None:
    """migration_statement_timeout_ms > 0 → SET LOCAL statement_timeout issued on Postgres."""
    conn = _mock_connection("postgresql")
    with patch(
        "worldmonitor.db._migration_guard.get_settings",
        return_value=_mock_settings(lock_ms=0, stmt_ms=60000),
    ):
        apply_migration_timeouts(conn)

    conn.execute.assert_called_once()
    issued_sql = str(conn.execute.call_args[0][0])
    assert "SET LOCAL statement_timeout" in issued_sql
    assert "60000ms" in issued_sql


def test_postgres_statement_timeout_zero_skips_set_local() -> None:
    """migration_statement_timeout_ms=0 (default) → no statement_timeout SET issued."""
    conn = _mock_connection("postgresql")
    with patch(
        "worldmonitor.db._migration_guard.get_settings",
        return_value=_mock_settings(lock_ms=0, stmt_ms=0),
    ):
        apply_migration_timeouts(conn)

    conn.execute.assert_not_called()


def test_postgres_both_timeouts_positive_issues_two_sets() -> None:
    """Both lock_timeout and statement_timeout positive → two SET LOCAL calls in order."""
    conn = _mock_connection("postgresql")
    with patch(
        "worldmonitor.db._migration_guard.get_settings",
        return_value=_mock_settings(lock_ms=2000, stmt_ms=30000),
    ):
        apply_migration_timeouts(conn)

    assert conn.execute.call_count == 2
    calls = conn.execute.call_args_list
    # calls[i][0][0] is the first positional arg (the TextClause); str() gives the SQL text.
    first_sql = str(calls[0][0][0])
    second_sql = str(calls[1][0][0])
    assert "lock_timeout" in first_sql
    assert "2000ms" in first_sql
    assert "statement_timeout" in second_sql
    assert "30000ms" in second_sql


# ---------------------------------------------------------------------------
# Settings defaults: verify the new fields are accepted by the Settings model
# ---------------------------------------------------------------------------


def test_settings_default_migration_lock_timeout_ms() -> None:
    """migration_lock_timeout_ms defaults to 3000 (3 s fail-fast)."""
    from worldmonitor.settings import Settings

    s = Settings()
    assert s.migration_lock_timeout_ms == 3000


def test_settings_default_migration_statement_timeout_ms() -> None:
    """migration_statement_timeout_ms defaults to 0 (disabled)."""
    from worldmonitor.settings import Settings

    s = Settings()
    assert s.migration_statement_timeout_ms == 0


def test_settings_migration_lock_timeout_zero_accepted() -> None:
    """migration_lock_timeout_ms=0 is accepted (opt-out)."""
    from worldmonitor.settings import Settings

    s = Settings(migration_lock_timeout_ms=0)
    assert s.migration_lock_timeout_ms == 0


def test_settings_migration_lock_timeout_negative_rejected() -> None:
    """Negative migration_lock_timeout_ms is rejected by pydantic (ge=0)."""
    from pydantic import ValidationError

    from worldmonitor.settings import Settings

    with pytest.raises(ValidationError):
        Settings(migration_lock_timeout_ms=-1)
