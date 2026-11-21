"""Inbox for provider webhooks (DESIGN 6.3).

The endpoint only verifies the signature and inserts here; the webhook
processor applies the event later. (provider, provider_event_id) is unique,
so a redelivered event is stored once however many times it arrives.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    UUID,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class WebhookStatus(enum.StrEnum):
    RECEIVED = "received"  # stored, not yet processed
    PROCESSED = "processed"  # applied (or found already applied)
    PARKED = "parked"  # cannot apply yet; retried at next_attempt_at
    DEAD = "dead"  # gave up or conflicting; a dlq.v1 event was written
    IGNORED = "ignored"  # an event type we do not act on


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = mapped_column(String(32), nullable=False)
    provider_event_id = mapped_column(String(128), nullable=False)
    event_type = mapped_column(String(64), nullable=False)
    payload = mapped_column(JSONB, nullable=False)
    status = mapped_column(
        Enum(
            WebhookStatus,
            native_enum=False,
            create_constraint=False,
            length=16,
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=WebhookStatus.RECEIVED,
    )
    # Resolved from the payment or refund at processing time: the provider
    # does not know our tenants.
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True
    )
    attempts = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at = mapped_column(DateTime(timezone=True), nullable=True)
    last_error = mapped_column(Text, nullable=True)
    received_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    processed_at = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_event_id", name="uq_webhook_events_provider_event"
        ),
    )
