"""
Stage 4 exit gate: the provider boundary behaves like production and the
system survives it.

The charger and sweeper are driven step by step (handle / run_once) with a
fake clock against the real database and an in-process PSP simulator whose
store is the ground truth. No sleeps: time moves when the test says so.
"""

import asyncio
import random
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.psp import PspClient
from app.config import get_settings
from app.core.clock import FakeClock
from app.core.failpoints import SimulatedCrash, armed
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.models.processed_event import ProcessedEvent
from app.models.refund import Refund, RefundStatus
from app.services.payment_service import create_payment
from app.services.refunds import request_refund
from app.workers.charger import Charger, ChargerEvent, Result
from app.workers.sweeper import Sweeper
from psp_sim.main import create_app
from verification.invariants import check_ledger, i3_one_charge_per_payment

Factory = async_sessionmaker[AsyncSession]
MAX_ATTEMPTS = 4


class Env:
    def __init__(self, factory: Factory, sim_app: Any, sim: AsyncClient) -> None:
        self.factory = factory
        self.sim_app = sim_app
        self.sim = sim
        self.clock = FakeClock(datetime.now(UTC))
        settings = get_settings().model_copy(
            update={
                "PSP_MAX_ATTEMPTS": MAX_ATTEMPTS,
                "PSP_LEASE_SECONDS": 30.0,
                "PSP_RETRY_BASE_SECONDS": 2.0,
                "SWEEPER_PENDING_AFTER_SECONDS": 60.0,
            }
        )
        psp = PspClient(sim, timeout_seconds=0.2)
        self.charger = Charger(factory, psp, self.clock, settings, random.Random(7))
        self.sweeper = Sweeper(factory, self.charger, self.clock, settings)

    async def faults(self, **profile: Any) -> None:
        resp = await self.sim.put("/admin/faults", json=profile)
        assert resp.status_code == 200, resp.text

    def charges(self, payment_id: uuid.UUID | None = None) -> list[dict[str, Any]]:
        truth = self.sim_app.state.store.ground_truth()["charges"]
        if payment_id is None:
            return list(truth)
        return [c for c in truth if c["merchant_reference"] == str(payment_id)]

    def refunds(self) -> list[dict[str, Any]]:
        return list(self.sim_app.state.store.ground_truth()["refunds"])

    async def payment(
        self, creds: Any, amount: int = 1_000
    ) -> tuple[uuid.UUID, ChargerEvent]:
        async with self.factory() as db:
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
        payload = {
            "payment_id": str(result.resource_id),
            "tenant_id": str(creds.tenant_id),
        }
        return result.resource_id, ChargerEvent(
            "payment.initiated", payload, uuid.uuid4()
        )

    async def row(self, model: Any, row_id: uuid.UUID) -> Any:
        async with self.factory() as db:
            return await db.get(model, row_id)

    async def sweep_until_quiet(
        self, step: float = 400.0, rounds: int = 20
    ) -> list[Result]:
        seen: list[Result] = []
        for _ in range(rounds):
            self.clock.advance(seconds=step)
            results = await self.sweeper.run_once()
            if not results:
                break
            seen.extend(results)
        return seen

    async def clean(self, creds: Any) -> None:
        async with self.factory() as db:
            assert await check_ledger(db, creds.tenant_id) == []
        assert i3_one_charge_per_payment(self.charges()) == []


@pytest_asyncio.fixture
async def env(session_factory: Factory) -> AsyncIterator[Env]:
    sim_app = create_app(db_path=":memory:", response_timeout_seconds=1.0)
    async with AsyncClient(
        transport=ASGITransport(app=sim_app), base_url="http://sim"
    ) as sim:
        yield Env(session_factory, sim_app, sim)


# --- Happy paths ----------------------------------------------------------------------


async def test_payment_is_charged_once_and_captured(env: Env, make_tenant: Any) -> None:
    creds = await make_tenant()
    payment_id, event = await env.payment(creds)
    assert await env.charger.handle(event) is Result.CAPTURED

    payment = await env.row(Payment, payment_id)
    [charge] = env.charges(payment_id)
    assert payment.status is PaymentStatus.CAPTURED
    assert payment.psp_reference == charge["id"]
    assert payment.attempts == 1 and payment.next_attempt_at is None
    async with env.factory() as db:
        recorded = await db.get(ProcessedEvent, (event.event_id, "charger"))
        assert recorded is not None
    await env.clean(creds)


async def test_decline_fails_the_payment(env: Env, make_tenant: Any) -> None:
    creds = await make_tenant()
    await env.faults(decline_rate=1.0)
    payment_id, event = await env.payment(creds)
    assert await env.charger.handle(event) is Result.DECLINED
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.FAILED
    await env.clean(creds)


async def test_duplicate_delivery_is_processed_once(env: Env, make_tenant: Any) -> None:
    creds = await make_tenant()
    payment_id, event = await env.payment(creds)
    assert await env.charger.handle(event) is Result.CAPTURED
    assert await env.charger.handle(event) is Result.DUPLICATE
    assert len(env.charges(payment_id)) == 1


# --- Exit gate 1: charge-then-timeout at 100% ------------------------------------------


async def test_charge_then_timeout_ends_captured_with_exactly_one_charge(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    await env.faults(charge_then_timeout_rate=1.0)
    payments = [await env.payment(creds) for _ in range(10)]

    first = await asyncio.gather(*[env.charger.handle(e) for _, e in payments])
    assert set(first) == {Result.RETRY_SCHEDULED}  # outcome unknown, not failed
    for payment_id, _ in payments:
        assert (await env.row(Payment, payment_id)).status is PaymentStatus.PROCESSING
        assert len(env.charges(payment_id)) == 1  # the provider did charge

    assert await env.sweeper.run_once() == []  # retries are not due yet
    later = await env.sweep_until_quiet()
    assert later.count(Result.CAPTURED) == 10

    for payment_id, _ in payments:
        payment = await env.row(Payment, payment_id)
        [charge] = env.charges(payment_id)
        assert payment.status is PaymentStatus.CAPTURED
        assert payment.psp_reference == charge["id"]
    await env.clean(creds)


# --- Exit gate 2: crash after the charge, before the commit ----------------------------


async def test_crash_after_charge_before_commit_recovers_with_one_charge(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id, event = await env.payment(creds)
    with armed("charger.after_provider_call"), pytest.raises(SimulatedCrash):
        await env.charger.handle(event)

    payment = await env.row(Payment, payment_id)
    assert payment.status is PaymentStatus.PROCESSING
    assert len(env.charges(payment_id)) == 1  # charged, not recorded

    # "Restart": the same event is redelivered (its offset was never committed)
    assert await env.charger.handle(event) is Result.SKIPPED  # already claimed
    assert await env.sweeper.run_once() == []  # lease still running
    env.clock.advance(seconds=31)  # lease expires
    assert await env.sweeper.run_once() == [Result.CAPTURED]

    payment = await env.row(Payment, payment_id)
    assert payment.status is PaymentStatus.CAPTURED
    assert payment.attempts == 2
    assert len(env.charges(payment_id)) == 1
    await env.clean(creds)


async def test_crash_after_claim_before_call_recovers(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id, event = await env.payment(creds)
    with armed("charger.after_claim"), pytest.raises(SimulatedCrash):
        await env.charger.handle(event)
    assert env.charges(payment_id) == []
    env.clock.advance(seconds=31)
    assert await env.sweeper.run_once() == [Result.CAPTURED]
    assert len(env.charges(payment_id)) == 1


# --- Exit gate 3: exhausted retries -> one DLQ event, payment stays PROCESSING ----------


async def test_exhausted_retries_dead_letter_once_and_leave_processing(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    await env.faults(unavailable_rate=1.0)
    payment_id, event = await env.payment(creds)
    assert await env.charger.handle(event) is Result.RETRY_SCHEDULED

    later = await env.sweep_until_quiet()
    assert later == [Result.RETRY_SCHEDULED] * (MAX_ATTEMPTS - 2) + [
        Result.DEAD_LETTERED
    ]
    assert await env.sweep_until_quiet() == []  # nothing left to schedule

    payment = await env.row(Payment, payment_id)
    assert payment.status is PaymentStatus.PROCESSING  # reconciliation decides
    assert payment.attempts == MAX_ATTEMPTS
    assert payment.next_attempt_at is None
    async with env.factory() as db:
        dlq = (
            (
                await db.execute(
                    select(OutboxEvent.payload).where(
                        OutboxEvent.event_type == "dlq.v1",
                        OutboxEvent.payload["id"].as_string() == str(payment_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(dlq) == 1 and dlq[0]["kind"] == "payment"
    assert env.charges(payment_id) == []
    await env.clean(creds)


def test_backoff_grows_and_stays_within_bounds(env: Env) -> None:
    for attempts in range(1, 12):
        ceiling = min(300.0, 2.0 * 2 ** (attempts - 1))
        delay = env.charger.backoff_seconds(attempts)
        assert ceiling / 2 <= delay <= ceiling


# --- Sweeper ---------------------------------------------------------------------------


async def test_pending_payment_whose_event_was_lost_is_swept(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id, _ = await env.payment(creds)  # never handled
    assert await env.sweeper.run_once() == []  # too recent
    env.clock.advance(seconds=61)
    assert await env.sweeper.run_once() == [Result.CAPTURED]
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.CAPTURED


async def test_charger_and_sweeper_racing_charge_once(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    await env.faults(slow_rate=1.0, slow_seconds=0.05)
    payments = [await env.payment(creds) for _ in range(10)]
    env.clock.advance(seconds=61)  # every PENDING row is now sweepable too
    await asyncio.gather(
        env.sweeper.run_once(), *[env.charger.handle(e) for _, e in payments]
    )
    for payment_id, _ in payments:
        assert (await env.row(Payment, payment_id)).status is PaymentStatus.CAPTURED
        assert len(env.charges(payment_id)) == 1
    await env.clean(creds)


async def test_legacy_payments_are_never_swept(env: Env, make_tenant: Any) -> None:
    creds = await make_tenant()
    async with env.factory() as db:
        db.add(
            Payment(
                id=uuid.uuid4(),
                tenant_id=creds.tenant_id,
                idempotency_key="v1",
                amount=10,
                currency="USD",
                status=PaymentStatus.PENDING,
            )
        )
        await db.commit()
    env.clock.advance(seconds=3_600)
    assert await env.sweeper.run_once() == []
    assert env.charges() == []


# --- Refunds ---------------------------------------------------------------------------


async def _captured(env: Env, creds: Any) -> uuid.UUID:
    payment_id, event = await env.payment(creds)
    assert await env.charger.handle(event) is Result.CAPTURED
    return payment_id


async def _refund(
    env: Env, creds: Any, payment_id: uuid.UUID
) -> tuple[uuid.UUID, ChargerEvent]:
    async with env.factory() as db:
        async with db.begin_nested():
            result = await request_refund(
                db, tenant_id=creds.tenant_id, payment_id=payment_id
            )
        await db.commit()
    assert result.resource_id is not None
    payload = {"refund_id": str(result.resource_id), "tenant_id": str(creds.tenant_id)}
    return result.resource_id, ChargerEvent("refund.created", payload, uuid.uuid4())


async def test_refund_is_sent_once_and_confirmed(env: Env, make_tenant: Any) -> None:
    creds = await make_tenant()
    payment_id = await _captured(env, creds)
    refund_id, event = await _refund(env, creds, payment_id)
    assert await env.charger.handle(event) is Result.REFUNDED
    assert (await env.row(Refund, refund_id)).status is RefundStatus.SUCCEEDED
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.REFUNDED
    assert len(env.refunds()) == 1
    await env.clean(creds)


async def test_refund_rejected_by_provider_returns_the_hold(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id = await _captured(env, creds)
    await env.faults(refund_fail_rate=1.0)
    refund_id, event = await _refund(env, creds, payment_id)
    assert await env.charger.handle(event) is Result.REFUND_FAILED
    assert (await env.row(Refund, refund_id)).status is RefundStatus.FAILED
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.CAPTURED
    await env.clean(creds)


async def test_refund_then_timeout_is_retried_with_the_same_key(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    payment_id = await _captured(env, creds)
    await env.faults(refund_then_timeout_rate=1.0)
    refund_id, event = await _refund(env, creds, payment_id)
    assert await env.charger.handle(event) is Result.RETRY_SCHEDULED
    assert Result.REFUNDED in await env.sweep_until_quiet()
    assert (await env.row(Refund, refund_id)).status is RefundStatus.SUCCEEDED
    assert len(env.refunds()) == 1
    await env.clean(creds)


async def test_processed_events_written_only_with_the_effect(
    env: Env, make_tenant: Any
) -> None:
    """A crash before the effect must leave the event unrecorded, so a
    redelivery is not mistaken for a duplicate."""
    creds = await make_tenant()
    _, event = await env.payment(creds)
    with armed("charger.after_provider_call"), pytest.raises(SimulatedCrash):
        await env.charger.handle(event)
    async with env.factory() as db:
        count = await db.execute(
            select(func.count())
            .select_from(ProcessedEvent)
            .where(ProcessedEvent.event_id == event.event_id)
        )
        assert count.scalar_one() == 0
