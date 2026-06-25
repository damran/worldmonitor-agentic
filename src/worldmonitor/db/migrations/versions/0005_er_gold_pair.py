"""ER measurement-harness gold-pair set (ADR 0043 / Gate A)

Adds the single new table behind the ER measurement harness: ``er_gold_pair`` — a small,
seeded, reproducible set of labelled (``match`` / ``non_match``) record pairs the evaluation
harness (:mod:`worldmonitor.resolution.eval`) scores the resolver against. It is purely
additive and person-NEUTRAL: it stores id references plus a clerical label, never graph
mutations, and changes no live merge value (the threshold/weight promotion is the separate,
human-gated slice-2 — NOT this migration).

The pair is stored canonically ordered (``left_id <= right_id``) and is unique on
``(left_id, right_id)`` — the same idiom as ``uq_resolver_judgement_pair`` (ADR 0031).

* ``upgrade()`` creates ``er_gold_pair`` + its ``uq_er_gold_pair`` unique constraint ONLY.
* ``downgrade()`` drops the table.

Do NOT edit 0001/0002/0003/0004 — migration history is immutable; the delta lives here only.
This migration head and the ``ErGoldPair`` model in :mod:`worldmonitor.db.models` MUST agree
byte-for-byte (``tests/integration/test_migrations.py`` drift guard, ADR 0030).

Revision ID: 0005_er_gold_pair
Revises: 0004_drop_tenant_id
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_er_gold_pair"
down_revision: str | None = "0004_drop_tenant_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "er_gold_pair",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("left_id", sa.String(length=255), nullable=False),
        sa.Column("right_id", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("clerical_score", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("left_id", "right_id", name="uq_er_gold_pair"),
    )


def downgrade() -> None:
    op.drop_table("er_gold_pair")
