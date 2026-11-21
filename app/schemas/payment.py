import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.money import Amount, Currency


class PaymentRequest(BaseModel):
    amount: Amount
    currency: Currency
    wallet_id: uuid.UUID  # credited when the provider confirms the capture


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    payment_id: str
    status: str
    amount: int
    currency: str
    wallet_id: str | None = None  # None only for pre-v2 payments
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RefundRequest(BaseModel):
    payment_id: uuid.UUID  # full refunds only


class RefundResponse(BaseModel):
    refund_id: str
    payment_id: str
    status: str
    amount: int
    currency: str
    created_at: datetime
