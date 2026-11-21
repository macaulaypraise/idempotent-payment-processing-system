import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, UUID, BigInteger, DateTime, ForeignKey, Identity, String
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Publish order (ADR 0005). Assigned at INSERT; events for one payment are
    # written under its row lock, so their seq order is their commit order.
    seq = mapped_column(BigInteger, Identity(always=True), nullable=False)
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False
    )
    event_type = mapped_column(String, nullable=False)
    payload = mapped_column(JSON, nullable=False)
    published_at = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
