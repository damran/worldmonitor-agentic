"""stream resume cursor on connector_instance (Gate 3f / ADR 0070)

Adds the single new column behind the G8 stream resume protocol:
``connector_instance.stream_cursor`` — the saved source position a STREAM connector resumes from.
The driver injects it into ``config["_cursor"]`` before a run and persists
``IngestStats.last_cursor`` back onto it after a committed window. Nullable, default NULL — every
existing batch connector is unaffected (it never sets a cursor), so this is purely additive and
behaviour-preserving for batch.

Purely additive and person-NEUTRAL: it stores an opaque source position string, never a graph
mutation, and changes no live merge value (no Splink weight/score/threshold).

* ``upgrade()`` adds the nullable ``stream_cursor`` column ONLY.
* ``downgrade()`` drops it.

Do NOT edit 0001/0002/0003/0004/0005/0006 — migration history is immutable; the delta lives here
only. This migration head and the ``ConnectorInstance`` model in :mod:`worldmonitor.db.models` MUST
agree byte-for-byte (``tests/integration/test_migrations.py`` drift guard, ADR 0030).

Revision ID: 0007_stream_cursor
Revises: 0006_canonical_ledger
Create Date: 2026-06-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_stream_cursor"
down_revision: str | None = "0006_canonical_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("connector_instance", sa.Column("stream_cursor", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("connector_instance", "stream_cursor")
