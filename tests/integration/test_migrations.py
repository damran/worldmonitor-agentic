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

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, inspect, make_url, text

from worldmonitor.db.engine import _alembic_config, create_all, make_engine, migrate_to_head

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
