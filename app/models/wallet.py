import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    UUID,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import mapped_column

from app.core.database import Base


class Wallet(Base):
    """A user-facing balance holder that owns two guarded ledger accounts."""

    __tablename__ = "wallets"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    currency = mapped_column(String(3), nullable=False)
    pending_account_id = mapped_column(UUID(as_uuid=True), nullable=False)
    available_account_id = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=text("now()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["pending_account_id", "currency"], ["accounts.id", "accounts.currency"]
        ),
        ForeignKeyConstraint(
            ["available_account_id", "currency"], ["accounts.id", "accounts.currency"]
        ),
        UniqueConstraint("pending_account_id"),
        UniqueConstraint("available_account_id"),
    )
