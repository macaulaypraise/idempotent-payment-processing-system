import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import UUID, DateTime, Enum, Integer, Numeric, String
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
    idempotency_key = mapped_column(String, unique=True, nullable=False)
    amount = mapped_column(Numeric(precision=12, scale=2), nullable=False)
    currency = mapped_column(String(3), nullable=False)
    version = mapped_column(Integer, nullable=False, default=1)
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
