"""stage 5: webhook inbox, ordered outbox with NOTIFY

Revision ID: a7f3c2d8e190
Revises: e6c1a9d4b582
Create Date: 2026-11-21

- webhook_events: the inbox. Unique (provider, provider_event_id) makes
  ingestion idempotent; a partial index serves the processor's due-rows scan.
- outbox_events.seq: publish order. Existing rows are numbered by created_at
  first, so a backlog that predates this migration still drains oldest first;
  the column then becomes an identity that continues after them.
- A partial index over unpublished rows, and a statement-level trigger that
  NOTIFYs the relay. NOTIFY is delivered at COMMIT and coalesced within a
  transaction, so a batch insert wakes the relay once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7f3c2d8e190"
down_revision: str | None = "e6c1a9d4b582"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEBHOOK_STATUSES = ("received", "processed", "parked", "dead", "ignored")
NOTIFY_CHANNEL = "outbox_events"


def _run(*statements: str) -> None:
    # asyncpg runs one statement per call
    for statement in statements:
        op.execute(statement)


def upgrade() -> None:
    # --- inbox ------------------------------------------------------------------
    statuses = ", ".join(f"'{s}'" for s in WEBHOOK_STATUSES)
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="received"
        ),
        sa.Column("tenant_id", sa.UUID(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"status IN ({statuses})", name="ck_webhook_events_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider", "provider_event_id", name="uq_webhook_events_provider_event"
        ),
    )
    op.create_index(
        "ix_webhook_events_due",
        "webhook_events",
        ["next_attempt_at"],
        postgresql_where=sa.text("status IN ('received', 'parked')"),
    )

    # --- ordered outbox -----------------------------------------------------------
    op.add_column("outbox_events", sa.Column("seq", sa.BigInteger(), nullable=True))
    _run(
        """
        UPDATE outbox_events o SET seq = n.rn
        FROM (SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn
              FROM outbox_events) n
        WHERE o.id = n.id
        """,
        "ALTER TABLE outbox_events ALTER COLUMN seq SET NOT NULL",
    )
    start = (
        op.get_bind()
        .execute(sa.text("SELECT coalesce(max(seq), 0) + 1 FROM outbox_events"))
        .scalar_one()
    )
    _run(
        "ALTER TABLE outbox_events ALTER COLUMN seq "
        f"ADD GENERATED ALWAYS AS IDENTITY (START WITH {int(start)})",
        "CREATE UNIQUE INDEX uq_outbox_events_seq ON outbox_events (seq)",
        "CREATE INDEX ix_outbox_events_unpublished ON outbox_events (seq) "
        "WHERE published_at IS NULL",
        f"""
        CREATE FUNCTION outbox_notify() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM pg_notify('{NOTIFY_CHANNEL}', '');
            RETURN NULL;
        END $$
        """,
        "CREATE TRIGGER outbox_notify AFTER INSERT ON outbox_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION outbox_notify()",
    )


def downgrade() -> None:
    _run(
        "DROP TRIGGER IF EXISTS outbox_notify ON outbox_events",
        "DROP FUNCTION IF EXISTS outbox_notify()",
    )
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    op.drop_index("uq_outbox_events_seq", table_name="outbox_events")
    op.drop_column("outbox_events", "seq")
    op.drop_index("ix_webhook_events_due", table_name="webhook_events")
    op.drop_table("webhook_events")
