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
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class PaymentStatus(enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SETTLED = "settled"
    FAILED = "failed"
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
        Enum(PaymentStatus), nullable=False, default=PaymentStatus.PENDING
    )
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
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_payments_tenant_idempotency_key"
        ),
    )
