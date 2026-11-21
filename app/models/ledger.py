"""Double-entry ledger tables (ADR 0002).

The constraints that matter most (balanced transfers, append-only entries
and transfers, the guard CHECK) are written as raw SQL in migration
c3a8d1e6f204; the declarations below mirror the parts autogenerate can see.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    UUID,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class AccountKind(enum.StrEnum):
    WALLET_PENDING = "wallet_pending"
    WALLET_AVAILABLE = "wallet_available"
    PSP_CLEARING = "psp_clearing"  # the provider owes us
    BANK = "bank"
    PSP_FEES = "psp_fees"
    REFUNDS_PAYABLE = "refunds_payable"


class Side(enum.StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


WALLET_KINDS = frozenset({AccountKind.WALLET_PENDING, AccountKind.WALLET_AVAILABLE})
SYSTEM_KINDS = (
    AccountKind.PSP_CLEARING,
    AccountKind.BANK,
    AccountKind.PSP_FEES,
    AccountKind.REFUNDS_PAYABLE,
)
# Assets and expenses are debit-normal; what we owe (wallets, refunds) is
# credit-normal.
NORMAL_SIDE: dict[AccountKind, Side] = {
    AccountKind.WALLET_PENDING: Side.CREDIT,
    AccountKind.WALLET_AVAILABLE: Side.CREDIT,
    AccountKind.PSP_CLEARING: Side.DEBIT,
    AccountKind.BANK: Side.DEBIT,
    AccountKind.PSP_FEES: Side.DEBIT,
    AccountKind.REFUNDS_PAYABLE: Side.CREDIT,
}


class Account(Base):
    __tablename__ = "accounts"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    kind = mapped_column(String(32), nullable=False)
    currency = mapped_column(String(3), nullable=False)
    normal_side = mapped_column(String(6), nullable=False)
    debits_posted = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    credits_posted = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    guarded = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    wallet_id = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('wallet_pending', 'wallet_available', 'psp_clearing', "
            "'bank', 'psp_fees', 'refunds_payable')",
            name="ck_accounts_kind",
        ),
        CheckConstraint(
            "normal_side IN ('debit', 'credit')", name="ck_accounts_normal_side"
        ),
        CheckConstraint(
            "debits_posted >= 0 AND credits_posted >= 0", name="ck_accounts_totals"
        ),
        CheckConstraint(
            "NOT guarded OR debits_posted <= credits_posted", name="ck_accounts_guard"
        ),
        CheckConstraint(
            "(wallet_id IS NULL) = "
            "(kind NOT IN ('wallet_pending', 'wallet_available'))",
            name="ck_accounts_wallet_kinds",
        ),
        UniqueConstraint("id", "currency", name="uq_accounts_id_currency"),
        Index(
            "uq_accounts_system_kind",
            "tenant_id",
            "kind",
            "currency",
            unique=True,
            postgresql_where=text("wallet_id IS NULL"),
        ),
        Index(
            "uq_accounts_wallet_kind",
            "wallet_id",
            "kind",
            unique=True,
            postgresql_where=text("wallet_id IS NOT NULL"),
        ),
    )


class Transfer(Base):
    """One money event. Unique on its reference, so posting twice is a no-op."""

    __tablename__ = "transfers"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    kind = mapped_column(String(32), nullable=False)
    reference_type = mapped_column(String(32), nullable=False)
    reference_id = mapped_column(UUID(as_uuid=True), nullable=False)
    currency = mapped_column(String(3), nullable=False)
    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "kind",
            "reference_type",
            "reference_id",
            name="uq_transfers_reference",
        ),
    )


class Entry(Base):
    __tablename__ = "entries"

    id = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    transfer_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("transfers.id"), nullable=False, index=True
    )
    account_id = mapped_column(UUID(as_uuid=True), nullable=False)
    direction = mapped_column(String(6), nullable=False)
    amount = mapped_column(BigInteger, nullable=False)
    currency = mapped_column(String(3), nullable=False)
    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_entries_amount_positive"),
        CheckConstraint(
            "direction IN ('debit', 'credit')", name="ck_entries_direction"
        ),
        ForeignKeyConstraint(
            ["account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_entries_account_currency",
        ),
        Index("ix_entries_account_id_id", "account_id", "id"),
    )
