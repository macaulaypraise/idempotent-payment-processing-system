from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.dependencies import get_db_dep, get_redis
from app.schemas.payment import PaymentRequest, PaymentResponse
from app.services.payment_service import create_payment
from app.core.exceptions import IdempotencyConflictError, PaymentNotFoundError
from app.models.payment import Payment
from sqlalchemy import select
import uuid

router = APIRouter()

@router.post("/payments", response_model=PaymentResponse, status_code=202)
async def create_payment_route(
    payload: PaymentRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db_dep),
    redis: Redis = Depends(get_redis)
):
    try:
        result = await create_payment(
            db=db,
            redis=redis,
            idempotency_key=idempotency_key,
            amount=float(payload.amount),
            currency=payload.currency
        )
        if "body" in result:
            return result["body"]
        return result
    except ValueError:
        raise IdempotencyConflictError()

@router.get("/payments/{payment_id}", response_model=PaymentResponse)
async def get_payment_route(
    payment_id: str,
    db: AsyncSession = Depends(get_db_dep)
):
    result = await db.execute(
        select(Payment).where(Payment.id == uuid.UUID(payment_id))
    )
    payment = result.scalar_one_or_none()
    if not payment:
        raise PaymentNotFoundError(payment_id)
    return {
        "payment_id": str(payment.id),
        "status": payment.status.value,
        "amount": str(payment.amount),
        "currency": payment.currency
    }
