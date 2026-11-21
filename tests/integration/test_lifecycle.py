"""
Stage 3 exit gate: every state change that moves money posts exactly one
transfer, outcomes are idempotent, and races keep I1, I4, I6.

Real PostgreSQL, app role. "Settlement" here is a stand-in for Stage 6: it
posts the settlement and release rules and marks the payment SETTLED.
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.ledger import Account, Entry, Transfer
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.models.refund import Refund, RefundStatus
from app.services import posting_rules
from app.services.idempotency import BusinessRejection
from app.services.ledger import Reference, balances, post_transfer
from app.services.payment_service import create_payment
from app.services.psp_results import (
    Applied,
    ConflictingOutcomeError,
    LegacyPaymentError,
    NotApplicableYetError,
    OutcomeKind,
    PspOutcome,
    apply_psp_result,
    confirm_refund,
    fail_refund,
    payment_accounts,
    start_refund_processing,
)
from app.services.refunds import request_refund
from verification.invariants import check_ledger

pytestmark = pytest.mark.asyncio
Factory = async_sessionmaker[AsyncSession]
CAPTURED = PspOutcome(OutcomeKind.CAPTURED, "psp_ch_1")
DECLINED = PspOutcome(OutcomeKind.DECLINED, reason="card_declined")


# --- helpers ------------------------------------------------------------------------


async def _payment(factory: Factory, creds: Any, amount: int = 1_000) -> uuid.UUID:
    async with factory() as db:
        result = await create_payment(
            db,
            tenant_id=creds.tenant_id,
            idempotency_key=str(uuid.uuid4()),
            amount=amount,
            currency="USD",
            wallet_id=creds.wallet_id,
        )
        await db.commit()
    assert result.resource_id is not None
    return result.resource_id


async def _processing(factory: Factory, payment_id: uuid.UUID) -> None:
    async with factory() as db:
        await db.execute(
            update(Payment)
            .where(Payment.id == payment_id)
            .values(status=PaymentStatus.PROCESSING)
        )
        await db.commit()


async def _apply(
    factory: Factory, creds: Any, payment_id: uuid.UUID, outcome: PspOutcome = CAPTURED
) -> Applied:
    async with factory() as db:
        result = await apply_psp_result(
            db,
            tenant_id=creds.tenant_id,
            payment_id=payment_id,
            outcome=outcome,
            source="test",
        )
        await db.commit()
        return result


async def _captured(factory: Factory, creds: Any, amount: int = 1_000) -> uuid.UUID:
    payment_id = await _payment(factory, creds, amount)
    await _processing(factory, payment_id)
    assert await _apply(factory, creds, payment_id) is Applied.APPLIED
    return payment_id


async def _settle(
    factory: Factory, creds: Any, payment_id: uuid.UUID, fee: int = 0
) -> None:
    """Stage 6 stand-in: settlement + release postings, then SETTLED."""
    async with factory() as db:
        payment = await db.get(Payment, payment_id)
        assert payment is not None
        wallet, system = await payment_accounts(db, payment)
        await post_transfer(
            db,
            tenant_id=creds.tenant_id,
            reference=Reference(posting_rules.SETTLEMENT_CHARGE, "payment", payment_id),
            lines=posting_rules.settlement_charge(payment.amount, fee, system),
        )
        await post_transfer(
            db,
            tenant_id=creds.tenant_id,
            reference=Reference(posting_rules.RELEASE, "payment", payment_id),
            lines=posting_rules.release(payment.amount, wallet),
        )
        payment.status = PaymentStatus.SETTLED
        await db.commit()


async def _refund(factory: Factory, creds: Any, payment_id: uuid.UUID) -> uuid.UUID:
    async with factory() as db:
        async with db.begin_nested():
            result = await request_refund(
                db, tenant_id=creds.tenant_id, payment_id=payment_id
            )
        await db.commit()
    assert result.resource_id is not None
    return result.resource_id


async def _refund_processing(
    factory: Factory, creds: Any, refund_id: uuid.UUID
) -> None:
    async with factory() as db:
        await start_refund_processing(
            db, tenant_id=creds.tenant_id, refund_id=refund_id
        )
        await db.commit()


async def _lines(
    factory: Factory, kind: str, reference_id: uuid.UUID
) -> list[tuple[str, str, int]]:
    """Entries of one transfer as (direction, account kind, amount), in order."""
    async with factory() as db:
        rows = (
            await db.execute(
                select(Entry.direction, Account.kind, Entry.amount)
                .join(Transfer, Transfer.id == Entry.transfer_id)
                .join(Account, Account.id == Entry.account_id)
                .where(Transfer.kind == kind, Transfer.reference_id == reference_id)
                .order_by(Entry.id)
            )
        ).all()
    return [tuple(r) for r in rows]  # type: ignore[misc]


async def _snapshot(
    factory: Factory, creds: Any, payment_id: uuid.UUID
) -> tuple[Any, ...]:
    async with factory() as db:
        counts = []
        for model in (Transfer, Entry, OutboxEvent, Refund):
            counts.append(
                (await db.execute(select(func.count()).select_from(model))).scalar_one()
            )
        row = (
            await db.execute(
                select(Payment.status, Payment.psp_reference, Payment.updated_at).where(
                    Payment.id == payment_id
                )
            )
        ).one()
        totals = (
            await db.execute(
                select(
                    func.sum(Account.debits_posted), func.sum(Account.credits_posted)
                ).where(Account.tenant_id == creds.tenant_id)
            )
        ).one()
    return (*counts, tuple(row), tuple(totals))


async def _wallet_balances(factory: Factory, creds: Any) -> tuple[int, int]:
    async with factory() as db:
        from app.models.wallet import Wallet

        wallet = await db.get(Wallet, creds.wallet_id)
        assert wallet is not None
        b = await balances(db, [wallet.pending_account_id, wallet.available_account_id])
        return b[wallet.pending_account_id], b[wallet.available_account_id]


async def _clean(factory: Factory, creds: Any) -> None:
    async with factory() as db:
        assert await check_ledger(db, creds.tenant_id) == []


# --- Exit gate: each wired posting rule posts exactly its lines ------------------------


async def test_capture_posts_exactly_the_capture_rule(
    session_factory: Factory, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id = await _captured(session_factory, creds, 1_000)
    assert await _lines(session_factory, "capture", payment_id) == [
        ("debit", "psp_clearing", 1_000),
        ("credit", "wallet_pending", 1_000),
    ]
    assert await _wallet_balances(session_factory, creds) == (1_000, 0)
    await _clean(session_factory, creds)


async def test_decline_posts_nothing(
    session_factory: Factory, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id = await _payment(session_factory, creds)
    await _processing(session_factory, payment_id)
    await _apply(session_factory, creds, payment_id, DECLINED)
    async with session_factory() as db:
        payment = await db.get(Payment, payment_id)
        assert payment is not None and payment.status is PaymentStatus.FAILED
        assert (
            await db.execute(
                select(func.count())
                .select_from(Transfer)
                .where(Transfer.reference_id == payment_id)
            )
        ).scalar_one() == 0
    await _clean(session_factory, creds)


@pytest.mark.parametrize("settled", [False, True], ids=["captured", "settled"])
async def test_refund_hold_then_confirm_posts_exact_lines(
    session_factory: Factory, make_tenant: Any, settled: bool
) -> None:
    creds = await make_tenant()
    payment_id = await _captured(session_factory, creds, 1_000)
    if settled:
        await _settle(session_factory, creds, payment_id)
    refund_id = await _refund(session_factory, creds, payment_id)
    source = "wallet_available" if settled else "wallet_pending"
    assert await _lines(session_factory, "refund_hold", refund_id) == [
        ("debit", source, 1_000),
        ("credit", "refunds_payable", 1_000),
    ]

    await _refund_processing(session_factory, creds, refund_id)
    async with session_factory() as db:
        assert (
            await confirm_refund(
                db,
                tenant_id=creds.tenant_id,
                refund_id=refund_id,
                psp_reference="psp_re_1",
                source="test",
            )
            is Applied.APPLIED
        )
        await db.commit()
    assert await _lines(session_factory, "refund_confirm", refund_id) == [
        ("debit", "refunds_payable", 1_000),
        ("credit", "psp_clearing", 1_000),
    ]
    async with session_factory() as db:
        payment = await db.get(Payment, payment_id)
        refund = await db.get(Refund, refund_id)
        assert payment is not None and refund is not None
        assert payment.status is PaymentStatus.REFUNDED
        assert refund.status is RefundStatus.SUCCEEDED
    assert await _wallet_balances(session_factory, creds) == (0, 0)
    await _clean(session_factory, creds)


@pytest.mark.parametrize(
    "settle_while_in_flight,returned_to",
    [(False, "wallet_pending"), (True, "wallet_available")],
    ids=["still-captured", "settled-meanwhile"],
)
async def test_failed_refund_returns_the_hold_where_the_money_belongs_now(
    session_factory: Factory,
    make_tenant: Any,
    settle_while_in_flight: bool,
    returned_to: str,
) -> None:
    creds = await make_tenant()
    payment_id = await _captured(session_factory, creds, 1_000)
    refund_id = await _refund(session_factory, creds, payment_id)  # hold from pending
    if settle_while_in_flight:
        # Release is skipped while a hold is active (the money already left
        # pending); only the settlement posting and the status change happen.
        async with session_factory() as db:
            payment = await db.get(Payment, payment_id)
            assert payment is not None
            _, system = await payment_accounts(db, payment)
            await post_transfer(
                db,
                tenant_id=creds.tenant_id,
                reference=Reference(
                    posting_rules.SETTLEMENT_CHARGE, "payment", payment_id
                ),
                lines=posting_rules.settlement_charge(1_000, 0, system),
            )
            payment.status = PaymentStatus.SETTLED
            await db.commit()

    await _refund_processing(session_factory, creds, refund_id)
    async with session_factory() as db:
        await fail_refund(
            db,
            tenant_id=creds.tenant_id,
            refund_id=refund_id,
            reason="provider_rejected",
            source="test",
        )
        await db.commit()

    assert await _lines(session_factory, "refund_release", refund_id) == [
        ("debit", "refunds_payable", 1_000),
        ("credit", returned_to, 1_000),
    ]
    expected = (0, 1_000) if settle_while_in_flight else (1_000, 0)
    assert await _wallet_balances(session_factory, creds) == expected
    await _clean(session_factory, creds)


# --- Exit gate: apply_psp_result twice changes nothing the second time ----------------


@pytest.mark.parametrize("outcome", [CAPTURED, DECLINED], ids=["captured", "declined"])
async def test_applying_the_same_outcome_twice_is_a_no_op(
    session_factory: Factory, make_tenant: Any, outcome: PspOutcome
) -> None:
    creds = await make_tenant()
    payment_id = await _payment(session_factory, creds)
    await _processing(session_factory, payment_id)
    assert await _apply(session_factory, creds, payment_id, outcome) is Applied.APPLIED
    before = await _snapshot(session_factory, creds, payment_id)
    assert (
        await _apply(session_factory, creds, payment_id, outcome)
        is Applied.ALREADY_APPLIED
    )
    assert await _snapshot(session_factory, creds, payment_id) == before


async def test_capture_is_already_applied_after_settlement_and_refund(
    session_factory: Factory, make_tenant: Any
) -> None:
    """A late duplicate webhook must not post a second capture."""
    creds = await make_tenant()
    payment_id = await _captured(session_factory, creds)
    await _settle(session_factory, creds, payment_id)
    assert await _apply(session_factory, creds, payment_id) is Applied.ALREADY_APPLIED
    await _clean(session_factory, creds)


@pytest.mark.parametrize(
    "first,second,error",
    [
        (DECLINED, CAPTURED, ConflictingOutcomeError),
        (CAPTURED, DECLINED, ConflictingOutcomeError),
    ],
    ids=["capture-after-decline", "decline-after-capture"],
)
async def test_contradicting_outcomes_are_refused(
    session_factory: Factory,
    make_tenant: Any,
    first: PspOutcome,
    second: PspOutcome,
    error: type,
) -> None:
    creds = await make_tenant()
    payment_id = await _payment(session_factory, creds)
    await _processing(session_factory, payment_id)
    await _apply(session_factory, creds, payment_id, first)
    with pytest.raises(error):
        await _apply(session_factory, creds, payment_id, second)


@pytest.mark.parametrize("outcome", [CAPTURED, DECLINED], ids=["captured", "declined"])
async def test_outcome_for_a_pending_payment_is_not_applicable_yet(
    session_factory: Factory, make_tenant: Any, outcome: PspOutcome
) -> None:
    creds = await make_tenant()
    payment_id = await _payment(session_factory, creds)
    with pytest.raises(NotApplicableYetError):
        await _apply(session_factory, creds, payment_id, outcome)


async def test_refund_outcomes_are_idempotent_and_exclusive(
    session_factory: Factory, make_tenant: Any
) -> None:
    creds = await make_tenant()
    refund_id = await _refund(
        session_factory, creds, await _captured(session_factory, creds)
    )
    async with session_factory() as db:
        with pytest.raises(NotApplicableYetError):
            await confirm_refund(
                db,
                tenant_id=creds.tenant_id,
                refund_id=refund_id,
                psp_reference=None,
                source="test",
            )
    await _refund_processing(session_factory, creds, refund_id)
    for expected in (Applied.APPLIED, Applied.ALREADY_APPLIED):
        async with session_factory() as db:
            assert (
                await confirm_refund(
                    db,
                    tenant_id=creds.tenant_id,
                    refund_id=refund_id,
                    psp_reference="r",
                    source="test",
                )
                is expected
            )
            await db.commit()
    async with session_factory() as db:
        with pytest.raises(ConflictingOutcomeError):
            await fail_refund(
                db,
                tenant_id=creds.tenant_id,
                refund_id=refund_id,
                reason=None,
                source="test",
            )
    await _clean(session_factory, creds)


# --- Exit gate: races ---------------------------------------------------------------------


async def test_capture_racing_a_refund_request_keeps_invariants(
    session_factory: Factory, make_tenant: Any
) -> None:
    """The refund either sees PROCESSING (rejected) or CAPTURED (held); never
    a half-state. Both lock the payment row first."""
    creds = await make_tenant()
    payments = [await _payment(session_factory, creds, 100) for _ in range(30)]
    for payment_id in payments:
        await _processing(session_factory, payment_id)

    async def refund_or_reject(payment_id: uuid.UUID) -> str:
        try:
            await _refund(session_factory, creds, payment_id)
            return "held"
        except BusinessRejection:
            return "rejected"

    results = await asyncio.gather(
        *[
            coro
            for payment_id in payments
            for coro in (
                _apply(session_factory, creds, payment_id),
                refund_or_reject(payment_id),
            )
        ]
    )
    assert all(r is Applied.APPLIED for r in results[0::2])
    assert set(results[1::2]) <= {"held", "rejected"}
    pending, available = await _wallet_balances(session_factory, creds)
    held = results[1::2].count("held")
    assert (pending, available) == (100 * (30 - held), 0)
    await _clean(session_factory, creds)


async def test_concurrent_refund_requests_create_exactly_one(
    session_factory: Factory, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id = await _captured(session_factory, creds)

    async def attempt() -> str:
        try:
            await _refund(session_factory, creds, payment_id)
            return "created"
        except BusinessRejection as exc:
            return f"rejected {exc.status_code}"

    results = await asyncio.gather(*[attempt() for _ in range(20)])
    assert sorted(results) == ["created"] + ["rejected 422"] * 19
    await _clean(session_factory, creds)


async def test_confirm_racing_fail_has_one_winner(
    session_factory: Factory, make_tenant: Any
) -> None:
    creds = await make_tenant()
    refund_id = await _refund(
        session_factory, creds, await _captured(session_factory, creds)
    )
    await _refund_processing(session_factory, creds, refund_id)

    async def run(op: str) -> str:
        async with session_factory() as db:
            try:
                if op == "confirm":
                    await confirm_refund(
                        db,
                        tenant_id=creds.tenant_id,
                        refund_id=refund_id,
                        psp_reference="r",
                        source="race",
                    )
                else:
                    await fail_refund(
                        db,
                        tenant_id=creds.tenant_id,
                        refund_id=refund_id,
                        reason="x",
                        source="race",
                    )
                await db.commit()
                return f"{op} applied"
            except ConflictingOutcomeError:
                return f"{op} refused"

    results = sorted(await asyncio.gather(run("confirm"), run("fail")))
    assert results in (
        ["confirm applied", "fail refused"],
        ["confirm refused", "fail applied"],
    )
    await _clean(session_factory, creds)


# --- Refund rules ---------------------------------------------------------------------------


async def test_second_refund_rejected_while_active_allowed_after_failure(
    session_factory: Factory, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id = await _captured(session_factory, creds)
    refund_id = await _refund(session_factory, creds, payment_id)
    with pytest.raises(BusinessRejection, match="already has a refund"):
        await _refund(session_factory, creds, payment_id)

    await _refund_processing(session_factory, creds, refund_id)
    async with session_factory() as db:
        await fail_refund(
            db,
            tenant_id=creds.tenant_id,
            refund_id=refund_id,
            reason=None,
            source="test",
        )
        await db.commit()
    second = await _refund(session_factory, creds, payment_id)
    assert second != refund_id
    await _clean(session_factory, creds)


@pytest.mark.parametrize(
    "status", [PaymentStatus.PENDING, PaymentStatus.PROCESSING, PaymentStatus.FAILED]
)
async def test_only_captured_or_settled_payments_are_refundable(
    session_factory: Factory, make_tenant: Any, status: PaymentStatus
) -> None:
    creds = await make_tenant()
    payment_id = await _payment(session_factory, creds)
    if status is not PaymentStatus.PENDING:
        await _processing(session_factory, payment_id)
    if status is PaymentStatus.FAILED:
        await _apply(session_factory, creds, payment_id, DECLINED)
    with pytest.raises(BusinessRejection, match="only captured or settled"):
        await _refund(session_factory, creds, payment_id)


async def test_refund_needs_the_money_to_still_be_in_the_wallet(
    session_factory: Factory, make_tenant: Any
) -> None:
    creds, other = await make_tenant("a"), await make_tenant("b")
    payment_id = await _captured(session_factory, creds, 1_000)
    await _settle(session_factory, creds, payment_id)
    # Move the settled funds to a second wallet of the same tenant.
    async with session_factory() as db:
        from app.services.wallets import create_wallet, transfer_between_wallets

        second = await create_wallet(db, tenant_id=creds.tenant_id, currency="USD")
        assert second.resource_id is not None
        await transfer_between_wallets(
            db,
            tenant_id=creds.tenant_id,
            idempotency_key="drain",
            from_wallet_id=creds.wallet_id,
            to_wallet_id=second.resource_id,
            amount=1_000,
        )
        await db.commit()
    with pytest.raises(BusinessRejection, match="too low"):
        await _refund(session_factory, creds, payment_id)
    with pytest.raises(BusinessRejection, match="not found"):  # other tenant
        await _refund(session_factory, other, payment_id)
    await _clean(session_factory, creds)


async def test_legacy_payments_without_a_wallet_are_left_alone(
    session_factory: Factory, make_tenant: Any
) -> None:
    creds = await make_tenant()
    async with session_factory() as db:
        legacy = Payment(
            id=uuid.uuid4(),
            tenant_id=creds.tenant_id,
            idempotency_key="v1",
            amount=500,
            currency="USD",
            status=PaymentStatus.PENDING,
        )
        db.add(legacy)
        await db.commit()
    await _processing(session_factory, legacy.id)
    with pytest.raises(LegacyPaymentError):
        await _apply(session_factory, creds, legacy.id)


# --- Database guards ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement,message",
    [
        (
            "UPDATE payments SET status = 'FAILED' WHERE id = :id",
            "invalid payment transition",
        ),
        (
            "UPDATE payments SET status = 'PENDING' WHERE id = :id",
            "invalid payment transition",
        ),
        ("UPDATE payments SET amount = amount + 1 WHERE id = :id", "immutable"),
        ("UPDATE payments SET currency = 'NGN' WHERE id = :id", "immutable"),
        ("UPDATE payments SET wallet_id = NULL WHERE id = :id", "immutable"),
        (
            "UPDATE payments SET status = 'NOPE' WHERE id = :id",
            "invalid payment transition|ck_payments_status",
        ),
    ],
)
async def test_database_guards_payments(
    session_factory: Factory, make_tenant: Any, statement: str, message: str
) -> None:
    creds = await make_tenant()
    payment_id = await _captured(session_factory, creds)  # status CAPTURED
    async with session_factory() as db:
        with pytest.raises(DBAPIError, match=message):
            await db.execute(text(statement), {"id": payment_id})


@pytest.mark.parametrize(
    "statement,message",
    [
        (
            "UPDATE refunds SET status = 'SUCCEEDED' WHERE id = :id",
            "invalid refund transition",
        ),
        ("UPDATE refunds SET amount = 1 WHERE id = :id", "immutable"),
        (
            "UPDATE refunds SET hold_source = 'wallet_available' WHERE id = :id",
            "immutable",
        ),
    ],
)
async def test_database_guards_refunds(
    session_factory: Factory, make_tenant: Any, statement: str, message: str
) -> None:
    creds = await make_tenant()
    refund_id = await _refund(
        session_factory, creds, await _captured(session_factory, creds)
    )
    async with session_factory() as db:
        with pytest.raises(DBAPIError, match=message):
            await db.execute(text(statement), {"id": refund_id})
