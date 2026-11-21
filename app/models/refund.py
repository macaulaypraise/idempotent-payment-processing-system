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
    text,
)
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class RefundStatus(enum.Enum):
    """PENDING -> PROCESSING -> SUCCEEDED | FAILED (guarded by refunds_guard)."""

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Refund(Base):
    """A full refund of a captured or settled payment.

    The hold (wallet -> refunds_payable) is posted when the refund is
    requested; hold_source records which wallet account it came from.
    """

    __tablename__ = "refunds"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    payment_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False
    )
    amount = mapped_column(BigInteger, nullable=False)
    currency = mapped_column(String(3), nullable=False)
    status = mapped_column(
        Enum(RefundStatus, native_enum=False, create_constraint=False, length=16),
        nullable=False,
        default=RefundStatus.PENDING,
    )
    hold_source = mapped_column(String(16), nullable=False)
    psp_reference = mapped_column(String(128), nullable=True)
    attempts = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = mapped_column(DateTime(timezone=True), nullable=True)
    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
    )
    updated_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_refunds_amount_positive"),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SUCCEEDED', 'FAILED')",
            name="ck_refunds_status",
        ),
        CheckConstraint(
            "hold_source IN ('wallet_pending', 'wallet_available')",
            name="ck_refunds_hold_source",
        ),
        Index(
            "ix_refunds_retry_due",
            "next_attempt_at",
            postgresql_where=text(
                "status = 'PROCESSING' AND next_attempt_at IS NOT NULL"
            ),
        ),
        Index(
            "ix_refunds_pending_created",
            "created_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        Index(
            "uq_refunds_one_active_per_payment",
            "payment_id",
            unique=True,
            postgresql_where=text("status <> 'FAILED'"),
        ),
    )
