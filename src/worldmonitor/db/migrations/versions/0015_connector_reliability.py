"""connector_instance.reliability — per-instance provenance grade (Gate S-4 slice 1, ADR 0120)

Adds the single new column ``connector_instance.reliability`` (nullable ``String(16)``,
admiralty A-F scale). ``ConnectorInstance.reliability`` was declared in neither the ORM nor any
prior migration (the spec's own research surfaced the drift — 0009's ``reliability`` column is on
``statement``, a different table); this closes it. NULL for every existing row — the driver falls
back to the historical hardcoded ``"B"`` default, byte-identical to before this column existed.

Purely additive and person-NEUTRAL: an opaque per-instance grade string, no graph mutation, no
live merge value.

* ``upgrade()`` adds the nullable ``reliability`` column ONLY.
* ``downgrade()`` drops it.

Do NOT edit 0001-0014 — migration history is immutable; the delta lives here only. This migration
head and ``ConnectorInstance`` in :mod:`worldmonitor.db.models` MUST agree byte-for-byte
(``tests/integration/test_migrations.py`` drift guard, ADR 0030).

**[CODE-WINS deviation]** the spec names this file/revision ``0015_connector_instance_reliability``
(35 chars); ``alembic_version.version_num`` is ``VARCHAR(32)`` (the repo's whole chain observes this
— every prior revision id equals its filename, and ``0013_erasure_scrub_dataset_index`` sits exactly
at the 32-char boundary), so the 35-char id would fail ``UPDATE alembic_version`` with
``StringDataRightTruncation`` on first upgrade. Shortened to ``0015_connector_reliability`` (26
chars) — same column, same semantics, only the id/filename differ from the spec's literal text.

Revision ID: 0015_connector_reliability
Revises: 0014_article_text
Create Date: 2026-07-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0015_connector_reliability"
down_revision: str | None = "0014_article_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "connector_instance", sa.Column("reliability", sa.String(length=16), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("connector_instance", "reliability")
