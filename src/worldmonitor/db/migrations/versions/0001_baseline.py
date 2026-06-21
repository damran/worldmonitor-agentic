"""baseline: pre-runway Phase-1 schema

The schema as it stood *before* the resolution/ingest runway (the state an
existing deployment's create_all already produced): the connector-instance
registry, the ER queue (without the A6 dedup column/constraint), and the two
merge-audit tables. The runway deltas are applied by 0002 — so a pre-runway
database stamps this revision and upgrades, converging on the fresh-install
schema (ADR 0030).

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connector_instance",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("connector_id", sa.String(length=128), nullable=False),
        sa.Column("config_encrypted", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_connector_instance_tenant_id", "connector_instance", ["tenant_id"])
    op.create_index("ix_connector_instance_connector_id", "connector_instance", ["connector_id"])

    op.create_table(
        "er_queue_item",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("connector_id", sa.String(length=128), nullable=False),
        sa.Column("raw_entity", postgresql.JSONB(), nullable=False),
        sa.Column("source_record", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_er_queue_item_tenant_id", "er_queue_item", ["tenant_id"])
    op.create_index("ix_er_queue_item_status", "er_queue_item", ["status"])

    op.create_table(
        "merge_audit",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("canonical_id", sa.String(length=255), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_merge_audit_tenant_id", "merge_audit", ["tenant_id"])
    op.create_index("ix_merge_audit_canonical_id", "merge_audit", ["canonical_id"])
    op.create_index("ix_merge_audit_decision", "merge_audit", ["decision"])

    op.create_table(
        "merge_alerts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("canonical_id", sa.String(length=255), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_merge_alerts_tenant_id", "merge_alerts", ["tenant_id"])
    op.create_index("ix_merge_alerts_canonical_id", "merge_alerts", ["canonical_id"])


def downgrade() -> None:
    op.drop_table("merge_alerts")
    op.drop_table("merge_audit")
    op.drop_table("er_queue_item")
    op.drop_table("connector_instance")
