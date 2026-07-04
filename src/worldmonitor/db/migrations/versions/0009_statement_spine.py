"""statement + decision tables — append-only SoR spine (Gate 2a / ADR 0099)

Creates the two new tables behind the statement-spine step 1:
* ``statement`` — the append-only per-claim log: one row per
  ``(subject, entity_id, schema, prop, value, dataset)`` claim the fused
  ``StatementEntity`` yields at merge time (the ``id`` pseudo-statement excluded).
  Realises G1 provenance at the per-claim grain.
* ``decision`` — the append-only merge / belief-revision log: one row per promoted
  merge (``kind="merge"`` only in step 1; ``"split"`` / ``"negative"`` reserved).

Purely additive — Neo4j stays the live system of record (ADR 0095 step 1); no
cutover, no user-facing change.  ``scope`` on both tables is a reserved
forward-compatibility column (ADR 0099 Decision A) — UNENFORCED (no code reads,
writes beyond the server default, or filters on it); NOT re-adding tenant scoping
(ADR 0042, single-tenant D1 unchanged).

* ``upgrade()`` creates ``statement`` (+ indexes) then ``decision`` (+ indexes).
* ``downgrade()`` drops indexes then both tables in reverse order.

Do NOT edit 0001–0008 — migration history is immutable; the delta lives here only.
This migration head and the ``StatementRecord`` / ``DecisionRecord`` models in
:mod:`worldmonitor.db.models` MUST agree byte-for-byte
(``tests/integration/test_migrations.py`` drift guard, ADR 0030).

Revision ID: 0009_statement_spine
Revises: 0008_task_run_audit
Create Date: 2026-07-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_statement_spine"
down_revision: str | None = "0008_task_run_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ statement
    op.create_table(
        "statement",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("statement_id", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=255), nullable=False),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("schema", sa.String(length=64), nullable=False),
        sa.Column("prop", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("dataset", sa.String(length=255), nullable=False),
        sa.Column("reliability", sa.String(length=16), nullable=True),
        sa.Column("retrieved_at", sa.String(length=64), nullable=True),
        sa.Column("raw_pointer", sa.Text(), nullable=True),
        sa.Column("first_seen", sa.String(length=64), nullable=True),
        sa.Column("last_seen", sa.String(length=64), nullable=True),
        sa.Column("method", sa.String(length=64), nullable=True),
        sa.Column(
            "scope",
            sa.String(length=64),
            server_default=sa.text("'default'"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_statement_statement_id", "statement", ["statement_id"])
    op.create_index("ix_statement_canonical_id", "statement", ["canonical_id"])
    op.create_index("ix_statement_entity_id", "statement", ["entity_id"])

    # ------------------------------------------------------------------ decision
    op.create_table(
        "decision",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("member_ids", postgresql.JSONB(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("decided_by", sa.String(length=255), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("supersedes", sa.String(length=64), nullable=True),
        sa.Column("superseded_by", sa.String(length=64), nullable=True),
        sa.Column(
            "scope",
            sa.String(length=64),
            server_default=sa.text("'default'"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_decision_canonical_id", "decision", ["canonical_id"])
    op.create_index("ix_decision_kind", "decision", ["kind"])


def downgrade() -> None:
    # decision
    op.drop_index("ix_decision_kind", "decision")
    op.drop_index("ix_decision_canonical_id", "decision")
    op.drop_table("decision")
    # statement
    op.drop_index("ix_statement_entity_id", "statement")
    op.drop_index("ix_statement_canonical_id", "statement")
    op.drop_index("ix_statement_statement_id", "statement")
    op.drop_table("statement")
