import uuid
from datetime import date, datetime

from pydantic import BaseModel


class ReconciliationRequest(BaseModel):
    date: date  # the provider file date, UTC


class ReconItemResponse(BaseModel):
    id: uuid.UUID
    classification: str
    settlement_line_id: uuid.UUID | None
    line_id: str | None
    tenant_id: uuid.UUID | None
    payment_id: uuid.UUID | None
    refund_id: uuid.UUID | None
    expected: int | None
    actual: int | None
    posted: int
    detail: str | None
    resolved: bool


class ReconciliationResponse(BaseModel):
    id: uuid.UUID
    provider: str
    file_date: date
    status: str
    lines: int
    counts: dict[str, int]
    unexplained: int
    posted: int
    error: str | None
    started_at: datetime
    finished_at: datetime | None
    items: list[ReconItemResponse] | None = None
