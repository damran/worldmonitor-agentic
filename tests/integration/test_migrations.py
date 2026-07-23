"""Integration tests: Alembic migrations + the create_all→alembic switch (ADR 0030).

Proves the two paths converge on an IDENTICAL schema (requirement #4) by comparing
introspected schema *snapshots* (robust against autogenerate's type/default quirks):
* a FRESH install via ``migrate_to_head`` (alembic upgrade head) equals ``create_all``
  (which *is* the ORM models, ``Base.metadata``);
* a **pre-runway** ``er_queue_item`` (no ``entity_id``, no Alembic bookkeeping — the
  previously-broken case) is adopted and upgraded, converging on that same schema;
* ``migrate_to_head`` is idempotent.

Each test runs against its OWN freshly-created database (the session-scoped Postgres
already holds other tests' tables), so the comparison is clean.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, make_url, text

from worldmonitor.db._migration_guard import apply_migration_timeouts
from worldmonitor.db.engine import (
    _alembic_config,
    create_all,
    make_engine,
    migrate_to_head,
    session_factory,
)
from worldmonitor.db.models import ConnectorInstance

pytestmark = pytest.mark.integration


def _create_fresh_database(postgres_dsn: str) -> str:
    """Create a uniquely-named empty database on the test server; return its DSN."""
    url = make_url(postgres_dsn)
    name = f"alembic_conv_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


def _snapshot(engine: Engine) -> dict[str, Any]:
    """A comparable structural snapshot of the live schema (excludes alembic_version)."""
    inspector = inspect(engine)
    schema: dict[str, Any] = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        schema[table] = {
            "columns": {
                c["name"]: (str(c["type"]), bool(c["nullable"]))
                for c in inspector.get_columns(table)
            },
            "indexes": {
                ix["name"]: (sorted(ix["column_names"]), bool(ix["unique"]))
                for ix in inspector.get_indexes(table)
            },
            "uniques": {
                u["name"]: sorted(u["column_names"])
                for u in inspector.get_unique_constraints(table)
            },
            "pk": sorted(inspector.get_pk_constraint(table)["constrained_columns"]),
        }
    return schema


@pytest.fixture
def reference_schema(postgres_dsn: str) -> dict[str, Any]:
    """The schema create_all produces (i.e. the ORM models) on a fresh database."""
    engine = make_engine(_create_fresh_database(postgres_dsn))
    create_all(engine)
    snap = _snapshot(engine)
    engine.dispose()
    return snap


def test_fresh_install_via_alembic_matches_create_all(
    postgres_dsn: str, reference_schema: dict[str, Any]
) -> None:
    engine = make_engine(_create_fresh_database(postgres_dsn))
    migrate_to_head(engine)

    assert _snapshot(engine) == reference_schema, "alembic head must equal create_all (the models)"
    # And the runway schema is actually present.
    inspector = inspect(engine)
    assert "entity_id" in {c["name"] for c in inspector.get_columns("er_queue_item")}
    assert "uq_er_queue_dedup" in {
        u["name"] for u in inspector.get_unique_constraints("er_queue_item")
    }
    assert {"task_run", "ingest_dead_letter"} <= set(inspector.get_table_names())
    engine.dispose()


def test_pre_runway_database_converges_on_fresh_schema(
    postgres_dsn: str, reference_schema: dict[str, Any]
) -> None:
    """The previously-broken case: a pre-runway er_queue_item is adopted + upgraded."""
    engine = make_engine(_create_fresh_database(postgres_dsn))

    # Build the pre-runway (baseline) schema, then strip the Alembic bookkeeping so
    # the database looks like an old create_all deployment that never knew Alembic.
    command.upgrade(_alembic_config(engine), "0001_baseline")
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))

    pre = inspect(engine)
    assert "entity_id" not in {c["name"] for c in pre.get_columns("er_queue_item")}
    assert "task_run" not in pre.get_table_names()

    migrate_to_head(engine)  # adoption: stamp baseline, then apply the runway delta

    assert _snapshot(engine) == reference_schema, "pre-runway db must converge on the fresh schema"
    post = inspect(engine)
    assert "entity_id" in {c["name"] for c in post.get_columns("er_queue_item")}
    assert "uq_er_queue_dedup" in {u["name"] for u in post.get_unique_constraints("er_queue_item")}
    assert {"task_run", "ingest_dead_letter"} <= set(post.get_table_names())
    engine.dispose()


def test_migrate_to_head_is_idempotent(postgres_dsn: str, reference_schema: dict[str, Any]) -> None:
    engine = make_engine(_create_fresh_database(postgres_dsn))
    migrate_to_head(engine)
    migrate_to_head(engine)  # second run sees alembic_version -> upgrade head is a no-op
    assert _snapshot(engine) == reference_schema
    engine.dispose()


def _alembic_version(engine: Engine) -> str | None:
    """The revision stamped in alembic_version, or None if the DB is not Alembic-managed."""
    if "alembic_version" not in inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()


def _script_head(engine: Engine) -> str | None:
    """The current head revision id — so the assertion tracks new migrations, not a literal."""
    return ScriptDirectory.from_config(_alembic_config(engine)).get_current_head()


def test_post_runway_create_all_database_is_stamped_at_head(
    postgres_dsn: str, reference_schema: dict[str, Any]
) -> None:
    """A DB at TODAY's schema via create_all but with no Alembic bookkeeping is adopted
    by stamping head — no DDL re-runs — and a subsequent upgrade is a no-op. This is the
    state of any post-runway deployment that predates Alembic."""
    engine = make_engine(_create_fresh_database(postgres_dsn))
    create_all(engine)
    assert _alembic_version(engine) is None  # unmanaged, but already at the current schema
    head = _script_head(engine)

    migrate_to_head(engine)  # adoption branch (c): er_queue_item has entity_id -> stamp head

    assert _alembic_version(engine) == head, "must stamp head, not re-run the baseline"
    assert _snapshot(engine) == reference_schema, "stamping must not change the schema"

    migrate_to_head(engine)  # already managed at head -> upgrade is a no-op
    assert _alembic_version(engine) == head
    assert _snapshot(engine) == reference_schema
    engine.dispose()


def test_no_autogenerate_drift(postgres_dsn: str) -> None:
    """Drift guard: ``alembic check`` against head must detect NO changes, so a model
    edit without a matching migration fails the build (raises AutogenerateDiffsDetected)."""
    engine = make_engine(_create_fresh_database(postgres_dsn))
    migrate_to_head(engine)
    command.check(_alembic_config(engine))  # raises on any model/migration drift
    engine.dispose()


# ---------------------------------------------------------------------------
# ADR 0084 / M-5: dialect-aware lock_timeout integration tests
# ---------------------------------------------------------------------------


def test_lock_timeout_applied_on_real_postgres_connection(postgres_dsn: str) -> None:
    """apply_migration_timeouts issues SET LOCAL lock_timeout on a real Postgres connection.

    Uses a distinctive, non-whole-second value (2500 ms) so Postgres cannot normalize
    it to seconds — ``SHOW lock_timeout`` must return exactly ``'2500ms'``.
    The value is visible inside the same transaction (SET LOCAL semantics).
    """
    engine = make_engine(postgres_dsn)
    mock_settings = MagicMock()
    mock_settings.migration_lock_timeout_ms = 2500
    mock_settings.migration_statement_timeout_ms = 0

    with engine.begin() as conn:
        with patch("worldmonitor.db._migration_guard.get_settings", return_value=mock_settings):
            apply_migration_timeouts(conn)
        # SET LOCAL → visible inside this transaction
        result = conn.execute(text("SHOW lock_timeout")).scalar_one()
        assert result == "2500ms", (
            f"expected lock_timeout='2500ms' inside the transaction, got {result!r}"
        )
    # After the transaction the value reverts (SET LOCAL semantics — no session bleed)
    engine.dispose()


def test_lock_timeout_reverts_after_transaction(postgres_dsn: str) -> None:
    """SET LOCAL lock_timeout reverts to the session default after the transaction ends.

    Confirms the guard does NOT bleed onto subsequent app queries that use the same
    shared connection (the most important safety property for the shared-conn path).
    """
    engine = make_engine(postgres_dsn)

    # Capture the session-level default (Postgres default = '0' = no timeout)
    with engine.connect() as conn:
        default_timeout = conn.execute(text("SHOW lock_timeout")).scalar_one()

    mock_settings = MagicMock()
    mock_settings.migration_lock_timeout_ms = 1750  # non-whole-second
    mock_settings.migration_statement_timeout_ms = 0

    with engine.begin() as conn:
        with patch("worldmonitor.db._migration_guard.get_settings", return_value=mock_settings):
            apply_migration_timeouts(conn)
        # Verify it was set during the transaction
        mid_tx = conn.execute(text("SHOW lock_timeout")).scalar_one()
        assert mid_tx == "1750ms"
    # After commit, SET LOCAL reverts — open a new connection to verify
    with engine.connect() as conn:
        post_tx = conn.execute(text("SHOW lock_timeout")).scalar_one()
    assert post_tx == default_timeout, (
        f"SET LOCAL must not bleed to subsequent connections; expected {default_timeout!r}, "
        f"got {post_tx!r}"
    )
    engine.dispose()


def test_lock_timeout_zero_leaves_postgres_default(postgres_dsn: str) -> None:
    """migration_lock_timeout_ms=0 (opt-out) → lock_timeout is unchanged (Postgres default '0')."""
    engine = make_engine(postgres_dsn)
    mock_settings = MagicMock()
    mock_settings.migration_lock_timeout_ms = 0
    mock_settings.migration_statement_timeout_ms = 0

    with engine.begin() as conn:
        before = conn.execute(text("SHOW lock_timeout")).scalar_one()
        with patch("worldmonitor.db._migration_guard.get_settings", return_value=mock_settings):
            apply_migration_timeouts(conn)
        after = conn.execute(text("SHOW lock_timeout")).scalar_one()
    assert before == after, (
        f"migration_lock_timeout_ms=0 must not change lock_timeout; was {before!r}, now {after!r}"
    )
    engine.dispose()


def test_migrate_to_head_succeeds_with_lock_timeout_configured(
    postgres_dsn: str, reference_schema: dict[str, Any]
) -> None:
    """migrate_to_head completes successfully even when migration_lock_timeout_ms is active.

    No lock contention in isolation → the timeout is never triggered; the guard just adds
    the fast-abort safety net without affecting the normal migration path.
    """
    engine = make_engine(_create_fresh_database(postgres_dsn))
    migrate_to_head(engine)
    assert _snapshot(engine) == reference_schema
    engine.dispose()


def test_partial_restore_is_refused_not_blindly_stamped(
    postgres_dsn: str, reference_schema: dict[str, Any]
) -> None:
    """A partially-restored DB must NOT be blind-stamped at head (ADR 0056, Phase-B #3).

    The buggy ``migrate_to_head`` (db/engine.py:72) decides "already at head" from a single
    column: ``entity_id in er_queue_item -> stamp(head)``. A DB that has ``er_queue_item``
    *with* ``entity_id`` (so it looks post-runway) but is MISSING a later-migration table
    (``sign_off``) and has NO ``alembic_version`` hits that branch and is stamped at head
    while incomplete — the missing tables are never created and Alembic reports it current.

    Fix: a full-schema completeness check against ``Base.metadata`` must refuse such a
    partial restore with ``SchemaIncompleteError`` naming the missing table. Fail-closed.
    """
    # Imported in-body so the (initially-missing) symbol doesn't break collection of the
    # other tests in this module — its absence is a legitimate RED reason here.
    from worldmonitor.db.engine import SchemaIncompleteError

    # A full head schema via create_all (every table, er_queue_item.entity_id present),
    # with NO alembic_version (create_all does not stamp).
    engine = make_engine(_create_fresh_database(postgres_dsn))
    create_all(engine)

    # Simulate a partial restore: drop a later-migration table that is NOT er_queue_item.
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE sign_off CASCADE"))

    # Preconditions that make the BUGGY branch (entity_id present -> stamp head) fire:
    pre = inspect(engine)
    tables = set(pre.get_table_names())
    assert "sign_off" not in tables, "partial restore must be missing sign_off"
    assert "alembic_version" not in tables, "an unmanaged (pre-Alembic) restore"
    assert "er_queue_item" in tables, "er_queue_item must remain"
    assert "entity_id" in {c["name"] for c in pre.get_columns("er_queue_item")}, (
        "entity_id present -> the buggy branch would stamp head"
    )

    # Fail-closed: refuse the partial restore, naming the missing table.
    with pytest.raises(SchemaIncompleteError, match="sign_off"):
        migrate_to_head(engine)

    # And it must NOT have silently stamped the incomplete DB as current.
    assert _alembic_version(engine) is None, "a refused partial restore must not be stamped"
    assert "sign_off" not in set(inspect(engine).get_table_names())
    engine.dispose()


# ---------------------------------------------------------------------------
# Gate S-4 slice 1 (AC3) — connector_instance.reliability migration round-trip
# ---------------------------------------------------------------------------


def test_connector_instance_reliability_migration_round_trips(postgres_dsn: str) -> None:
    """GATE_S4_RANSOMWARE_LIVE_SPEC.md §5/§9, ADR 0120 slice 1: a new migration adds the
    (currently-missing) ``connector_instance.reliability`` column on ``alembic upgrade head`` —
    closing the ORM/DB drift the spec's own research surfaced (the ORM declared no such column
    and neither did any migration) — and downgrading ONE step drops it again.

    The existence check is deliberately ``create_all``-independent (asserted straight off
    ``inspect(engine)`` after ``migrate_to_head``, never compared against a ``create_all``
    reference schema) so this test cannot pass merely because the ORM model and a *different*
    migration happen to agree; it pins the migration itself. A follow-up ORM write/read-back
    proves the column is actually usable, not just declared.
    """
    engine = make_engine(_create_fresh_database(postgres_dsn))
    try:
        migrate_to_head(engine)

        columns = {c["name"]: c for c in inspect(engine).get_columns("connector_instance")}
        assert "reliability" in columns, (
            "alembic upgrade head must add connector_instance.reliability "
            f"(create_all-independent check) — got columns {sorted(columns)}"
        )
        assert columns["reliability"]["nullable"] is True, (
            f"connector_instance.reliability must be nullable — got {columns['reliability']}"
        )

        # The column is genuinely usable (ORM write, then a read-back off a fresh session).
        sessions = session_factory(engine)
        with sessions() as session:
            session.add(
                ConnectorInstance(
                    id="s4-ac3-migration",
                    connector_id="ransomware_live",
                    config_encrypted="x",
                    status="enabled",
                    reliability="E",
                )
            )
            session.commit()
        with sessions() as session:
            row = session.get(ConnectorInstance, "s4-ac3-migration")
            assert row is not None and row.reliability == "E", (
                f"expected the written 'E' to read back unchanged, got {row!r}"
            )

        # Downgrading exactly one step must remove the column again (a clean revert).
        command.downgrade(_alembic_config(engine), "-1")
        post_columns = {c["name"] for c in inspect(engine).get_columns("connector_instance")}
        assert "reliability" not in post_columns, (
            "downgrading one step must drop connector_instance.reliability again "
            f"— still present: {sorted(post_columns)}"
        )
    finally:
        engine.dispose()
