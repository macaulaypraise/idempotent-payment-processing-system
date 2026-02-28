import uuid
from datetime import datetime, timezone
from app.core.database import Base
from sqlalchemy import UUID, JSON, DateTime, String
from sqlalchemy.orm import mapped_column

class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    event_type = mapped_column(
        String,
        nullable=False
    )
    payload = mapped_column(
        JSON,
        nullable=False
    )
    published_at = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None
    )
    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )
