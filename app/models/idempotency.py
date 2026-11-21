from datetime import UTC, datetime

from sqlalchemy import (
    UUID,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class IdempotencyKey(Base):
    """Source of truth for idempotency (ADR 0001).

    The row is inserted in the same transaction as the business write, so the
    unique primary key doubles as the lock for concurrent duplicates, and the
    stored response is what every retry gets back.
    """

    __tablename__ = "idempotency_keys"

    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    key = mapped_column(String(255), nullable=False)
    fingerprint = mapped_column(String(64), nullable=False)
    status = mapped_column(String(16), nullable=False, default="in_progress")
    response_status = mapped_column(Integer, nullable=True)
    response_body = mapped_column(JSONB, nullable=True)
    resource_id = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "key"),
        CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="ck_idempotency_keys_status",
        ),
        CheckConstraint(
            "status <> 'completed' OR "
            "(response_status IS NOT NULL AND response_body IS NOT NULL)",
            name="ck_idempotency_keys_completed_has_response",
        ),
    )
