"""Refund requests (full refunds only). Outcomes are applied through
app.services.psp_results.apply_refund_result."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox_event import OutboxEvent
from app.models.refund import Refund, RefundStatus
from app.services import posting_rules
from app.services.idempotency import BusinessRejection, HandlerResult
from app.services.ledger import InsufficientFundsError, Reference, post_transfer
from app.services.psp_results import LegacyPaymentError, lock_payment, payment_accounts
from app.services.state_machine import REFUNDABLE_STATES


def refund_body(refund: Refund) -> dict[str, Any]:
    return {
        "refund_id": str(refund.id),
        "payment_id": str(refund.payment_id),
        "status": refund.status.value,
        "amount": refund.amount,
        "currency": refund.currency,
        "created_at": refund.created_at.isoformat(),
    }


async def request_refund(
    db: AsyncSession, *, tenant_id: uuid.UUID, payment_id: uuid.UUID
) -> HandlerResult:
    """Business write for POST /v1/refunds (inside the idempotency savepoint).

    Locks the payment, checks it is refundable and has no active refund,
    moves the amount from the wallet into refunds_payable (the hold), and
    records the refund and its outbox event, all in one transaction.
    """
    payment = await lock_payment(db, tenant_id=tenant_id, payment_id=payment_id)
    if payment is None:
        raise BusinessRejection(404, {"detail": f"Payment {payment_id} not found"})
    if payment.status not in REFUNDABLE_STATES:
        raise BusinessRejection(
            422,
            {
                "detail": f"Payment is {payment.status.value}; only captured or "
                "settled payments can be refunded"
            },
        )
    try:
        wallet, system = await payment_accounts(db, payment)
    except LegacyPaymentError as exc:
        raise BusinessRejection(
            422, {"detail": "Payment predates the ledger and cannot be refunded here"}
        ) from exc

    active = (
        await db.execute(
            select(Refund.id, Refund.status).where(
                Refund.payment_id == payment.id, Refund.status != RefundStatus.FAILED
            )
        )
    ).first()
    if active is not None:
        raise BusinessRejection(
            422,
            {
                "detail": "Payment already has a refund "
                f"({active.status.value}); only one is allowed",
                "refund_id": str(active.id),
            },
        )

    source = posting_rules.hold_source(payment.status)
    refund = Refund(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        payment_id=payment.id,
        amount=payment.amount,
        currency=payment.currency,
        status=RefundStatus.PENDING,
        hold_source=source.value,
    )
    db.add(refund)
    await db.flush()  # the partial unique index backs up the check above
    try:
        await post_transfer(
            db,
            tenant_id=tenant_id,
            reference=Reference(posting_rules.REFUND_HOLD, "refund", refund.id),
            lines=posting_rules.refund_hold(
                refund.amount, payment.status, wallet, system
            ),
        )
    except InsufficientFundsError as exc:
        raise BusinessRejection(
            422,
            {
                "detail": "Wallet balance is too low to hold the refund",
                "wallet_id": str(payment.wallet_id),
            },
        ) from exc

    db.add(
        OutboxEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            event_type="refund.created",
            payload={
                "refund_id": str(refund.id),
                "payment_id": str(payment.id),
                "tenant_id": str(tenant_id),
                "amount": refund.amount,
                "currency": refund.currency,
            },
        )
    )
    await db.flush()
    await db.refresh(refund)
    return HandlerResult(
        status_code=201, body=refund_body(refund), resource_id=refund.id
    )


async def get_refund(
    db: AsyncSession, *, tenant_id: uuid.UUID, refund_id: uuid.UUID
) -> Refund | None:
    return (
        await db.execute(
            select(Refund).where(Refund.id == refund_id, Refund.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
