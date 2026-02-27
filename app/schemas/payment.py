from pydantic import BaseModel, ConfigDict
from decimal import Decimal
from uuid import UUID
from datetime import datetime
from app.models.payment import PaymentStatus

class PaymentRequest(BaseModel):
    amount: Decimal
    currency: str

class PaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    payment_id: str
    status: str
    amount: str
    currency: str
