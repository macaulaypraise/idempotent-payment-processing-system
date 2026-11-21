import uuid
from datetime import UTC, datetime

from sqlalchemy import UUID, DateTime, ForeignKey, String
from sqlalchemy.orm import mapped_column

from app.core.database import Base

# Seeded by the Stage 1 migration; owns every row that existed before tenants.
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Tenant(Base):
    __tablename__ = "tenants"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = mapped_column(String, nullable=False)
    created_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class ApiKey(Base):
    """An API key is stored only as its SHA-256; the plaintext is shown once."""

    __tablename__ = "api_keys"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    key_hash = mapped_column(String(64), nullable=False, unique=True)
    prefix = mapped_column(String(16), nullable=False)
    created_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    revoked_at = mapped_column(DateTime(timezone=True), nullable=True, default=None)
