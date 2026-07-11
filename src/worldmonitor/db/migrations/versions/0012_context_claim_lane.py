"""context-claim capture lane — context_claim table + checkpoint watermark (Gate P1 / ADR 0106)

Adds the second append-only SoR lane: ``context_claim`` — an INSERT-only Postgres table
banking anchor/enricher evidence as provenance-stamped claims, written at both promote points
(``resolution/pipeline.py`` + ``resolution/signoff.py``). Mirrors the ``0009_statement_spine``
idiom (surrogate PK, ``seq`` IDENTITY watermark, reserved ``scope`` column).

Purely additive:

* ``upgrade()`` creates ``context_claim`` (+ indexes on ``seq``, ``canonical_id``,
  ``entity_id``) and adds ``projection_checkpoint.last_context_claim_seq`` (``BigInteger``,
  ``server_default '0'``, ``NOT NULL``) — the additive watermark column mirroring
  ``last_statement_seq``/``last_decision_seq``. The ``server_default`` is REQUIRED: this
  NOT-NULL add-column must succeed against a ``projection_checkpoint`` table that already
  holds a row (the 0008 precedent; pinned by the ADR-0106 §4(e) integration test).
* ``downgrade()`` drops the column then the table + its indexes.

Do NOT edit 0001-0011 — migration history is immutable; the delta lives here only.
This migration head and the ``ContextClaimRecord`` / ``ProjectionCheckpoint.
last_context_claim_seq`` in :mod:`worldmonitor.db.models` MUST agree byte-for-byte
(``tests/integration/test_migrations.py`` drift guard, ADR 0030).

Revision ID: 0012_context_claim_lane
Revises: 0011_llm_egress_audit
Create Date: 2026-07-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0012_context_claim_lane"
down_revision: str | None = "0011_llm_egress_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_claim",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("canonical_id", sa.String(length=255), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("dataset", sa.String(length=255), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("retrieved_at", sa.String(length=64), nullable=False),
        sa.Column(
            "scope", sa.String(length=64), server_default=sa.text("'default'"), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_context_claim_seq", "context_claim", ["seq"])
    op.create_index("ix_context_claim_canonical_id", "context_claim", ["canonical_id"])
    op.create_index("ix_context_claim_entity_id", "context_claim", ["entity_id"])

    op.add_column(
        "projection_checkpoint",
        sa.Column(
            "last_context_claim_seq",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("projection_checkpoint", "last_context_claim_seq")
    op.drop_index("ix_context_claim_entity_id", table_name="context_claim")
    op.drop_index("ix_context_claim_canonical_id", table_name="context_claim")
    op.drop_index("ix_context_claim_seq", table_name="context_claim")
    op.drop_table("context_claim")
