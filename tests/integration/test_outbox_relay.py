"""
Stage 5 exit gate (outbox): publish order, one leader among replicas,
failover, and NOTIFY wake-up, against the real database with a recording
producer in place of Kafka.
"""

import asyncio
import statistics
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.outbox_event import OutboxEvent
from app.services.outbox_poller import poll_and_publish
from app.workers.outbox_poller import OutboxRelay, asyncpg_dsn
from tests.conftest import DEFAULT_TENANT_ID

Factory = async_sessionmaker[AsyncSession]


class RecordingProducer:
    """Stands in for AIOKafkaProducer.send_and_wait; can fail on demand."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, dict[str, bytes], float]] = []
        self.fail_next = 0
        self.received = asyncio.Event()

    async def send_and_wait(
        self, topic: str, value: bytes, *, key: bytes, headers: list[Any]
    ) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("broker unavailable")
        self.sent.append((topic, key, dict(headers), time.monotonic()))
        self.received.set()

    def keys(self) -> list[str]:
        return [key.decode() for _, key, _, _ in self.sent]


async def add_events(factory: Factory, keys: list[str]) -> None:
    async with factory() as db:
        for key in keys:
            db.add(
                OutboxEvent(
                    id=uuid.uuid4(),
                    tenant_id=DEFAULT_TENANT_ID,
                    event_type="payment.captured",
                    payload={"payment_id": key},
                )
            )
        await db.commit()


async def unpublished(factory: Factory) -> int:
    async with factory() as db:
        rows = await db.execute(
            select(OutboxEvent.id).where(OutboxEvent.published_at.is_(None))
        )
        return len(rows.all())


async def eventually(check: Callable[[], Any], timeout: float = 10.0) -> float:
    """Wait until check() is truthy; return how long it took."""
    start = time.monotonic()
    while True:
        result = check()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


@pytest.fixture
def relays(session_factory: Factory, migrated_database: str) -> Callable[..., Any]:
    @asynccontextmanager
    async def _start(
        count: int, fallback: float, producer: RecordingProducer | None = None
    ) -> AsyncIterator[list[tuple[OutboxRelay, asyncio.Event, asyncio.Task[None]]]]:
        settings = get_settings().model_copy(
            update={"OUTBOX_FALLBACK_POLL_SECONDS": fallback, "OUTBOX_BATCH_SIZE": 50}
        )
        started = []
        for n in range(count):
            relay = OutboxRelay(
                asyncpg_dsn(migrated_database),
                session_factory,
                producer or RecordingProducer(),
                settings,
                name=f"relay-{n}",
            )
            stop = asyncio.Event()
            started.append((relay, stop, asyncio.create_task(relay.run(stop))))
        try:
            yield started
        finally:
            for _, stop, task in started:
                stop.set()
            await asyncio.gather(*(t for _, _, t in started), return_exceptions=True)

    return _start


async def test_failure_mid_batch_never_reorders(session_factory: Factory) -> None:
    keys = [f"pay-{i}" for i in range(6)]
    await add_events(session_factory, keys)
    producer = RecordingProducer()

    async with session_factory() as db:
        # fail the 4th send: 3 go out, the batch stops
        original = producer.send_and_wait
        calls = 0

        async def flaky(*args: Any, **kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise ConnectionError("broker unavailable")
            await original(*args, **kwargs)

        producer.send_and_wait = flaky  # type: ignore[method-assign]
        first = await poll_and_publish(db, producer)
    assert (first.published, first.failed) == (3, True)
    assert await unpublished(session_factory) == 3

    async with session_factory() as db:
        second = await poll_and_publish(db, producer)
    assert (second.published, second.failed) == (3, False)
    assert producer.keys() == keys  # each once, in insert order
    assert await unpublished(session_factory) == 0


async def test_only_the_leader_publishes(
    session_factory: Factory, relays: Callable[..., Any]
) -> None:
    async with relays(2, fallback=0.5) as started:
        await eventually(lambda: sum(r.is_leader for r, _, _ in started) == 1)
        await add_events(session_factory, [f"pay-{i}" for i in range(40)])
        await eventually(lambda: sum(r.published for r, _, _ in started) == 40)
        await asyncio.sleep(1.0)  # give a standby every chance to misbehave

        leader = next(r for r, _, _ in started if r.is_leader)
        standby = next(r for r, _, _ in started if not r.is_leader)
        assert leader.published == 40 and standby.published == 0
        assert len(leader.producer.sent) == 40  # no duplicates
        assert await unpublished(session_factory) == 0


async def test_standby_takes_over_when_the_leader_stops(
    session_factory: Factory, relays: Callable[..., Any]
) -> None:
    async with relays(2, fallback=0.5) as started:
        await eventually(lambda: any(r.is_leader for r, _, _ in started))
        old, old_stop, old_task = next(s for s in started if s[0].is_leader)
        new = next(r for r, _, _ in started if r is not old)

        old_stop.set()
        await old_task
        handover = await eventually(lambda: new.is_leader)
        assert handover < 0.5 + 0.5  # within one fallback interval, plus slack

        await add_events(session_factory, ["after-handover"])
        await eventually(lambda: new.published == 1)


async def test_standby_takes_over_when_the_leader_connection_dies(
    session_factory: Factory, owner_database_url: str, relays: Callable[..., Any]
) -> None:
    """A crash, not a clean stop: Postgres kills the leader's lock session.
    The lock goes with it; the old leader notices at its next check."""
    async with relays(2, fallback=0.5) as started:
        await eventually(lambda: any(r.is_leader for r, _, _ in started))
        old = next(r for r, _, _ in started if r.is_leader)
        new = next(r for r, _, _ in started if r is not old)

        owner = create_async_engine(owner_database_url)
        async with owner.begin() as conn:
            await conn.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": old.backend_pid}
            )
        await owner.dispose()

        handover = await eventually(lambda: new.is_leader and not old.is_leader)
        assert handover < 2.0
        await add_events(session_factory, ["after-crash"])
        await eventually(lambda: old.published + new.published == 1)


async def test_notify_wakes_the_leader_long_before_the_fallback_poll(
    session_factory: Factory, relays: Callable[..., Any]
) -> None:
    producer = RecordingProducer()
    async with relays(1, fallback=30.0, producer=producer) as started:
        relay = started[0][0]
        await eventually(lambda: relay.is_leader)
        await asyncio.sleep(0.2)  # let it go idle on the 30 s wait

        latencies = []
        for i in range(10):
            producer.received.clear()
            start = time.monotonic()
            await add_events(session_factory, [f"pay-{i}"])
            await asyncio.wait_for(producer.received.wait(), timeout=5)
            latencies.append(time.monotonic() - start)
        assert max(latencies) < 1.0, latencies  # vs 30 s without NOTIFY
        print(f"NOTIFY wake-up: median {statistics.median(latencies) * 1000:.1f} ms")


async def test_seq_follows_commit_order_for_one_payment(
    session_factory: Factory, make_tenant: Any
) -> None:
    """Events for one payment are written under its row lock, so seq order is
    the lifecycle order the relay must publish."""
    from app.models.payment import Payment, PaymentStatus
    from app.services.payment_service import create_payment
    from app.services.psp_results import OutcomeKind, PspOutcome, apply_psp_result

    creds = await make_tenant()
    async with session_factory() as db:
        result = await create_payment(
            db,
            tenant_id=creds.tenant_id,
            idempotency_key="k-seq",
            amount=500,
            currency="USD",
            wallet_id=creds.wallet_id,
        )
        await db.commit()
    payment_id = result.resource_id
    async with session_factory() as db:
        payment = await db.get(Payment, payment_id)
        payment.status = PaymentStatus.PROCESSING
        await db.commit()
    async with session_factory() as db:
        await apply_psp_result(
            db,
            tenant_id=creds.tenant_id,
            payment_id=payment_id,
            outcome=PspOutcome(OutcomeKind.CAPTURED, "ch_1"),
            source="test",
        )
        await db.commit()

    async with session_factory() as db:
        rows = await db.execute(
            select(OutboxEvent.event_type)
            .where(OutboxEvent.tenant_id == creds.tenant_id)
            .order_by(OutboxEvent.seq)
        )
        assert list(rows.scalars()) == ["payment.initiated", "payment.captured"]
