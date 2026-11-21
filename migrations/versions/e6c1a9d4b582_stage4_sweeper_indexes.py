"""stage 4: indexes for the sweeper

Revision ID: e6c1a9d4b582
Revises: d4b9e2f7a315
Create Date: 2026-11-21

The sweeper looks for retries that are due and for PENDING rows whose event
was lost. Partial indexes keep both lookups to the rows that can qualify, so
the v1 rows (no wallet) are never scanned.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6c1a9d4b582"
down_revision: str | None = "d4b9e2f7a315"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_payments_retry_due",
        "payments",
        ["next_attempt_at"],
        postgresql_where=sa.text(
            "status = 'PROCESSING' AND wallet_id IS NOT NULL "
            "AND next_attempt_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_payments_pending_created",
        "payments",
        ["created_at"],
        postgresql_where=sa.text("status = 'PENDING' AND wallet_id IS NOT NULL"),
    )
    op.create_index(
        "ix_refunds_retry_due",
        "refunds",
        ["next_attempt_at"],
        postgresql_where=sa.text(
            "status = 'PROCESSING' AND next_attempt_at IS NOT NULL"
        ),
    )
    op.create_index(
        "ix_refunds_pending_created",
        "refunds",
        ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("ix_refunds_pending_created", table_name="refunds")
    op.drop_index("ix_refunds_retry_due", table_name="refunds")
    op.drop_index("ix_payments_pending_created", table_name="payments")
    op.drop_index("ix_payments_retry_due", table_name="payments")
