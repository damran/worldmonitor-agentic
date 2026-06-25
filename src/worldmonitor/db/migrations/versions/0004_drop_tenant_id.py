"""single-tenancy teardown — drop tenant_id (D1 / ADR 0042)

Reverses ADR 0017 ("tenant-scoped from day one"). Under D1 there is exactly one
tenant, so ``tenant_id`` is dead weight: an 8-table NOT NULL column, its per-table
index, and the leading column of two composite uniques. This migration removes all
three across the relational schema. The graph side (the Neo4j ``tenant_id`` property
+ composite constraint) is handled in :mod:`worldmonitor.graph.constraints`, not here.

* Drop the eight ``ix_<table>_tenant_id`` indexes.
* Redefine the two composite uniques to drop the leading ``tenant_id`` column:
  ``uq_er_queue_dedup`` -> ``(source_record, entity_id)`` and
  ``uq_resolver_judgement_pair`` -> ``(left_id, right_id)``. With one tenant the
  leading column was constant, so the shrunk uniques are exactly as selective —
  the enqueue/judgement idempotency behaviour is unchanged (ADR 0042 §1.1).
* Drop the ``tenant_id`` column from all eight tables.

``downgrade()`` re-adds the columns/indexes/uniques for **schema symmetry only** — it
re-adds ``tenant_id`` ``nullable=True`` because the dropped data is gone and cannot be
round-tripped; a real downgrade would need a backfill before the column could be made
NOT NULL again. Do NOT edit 0001/0002/0003 — migration history is immutable; the delta
lives here, the same discipline as the 0002 runway delta on the 0001 baseline.

Revision ID: 0004_drop_tenant_id
Revises: 0003_signoff_judgements
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_drop_tenant_id"
down_revision: str | None = "0003_signoff_judgements"
branch_labels = None
depends_on = None

# Every table that carried a tenant_id column + its per-table index name.
_TENANT_TABLES: tuple[tuple[str, str], ...] = (
    ("connector_instance", "ix_connector_instance_tenant_id"),
    ("er_queue_item", "ix_er_queue_item_tenant_id"),
    ("merge_audit", "ix_merge_audit_tenant_id"),
    ("ingest_dead_letter", "ix_ingest_dead_letter_tenant_id"),
    ("merge_alerts", "ix_merge_alerts_tenant_id"),
    ("task_run", "ix_task_run_tenant_id"),
    ("resolver_judgement", "ix_resolver_judgement_tenant_id"),
    ("sign_off", "ix_sign_off_tenant_id"),
)


def upgrade() -> None:
    # Drop the per-table tenant_id indexes.
    for table, index_name in _TENANT_TABLES:
        op.drop_index(index_name, table_name=table)

    # Redefine the two composite uniques (drop the leading tenant_id column).
    op.drop_constraint("uq_er_queue_dedup", "er_queue_item", type_="unique")
    op.create_unique_constraint(
        "uq_er_queue_dedup", "er_queue_item", ["source_record", "entity_id"]
    )
    op.drop_constraint("uq_resolver_judgement_pair", "resolver_judgement", type_="unique")
    op.create_unique_constraint(
        "uq_resolver_judgement_pair", "resolver_judgement", ["left_id", "right_id"]
    )

    # Drop the tenant_id column from every table.
    for table, _index_name in _TENANT_TABLES:
        op.drop_column(table, "tenant_id")


def downgrade() -> None:
    # Schema symmetry only — the column's data is gone and cannot be round-tripped,
    # so tenant_id comes back nullable (a real downgrade would backfill then ALTER).
    for table, _index_name in _TENANT_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.String(length=128), nullable=True))

    op.drop_constraint("uq_resolver_judgement_pair", "resolver_judgement", type_="unique")
    op.create_unique_constraint(
        "uq_resolver_judgement_pair",
        "resolver_judgement",
        ["tenant_id", "left_id", "right_id"],
    )
    op.drop_constraint("uq_er_queue_dedup", "er_queue_item", type_="unique")
    op.create_unique_constraint(
        "uq_er_queue_dedup",
        "er_queue_item",
        ["tenant_id", "source_record", "entity_id"],
    )

    for table, index_name in _TENANT_TABLES:
        op.create_index(index_name, table, ["tenant_id"])
