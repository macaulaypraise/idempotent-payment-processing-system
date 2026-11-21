"""stage 3: payment lifecycle on the ledger, refunds

Revision ID: d4b9e2f7a315
Revises: c3a8d1e6f204
Create Date: 2026-11-21

- payments.status: Postgres enum -> text with a CHECK (adds CAPTURED), and a
  trigger that rejects illegal transitions and changes to immutable columns
- payments: wallet_id (NULL only for v1 rows), psp_reference, attempts,
  next_attempt_at, settled_at
- refunds, with at most one non-failed refund per payment and its own
  transition guard
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b9e2f7a315"
down_revision: str | None = "c3a8d1e6f204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PAYMENT_STATUSES = (
    "PENDING",
    "PROCESSING",
    "CAPTURED",
    "FAILED",
    "SETTLED",
    "REFUNDED",
)
PAYMENT_TRANSITIONS = (
    ("PENDING", "PROCESSING"),
    ("PROCESSING", "CAPTURED"),
    ("PROCESSING", "FAILED"),
    ("CAPTURED", "SETTLED"),
    ("CAPTURED", "REFUNDED"),
    ("SETTLED", "REFUNDED"),
)
REFUND_STATUSES = ("PENDING", "PROCESSING", "SUCCEEDED", "FAILED")
REFUND_TRANSITIONS = (
    ("PENDING", "PROCESSING"),
    ("PROCESSING", "SUCCEEDED"),
    ("PROCESSING", "FAILED"),
)


def _run(*statements: str) -> None:
    for statement in statements:
        op.execute(statement)


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _pairs(pairs: Sequence[tuple[str, str]]) -> str:
    return ", ".join(f"('{a}', '{b}')" for a, b in pairs)


def upgrade() -> None:
    # --- payments.status: enum -> text + CHECK ----------------------------------
    _run(
        "ALTER TABLE payments ALTER COLUMN status TYPE varchar(16) USING status::text",
        "DROP TYPE paymentstatus",
    )
    op.create_check_constraint(
        "ck_payments_status", "payments", f"status IN ({_in(PAYMENT_STATUSES)})"
    )

    op.add_column("payments", sa.Column("wallet_id", sa.UUID(), nullable=True))
    op.add_column(
        "payments", sa.Column("psp_reference", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "payments",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "payments",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments", sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_payments_wallet_id", "payments", "wallets", ["wallet_id"], ["id"]
    )
    op.create_index("ix_payments_wallet_id", "payments", ["wallet_id"])

    # --- payments transition guard ------------------------------------------------
    _run(
        f"""
        CREATE FUNCTION payments_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.amount IS DISTINCT FROM OLD.amount
               OR NEW.currency IS DISTINCT FROM OLD.currency
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR (OLD.wallet_id IS NOT NULL
                   AND NEW.wallet_id IS DISTINCT FROM OLD.wallet_id) THEN
                RAISE EXCEPTION 'payment %: tenant, amount, currency, key and '
                    'wallet are immutable', OLD.id USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status <> OLD.status
               AND (OLD.status, NEW.status) NOT IN ({_pairs(PAYMENT_TRANSITIONS)}) THEN
                RAISE EXCEPTION 'invalid payment transition % -> % (payment %)',
                    OLD.status, NEW.status, OLD.id USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """,
        "CREATE TRIGGER payments_guard BEFORE UPDATE ON payments "
        "FOR EACH ROW EXECUTE FUNCTION payments_guard()",
    )

    # --- refunds ------------------------------------------------------------------
    op.create_table(
        "refunds",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("hold_source", sa.String(length=16), nullable=False),
        sa.Column("psp_reference", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_refunds_amount_positive"),
        sa.CheckConstraint(
            f"status IN ({_in(REFUND_STATUSES)})", name="ck_refunds_status"
        ),
        sa.CheckConstraint(
            "hold_source IN ('wallet_pending', 'wallet_available')",
            name="ck_refunds_hold_source",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_refunds_tenant_id", "refunds", ["tenant_id"])
    op.create_index(
        "uq_refunds_one_active_per_payment",
        "refunds",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'FAILED'"),
    )
    _run(
        f"""
        CREATE FUNCTION refunds_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.payment_id IS DISTINCT FROM OLD.payment_id
               OR NEW.amount IS DISTINCT FROM OLD.amount
               OR NEW.currency IS DISTINCT FROM OLD.currency
               OR NEW.hold_source IS DISTINCT FROM OLD.hold_source THEN
                RAISE EXCEPTION 'refund %: tenant, payment, amount, currency and '
                    'hold source are immutable', OLD.id
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status <> OLD.status
               AND (OLD.status, NEW.status) NOT IN ({_pairs(REFUND_TRANSITIONS)}) THEN
                RAISE EXCEPTION 'invalid refund transition % -> % (refund %)',
                    OLD.status, NEW.status, OLD.id USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """,
        "CREATE TRIGGER refunds_guard BEFORE UPDATE ON refunds "
        "FOR EACH ROW EXECUTE FUNCTION refunds_guard()",
    )


def downgrade() -> None:
    _run(
        "DROP TRIGGER IF EXISTS refunds_guard ON refunds",
        "DROP FUNCTION IF EXISTS refunds_guard()",
    )
    op.drop_index("uq_refunds_one_active_per_payment", table_name="refunds")
    op.drop_index("ix_refunds_tenant_id", table_name="refunds")
    op.drop_table("refunds")

    _run(
        "DROP TRIGGER IF EXISTS payments_guard ON payments",
        "DROP FUNCTION IF EXISTS payments_guard()",
    )
    op.drop_index("ix_payments_wallet_id", table_name="payments")
    op.drop_constraint("fk_payments_wallet_id", "payments", type_="foreignkey")
    for column in ("settled_at", "next_attempt_at", "attempts", "psp_reference"):
        op.drop_column("payments", column)
    op.drop_column("payments", "wallet_id")

    # CAPTURED has no v1 equivalent; captured payments go back to SETTLED,
    # which is what v1 called a payment the provider accepted.
    op.drop_constraint("ck_payments_status", "payments", type_="check")
    _run(
        "UPDATE payments SET status = 'SETTLED' WHERE status = 'CAPTURED'",
        "CREATE TYPE paymentstatus AS ENUM "
        "('PENDING', 'PROCESSING', 'SETTLED', 'FAILED', 'REFUNDED')",
        "ALTER TABLE payments ALTER COLUMN status TYPE paymentstatus "
        "USING status::paymentstatus",
    )
