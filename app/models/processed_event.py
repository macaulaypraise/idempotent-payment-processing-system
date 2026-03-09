from datetime import UTC, datetime

from sqlalchemy import UUID, DateTime, PrimaryKeyConstraint, String
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    event_id = mapped_column(UUID(as_uuid=True), nullable=False)
    consumer_group = mapped_column(String, nullable=False)
    processed_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (PrimaryKeyConstraint("event_id", "consumer_group"),)
