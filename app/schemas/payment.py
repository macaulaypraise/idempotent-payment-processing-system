from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PaymentRequest(BaseModel):
    amount: float = Field(gt=0, description="Payment amount must be greater than zero")
    currency: str


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    payment_id: str
    status: str
    amount: str
    currency: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
