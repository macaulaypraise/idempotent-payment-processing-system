import uuid
import json
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis
from app.models.payment import Payment, PaymentStatus
from app.models.outbox_event import OutboxEvent
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
    # Step 1: check cache — return immediately if hit
    cached = await get_cached_response(redis, idempotency_key)
    if cached:
        return cached

    # Step 2: acquire lock — return 409 if cannot acquire
    lock_acquired = await acquire_lock(redis, idempotency_key)
    if not lock_acquired:
        raise ValueError("Duplicate request in progress")

    try:
        # Step 3: open DB transaction
        payment = Payment(
            id=uuid.uuid4(),
            idempotency_key=idempotency_key,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING,
            version=1
        )
        db.add(payment)

        # create outbox event in same transaction
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

        # Step 4: cache the response
        response = {
            "payment_id": str(payment.id),
            "status": payment.status.value,
            "amount": str(payment.amount),
            "currency": payment.currency
        }
        await cache_response(redis, idempotency_key, response, 202)
        return response

    except Exception as e:
        await db.rollback()
        raise e

    finally:
        # Step 5: always release lock
        await release_lock(redis, idempotency_key)
