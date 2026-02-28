import uuid
import json
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from redis.asyncio import Redis
from app.models.payment import Payment, PaymentStatus
from app.models.outbox_event import OutboxEvent
from app.core.metrics import payments_created_total
from app.services.idempotency import (
    get_cached_response,
    cache_response,
    acquire_lock,
    release_lock
)

async def create_payment(
    db: AsyncSession,
    redis: Redis,
    idempotency_key: str,
    amount: float,
    currency: str
) -> dict:
    cached = await get_cached_response(redis, idempotency_key)
    if cached:
        payments_created_total.labels(status="idempotent_hit").inc()
        return cached.get("body", cached)

    lock_acquired = await acquire_lock(redis, idempotency_key)
    if not lock_acquired:
        raise ValueError("Duplicate request in progress")

    try:
        payment = Payment(
            id=uuid.uuid4(),
            idempotency_key=idempotency_key,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING,
            version=1
        )
        db.add(payment)
        outbox_event = OutboxEvent(
            id=uuid.uuid4(),
            event_type="payment.initiated",
            payload={
                "payment_id": str(payment.id),
                "amount": str(amount),
                "currency": currency
            }
        )
        db.add(outbox_event)
        await db.commit()
        await db.refresh(payment)
        response = {
            "payment_id": str(payment.id),
            "status": payment.status.value,
            "amount": str(payment.amount),
            "currency": payment.currency
        }
        await cache_response(redis, idempotency_key, response, 202)
        payments_created_total.labels(status="success").inc()
        return response

    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        )
        existing = result.scalar_one()
        response = {
            "payment_id": str(existing.id),
            "status": existing.status.value,
            "amount": str(existing.amount),
            "currency": existing.currency
        }
        await cache_response(redis, idempotency_key, response, 202)
        payments_created_total.labels(status="conflict").inc()
        return response

    except Exception as e:
        await db.rollback()
        raise e

    finally:
        await release_lock(redis, idempotency_key)
