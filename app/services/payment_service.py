import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_errors import violates_constraint
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.services.idempotency import BusinessRejection, HandlerResult


def payment_body(payment: Payment) -> dict[str, Any]:
    return {
        "payment_id": str(payment.id),
        "status": payment.status.value,
        "amount": payment.amount,
        "currency": payment.currency,
    }


async def create_payment(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    idempotency_key: str,
    amount: int,
    currency: str,
) -> HandlerResult:
    """Business write for POST /v1/payments: the payment row and its outbox
    event. Runs inside execute_idempotent's savepoint; no commit here."""
    payment = Payment(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        idempotency_key=idempotency_key,
        amount=amount,
        currency=currency,
        status=PaymentStatus.PENDING,
    )
    db.add(payment)
    db.add(
        OutboxEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            event_type="payment.initiated",
            payload={
                "payment_id": str(payment.id),
                "tenant_id": str(tenant_id),
                "amount": amount,
                "currency": currency,
            },
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        if violates_constraint(exc, "uq_payments_tenant_idempotency_key"):
            # The key row expired and was swept, but the payment it created is
            # still here. Refuse rather than create a second payment.
            raise BusinessRejection(
                422,
                {
                    "detail": "Idempotency-Key was already used for a payment "
                    "and has expired; use a new key"
                },
            ) from exc
        raise
    return HandlerResult(
        status_code=202, body=payment_body(payment), resource_id=payment.id
    )


async def get_payment(
    db: AsyncSession, *, tenant_id: uuid.UUID, payment_id: uuid.UUID
) -> Payment | None:
    result = await db.execute(
        select(Payment).where(Payment.id == payment_id, Payment.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()
