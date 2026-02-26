from datetime import datetime, timezone
from app.core.database import Base
from sqlalchemy import UUID, DateTime, String, PrimaryKeyConstraint
from sqlalchemy.orm import mapped_column

class ProcessedEvents(Base):
    __tablename__ = "processed_events"

    event_id = mapped_column(
        UUID(as_uuid=True),
        nullable=False
    )
    consumer_group = mapped_column(
        String,
        nullable=False
    )
    processed_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )

    __table_args__ = (
        PrimaryKeyConstraint("event_id", "consumer_group"),
    )
