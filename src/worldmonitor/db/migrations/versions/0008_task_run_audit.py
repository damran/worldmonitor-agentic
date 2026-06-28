"""task_run ACTIVE-capability gating audit columns (Gate 3g / ADR 0071)

Adds the three audit columns behind the ACTIVE-capability operator-run path:
``task_run.run_mode`` (``"cadence"`` for a cadence-driven run — the server default — or
``"operator"`` for an operator-triggered one), ``task_run.triggered_by`` (the authenticated operator
subject) and ``task_run.scope_token`` (the minted, tamper-evident per-run authorization). Every
ACTIVE run is therefore queryable by operator + scope.

Purely additive and person-NEUTRAL: a cadence run keeps ``run_mode="cadence"`` with the other two
columns ``NULL`` — no behaviour change, no graph mutation, no live merge value touched. ``run_mode``
is ``NOT NULL`` with a ``server_default`` so existing rows backfill to ``"cadence"``.

Do NOT edit 0001–0007 — migration history is immutable; the delta lives here only. This migration
head and the ``TaskRun`` model in :mod:`worldmonitor.db.models` MUST agree byte-for-byte
(``tests/integration/test_migrations.py`` drift guard, ADR 0030).

* ``upgrade()`` adds ``run_mode`` / ``triggered_by`` / ``scope_token``.
* ``downgrade()`` drops them.

Revision ID: 0008_task_run_audit
Revises: 0007_stream_cursor
Create Date: 2026-06-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_task_run_audit"
down_revision: str | None = "0007_stream_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_run",
        sa.Column("run_mode", sa.String(length=16), nullable=False, server_default="cadence"),
    )
    op.add_column("task_run", sa.Column("triggered_by", sa.String(length=255), nullable=True))
    op.add_column("task_run", sa.Column("scope_token", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("task_run", "scope_token")
    op.drop_column("task_run", "triggered_by")
    op.drop_column("task_run", "run_mode")
