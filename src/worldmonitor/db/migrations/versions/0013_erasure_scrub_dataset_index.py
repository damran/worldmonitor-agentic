"""erasure-scrub dataset index — b-tree index on statement.dataset + context_claim.dataset
(Gate P2 / ADR 0107 SF-1)

Adds a b-tree index on the two columns the P2 erasure scrub's PRIMARY reach predicate filters
on: ``statement.dataset`` and ``context_claim.dataset`` (both previously unindexed,
``db/models.py``). Erasure *correctness* does not depend on the index (the scrub's
``(dataset = source_id) OR (entity_id IN erased_member_ids)`` predicate is correct either way);
erasure *latency at scale* does — an unindexed full-table scan per erased source, repeated once
per source by the one-off stock scrub (``resolution.erasure_scrub.scrub_stock``) and again by
verification, is a production-scale finding (ADR 0107 §Decided SF-1).

Purely additive — no table/column added or altered, only two indexes:

* ``upgrade()`` creates ``ix_statement_dataset`` and ``ix_context_claim_dataset``.
* ``downgrade()`` drops them.

Do NOT edit 0001-0012 — migration history is immutable; the delta lives here only.
This migration head and ``StatementRecord.dataset`` / ``ContextClaimRecord.dataset`` in
:mod:`worldmonitor.db.models` (``index=True``) MUST agree byte-for-byte
(``tests/integration/test_migrations.py`` drift guard, ADR 0030). No erasure-event table is
added here (SF-3(a) — the direct-prune carve-out stays the live-removal mechanism; see
``resolution/erasure_scrub.py``).

Revision ID: 0013_erasure_scrub_dataset_index
Revises: 0012_context_claim_lane
Create Date: 2026-07-11
"""

from __future__ import annotations

from alembic import op

revision: str = "0013_erasure_scrub_dataset_index"
down_revision: str | None = "0012_context_claim_lane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_statement_dataset", "statement", ["dataset"])
    op.create_index("ix_context_claim_dataset", "context_claim", ["dataset"])


def downgrade() -> None:
    op.drop_index("ix_context_claim_dataset", table_name="context_claim")
    op.drop_index("ix_statement_dataset", table_name="statement")
