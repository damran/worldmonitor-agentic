"""runway schema deltas (G8 / G9 / Gate A)

Brings the pre-runway baseline (0001) up to the current schema:
* er_queue_item gains the A6 idempotent-enqueue key — the ``entity_id`` column,
  its index, and ``uq_er_queue_dedup`` (tenant_id, source_record, entity_id);
* the ``ingest_dead_letter`` table (G8 / ADR 0027);
* the ``task_run`` table (Gate A / ADR 0029).

Not expressed here (no app-schema change): ConnectorInstance.status changed only
its set of valid *values*, not its column DDL; and the D1 fix (ADR 0028) isolated
nomenklatura's own external store, not an app table.

Revision ID: 0002_runway
Revises: 0001_baseline
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_runway"
down_revision: str | None = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A6 idempotent enqueue (ADR 0029): add the dedup key to the existing table.
    op.add_column("er_queue_item", sa.Column("entity_id", sa.String(length=255), nullable=True))
    op.create_index("ix_er_queue_item_entity_id", "er_queue_item", ["entity_id"])
    op.create_unique_constraint(
        "uq_er_queue_dedup", "er_queue_item", ["tenant_id", "source_record", "entity_id"]
    )

    # Dead-letter trail (G8 / ADR 0027).
    op.create_table(
        "ingest_dead_letter",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("connector_id", sa.String(length=128), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("source_record", sa.String(), nullable=True),
        sa.Column("stage", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingest_dead_letter_tenant_id", "ingest_dead_letter", ["tenant_id"])
    op.create_index("ix_ingest_dead_letter_stage", "ingest_dead_letter", ["stage"])

    # Driver run-history (Gate A / ADR 0029).
    op.create_table(
        "task_run",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("connector_instance_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stats", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_task_run_tenant_id", "task_run", ["tenant_id"])
    op.create_index("ix_task_run_connector_instance_id", "task_run", ["connector_instance_id"])
    op.create_index("ix_task_run_kind", "task_run", ["kind"])
    op.create_index("ix_task_run_status", "task_run", ["status"])


def downgrade() -> None:
    op.drop_table("task_run")
    op.drop_table("ingest_dead_letter")
    op.drop_constraint("uq_er_queue_dedup", "er_queue_item", type_="unique")
    op.drop_index("ix_er_queue_item_entity_id", "er_queue_item")
    op.drop_column("er_queue_item", "entity_id")
