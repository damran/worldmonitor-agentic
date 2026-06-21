"""sign-off + durable resolver judgements (ADR 0031)

Adds the two tenant-scoped tables behind the return-to-block sign-off mechanism:
* ``resolver_judgement`` — durable human judgements that every batch's ephemeral
  resolver loads and respects (so a reviewed cluster never re-parks);
* ``sign_off`` — the human-sign-off audit trail (approve / reject).

First migration after the Alembic gate — exercises the migration + drift-guard flow
end to end.

Revision ID: 0003_signoff_judgements
Revises: 0002_runway
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_signoff_judgements"
down_revision: str | None = "0002_runway"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resolver_judgement",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("left_id", sa.String(length=255), nullable=False),
        sa.Column("right_id", sa.String(length=255), nullable=False),
        sa.Column("judgement", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "left_id", "right_id", name="uq_resolver_judgement_pair"),
    )
    op.create_index("ix_resolver_judgement_tenant_id", "resolver_judgement", ["tenant_id"])

    op.create_table(
        "sign_off",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("canonical_id", sa.String(length=255), nullable=False),
        sa.Column("source_ids", postgresql.JSONB(), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("approver", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sign_off_tenant_id", "sign_off", ["tenant_id"])
    op.create_index("ix_sign_off_canonical_id", "sign_off", ["canonical_id"])
    op.create_index("ix_sign_off_decision", "sign_off", ["decision"])


def downgrade() -> None:
    op.drop_table("sign_off")
    op.drop_table("resolver_judgement")
