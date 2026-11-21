"""The single entry point for provider outcomes (DESIGN 6.2).

Whatever reports an outcome (the charger's own call, a webhook, the sweeper
or reconciliation) goes through these functions. They are idempotent: an
outcome that is already reflected changes nothing. `source` is recorded for
metrics and logs only; behaviour never depends on it.

All functions run inside the caller's transaction and do not commit, so the
caller can record its own dedup row in the same transaction.

Lock order everywhere: payment row first, then refund row, then accounts in
ascending id (inside post_transfer).
"""

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import psp_results_total
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.models.refund import Refund, RefundStatus
from app.models.wallet import Wallet
from app.services import posting_rules
from app.services.ledger import Reference, post_transfer, system_accounts
from app.services.posting_rules import SystemAccounts, WalletAccounts
from app.services.state_machine import CAPTURED_STATES

logger = structlog.get_logger()


class OutcomeKind(enum.StrEnum):
    CAPTURED = "captured"
    DECLINED = "declined"


class RefundOutcomeKind(enum.StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class PspOutcome:
    kind: OutcomeKind
    psp_reference: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class RefundOutcome:
    kind: RefundOutcomeKind
    psp_reference: str | None = None
    reason: str | None = None


class Applied(enum.StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"


class NotApplicableYetError(Exception):
    """The outcome arrived before the state it applies to (e.g. a provider
    result for a payment not yet PROCESSING). Park and retry later."""


class ConflictingOutcomeError(Exception):
    """The outcome contradicts what is recorded (e.g. a capture for a FAILED
    payment). Never applied automatically: reconciliation and a human decide."""


class PaymentNotFoundError(Exception):
    pass


class LegacyPaymentError(Exception):
    """A payment created before the ledger existed (no wallet)."""


async def lock_payment(
    db: AsyncSession, *, tenant_id: uuid.UUID, payment_id: uuid.UUID
) -> Payment | None:
    return (
        await db.execute(
            select(Payment)
            .where(Payment.id == payment_id, Payment.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def payment_accounts(
    db: AsyncSession, payment: Payment
) -> tuple[WalletAccounts, SystemAccounts]:
    if payment.wallet_id is None:
        raise LegacyPaymentError(f"payment {payment.id} has no wallet (pre-v2 row)")
    wallet = await db.get(Wallet, payment.wallet_id)
    if wallet is None:  # pragma: no cover - FK makes this impossible
        raise LegacyPaymentError(f"wallet {payment.wallet_id} missing")
    system = await system_accounts(
        db, tenant_id=payment.tenant_id, currency=payment.currency
    )
    return (
        WalletAccounts(wallet.pending_account_id, wallet.available_account_id),
        SystemAccounts.from_kinds(system),
    )


def _event(payment: Payment, event_type: str, **extra: object) -> OutboxEvent:
    return OutboxEvent(
        id=uuid.uuid4(),
        tenant_id=payment.tenant_id,
        event_type=event_type,
        payload={
            "payment_id": str(payment.id),
            "tenant_id": str(payment.tenant_id),
            "status": payment.status.value,
            "amount": payment.amount,
            "currency": payment.currency,
            **extra,
        },
    )


async def apply_psp_result(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    payment_id: uuid.UUID,
    outcome: PspOutcome,
    source: str,
) -> Applied:
    """Apply a charge outcome to a payment, posting the capture on success."""
    payment = await lock_payment(db, tenant_id=tenant_id, payment_id=payment_id)
    if payment is None:
        raise PaymentNotFoundError(str(payment_id))
    status = payment.status

    if outcome.kind is OutcomeKind.CAPTURED:
        if status in CAPTURED_STATES:
            return _record(source, outcome.kind, Applied.ALREADY_APPLIED)
        if status is PaymentStatus.FAILED:
            raise ConflictingOutcomeError(f"capture reported for FAILED {payment.id}")
        if status is PaymentStatus.PENDING:
            raise NotApplicableYetError(f"payment {payment.id} is not PROCESSING yet")
        wallet, system = await payment_accounts(db, payment)
        await post_transfer(
            db,
            tenant_id=tenant_id,
            reference=Reference(posting_rules.CAPTURE, "payment", payment.id),
            lines=posting_rules.capture(payment.amount, wallet, system),
        )
        payment.status = PaymentStatus.CAPTURED
        payment.psp_reference = outcome.psp_reference
        db.add(
            _event(
                payment,
                "payment.captured",
                source=source,
                psp_reference=outcome.psp_reference,
            )
        )
    else:
        if status is PaymentStatus.FAILED:
            return _record(source, outcome.kind, Applied.ALREADY_APPLIED)
        if status in CAPTURED_STATES:
            raise ConflictingOutcomeError(
                f"decline reported for {status.name} {payment.id}"
            )
        if status is PaymentStatus.PENDING:
            raise NotApplicableYetError(f"payment {payment.id} is not PROCESSING yet")
        payment.status = PaymentStatus.FAILED
        payment.psp_reference = outcome.psp_reference or payment.psp_reference
        db.add(_event(payment, "payment.failed", source=source, reason=outcome.reason))

    await db.flush()
    logger.info(
        "psp_result_applied",
        payment_id=str(payment.id),
        outcome=outcome.kind.value,
        source=source,
    )
    return _record(source, outcome.kind, Applied.APPLIED)


def _record(source: str, kind: enum.StrEnum, applied: Applied) -> Applied:
    psp_results_total.labels(
        source=source, outcome=kind.value, result=applied.value
    ).inc()
    return applied


async def lock_refund(
    db: AsyncSession, *, tenant_id: uuid.UUID, refund_id: uuid.UUID
) -> tuple[Payment, Refund]:
    payment_id = (
        await db.execute(
            select(Refund.payment_id).where(
                Refund.id == refund_id, Refund.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if payment_id is None:
        raise PaymentNotFoundError(f"refund {refund_id}")
    payment = await lock_payment(db, tenant_id=tenant_id, payment_id=payment_id)
    refund = (
        await db.execute(
            select(Refund)
            .where(Refund.id == refund_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert payment is not None
    return payment, refund


def _refund_event(refund: Refund, event_type: str, **extra: object) -> OutboxEvent:
    return OutboxEvent(
        id=uuid.uuid4(),
        tenant_id=refund.tenant_id,
        event_type=event_type,
        payload={
            "refund_id": str(refund.id),
            "payment_id": str(refund.payment_id),
            "tenant_id": str(refund.tenant_id),
            "status": refund.status.value,
            "amount": refund.amount,
            "currency": refund.currency,
            **extra,
        },
    )


async def start_refund_processing(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    lease_until: datetime | None = None,
) -> Applied:
    """PENDING -> PROCESSING, committed before the provider is called.

    `lease_until` is when the sweeper may retry if this attempt never reports.
    """
    _, refund = await lock_refund(db, tenant_id=tenant_id, refund_id=refund_id)
    if refund.status is not RefundStatus.PENDING:
        return Applied.ALREADY_APPLIED
    refund.status = RefundStatus.PROCESSING
    refund.attempts += 1
    refund.next_attempt_at = lease_until
    await db.flush()
    return Applied.APPLIED


async def apply_refund_result(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    outcome: RefundOutcome,
    source: str,
) -> Applied:
    """Confirm (refunds_payable -> psp_clearing, payment REFUNDED) or fail
    (hold returned to the wallet account the money belongs in now)."""
    payment, refund = await lock_refund(db, tenant_id=tenant_id, refund_id=refund_id)
    target = (
        RefundStatus.SUCCEEDED
        if outcome.kind is RefundOutcomeKind.SUCCEEDED
        else RefundStatus.FAILED
    )
    if refund.status is target:
        return _record(source, outcome.kind, Applied.ALREADY_APPLIED)
    if refund.status in (RefundStatus.SUCCEEDED, RefundStatus.FAILED):
        raise ConflictingOutcomeError(
            f"{outcome.kind.value} reported for {refund.status.name} refund {refund.id}"
        )
    if refund.status is RefundStatus.PENDING:
        raise NotApplicableYetError(f"refund {refund.id} is not PROCESSING yet")

    wallet, system = await payment_accounts(db, payment)
    if target is RefundStatus.SUCCEEDED:
        await post_transfer(
            db,
            tenant_id=tenant_id,
            reference=Reference(posting_rules.REFUND_CONFIRM, "refund", refund.id),
            lines=posting_rules.refund_confirm(refund.amount, system),
        )
        refund.status = RefundStatus.SUCCEEDED
        refund.psp_reference = outcome.psp_reference
        payment.status = PaymentStatus.REFUNDED
        db.add(_refund_event(refund, "refund.succeeded", source=source))
        db.add(
            _event(payment, "payment.refunded", source=source, refund_id=str(refund.id))
        )
    else:
        await post_transfer(
            db,
            tenant_id=tenant_id,
            reference=Reference(posting_rules.REFUND_RELEASE, "refund", refund.id),
            lines=posting_rules.refund_fail(
                refund.amount, payment.status, wallet, system
            ),
        )
        refund.status = RefundStatus.FAILED
        refund.psp_reference = outcome.psp_reference or refund.psp_reference
        db.add(
            _refund_event(refund, "refund.failed", source=source, reason=outcome.reason)
        )

    await db.flush()
    logger.info(
        "refund_result_applied",
        refund_id=str(refund.id),
        outcome=outcome.kind.value,
        source=source,
    )
    return _record(source, outcome.kind, Applied.APPLIED)


async def confirm_refund(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    psp_reference: str | None,
    source: str,
) -> Applied:
    return await apply_refund_result(
        db,
        tenant_id=tenant_id,
        refund_id=refund_id,
        outcome=RefundOutcome(RefundOutcomeKind.SUCCEEDED, psp_reference),
        source=source,
    )


async def fail_refund(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    refund_id: uuid.UUID,
    reason: str | None,
    source: str,
) -> Applied:
    return await apply_refund_result(
        db,
        tenant_id=tenant_id,
        refund_id=refund_id,
        outcome=RefundOutcome(RefundOutcomeKind.FAILED, reason=reason),
        source=source,
    )
