"""Settlement files and reconciliation (DESIGN 6.6, docs/reconciliation.md)."""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    UUID,
    BigInteger,
    Boolean,
    Date,
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


class Classification(enum.StrEnum):
    MATCHED = "matched"  # agrees with the ledger; settlement posted
    HEALED = (
        "healed"  # payment/refund was stuck PROCESSING; outcome applied, then matched
    )
    AMOUNT_MISMATCH = "amount_mismatch"  # gross, net or currency disagree
    MISSING_IN_LEDGER = (
        "missing_in_ledger"  # the provider reports something we do not know
    )
    DUPLICATE_LINE = "duplicate_line"  # already settled by another line
    MISSING_AT_PSP = (
        "missing_at_psp"  # we captured/refunded; no line arrived in the window
    )
    STATUS_CONFLICT = (
        "status_conflict"  # e.g. provider settled a charge we recorded as FAILED
    )


EXPLAINED = frozenset({Classification.MATCHED, Classification.HEALED})


class RunStatus(enum.StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"  # a crashed run, found by the next run of the date


def _values(members: type[enum.StrEnum]) -> list[str]:
    return [m.value for m in members]


class SettlementLine(Base):
    """One row of a provider settlement file, exactly as loaded. Immutable."""

    __tablename__ = "settlement_lines"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = mapped_column(String(32), nullable=False)
    file_date = mapped_column(Date, nullable=False)
    line_id = mapped_column(String(128), nullable=False)
    type = mapped_column(String(16), nullable=False)  # charge | refund
    psp_reference = mapped_column(String(128), nullable=False)
    merchant_reference = mapped_column(String(128), nullable=True)
    gross = mapped_column(BigInteger, nullable=False)
    fee = mapped_column(BigInteger, nullable=False)
    net = mapped_column(BigInteger, nullable=False)
    currency = mapped_column(String(3), nullable=False)
    loaded_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint(
            "provider", "file_date", "line_id", name="uq_settlement_lines_line"
        ),
    )


class ReconRun(Base):
    """One reconciliation of one provider file date (platform-wide)."""

    __tablename__ = "recon_runs"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider = mapped_column(String(32), nullable=False)
    file_date = mapped_column(Date, nullable=False)
    status = mapped_column(
        Enum(
            RunStatus,
            native_enum=False,
            create_constraint=False,
            length=16,
            values_callable=_values,
        ),
        nullable=False,
        default=RunStatus.RUNNING,
    )
    lines = mapped_column(Integer, nullable=False, default=0)
    counts = mapped_column(JSONB, nullable=False, default=dict)
    unexplained = mapped_column(Integer, nullable=False, default=0)
    posted = mapped_column(Integer, nullable=False, default=0)
    error = mapped_column(Text, nullable=True)
    started_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    finished_at = mapped_column(DateTime(timezone=True), nullable=True)


class ReconItem(Base):
    __tablename__ = "recon_items"

    id = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("recon_runs.id"), nullable=False
    )
    classification = mapped_column(
        Enum(
            Classification,
            native_enum=False,
            create_constraint=False,
            length=24,
            values_callable=_values,
        ),
        nullable=False,
    )
    settlement_line_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("settlement_lines.id"), nullable=True
    )
    tenant_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True
    )
    payment_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("payments.id"), nullable=True
    )
    refund_id = mapped_column(
        UUID(as_uuid=True), ForeignKey("refunds.id"), nullable=True
    )
    expected = mapped_column(BigInteger, nullable=True)  # what the ledger says
    actual = mapped_column(BigInteger, nullable=True)  # what the provider says
    posted = mapped_column(Integer, nullable=False, default=0)  # transfers created
    detail = mapped_column(Text, nullable=True)
    resolved = mapped_column(Boolean, nullable=False, default=False)
    created_at = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
