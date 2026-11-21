import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    UUID,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class PaymentStatus(enum.Enum):
    """Stored by name (PENDING ...), shown in the API by value (pending ...).

    PENDING -> PROCESSING -> CAPTURED -> SETTLED
                         \\-> FAILED (terminal)
    CAPTURED | SETTLED -> REFUNDED

    The database enforces the same transitions (trigger payments_guard).
    """

    PENDING = "pending"
    PROCESSING = "processing"
    CAPTURED = "captured"
    FAILED = "failed"
    SETTLED = "settled"
    REFUNDED = "refunded"


class Payment(Base):
    __tablename__ = "payments"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    # Kept unique per tenant as a second line of defence behind
    # idempotency_keys: even a bug in the idempotency flow cannot create two
    # payments for one key.
    idempotency_key = mapped_column(String, nullable=False)
    amount = mapped_column(BigInteger, nullable=False)  # minor units (cents, kobo)
    currency = mapped_column(String(3), nullable=False)
    status = mapped_column(
        Enum(
            PaymentStatus,
            native_enum=False,
            create_constraint=False,  # ck_payments_status below
            length=16,
        ),
        nullable=False,
        default=PaymentStatus.PENDING,
    )
    # The wallet a capture credits. NULL only for payments created before v2
    # Stage 3, which have no ledger postings.
    wallet_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=True
    )
    psp_reference = mapped_column(String(128), nullable=True)
    attempts = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'CAPTURED', 'FAILED', "
            "'SETTLED', 'REFUNDED')",
            name="ck_payments_status",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_payments_tenant_idempotency_key"
        ),
        Index("ix_payments_wallet_id", "wallet_id"),
        # Sweeper lookups (migration e6c1a9d4b582)
        Index(
            "ix_payments_retry_due",
            "next_attempt_at",
            postgresql_where=text(
                "status = 'PROCESSING' AND wallet_id IS NOT NULL "
                "AND next_attempt_at IS NOT NULL"
            ),
        ),
        Index(
            "ix_payments_pending_created",
            "created_at",
            postgresql_where=text("status = 'PENDING' AND wallet_id IS NOT NULL"),
        ),
    )
