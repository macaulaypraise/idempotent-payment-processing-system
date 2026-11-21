"""
Unit tests for app/services/payment_service.py (database mocked).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.services.idempotency import BusinessRejection
from app.services.payment_service import create_payment, payment_body

TENANT = uuid.uuid4()


def _db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    return db


async def _create(db: AsyncMock) -> object:
    return await create_payment(
        db,
        tenant_id=TENANT,
        idempotency_key="k1",
        amount=5050,
        currency="USD",
    )


@pytest.mark.asyncio
async def test_create_payment_writes_payment_and_outbox_for_tenant() -> None:
    db = _db()
    result = await _create(db)

    added = [call.args[0] for call in db.add.call_args_list]
    payment = next(o for o in added if isinstance(o, Payment))
    event = next(o for o in added if isinstance(o, OutboxEvent))
    assert payment.tenant_id == TENANT and event.tenant_id == TENANT
    assert payment.status is PaymentStatus.PENDING
    assert event.event_type == "payment.initiated"
    assert event.payload["payment_id"] == str(payment.id)
    assert event.payload["tenant_id"] == str(TENANT)

    assert result.status_code == 202  # type: ignore[attr-defined]
    assert result.resource_id == payment.id  # type: ignore[attr-defined]
    assert result.body["amount"] == 5050  # type: ignore[attr-defined]
    assert event.payload["amount"] == 5050
    db.flush.assert_awaited_once()
    db.commit.assert_not_awaited()  # the idempotency layer owns the transaction


@pytest.mark.asyncio
async def test_unique_key_violation_becomes_stored_rejection() -> None:
    db = _db()
    db.flush.side_effect = IntegrityError(
        "INSERT", {}, Exception('violates "uq_payments_tenant_idempotency_key"')
    )
    with pytest.raises(BusinessRejection) as caught:
        await _create(db)
    assert caught.value.status_code == 422
    assert "expired" in caught.value.body["detail"]


@pytest.mark.asyncio
async def test_other_integrity_errors_propagate() -> None:
    db = _db()
    db.flush.side_effect = IntegrityError("INSERT", {}, Exception("fk_violation"))
    with pytest.raises(IntegrityError):
        await _create(db)


def test_payment_body_reports_minor_units() -> None:
    payment = Payment(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        idempotency_key="k",
        amount=700,
        currency="NGN",
        status=PaymentStatus.SETTLED,
    )
    assert payment_body(payment) == {
        "payment_id": str(payment.id),
        "status": "settled",
        "amount": 700,
        "currency": "NGN",
    }
