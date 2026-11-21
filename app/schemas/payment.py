from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.money import Amount, Currency


class PaymentRequest(BaseModel):
    amount: Amount
    currency: Currency


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    payment_id: str
    status: str
    amount: int
    currency: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
