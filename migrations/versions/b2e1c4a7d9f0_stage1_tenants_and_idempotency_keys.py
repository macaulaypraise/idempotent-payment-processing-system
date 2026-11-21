"""stage 1: tenants, api keys, idempotency keys, tenant scoping

Revision ID: b2e1c4a7d9f0
Revises: f7355da969c5
Create Date: 2026-11-21

- tenants, api_keys (hashed), idempotency_keys (ADR 0001)
- tenant_id on payments, outbox_events, processed_events in three steps:
  nullable column -> backfill with the default tenant -> NOT NULL + FK
- payments: global unique idempotency_key -> unique (tenant_id, key)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2e1c4a7d9f0"
down_revision: str | None = "f7355da969c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
SCOPED_TABLES = ("payments", "outbox_events", "processed_events")


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        f"INSERT INTO tenants (id, name) VALUES ('{DEFAULT_TENANT_ID}', 'default')"
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="in_progress",
        ),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column(
            "response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("resource_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="ck_idempotency_keys_status",
        ),
        sa.CheckConstraint(
            "status <> 'completed' OR "
            "(response_status IS NOT NULL AND response_body IS NOT NULL)",
            name="ck_idempotency_keys_completed_has_response",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("tenant_id", "key"),
    )
    op.create_index(
        "ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"]
    )

    # tenant_id: add nullable, backfill, then enforce.
    for table in SCOPED_TABLES:
        op.add_column(table, sa.Column("tenant_id", sa.UUID(), nullable=True))
        op.execute(
            f"UPDATE {table} SET tenant_id = '{DEFAULT_TENANT_ID}' "
            "WHERE tenant_id IS NULL"
        )
        op.alter_column(table, "tenant_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_tenant_id", table, "tenants", ["tenant_id"], ["id"]
        )

    op.drop_constraint("payments_idempotency_key_key", "payments", type_="unique")
    op.create_unique_constraint(
        "uq_payments_tenant_idempotency_key",
        "payments",
        ["tenant_id", "idempotency_key"],
    )


def downgrade() -> None:
    # Only safe while every row still belongs to the default tenant: the old
    # global unique constraint would reject keys reused across tenants.
    op.drop_constraint("uq_payments_tenant_idempotency_key", "payments", type_="unique")
    op.create_unique_constraint(
        "payments_idempotency_key_key", "payments", ["idempotency_key"]
    )
    for table in reversed(SCOPED_TABLES):
        op.drop_constraint(f"fk_{table}_tenant_id", table, type_="foreignkey")
        op.drop_column(table, "tenant_id")

    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("tenants")
