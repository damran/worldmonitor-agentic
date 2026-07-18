"""article_text — derived full-text cache for curated-feed Articles (ADR 0116)

One row per Article entity id: the plain-text body derived from the landed page HTML, with
provenance columns (``source_id``/``retrieved_at``/``raw_pointer``) and a bounded retry ledger
(``attempts``/``last_error``). A rebuildable read-model cache, NOT a system of record — the raw
HTML lives in the landing zone; dropping this table loses nothing that cannot be re-derived.

Purely additive — one new table; nothing existing is altered.

Do NOT edit 0001-0013 — migration history is immutable; the delta lives here only.
This migration head and ``ArticleText`` in :mod:`worldmonitor.db.models` MUST agree byte-for-byte
(``tests/integration/test_migrations.py`` drift guard, ADR 0030).

Revision ID: 0014_article_text
Revises: 0013_erasure_scrub_dataset_index
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0014_article_text"
down_revision: str | None = "0013_erasure_scrub_dataset_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "article_text",
        sa.Column("entity_id", sa.String(255), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("raw_pointer", sa.Text(), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False),
        sa.Column("retrieved_at", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("article_text")
