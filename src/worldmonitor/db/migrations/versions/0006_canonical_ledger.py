"""anchor-preferred durable canonical-id ledger (Gate B-front / ADR 0044)

Adds the single new table behind the anchor-preferred stable-id derivation:
``canonical_id_ledger`` — the durable-identity mapping that separates the two id concepts
ADR 0036 conflated. ``wmc-<hash>`` stays *strictly* a crash-retry idempotency fingerprint (the
fallback id of an *unanchored* merge); DURABLE identity lives in this ledger, anchor-preferred
(``qid:``/``lei:``/``regno:``/``taxno:``) or minted (``wm-mint-<uuid>``) and stable across
re-ingest.

Two row kinds share the table (distinguished by whether ``canonical_alias == canonical_id``): a
canonical self-row recording a durable id with its anchor kind/value, and an APPEND-ONLY alias row
mapping a superseded/prior id (a collapsed merge member, the prior ``wmc-`` fingerprint, or a
split-ejected id) to the surviving durable ``canonical_id``. ``(canonical_id, canonical_alias)`` is
unique so the canonical self-row and ``record_alias`` are both idempotent. Purely additive and
person-NEUTRAL: it stores id references + an anchor kind/value, never graph mutations, and changes
no live merge value (no Splink weight/score/threshold).

* ``upgrade()`` creates ``canonical_id_ledger`` + its indexes + its ``uq_canonical_id_ledger_alias``
  unique constraint ONLY.
* ``downgrade()`` drops the table (and its indexes/constraints with it).

Do NOT edit 0001/0002/0003/0004/0005 — migration history is immutable; the delta lives here only.
This migration head and the ``CanonicalIdLedger`` model in :mod:`worldmonitor.db.models` MUST agree
byte-for-byte (``tests/integration/test_migrations.py`` drift guard, ADR 0030).

Revision ID: 0006_canonical_ledger
Revises: 0005_er_gold_pair
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_canonical_ledger"
down_revision: str | None = "0005_er_gold_pair"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canonical_id_ledger",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("canonical_id", sa.String(length=255), nullable=False),
        sa.Column("canonical_alias", sa.String(length=255), nullable=False),
        sa.Column("anchor_kind", sa.String(length=16), nullable=False),
        sa.Column("anchor_value", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_id", "canonical_alias", name="uq_canonical_id_ledger_alias"),
    )
    op.create_index("ix_canonical_id_ledger_canonical_id", "canonical_id_ledger", ["canonical_id"])
    op.create_index(
        "ix_canonical_id_ledger_canonical_alias", "canonical_id_ledger", ["canonical_alias"]
    )


def downgrade() -> None:
    op.drop_index("ix_canonical_id_ledger_canonical_alias", "canonical_id_ledger")
    op.drop_index("ix_canonical_id_ledger_canonical_id", "canonical_id_ledger")
    op.drop_table("canonical_id_ledger")
