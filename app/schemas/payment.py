from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class PaymentRequest(BaseModel):
    # Decimal, not float: money never passes through binary floating point.
    # Stage 2 moves to integer minor units.
    amount: Decimal = Field(
        gt=0, max_digits=12, decimal_places=2, description="Amount, e.g. 10.50"
    )
    currency: str = Field(min_length=3, max_length=3, pattern="^[A-Z]{3}$")


class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    payment_id: str
    status: str
    amount: str
    currency: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
