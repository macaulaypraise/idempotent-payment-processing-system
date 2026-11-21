"""
Stage 5 exit gate (inbox): provider webhooks are verified, stored once,
applied once, in any order, alongside the charger.

The PSP simulator signs and sends real webhooks to the real API route
(in-process), which stores them in the real database; the processor, the
charger and the sweeper are driven step by step with one fake clock shared
by everything, including signature timestamps.
"""

import asyncio
import itertools
import json
import random
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.psp import PspClient
from app.config import get_settings
from app.core.clock import FakeClock, get_clock
from app.core.failpoints import SimulatedCrash, armed
from app.core.security import SIGNATURE_HEADER, sign_webhook
from app.models.ledger import Transfer
from app.models.outbox_event import OutboxEvent
from app.models.payment import Payment, PaymentStatus
from app.models.refund import Refund, RefundStatus
from app.models.wallet import Wallet
from app.models.webhook_event import WebhookEvent, WebhookStatus
from app.services.payment_service import create_payment
from app.services.refunds import request_refund
from app.services.wallets import wallet_balance
from app.workers.charger import Charger, ChargerEvent, Result
from app.workers.sweeper import Sweeper
from app.workers.webhook_processor import Outcome, WebhookProcessor
from psp_sim.main import create_app
from tests.conftest import DEFAULT_TENANT_ID
from verification.invariants import check_ledger, i3_one_charge_per_payment

Factory = async_sessionmaker[AsyncSession]
SECRET = "whsec_integration"
MAX_ATTEMPTS = 4


class Env:
    def __init__(
        self, factory: Factory, sim_app: Any, sim: AsyncClient, api: AsyncClient
    ) -> None:
        self.factory = factory
        self.sim_app = sim_app
        self.sim = sim
        self.api = api
        self.clock = FakeClock(datetime.now(UTC))
        self.dispatcher = sim_app.state.webhooks
        self.dispatcher.clock = lambda: self.clock.now().timestamp()
        self.settings = get_settings().model_copy(
            update={
                "PSP_MAX_ATTEMPTS": 4,
                "WEBHOOK_MAX_ATTEMPTS": MAX_ATTEMPTS,
                "WEBHOOK_RETRY_BASE_SECONDS": 1.0,
                "WEBHOOK_RETRY_MAX_SECONDS": 60.0,
                "PSP_WEBHOOK_SECRET": SECRET,
            }
        )
        psp = PspClient(sim, timeout_seconds=0.2)
        self.charger = Charger(
            factory, psp, self.clock, self.settings, random.Random(7)
        )
        self.sweeper = Sweeper(factory, self.charger, self.clock, self.settings)
        self.processor = WebhookProcessor(
            factory, self.clock, self.settings, random.Random(3)
        )

    # --- provider side ----------------------------------------------------------

    async def faults(self, **profile: Any) -> None:
        resp = await self.sim.put("/admin/faults", json=profile)
        assert resp.status_code == 200, resp.text

    def charges(self) -> list[dict[str, Any]]:
        return list(self.sim_app.state.store.ground_truth()["charges"])

    def queued(self) -> list[Any]:
        """Webhooks the simulator has recorded and not yet delivered."""
        return self.dispatcher.store.webhooks("pending")

    async def send(self, body: bytes, timestamp: float | None = None) -> Any:
        ts = int(self.clock.now().timestamp() if timestamp is None else timestamp)
        return await self.api.post(
            "/v1/webhooks/psp",
            content=body,
            headers={SIGNATURE_HEADER: sign_webhook(SECRET, body, ts)},
        )

    def event(self, event_type: str, data: dict[str, Any]) -> bytes:
        return json.dumps(
            {
                "id": f"evt_{uuid.uuid4().hex[:12]}",
                "type": event_type,
                "created": 0,
                "data": data,
            }
        ).encode()

    # --- our side -----------------------------------------------------------------

    async def payment(
        self, tenant_id: uuid.UUID, wallet_id: uuid.UUID, amount: int = 1_000
    ) -> tuple[uuid.UUID, ChargerEvent]:
        async with self.factory() as db:
            result = await create_payment(
                db,
                tenant_id=tenant_id,
                idempotency_key=str(uuid.uuid4()),
                amount=amount,
                currency="USD",
                wallet_id=wallet_id,
            )
            await db.commit()
        assert result.resource_id is not None
        payload = {"payment_id": str(result.resource_id), "tenant_id": str(tenant_id)}
        return result.resource_id, ChargerEvent(
            "payment.initiated", payload, uuid.uuid4()
        )

    async def refund(
        self, tenant_id: uuid.UUID, payment_id: uuid.UUID
    ) -> tuple[uuid.UUID, ChargerEvent]:
        async with self.factory() as db:
            result = await request_refund(
                db, tenant_id=tenant_id, payment_id=payment_id
            )
            await db.commit()
        assert result.resource_id is not None
        payload = {"refund_id": str(result.resource_id), "tenant_id": str(tenant_id)}
        return result.resource_id, ChargerEvent("refund.created", payload, uuid.uuid4())

    async def process(self) -> list[Outcome]:
        seen: list[Outcome] = []
        while outcomes := await self.processor.run_once():
            seen.extend(outcomes)
        return seen

    async def row(self, model: Any, row_id: uuid.UUID) -> Any:
        async with self.factory() as db:
            return await db.get(model, row_id)

    async def inbox(self) -> list[WebhookEvent]:
        async with self.factory() as db:
            rows = await db.execute(
                select(WebhookEvent).order_by(WebhookEvent.received_at)
            )
            return list(rows.scalars().all())

    async def dlq_events(self) -> list[dict[str, Any]]:
        async with self.factory() as db:
            rows = await db.execute(
                select(OutboxEvent.payload).where(OutboxEvent.event_type == "dlq.v1")
            )
            return [dict(p) for p in rows.scalars().all()]

    async def transfers(self, reference_id: uuid.UUID) -> int:
        async with self.factory() as db:
            count = await db.execute(
                select(func.count())
                .select_from(Transfer)
                .where(Transfer.reference_id == reference_id)
            )
            return int(count.scalar_one())

    async def clean(self, tenant_id: uuid.UUID) -> None:
        async with self.factory() as db:
            assert await check_ledger(db, tenant_id) == []
        assert i3_one_charge_per_payment(self.charges()) == []


@pytest_asyncio.fixture
async def env(
    session_factory: Factory, api_client: Callable[..., Any]
) -> AsyncIterator[Env]:
    async with api_client(PSP_WEBHOOK_SECRET=SECRET) as api:
        from app.main import app

        async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
            return (await api.post(url, content=body, headers=headers)).status_code

        sim_app = create_app(
            db_path=":memory:",
            response_timeout_seconds=1.0,
            webhook_url="/v1/webhooks/psp",
            webhook_secret=SECRET,
            webhook_sender=sender,
        )
        async with AsyncClient(
            transport=ASGITransport(app=sim_app), base_url="http://sim"
        ) as sim:
            environment = Env(session_factory, sim_app, sim, api)
            app.dependency_overrides[get_clock] = lambda: environment.clock
            yield environment


# --- ingestion ------------------------------------------------------------------------


async def test_same_webhook_five_times_is_stored_once_and_applied_once(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    await env.faults(charge_then_timeout_rate=1.0)  # the response is lost
    payment_id, event = await env.payment(creds.tenant_id, creds.wallet_id)
    assert await env.charger.handle(event) is Result.RETRY_SCHEDULED

    [webhook] = env.queued()
    body = webhook.body.encode()
    replies = [await env.send(body) for _ in range(5)]
    assert all(r.status_code == 200 for r in replies)
    assert [r.json()["duplicate"] for r in replies] == [False, True, True, True, True]

    assert await env.process() == [Outcome.PROCESSED]
    [row] = await env.inbox()
    assert row.status is WebhookStatus.PROCESSED and row.tenant_id == creds.tenant_id
    payment = await env.row(Payment, payment_id)
    assert payment.status is PaymentStatus.CAPTURED
    assert payment.psp_reference == env.charges()[0]["id"]
    assert payment.next_attempt_at is None  # the sweeper has nothing to retry
    assert await env.transfers(payment_id) == 1
    await env.clean(creds.tenant_id)


@pytest.mark.parametrize(
    "tamper",
    ["body", "secret", "stale", "future", "unsigned"],
)
async def test_tampered_or_stale_webhooks_are_rejected_and_not_stored(
    env: Env, tamper: str
) -> None:
    body = env.event("charge.succeeded", {"merchant_reference": str(uuid.uuid4())})
    now = int(env.clock.now().timestamp())
    headers = {SIGNATURE_HEADER: sign_webhook(SECRET, body, now)}
    if tamper == "body":
        body = body.replace(b"charge.succeeded", b"charge.failed")
    elif tamper == "secret":
        headers = {SIGNATURE_HEADER: sign_webhook("not-the-secret", body, now)}
    elif tamper == "stale":
        headers = {SIGNATURE_HEADER: sign_webhook(SECRET, body, now - 301)}
    elif tamper == "future":
        headers = {SIGNATURE_HEADER: sign_webhook(SECRET, body, now + 301)}
    else:
        headers = {}
    resp = await env.api.post("/v1/webhooks/psp", content=body, headers=headers)
    assert resp.status_code == 400
    assert await env.inbox() == []


async def test_signed_but_malformed_events_are_rejected(env: Env) -> None:
    for body in (b"not json", b'{"type":"x","data":{}}', b'{"id":"e","type":"x"}'):
        assert (await env.send(body)).status_code == 400
    oversized = b'{"id":"e","type":"x","data":{"pad":"' + b"x" * 70_000 + b'"}}'
    assert (await env.send(oversized)).status_code == 413
    assert await env.inbox() == []


# --- processing outcomes --------------------------------------------------------------


async def test_crash_between_apply_and_commit_loses_nothing(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    await env.faults(charge_then_timeout_rate=1.0)
    payment_id, event = await env.payment(creds.tenant_id, creds.wallet_id)
    await env.charger.handle(event)
    await env.dispatcher.drain()

    with armed("webhook_processor.before_commit"), pytest.raises(SimulatedCrash):
        await env.processor.process_one()
    [row] = await env.inbox()
    assert row.status is WebhookStatus.RECEIVED and row.attempts == 0
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.PROCESSING

    assert await env.process() == [Outcome.PROCESSED]
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.CAPTURED
    assert await env.transfers(payment_id) == 1
    await env.clean(creds.tenant_id)


async def test_event_for_a_payment_not_ready_yet_is_parked_then_applied(
    env: Env, make_tenant: Any
) -> None:
    """A charge.succeeded for a payment still PENDING (e.g. reported by a path
    other than our charger) cannot apply yet: it is parked, and applied once
    the payment reaches PROCESSING."""
    creds = await make_tenant()
    payment_id, _ = await env.payment(creds.tenant_id, creds.wallet_id)
    body = env.event(
        "charge.succeeded", {"id": "ch_early", "merchant_reference": str(payment_id)}
    )
    await env.send(body)

    assert await env.process() == [Outcome.PARKED]
    [row] = await env.inbox()
    assert row.status is WebhookStatus.PARKED and row.attempts == 1
    assert "not PROCESSING yet" in (row.last_error or "")
    assert await env.process() == []  # not due until the backoff passes

    claim = await env.charger._claim_payment(creds.tenant_id, payment_id)
    assert claim is not None  # PENDING -> PROCESSING, provider not called
    env.clock.advance(seconds=60)
    assert await env.process() == [Outcome.PROCESSED]
    payment = await env.row(Payment, payment_id)
    assert payment.status is PaymentStatus.CAPTURED
    assert payment.psp_reference == "ch_early"
    await env.clean(creds.tenant_id)


async def test_event_for_a_payment_that_is_not_visible_yet_is_parked(
    env: Env, make_tenant: Any
) -> None:
    """The provider can be faster than our commit: an unknown id is parked,
    not dropped, and gives up into the DLQ after WEBHOOK_MAX_ATTEMPTS."""
    unknown = uuid.uuid4()
    await env.send(env.event("charge.succeeded", {"merchant_reference": str(unknown)}))

    outcomes = []
    for _ in range(MAX_ATTEMPTS):
        outcomes += await env.process()
        env.clock.advance(seconds=120)
    assert outcomes == [Outcome.PARKED] * (MAX_ATTEMPTS - 1) + [Outcome.DEAD]

    [row] = await env.inbox()
    assert row.status is WebhookStatus.DEAD and row.attempts == MAX_ATTEMPTS
    [dlq] = await env.dlq_events()
    assert dlq["kind"] == "webhook" and dlq["id"] == str(row.id)
    assert dlq["tenant_id"] == str(DEFAULT_TENANT_ID)  # no tenant could be resolved
    assert "gave up" in dlq["error"]


async def test_conflicting_outcome_is_dead_lettered_not_applied(
    env: Env, make_tenant: Any
) -> None:
    creds = await make_tenant()
    await env.faults(decline_rate=1.0)
    payment_id, event = await env.payment(creds.tenant_id, creds.wallet_id)
    assert await env.charger.handle(event) is Result.DECLINED

    await env.send(
        env.event(
            "charge.succeeded", {"id": "ch_x", "merchant_reference": str(payment_id)}
        )
    )
    outcomes = await env.process()
    assert Outcome.DEAD in outcomes

    payment = await env.row(Payment, payment_id)
    assert payment.status is PaymentStatus.FAILED  # never auto-applied
    dead = [r for r in await env.inbox() if r.status is WebhookStatus.DEAD]
    assert len(dead) == 1 and dead[0].tenant_id == creds.tenant_id
    assert any("conflicting" in d["error"] for d in await env.dlq_events())
    await env.clean(creds.tenant_id)


async def test_unknown_types_are_ignored_and_foreign_references_dead(env: Env) -> None:
    await env.send(env.event("customer.updated", {"id": "cus_1"}))
    await env.send(env.event("charge.succeeded", {"merchant_reference": "order-77"}))
    assert sorted(await env.process()) == sorted([Outcome.IGNORED, Outcome.DEAD])
    statuses = {r.event_type: r.status for r in await env.inbox()}
    assert statuses == {
        "customer.updated": WebhookStatus.IGNORED,
        "charge.succeeded": WebhookStatus.DEAD,
    }


# --- ordering: every order gives the same final state ---------------------------------


async def _charged_and_refunded(env: Env, creds: Any) -> tuple[uuid.UUID, uuid.UUID]:
    payment_id, event = await env.payment(creds.tenant_id, creds.wallet_id)
    assert await env.charger.handle(event) is Result.CAPTURED
    refund_id, refund_event = await env.refund(creds.tenant_id, payment_id)
    assert await env.charger.handle(refund_event) is Result.REFUNDED
    return payment_id, refund_id


async def _state(
    env: Env, creds: Any, payment_id: uuid.UUID, refund_id: uuid.UUID
) -> Any:
    payment = await env.row(Payment, payment_id)
    refund = await env.row(Refund, refund_id)
    async with env.factory() as db:
        wallet = await db.get(Wallet, creds.wallet_id)
        balance = await wallet_balance(db, wallet)
        balance.pop("wallet_id")
    return (
        payment.status,
        refund.status,
        await env.transfers(payment_id),
        await env.transfers(refund_id),
        json.dumps(balance, sort_keys=True, default=str),
    )


async def test_charge_and_refund_webhooks_in_every_order_give_one_final_state(
    env: Env, make_tenant: Any
) -> None:
    """The refund webhook can arrive before the charge webhook: the provider
    reorders, delays and duplicates. Every delivery order of {charge, refund,
    charge again, refund again}, processed after each delivery, must end in
    the same state with nothing dead-lettered."""
    orders = sorted(
        set(itertools.permutations(["charge", "refund", "charge", "refund"]))
    )
    assert len(orders) == 6
    states = set()
    for order in orders:
        creds = await make_tenant(f"perm-{'-'.join(order)}")
        payment_id, refund_id = await _charged_and_refunded(env, creds)
        bodies = {
            json.loads(w.body)["type"].split(".")[0]: w.body.encode()
            for w in env.queued()
        }
        assert set(bodies) == {"charge", "refund"}
        for webhook in env.queued():  # delivered by hand below, in order
            env.dispatcher.store.update_webhook(webhook.id, status="delivered")

        outcomes: list[Outcome] = []
        for name in order:
            assert (await env.send(bodies[name])).status_code == 200
            outcomes += await env.process()

        assert outcomes == [Outcome.ALREADY_APPLIED] * 2, order
        states.add(await _state(env, creds, payment_id, refund_id))
        await env.clean(creds.tenant_id)

    assert len(states) == 1
    [(payment_status, refund_status, payment_transfers, refund_transfers, _)] = states
    assert payment_status is PaymentStatus.REFUNDED
    assert refund_status is RefundStatus.SUCCEEDED
    assert (payment_transfers, refund_transfers) == (1, 2)  # capture; hold + confirm
    assert await env.dlq_events() == []


@pytest.mark.parametrize("first", ["webhook", "sweeper"])
async def test_lost_responses_heal_the_same_whichever_source_reports_first(
    env: Env, make_tenant: Any, first: str
) -> None:
    """Both provider responses lost: the webhook and the sweeper's retry each
    report the outcome; whichever lands first applies it, the other finds it
    already applied."""
    creds = await make_tenant()
    await env.faults(charge_then_timeout_rate=1.0, refund_then_timeout_rate=1.0)

    async def settle_by(source: str) -> None:
        if source == "webhook":
            await env.dispatcher.drain()
            await env.process()
        else:
            env.clock.advance(seconds=400)
            await env.sweeper.run_once()

    second = "sweeper" if first == "webhook" else "webhook"
    payment_id, event = await env.payment(creds.tenant_id, creds.wallet_id)
    assert await env.charger.handle(event) is Result.RETRY_SCHEDULED
    await settle_by(first)
    assert (await env.row(Payment, payment_id)).status is PaymentStatus.CAPTURED
    await settle_by(second)

    refund_id, refund_event = await env.refund(creds.tenant_id, payment_id)
    assert await env.charger.handle(refund_event) is Result.RETRY_SCHEDULED
    await settle_by(first)
    await settle_by(second)

    payment = await env.row(Payment, payment_id)
    refund = await env.row(Refund, refund_id)
    assert payment.status is PaymentStatus.REFUNDED
    assert refund.status is RefundStatus.SUCCEEDED
    assert (
        len(env.charges()) == 1
        and len(env.sim_app.state.store.ground_truth()["refunds"]) == 1
    )
    assert await env.transfers(payment_id) == 1 and await env.transfers(refund_id) == 2
    assert all(r.status is WebhookStatus.PROCESSED for r in await env.inbox())
    await env.clean(creds.tenant_id)


# --- healing and racing ---------------------------------------------------------------


async def test_webhooks_heal_lost_responses_without_the_sweeper(
    env: Env, make_tenant: Any
) -> None:
    """Every charge response is lost and the sweeper never runs: the webhooks
    alone finish all payments, each charged exactly once."""
    creds = await make_tenant()
    await env.faults(charge_then_timeout_rate=1.0, webhook_duplicate_rate=0.5, seed=11)
    payments = [await env.payment(creds.tenant_id, creds.wallet_id) for _ in range(10)]
    results = await asyncio.gather(*[env.charger.handle(e) for _, e in payments])
    assert set(results) == {Result.RETRY_SCHEDULED}

    assert await env.dispatcher.drain() == 10
    outcomes = await env.process()
    assert outcomes.count(Outcome.PROCESSED) == 10

    for payment_id, _ in payments:
        payment = await env.row(Payment, payment_id)
        assert payment.status is PaymentStatus.CAPTURED
        assert payment.next_attempt_at is None
    assert len(env.charges()) == 10
    await env.clean(creds.tenant_id)


async def test_processor_racing_the_charger_applies_each_outcome_once(
    env: Env, make_tenant: Any
) -> None:
    """The charger's response and the webhook report the same capture at the
    same moment: both paths lock the payment, so one applies and the other
    finds it applied."""
    creds = await make_tenant()
    payments = [await env.payment(creds.tenant_id, creds.wallet_id) for _ in range(20)]
    claims = [
        await env.charger._claim_payment(creds.tenant_id, payment_id)
        for payment_id, _ in payments
    ]
    results = [
        await env.charger.psp.charge(
            key=str(c.id),
            amount=c.amount,
            currency=c.currency,
            merchant_reference=str(c.id),
        )
        for c in claims
        if c is not None
    ]
    assert await env.dispatcher.drain() == 20  # stored, not yet processed

    applied = await asyncio.gather(
        *[
            env.charger._apply(c, r, None)
            for c, r in zip(claims, results, strict=True)
            if c
        ],
        env.process(),
    )
    charger_results = applied[:-1]
    webhook_outcomes: list[Outcome] = applied[-1]
    assert set(charger_results) == {Result.CAPTURED}  # already-applied still returns it
    assert len(webhook_outcomes) == 20
    assert set(webhook_outcomes) <= {Outcome.PROCESSED, Outcome.ALREADY_APPLIED}

    for payment_id, _ in payments:
        assert (await env.row(Payment, payment_id)).status is PaymentStatus.CAPTURED
        assert await env.transfers(payment_id) == 1
    await env.clean(creds.tenant_id)
