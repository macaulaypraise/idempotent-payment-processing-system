"""
POST /v1/payments with a wallet and POST /v1/refunds, through the HTTP layer.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.payment import Payment, PaymentStatus
from app.services.psp_results import OutcomeKind, PspOutcome, apply_psp_result
from verification.invariants import check_ledger

pytestmark = pytest.mark.asyncio
Factory = async_sessionmaker[AsyncSession]


def _h(creds: Any, key: str | None = None) -> dict[str, str]:
    return {**creds.headers, "Idempotency-Key": key or str(uuid.uuid4())}


async def _capture(factory: Factory, creds: Any, payment_id: str) -> None:
    """What the charger does in Stage 4: PROCESSING, then the provider says yes."""
    async with factory() as db:
        await db.execute(
            update(Payment)
            .where(Payment.id == uuid.UUID(payment_id))
            .values(status=PaymentStatus.PROCESSING)
        )
        await apply_psp_result(
            db,
            tenant_id=creds.tenant_id,
            payment_id=uuid.UUID(payment_id),
            outcome=PspOutcome(OutcomeKind.CAPTURED, "psp_1"),
            source="test",
        )
        await db.commit()


async def test_payment_to_refund_round_trip(
    api_client: Any, make_tenant: Any, session_factory: Factory
) -> None:
    creds = await make_tenant()
    async with api_client() as client:
        created = await client.post(
            "/v1/payments", json=creds.payment(2_500), headers=_h(creds)
        )
        assert created.status_code == 202
        payment_id = created.json()["payment_id"]
        assert created.json()["wallet_id"] == str(creds.wallet_id)

        early = await client.post(
            "/v1/refunds", json={"payment_id": payment_id}, headers=_h(creds)
        )
        assert early.status_code == 422  # still PENDING

        await _capture(session_factory, creds, payment_id)
        balance = await client.get(
            f"/v1/wallets/{creds.wallet_id}/balance", headers=creds.headers
        )
        assert (balance.json()["pending"], balance.json()["available"]) == (2_500, 0)
        shown = await client.get(f"/v1/payments/{payment_id}", headers=creds.headers)
        assert shown.json()["status"] == "captured"

        refund = await client.post(
            "/v1/refunds", json={"payment_id": payment_id}, headers=_h(creds, "r1")
        )
        replay = await client.post(
            "/v1/refunds", json={"payment_id": payment_id}, headers=_h(creds, "r1")
        )
        second = await client.post(
            "/v1/refunds", json={"payment_id": payment_id}, headers=_h(creds, "r2")
        )
        fetched = await client.get(
            f"/v1/refunds/{refund.json()['refund_id']}", headers=creds.headers
        )
        balance = await client.get(
            f"/v1/wallets/{creds.wallet_id}/balance", headers=creds.headers
        )

    assert refund.status_code == 201
    assert refund.json()["status"] == "pending" and refund.json()["amount"] == 2_500
    assert replay.json() == refund.json()
    assert replay.headers["Idempotent-Replayed"] == "true"
    assert (
        second.status_code == 422 and "already has a refund" in second.json()["detail"]
    )
    assert fetched.json() == refund.json()
    assert balance.json()["pending"] == 0  # held in refunds_payable
    async with session_factory() as db:
        assert await check_ledger(db, creds.tenant_id) == []


async def test_payment_needs_a_wallet_of_the_same_tenant_and_currency(
    api_client: Any, make_tenant: Any
) -> None:
    alice, bob = await make_tenant("alice"), await make_tenant("bob")
    async with api_client() as client:
        foreign = await client.post(
            "/v1/payments", json=bob.payment(), headers=_h(alice)
        )
        wrong_ccy = await client.post(
            "/v1/payments", json=alice.payment(currency="NGN"), headers=_h(alice)
        )
        missing = await client.post(
            "/v1/payments", json={"amount": 100, "currency": "USD"}, headers=_h(alice)
        )
    assert foreign.status_code == 404
    assert (
        wrong_ccy.status_code == 422 and "does not match" in wrong_ccy.json()["detail"]
    )
    assert missing.status_code == 422


async def test_refunds_are_tenant_scoped(
    api_client: Any, make_tenant: Any, session_factory: Factory
) -> None:
    alice, bob = await make_tenant("alice"), await make_tenant("bob")
    async with api_client() as client:
        created = await client.post(
            "/v1/payments", json=alice.payment(), headers=_h(alice)
        )
        payment_id = created.json()["payment_id"]
        await _capture(session_factory, alice, payment_id)
        refund = await client.post(
            "/v1/refunds", json={"payment_id": payment_id}, headers=_h(alice)
        )
        by_bob = await client.post(
            "/v1/refunds", json={"payment_id": payment_id}, headers=_h(bob)
        )
        peek = await client.get(
            f"/v1/refunds/{refund.json()['refund_id']}", headers=bob.headers
        )
    assert by_bob.status_code == 404
    assert peek.status_code == 404
