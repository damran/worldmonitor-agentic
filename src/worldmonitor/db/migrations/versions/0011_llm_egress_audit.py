"""durable, append-only LLM-egress audit — llm_egress table (Gate F2 / ADR 0105)

Adds the single new table behind the durable LLM-egress audit: ``llm_egress`` — an
INSERT-only Postgres table sitting ALONGSIDE the existing stdlib-logging egress audit
(``llm/egress_log.py``, unchanged), written by the same single gateway choke point at the
same two points per crossing (pre-call "attempt" row, post-call "completed" row, correlated
by ``call_id``).

Purely additive: no existing table/column is touched. **No `seq` IDENTITY column** (SF-3 —
no consumer of a monotonic egress-log watermark today; ``created_at`` + ``call_id`` suffice
for ordering/correlation) — so this migration deliberately does NOT need the ADR-0100
dialect-guarded ``before_insert`` SQLite fallback.

* ``upgrade()`` creates ``llm_egress`` + indexes on ``call_id`` and ``phase``.
* ``downgrade()`` drops the indexes then the table.

Do NOT edit 0001-0010 — migration history is immutable; the delta lives here only.
This migration head and the ``LlmEgressRecord`` model in :mod:`worldmonitor.db.models` MUST
agree byte-for-byte (``tests/integration/test_migrations.py`` drift guard, ADR 0030).

Revision ID: 0011_llm_egress_audit
Revises: 0010_projection_outbox
Create Date: 2026-07-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_llm_egress_audit"
down_revision: str | None = "0010_projection_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_egress",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("call_id", sa.String(length=64), nullable=False),
        sa.Column("phase", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("confidentiality", sa.Text(), nullable=False),
        sa.Column("target_host", sa.String(length=255), nullable=False),
        sa.Column("data_left_perimeter", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("caller_tag", sa.Text(), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("entity_manifest", postgresql.JSONB(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_egress_call_id", "llm_egress", ["call_id"])
    op.create_index("ix_llm_egress_phase", "llm_egress", ["phase"])


def downgrade() -> None:
    op.drop_index("ix_llm_egress_phase", table_name="llm_egress")
    op.drop_index("ix_llm_egress_call_id", table_name="llm_egress")
    op.drop_table("llm_egress")
